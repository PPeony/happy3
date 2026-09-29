"""
feishu_table.py — 从**飞书活文档**里取词条表（链路 B 的另一个数据源）

跟 `docx_table.py` 产出的东西一样（`TableMeta` / `TableContent` / `DocRow`，共用的部分在
`table_doc.py`），区别只是数据来源：这边直接调飞书接口读**当下这一份**，
不用先导出 135MB 的 docx，也不会读到旧快照。

文档结构对应关系（飞书的文档是"块树"）：

    期        → 1 级标题块（block_type 3）
    小标题     → 2 级及以上标题块（block_type 4~11）
    表格       → 表格块（block_type 31），children 按**行优先**平铺着一堆单元格块（32）
    单元格内容  → 单元格块的 children 里的文本块（2）

顺带把两件容易出问题的事记进 `document.warnings`：单元格数对不上"行×列"（可能有合并单元格）、
以及表格块没带行列数（老文档可能有）。
"""
from __future__ import annotations

from typing import Any

import feishu_doc
from feishu_doc import FeishuDocClient, FeishuDocError
from models import TableMeta
from table_doc import (
    Document,
    TableDocError,
    TableSource,
)

BLOCK_PAGE = 1
BLOCK_HEADING1 = 3
BLOCK_HEADING9 = 11
BLOCK_TABLE = 31
BLOCK_TABLE_CELL = 32


def _text_of(block_id: str, by_id: dict[str, dict], depth: int = 0) -> str:
    """把一个块（含它下面的子块）的文字拼出来——单元格里可能不止一个块"""
    block = by_id.get(block_id) or {}
    parts = []
    own = feishu_doc.block_plain_text(block)
    if own:
        parts.append(own)
    if depth < 4:                      # 防呆：真有环也不至于递归到爆栈
        for child_id in (block.get("children") or []):
            child_text = _text_of(child_id, by_id, depth + 1)
            if child_text:
                parts.append(child_text)
    return " ".join(parts).strip()


def _walk(block_id: str, by_id: dict[str, dict], out: list[dict]) -> None:
    """按文档顺序（深度优先）把块摊平——标题和表格的先后关系要保住"""
    block = by_id.get(block_id)
    if not block:
        return
    out.append(block)
    for child_id in (block.get("children") or []):
        _walk(child_id, by_id, out)


def _heading_level(block: dict) -> int | None:
    """标题级别：1 级标题 → 1，2 级标题 → 2……不是标题返回 None"""
    block_type = block.get("block_type")
    if isinstance(block_type, int) and BLOCK_HEADING1 <= block_type <= BLOCK_HEADING9:
        return block_type - (BLOCK_HEADING1 - 1)
    return None


def _table_rows(block: dict, by_id: dict[str, dict], warnings: list[str]) -> tuple[list[str], list[list[str]]]:
    """表格块 → (表头, 数据行)。单元格按行优先平铺，所以按列数切开就是行"""
    cells = block.get("children") or []
    property_ = (block.get("table") or {}).get("property") or {}
    row_size = property_.get("row_size")
    column_size = property_.get("column_size")

    if not column_size:
        column_size = len(cells) or 1
        warnings.append(f"表格块 {block.get('block_id')} 没带列数，按 {column_size} 猜的")

    if row_size and row_size * column_size != len(cells):
        warnings.append(
            f"表格 {block.get('block_id')} 的单元格数 {len(cells)} "
            f"≠ 行×列 {row_size}×{column_size}——可能有合并单元格，列位置会错位"
        )

    grid = [[_text_of(cell_id, by_id) for cell_id in cells[i:i + column_size]]
            for i in range(0, len(cells), column_size)]

    header = grid[0] if grid else []
    return header, grid[1:] if len(grid) > 1 else []


def load_document(link_or_id: str, client: FeishuDocClient | None = None) -> Document:
    """
    读飞书文档，抽出所有「小标题 + 紧跟的表格」。

    link_or_id 支持 `/docx/xxx` 和 `/wiki/xxx` 两种链接；client 只在测试里注入。
    """
    client = client or FeishuDocClient()

    try:
        document_id = client.resolve(link_or_id)
    except FeishuDocError as e:
        raise TableDocError(f"没法把链接换成文档 id：{e}")

    try:
        blocks = client.list_blocks(document_id)
    except FeishuDocError as e:
        raise TableDocError(f"读文档块失败：{e}")

    by_id = {b.get("block_id"): b for b in blocks}
    root = next((b for b in blocks if b.get("block_type") == BLOCK_PAGE), None) or by_id.get(document_id)
    if root is None:
        raise TableDocError(f"文档 {document_id} 里没找到根块，读出来 {len(blocks)} 个块")

    flat: list[dict] = []
    _walk(root.get("block_id"), by_id, flat)

    warnings: list[str] = []
    tables: list[TableSource] = []
    period = ""
    title = ""
    order = 0

    for block in flat:
        level = _heading_level(block)
        if level == 1:
            period = feishu_doc.block_plain_text(block)
            title = ""
            continue
        if level and level > 1:
            title = feishu_doc.block_plain_text(block)
            continue
        if block.get("block_type") != BLOCK_TABLE:
            continue

        order += 1
        columns, data_rows = _table_rows(block, by_id, warnings)
        tables.append(TableSource(
            meta=TableMeta(period=period, title=title, columns=columns,
                           row_count=len(data_rows), order=order),
            rows=data_rows,
        ))

    if not tables:
        warnings.append("这个文档里没读到任何表格——确认一下链接给的是不是那份词条表所在的文档")

    return Document(source=link_or_id, tables=tables, warnings=warnings)
