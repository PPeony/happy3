"""
docx_table.py — 从《RezenOne翻译需求》docx 里取词条表

文档结构（按正文顺序）：期（1 级标题）→ 小标题（2/3 级标题）→ 紧跟的一张表格。
本模块只读 zip 包里的 word/document.xml，不解压内嵌截图——文档 135MB，几乎全是单元格截图，
整包解压既慢又占内存。

表的列契约见 models.EXPECTED_COLUMNS，列名或顺序不符会直接报错，不做兼容猜测。
"""
from __future__ import annotations

import difflib
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

from models import EXPECTED_COLUMNS, DocRow, TableContent, TableMeta

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOCUMENT_XML = "word/document.xml"


class DocxError(Exception):
    """文档解析失败"""


class DocxColumnError(DocxError):
    """表格列不符合六列契约"""


class TableNotFoundError(DocxError):
    """小标题没匹配到表格"""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _cell_text(tc: Any) -> str:
    """单元格文本：段落之间用空格连接"""
    parts = []
    for p in tc.findall(f"{W}p"):
        parts.append("".join(t.text or "" for t in p.iter(f"{W}t")).strip())
    return " ".join(x for x in parts if x).strip()


def _row_cells(tr: Any) -> list[str]:
    return [_cell_text(tc) for tc in tr.findall(f"{W}tc")]


def _heading_level(p: Any) -> int | None:
    """
    标题级别：0 = 期，>=1 = 小标题。

    优先看 w:outlineLvl；没有时退回 pStyle（本套文档的样式 id 与级别差 1）。
    """
    pr = p.find(f"{W}pPr")
    if pr is None:
        return None
    lvl = pr.find(f"{W}outlineLvl")
    if lvl is not None:
        val = lvl.get(f"{W}val")
        if val is not None and val.isdigit():
            return int(val)
    style = pr.find(f"{W}pStyle")
    if style is not None:
        val = style.get(f"{W}val") or ""
        if val in ("1", "2", "3"):
            return int(val) - 1
    return None


def _paragraph_text(p: Any) -> str:
    return "".join(t.text or "" for t in p.iter(f"{W}t")).strip()


def _norm(text: str) -> str:
    """匹配用归一化：全角转半角、去大小写、去掉所有空白"""
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", text).lower()


def _tokens(keyword: str) -> list[str]:
    """关键词按空白切成若干片段，每个片段都要命中才算匹配"""
    return [t for t in (_norm(x) for x in re.split(r"\s+", (keyword or "").strip())) if t]


def _haystack(meta: TableMeta) -> str:
    """匹配用文本：「期 + 小标题」拼在一起，方便"第二期 预订-入住"这类输入"""
    return _norm(f"{meta.period}{meta.title}")


@dataclass
class ParsedTable:
    meta: TableMeta
    element: Any


@dataclass
class Document:
    """解析后的文档，供列表、搜索、取内容复用（避免重复解 zip）"""
    path: str
    tables: list[ParsedTable] = field(default_factory=list)

    def list(self) -> list[TableMeta]:
        return [t.meta for t in self.tables]

    def _match(self, keyword: str) -> list[TableMeta]:
        """宽松匹配：关键词的每个片段都出现在「期 + 小标题」里"""
        tokens = _tokens(keyword)
        if not tokens:
            return self.list()
        return [t.meta for t in self.tables if all(tok in _haystack(t.meta) for tok in tokens)]

    def search(self, keyword: str) -> list[TableMeta]:
        """
        模糊搜索小标题，给人工/AI 选择用。

        先做片段匹配（忽略大小写、空格、全半角）；无命中时用 difflib 给相近候选。
        """
        hits = self._match(keyword)
        if hits:
            return hits

        candidates = {_norm(t.meta.title): t.meta for t in self.tables}
        close = difflib.get_close_matches(_norm(keyword), list(candidates), n=5, cutoff=0.4)
        return [candidates[c] for c in close]

    def content(self, keyword: str, period: str | None = None) -> TableContent:
        """
        取指定小标题的表内容。

        关键词可以只写一部分（如"预订-入住"），但必须唯一命中；命中多张时抛错并列出候选，
        由人工指定更具体的关键词或加 period——不允许工具自己挑一张。
        列不符合契约时抛 DocxColumnError。
        """
        matched = self._match(keyword)
        if period:
            matched = [m for m in matched if _norm(period) in _norm(m.period)]
        if not matched:
            tips = "、".join(f"{m.period} {m.title}" for m in self.search(keyword)[:5])
            raise TableNotFoundError(f"没找到小标题「{keyword}」。相近候选：{tips or '（无）'}")
        if len(matched) > 1:
            tips = "、".join(f"「{m.period} {m.title}」" for m in matched)
            raise TableNotFoundError(f"关键词「{keyword}」命中多张表：{tips}。请写得更具体，或用 --period 限定")

        table = next(t for t in self.tables if t.meta is matched[0])
        _check_columns(table)
        return _extract(table)


def _check_columns(table: ParsedTable) -> None:
    actual = table.meta.columns
    if actual != EXPECTED_COLUMNS:
        raise DocxColumnError(
            f"表格「{table.meta.title}」的列不符合契约，无法解析。\n"
            f"  期望：{EXPECTED_COLUMNS}\n"
            f"  实际：{actual}\n"
            f"请让文档维护者把表头改成固定六列（列名与顺序都不能变）。"
        )


def _extract(table: ParsedTable) -> TableContent:
    rows = table.element.findall(f"{W}tr")
    content = TableContent(meta=table.meta)

    for tr in rows[1:]:  # 第一行是表头
        cells = _row_cells(tr)
        if not any(cells):
            continue  # 整行空白
        cells += [""] * (len(EXPECTED_COLUMNS) - len(cells))  # 缺列按空处理
        seq, key, zh_cn, en_us, en_copywriter = cells[:5]
        key = key.strip()
        if not key:
            content.empty_key_seqs.append(seq or "?")
            continue
        content.rows.append(
            DocRow(
                seq=seq,
                key=key,
                zh_cn=zh_cn,
                en_us=en_us,
                en_copywriter=en_copywriter,
            )
        )

    return content


def _localname(el: Any) -> str:
    """去掉命名空间前缀的标签名（标准库的 QName 没有 localname，这里自己切）"""
    tag = el.tag
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def load_document(docx_path: str) -> Document:
    """读取 docx，按正文顺序抽出所有小标题与表格"""
    try:
        with zipfile.ZipFile(docx_path) as z:
            xml = z.read(DOCUMENT_XML)
    except FileNotFoundError:
        raise DocxError(f"文档不存在：{docx_path}")
    except KeyError:
        raise DocxError(f"不是合法的 docx（缺少 {DOCUMENT_XML}）：{docx_path}")
    except zipfile.BadZipFile:
        raise DocxError(f"不是合法的 docx（zip 解析失败）：{docx_path}")

    try:
        body = ET.fromstring(xml).find(f"{W}body")
    except ET.ParseError as e:
        raise DocxError(f"document.xml 解析失败：{e}")

    doc = Document(path=docx_path)
    period = ""
    title = ""
    order = 0

    for el in body:
        tag = _localname(el)
        if tag == "p":
            level = _heading_level(el)
            text = _paragraph_text(el)
            if level is None or not text:
                continue
            if level == 0:
                period = text
            else:
                title = text
        elif tag == "tbl":
            order += 1
            rows = el.findall(f"{W}tr")
            columns = _row_cells(rows[0]) if rows else []
            doc.tables.append(
                ParsedTable(
                    meta=TableMeta(
                        period=period,
                        title=title,
                        columns=columns,
                        row_count=max(len(rows) - 1, 0),
                        order=order,
                    ),
                    element=el,
                )
            )

    return doc
