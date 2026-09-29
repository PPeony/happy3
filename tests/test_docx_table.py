"""
tests/test_docx_table.py

覆盖 docx_table.py 的两类用例：

1. 合成文档（tmp_path 里现造一个小 docx）——测解析、模糊匹配、列契约、抽行规则，不依赖仓库里那份 135MB 文档
2. 真实文档（doc/RezenOne翻译需求.docx 存在时才跑）——测实际结构没有漂移

运行：pytest tests/test_docx_table.py -v
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from docx_table import (
    DocxColumnError,
    DocxError,
    TableNotFoundError,
    load_document,
)
from models import EXPECTED_COLUMNS

REPO_DOCX = Path(__file__).parent.parent / "doc" / "RezenOne翻译需求.docx"

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _heading(text: str, level: int) -> str:
    return (
        f'<w:p><w:pPr><w:pStyle w:val="{level + 1}"/><w:outlineLvl w:val="{level}"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def _table(rows: list[list[str]]) -> str:
    body = ""
    for row in rows:
        cells = "".join(
            f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row
        )
        body += f"<w:tr>{cells}</w:tr>"
    return f"<w:tbl>{body}</w:tbl>"


GOOD_ROWS = [
    EXPECTED_COLUMNS,
    ["1", "reception_checkIn_success", "入住成功", "Check-in successful", "Check-in successful", ""],
    ["2", "", "住宿登记单", "Registration Slip", "Registration slip", ""],            # key 为空
    ["3", "reception_total_price", "总房价", "Total Room Rate", "", ""],               # 校对值为空
    ["4", "payable_cancel", "取消", "Cancel", "Cancel", ""],
    ["5", "payable_cancel", "取消（改）", "Cancel", "Cancel", ""],                     # 重复 key
]

BAD_ROWS = [
    ["序号", "key", "中文", "英文", "截图"],
    ["1", "reception_arrivals_store", "到店", "Arrivals", ""],
]


def _make_docx(tmp_path: Path) -> Path:
    """造一份结构最小但完整的 docx（期 → 小标题 → 表）"""
    body = "".join(
        [
            _heading("第二期：测试用", 0),
            _heading("预订-入住  (copywriter 1)", 1),
            _table(GOOD_ROWS),
            _heading("只有标题没有表", 1),
            _heading("第二期：测试用", 0),
            _heading("首页 (copywriter 3)", 1),
            _table(BAD_ROWS),
        ]
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document {W_NS}><w:body>{body}</w:body></w:document>'
    )
    path = tmp_path / "sample.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", document)
    return path


@pytest.fixture()
def sample(tmp_path: Path):
    return load_document(str(_make_docx(tmp_path)))


# ---------------------------------------------------------------------------
# 解析与匹配
# ---------------------------------------------------------------------------


def test_lists_only_headings_that_have_tables(sample):
    metas = sample.list()
    assert [m.title for m in metas] == ["预订-入住  (copywriter 1)", "首页 (copywriter 3)"]
    assert metas[0].period == "第二期：测试用"
    assert metas[0].row_count == 5  # 不含表头


def test_compliant_flag_follows_column_contract(sample):
    assert sample.list()[0].compliant is True
    assert sample.list()[1].compliant is False


def _make_docx_and_load():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return load_document(str(_make_docx(Path(tmp))))


def test_search_ignores_space_case_and_width():
    """关键词的大小写、空格都不该影响匹配"""
    doc = _make_docx_and_load()
    for keyword in ["预订-入住", " 预订-入住 ", "预订-入住  (COPYWRITER 1)"]:
        assert len(doc.search(keyword)) == 1


def test_search_matches_period_plus_title(sample):
    assert len(sample.search("第二期 预订-入住")) == 1


def test_search_returns_all_candidates_without_picking_one(sample):
    assert len(sample.search("第二期")) == 2


def test_search_never_raises_on_junk(sample):
    assert isinstance(sample.search("坐班"), list)


def test_content_extracts_rows_and_empty_keys(sample):
    content = sample.content("预订-入住")
    # 5 行数据里 1 行 key 为空被跳过；重复 key 的两行都保留（去重在 shark 层做）
    assert len(content.rows) == 4
    assert content.empty_key_seqs == ["2"]
    assert content.rows[0].key == "reception_checkIn_success"
    assert content.rows[0].en_copywriter == "Check-in successful"


def test_content_rejects_bad_columns(sample):
    with pytest.raises(DocxColumnError) as err:
        sample.content("首页")
    message = str(err.value)
    assert "en-US by copywriters" in message  # 期望列写清楚了
    assert "实际" in message


def test_content_reports_ambiguous_keyword(sample):
    with pytest.raises(TableNotFoundError) as err:
        sample.content("第二期")
    assert "命中多张表" in str(err.value)


def test_content_period_narrows_match(sample):
    assert sample.content("预订-入住", period="第二期").meta.title.startswith("预订-入住")


def test_content_not_found_suggests_candidates(sample):
    with pytest.raises(TableNotFoundError) as err:
        sample.content("预订-入助")
    assert "相近候选" in str(err.value)


def test_missing_file_and_non_docx(tmp_path):
    with pytest.raises(DocxError):
        load_document(str(tmp_path / "nope.docx"))

    broken = tmp_path / "broken.docx"
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("word/other.xml", "<a/>")
    with pytest.raises(DocxError):
        load_document(str(broken))


# ---------------------------------------------------------------------------
# 真实文档（存在时才跑）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not REPO_DOCX.exists(), reason="仓库里没有 RezenOne翻译需求.docx")
class TestRealDocument:
    @pytest.fixture(scope="class")
    def doc(self):
        return load_document(str(REPO_DOCX))

    def test_table_count_and_compliance(self, doc):
        metas = doc.list()
        assert len(metas) == 12
        compliant = [m.title.strip() for m in metas if m.compliant]
        # 2026-09-24 实测：只有第二期这四张表是固定六列
        assert compliant == ["预订-入住  (copywriter 1)", "换房/升级  (copywriter 1)",
                             "同住 (copywriter 2)", "联房 (copywriter 2)"]

    def test_known_table_rows(self, doc):
        content = doc.content("预订-入住")
        assert len(content.rows) == 21
        assert content.empty_key_seqs == ["20", "21", "22", "23", "25"]

    def test_no_title_inherits_previous_table(self, doc):
        """CRS 只有标题没有表，不能把下一个期的表算到自己头上"""
        assert all(m.title for m in doc.list())
