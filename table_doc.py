"""
table_doc.py — 词条表数据源的公共部分

链路 B 现在有两个数据源，产出的东西完全一样（`TableMeta` / `TableContent` / `DocRow`）：

- `docx_table.py`：读本地导出的 docx（离线、无需网络）；
- `feishu_table.py`：直接读飞书**活文档**（不用先导出 135MB 的 docx，也不会读到旧快照）。

所以"跟来源无关"的部分放这里，两边共用：**六列契约、小标题模糊匹配、抽行规则**。
尤其列契约绝不能有两份实现——哪天改了列，两份不一致就会一边报错一边放行。
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from models import EXPECTED_COLUMNS, DocRow, TableContent, TableMeta


class TableDocError(Exception):
    """取表失败"""


class ColumnContractError(TableDocError):
    """表格列不符合六列契约"""


class TableNotFoundError(TableDocError):
    """小标题没匹配到表格"""


# ---------------------------------------------------------------------------
# 匹配用的小工具
# ---------------------------------------------------------------------------


def norm(text: str) -> str:
    """匹配用归一化：全角转半角、去大小写、去掉所有空白"""
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", text).lower()


def tokens(keyword: str) -> list[str]:
    """关键词按空白切成若干片段，每个片段都要命中才算匹配"""
    return [t for t in (norm(x) for x in re.split(r"\s+", (keyword or "").strip())) if t]


def haystack(meta: TableMeta) -> str:
    """匹配用文本：「期 + 小标题」拼在一起，方便"第二期 预订-入住"这类输入"""
    return norm(f"{meta.period}{meta.title}")


# ---------------------------------------------------------------------------
# 列契约与抽行
# ---------------------------------------------------------------------------


def check_columns(meta: TableMeta) -> None:
    """列不符合六列契约就直接报错，不做兼容猜测"""
    if list(meta.columns) != EXPECTED_COLUMNS:
        raise ColumnContractError(
            f"表格「{meta.title}」的列不符合契约，无法解析。\n"
            f"  期望：{EXPECTED_COLUMNS}\n"
            f"  实际：{list(meta.columns)}\n"
            f"请让文档维护者把表头改成固定六列（列名与顺序都不能变）。"
        )


def rows_to_content(meta: TableMeta, rows: list[list[str]]) -> TableContent:
    """
    数据行（不含表头）→ TableContent。

    - 整行空白 → 跳过；
    - key 为空 → 跳过并记下序号（人工去补）；
    - 缺列 → 按空处理。
    """
    content = TableContent(meta=meta)
    for cells in rows:
        if not any(cells):
            continue
        cells = list(cells) + [""] * (len(EXPECTED_COLUMNS) - len(cells))
        seq, key, zh_cn, en_us, en_copywriter = cells[:5]
        key = key.strip()
        if not key:
            content.empty_key_seqs.append(seq or "?")
            continue
        content.rows.append(DocRow(seq=seq, key=key, zh_cn=zh_cn,
                                   en_us=en_us, en_copywriter=en_copywriter))
    return content


# ---------------------------------------------------------------------------
# 文档对象：两个数据源共用
# ---------------------------------------------------------------------------


@dataclass
class TableSource:
    """文档里的一张表：定位信息 + 数据行（不含表头）"""
    meta: TableMeta
    rows: list[list[str]] = field(default_factory=list)


class Document:
    """
    解析好的文档，供列表、搜索、取内容复用。

    两个数据源（docx / 飞书）都构造这个对象，所以下游（换 appId、生成 xlsx、报告）
    完全不用关心数据是哪来的。
    """

    def __init__(self, source: str, tables: list[TableSource],
                 warnings: list[str] | None = None) -> None:
        self.source = source                  # 本地路径或飞书文档链接，报错时给人看
        self.tables = tables
        self.warnings: list[str] = list(warnings or [])   # 结构上可疑的地方，报给调用方

    # --- 列表 / 搜索 ---

    def list(self) -> list[TableMeta]:
        return [t.meta for t in self.tables]

    def _match(self, keyword: str) -> list[TableMeta]:
        """宽松匹配：关键词的每个片段都出现在「期 + 小标题」里"""
        parts = tokens(keyword)
        if not parts:
            return self.list()
        return [t.meta for t in self.tables if all(p in haystack(t.meta) for p in parts)]

    def search(self, keyword: str) -> list[TableMeta]:
        """
        模糊搜索小标题，给人工/AI 选择用。

        先做片段匹配（忽略大小写、空格、全半角）；无命中时用 difflib 给相近候选。
        """
        hits = self._match(keyword)
        if hits:
            return hits
        candidates = {norm(t.meta.title): t.meta for t in self.tables}
        close = difflib.get_close_matches(norm(keyword), list(candidates), n=5, cutoff=0.4)
        return [candidates[c] for c in close]

    # --- 取内容 ---

    def content(self, keyword: str, period: str | None = None) -> TableContent:
        """
        取指定小标题的表内容。

        关键词可以只写一部分（如"预订-入住"），但必须唯一命中；命中多张时抛错并列出候选，
        由人工指定更具体的关键词或加 period——不允许工具自己挑一张。
        列不符合契约时抛 ColumnContractError。
        """
        matched = self._match(keyword)
        if period:
            matched = [m for m in matched if norm(period) in norm(m.period)]
        if not matched:
            tips = "、".join(f"{m.period} {m.title}" for m in self.search(keyword)[:5])
            raise TableNotFoundError(f"没找到小标题「{keyword}」。相近候选：{tips or '（无）'}")
        if len(matched) > 1:
            tips = "、".join(f"「{m.period} {m.title}」" for m in matched)
            raise TableNotFoundError(
                f"关键词「{keyword}」命中多张表：{tips}。请写得更具体，或用 --period 限定"
            )

        meta = matched[0]
        check_columns(meta)
        source = next(t for t in self.tables if t.meta is meta)
        return rows_to_content(meta, source.rows)
