"""
tests/test_feishu_table.py

覆盖飞书数据源：把块树读成词条表（不联网，用假的 client + 假块）。
重点验三件事：标题→表格的归属、单元格按"行×列"切分、以及结构可疑时给警告。

运行：pytest tests/test_feishu_table.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# 飞书模块导入时要读一次应用凭据，这里塞假的，免得本机没配就跑不了
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

import feishu_table
from table_doc import ColumnContractError, TableDocError, TableNotFoundError

HEADER = ["序号", "key", "中文", "英文", "en-US by copywriters", "截图"]


# ---------------------------------------------------------------------------
# 造块
# ---------------------------------------------------------------------------


def _text_block(block_id, content, parent_id):
    return {"block_id": block_id, "block_type": 2, "parent_id": parent_id,
            "text": {"elements": [{"text_run": {"content": content}}]}}


def _cell(block_id, parent_id, text):
    cell = {"block_id": block_id, "block_type": 32, "parent_id": parent_id,
            "table_cell": {}, "children": [f"{block_id}_t"]}
    return cell, _text_block(f"{block_id}_t", text, block_id)


def _table_block(block_id, parent_id, rows, *, column_size=None, row_size=None):
    blocks, cell_ids, n = [], [], 0
    for row in rows:
        for value in row:
            n += 1
            cid = f"{block_id}_c{n}"
            cell, text_block = _cell(cid, parent_id, value)
            cell_ids.append(cid)
            blocks += [cell, text_block]
    prop = {"row_size": len(rows) if row_size is None else row_size,
            "column_size": len(rows[0]) if column_size is None else column_size}
    table = {"block_id": block_id, "block_type": 31, "parent_id": parent_id,
             "table": {"property": prop}, "children": cell_ids}
    return table, blocks


def _heading(block_id, parent_id, text, level):
    """标题块：文字在它自己的 headingN 字段里（跟真实文档一致），不是子块"""
    head = {"block_id": block_id, "block_type": 2 + level, "parent_id": parent_id,
            f"heading{level}": {"elements": [{"text_run": {"content": text}}]},
            "children": []}
    return head, []


def _doc(*groups):
    """每组是 (块, 它下面挂着的一批块)，全部挂在 page 下"""
    page = {"block_id": "page", "block_type": 1, "children": []}
    blocks = [page]
    for block, extras in groups:
        page["children"].append(block["block_id"])
        blocks.append(block)
        blocks.extend(extras)
    return blocks


class FakeClient:
    def __init__(self, blocks):
        self.blocks = blocks

    def resolve(self, link_or_id):
        return "docid"

    def list_blocks(self, document_id):
        return self.blocks


def _load(blocks):
    return feishu_table.load_document("https://x.feishu.cn/docx/abc", client=FakeClient(blocks))


# ---------------------------------------------------------------------------
# 基本读取
# ---------------------------------------------------------------------------


def test_reads_table_with_period_and_title():
    period = _heading("h_p", "page", "第二期：测试", 1)
    title = _heading("h_t", "h_p", "预订-入住  (copywriter 1)", 2)
    table, cells = _table_block("tbl", "h_t", [HEADER, ["1", "k_ok", "入住", "Check in", "Copy", ""]])
    doc = _load(_doc(period, title, (table, cells)))

    metas = doc.list()
    assert len(metas) == 1
    assert metas[0].period == "第二期：测试"
    assert metas[0].title == "预订-入住  (copywriter 1)"
    assert metas[0].columns == HEADER
    assert metas[0].row_count == 1
    assert metas[0].compliant is True

    content = doc.content("入住")
    assert len(content.rows) == 1
    assert content.rows[0].key == "k_ok"
    assert content.rows[0].en_copywriter == "Copy"


def test_only_the_first_table_after_title_belongs_to_it():
    """小标题后面紧跟的那张表才算它的；后面再出现表格归下一个标题"""
    period = _heading("h_p", "page", "第二期", 1)
    t1 = _heading("h1", "h_p", "预订-入住", 2)
    tbl1, cells1 = _table_block("tbl1", "h1", [HEADER, ["1", "a", "甲", "A", "A2", ""]])
    t2 = _heading("h2", "h_p", "同住", 2)
    tbl2, cells2 = _table_block("tbl2", "h2", [HEADER, ["1", "b", "乙", "B", "B2", ""]])
    doc = _load(_doc(period, t1, (tbl1, cells1), t2, (tbl2, cells2)))

    assert [m.title for m in doc.list()] == ["预订-入住", "同住"]
    assert doc.content("同住").rows[0].key == "b"


def test_empty_key_rows_are_skipped_and_recorded():
    period = _heading("h_p", "page", "第二期", 1)
    title = _heading("h_t", "h_p", "预订-入住", 2)
    table, cells = _table_block("tbl", "h_t", [
        HEADER,
        ["1", "k_ok", "甲", "A", "A2", ""],
        ["2", "", "乙", "B", "B2", ""],          # key 为空 → 跳过
    ])
    content = _load(_doc(period, title, (table, cells))).content("预订-入住")

    assert [r.key for r in content.rows] == ["k_ok"]
    assert content.empty_key_seqs == ["2"]


# ---------------------------------------------------------------------------
# 列契约：跟 docx 数据源共用同一套
# ---------------------------------------------------------------------------


def test_column_contract_is_enforced():
    period = _heading("h_p", "page", "第二期", 1)
    title = _heading("h_t", "h_p", "首页", 2)
    bad_header = ["序号", "key", "中文", "英文", "截图"]
    table, cells = _table_block("tbl", "h_t", [bad_header, ["1", "k", "甲", "A", ""]])
    doc = _load(_doc(period, title, (table, cells)))

    with pytest.raises(ColumnContractError) as err:
        doc.content("首页")
    assert "en-US by copywriters" in str(err.value)


def test_search_and_ambiguity_same_as_docx():
    period = _heading("h_p", "page", "第二期", 1)
    t1 = _heading("h1", "h_p", "预订-入住", 2)
    tbl1, cells1 = _table_block("tbl1", "h1", [HEADER, ["1", "a", "甲", "A", "A2", ""]])
    t2 = _heading("h2", "h_p", "预订-分房", 2)
    tbl2, cells2 = _table_block("tbl2", "h2", [HEADER, ["1", "b", "乙", "B", "B2", ""]])
    doc = _load(_doc(period, t1, (tbl1, cells1), t2, (tbl2, cells2)))

    assert len(doc.search("第二期 预订")) == 2
    with pytest.raises(TableNotFoundError):
        doc.content("预订")            # 命中多张 → 报错并列出候选
    assert doc.content("预订-分房").rows[0].key == "b"


# ---------------------------------------------------------------------------
# 结构可疑时要给警告（这是"直连读"能提前发现问题的关键）
# ---------------------------------------------------------------------------


def test_warns_when_cell_count_does_not_match_rows_times_columns():
    """单元格数 ≠ 行×列，多半是合并单元格——列位置会错位，必须报出来"""
    period = _heading("h_p", "page", "第二期", 1)
    title = _heading("h_t", "h_p", "预订-入住", 2)
    table, cells = _table_block("tbl", "h_t", [HEADER, ["1", "k", "甲", "A", "A2", ""]],
                                row_size=3)          # 撒谎：说 3 行，实际 2 行
    doc = _load(_doc(period, title, (table, cells)))

    assert any("合并单元格" in w for w in doc.warnings)


def test_warns_when_table_has_no_column_size():
    period = _heading("h_p", "page", "第二期", 1)
    title = _heading("h_t", "h_p", "预订-入住", 2)
    table, cells = _table_block("tbl", "h_t", [HEADER, ["1", "k", "甲", "A", "A2", ""]],
                                column_size=0)
    doc = _load(_doc(period, title, (table, cells)))

    assert any("没带列数" in w for w in doc.warnings)


def test_warns_when_document_has_no_table():
    period = _heading("h_p", "page", "第二期", 1)
    doc = _load(_doc(period))

    assert doc.list() == []
    assert any("没读到任何表格" in w for w in doc.warnings)


def test_read_failure_becomes_table_doc_error():
    class BrokenClient:
        def resolve(self, link_or_id):
            raise feishu_doc_error()

    def feishu_doc_error():
        from feishu_doc import FeishuDocError
        return FeishuDocError("拿不到文档")

    with pytest.raises(TableDocError):
        feishu_table.load_document("https://x.feishu.cn/wiki/abc", client=BrokenClient())
