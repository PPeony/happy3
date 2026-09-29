"""
docx_table.py — 从**本地导出的 docx** 里取词条表（链路 B 的数据源之一）

文档结构（按正文顺序）：期（1 级标题）→ 小标题（2/3 级标题）→ 紧跟的一张表格。
只读 zip 包里的 `word/document.xml`，不解压内嵌截图——文档 135MB，几乎全是单元格截图，
整包解压既慢又占内存。

跟来源无关的部分（六列契约、模糊匹配、抽行）在 `table_doc.py` 里，跟飞书数据源共用。
另一个数据源见 `feishu_table.py`（直接读飞书活文档，不用先导出）。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from models import EXPECTED_COLUMNS  # noqa: F401  (兼容外部 import)
from table_doc import (
    ColumnContractError,
    Document as _BaseDocument,
    TableDocError,
    TableNotFoundError,  # noqa: F401  (外部还在用这个名字)
    TableSource,
)
from models import TableMeta

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOCUMENT_XML = "word/document.xml"

# 兼容旧名字：以前这两个错误类就叫 DocxError / DocxColumnError，
# cli.py 与测试都在用，保留别名不破坏调用方。
DocxError = TableDocError
DocxColumnError = ColumnContractError


class Document(_BaseDocument):
    """本地 docx 解析出来的文档（list / search / content 见 table_doc.Document）"""


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


def _localname(el: Any) -> str:
    """去掉命名空间前缀的标签名（标准库的 QName 没有 localname，这里自己切）"""
    tag = el.tag
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def load_document(docx_path: str) -> Document:
    """读 docx，按正文顺序抽出所有小标题与表格"""
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

    tables: list[TableSource] = []
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
            data_rows = [_row_cells(tr) for tr in rows[1:]]
            tables.append(TableSource(
                meta=TableMeta(period=period, title=title, columns=columns,
                               row_count=len(data_rows), order=order),
                rows=data_rows,
            ))

    return Document(source=docx_path, tables=tables)
