"""
tests/test_shark.py

覆盖 shark.py：分组规则、skip 原因、xlsx 生成（按模板 zip 结构重写 sheet1.xml）、报告与 JSON 输出。
不依赖 openpyxl——生成的文件直接用 ElementTree 读回校验。

运行：pytest tests/test_shark.py -v
"""
from __future__ import annotations

import io
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import shark
import shark_template
from models import EXPECTED_COLUMNS, DocRow, KeyMatch, TableContent, TableMeta

REPO_TEMPLATE = Path(__file__).parent.parent / "doc" / "PrePublishImportTranslation.xlsx"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

SHEET_STUB = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<sheetData/></worksheet>"
)


def _make_template(tmp_path: Path) -> Path:
    """造一个最小模板：只要结构对，其余部件用来验证"原样复制"是否成立"""
    path = tmp_path / "template.xlsx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", SHEET_STUB)
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/styles.xml", "<styleSheet/>")
        z.writestr("[Content_Types].xml", "<Types/>")
    return path


def _content(rows: list[DocRow], title="预订-入住  (copywriter 1)", empty_key_seqs=None) -> TableContent:
    return TableContent(
        meta=TableMeta(
            period="第二期： ddl 3 Jul (complete)",
            title=title,
            columns=list(EXPECTED_COLUMNS),
            row_count=len(rows),
            order=6,
        ),
        rows=rows,
        empty_key_seqs=empty_key_seqs or [],
    )


def _row(seq: str, key: str, zh: str, en: str, cw: str) -> DocRow:
    return DocRow(seq=seq, key=key, zh_cn=zh, en_us=en, en_copywriter=cw)


def _match(doc_key: str, appids: list[str] | None = None, real_key: str | None = None,
           candidates: dict[str, list[str]] | None = None) -> KeyMatch:
    appids = list(appids or [])
    return KeyMatch(
        doc_key=doc_key,
        real_key=real_key or (doc_key if appids else None),
        appids=appids,
        candidates=candidates or {},
    )


def _lookup(**kw) -> dict[str, KeyMatch]:
    """_lookup(k_ok=["100074326"]) → {doc_key: KeyMatch}"""
    return {k: _match(k, v) for k, v in kw.items()}


# ---------------------------------------------------------------------------
# 组装规则
# ---------------------------------------------------------------------------


def test_build_rows_groups_and_filters():
    content = _content(
        [
            _row("1", "k_ok", "中文1", "AI1", "Copy1"),
            _row("2", "k_empty_cw", "中文2", "AI2", ""),        # 校对值为空 → 不导出
            _row("3", "k_unknown", "中文3", "AI3", "Copy3"),    # 查不到 appId → 不导出
            _row("4", "k_multi", "中文4", "AI4", "Copy4"),      # 命中多个 appId → 不导出
            _row("5", "k_ok", "中文5", "AI5", "Copy5"),         # 重复 key → 取最后一条
        ],
        empty_key_seqs=["9"],
    )
    appid_by_key = _lookup(k_ok=["100074326"], k_multi=["100074326", "100061217"])

    rows, skipped, expanded = shark.build_rows(content, appid_by_key)

    assert expanded == {}                     # 没有数字 key，不需要还原
    assert len(rows) == 1
    assert rows[0].en_us == "Copy5"           # 重复 key 取最后一条
    assert rows[0].zh_cn == "中文5"
    assert rows[0].appid == "100074326"
    assert rows[0].description == ""          # Description 固定留空

    reasons = {(s.key, s.reason) for s in skipped}
    assert ("k_empty_cw", "empty_copywriter") in reasons
    assert ("k_unknown", "not_found") in reasons
    assert ("k_multi", "multi_appid") in reasons
    assert ("", "empty_key") in reasons


def test_build_rows_expands_truncated_numeric_key():
    """文档里的纯数字 key 是截断过的，写进文件的必须是库里完整的那个"""
    content = _content([
        _row("1", "10194060", "中文1", "AI1", "Copy1"),
        _row("2", "k_plain", "中文2", "AI2", "Copy2"),
    ])
    lookup = {
        "10194060": _match("10194060", ["100074326"], real_key="1019406012"),
        "k_plain": _match("k_plain", ["100074326"]),
    }

    rows, skipped, expanded = shark.build_rows(content, lookup)

    assert [r.key for r in rows] == ["1019406012", "k_plain"]   # 数字 key 用了真身
    assert expanded == {"10194060": "1019406012"}               # 并把还原记录进报告
    assert skipped == []


def test_build_rows_numeric_key_with_multiple_real_keys_is_skipped():
    """LIKE 前缀命中多个完整 key 时不能猜，交人工"""
    content = _content([_row("1", "10194060", "中文1", "AI1", "Copy1")])
    lookup = {"10194060": _match(
        "10194060",
        candidates={"1019406012": ["100074326"], "1019406013": ["100074328"]},
    )}

    rows, skipped, expanded = shark.build_rows(content, lookup)

    assert rows == []
    assert expanded == {}
    assert len(skipped) == 1
    assert skipped[0].reason == "multi_real_key"
    assert "1019406012" in skipped[0].detail and "1019406013" in skipped[0].detail


def test_build_rows_without_lookup_entry_is_not_found():
    """lookup 里没有这个 key（比如查询漏了）也要归类成 not_found，不能当成已找到"""
    content = _content([_row("1", "k_missing", "中文1", "AI1", "Copy1")])
    rows, skipped, _ = shark.build_rows(content, {})
    assert rows == []
    assert skipped[0].reason == "not_found"


def test_group_by_appid_keeps_first_seen_order():
    rows = [
        shark.SharkRow(appid="B", key="k1", en_us="a", zh_cn="甲"),
        shark.SharkRow(appid="A", key="k2", en_us="b", zh_cn="乙"),
        shark.SharkRow(appid="B", key="k3", en_us="c", zh_cn="丙"),
    ]
    grouped = shark.group_by_appid(rows)
    assert list(grouped) == ["B", "A"]
    assert len(grouped["B"]) == 2


# ---------------------------------------------------------------------------
# 写文件
# ---------------------------------------------------------------------------


def _read_sheet(path: Path):
    with zipfile.ZipFile(path) as z:
        return ET.fromstring(z.read(shark.SHEET_XML))


def test_write_workbook_structure(tmp_path):
    template = _make_template(tmp_path)
    out = tmp_path / "out.xlsx"
    rows = [
        shark.SharkRow(appid="100074326", key="reception_checkIn_success",
                       en_us="Check-in successful", zh_cn="入住成功"),
        shark.SharkRow(appid="100074326", key="payable_cancel",
                       en_us="Cancel", zh_cn="取消"),
    ]
    shark.write_workbook(str(out), rows, str(template))

    root = _read_sheet(out)
    assert root.find(f"{NS}dimension").get("ref") == "A1:D3"   # 表头 + 2 行

    xml_rows = root.findall(f"{NS}sheetData/{NS}row")
    assert [r.get("r") for r in xml_rows] == ["1", "2", "3"]

    header = [c.find(f"{NS}v").text for c in xml_rows[0]]
    assert header == ["TransKey", "Description", "en-US", "zh-CN"]

    first = [c.find(f"{NS}v").text for c in xml_rows[1]]
    assert first[0] == "reception_checkIn_success"
    assert first[1] in (None, "")          # Description 固定留空
    assert first[2] == "Check-in successful"
    assert first[3] == "入住成功"


def test_write_workbook_keeps_other_parts(tmp_path):
    template = _make_template(tmp_path)
    out = tmp_path / "out.xlsx"
    shark.write_workbook(str(out), [], str(template))

    with zipfile.ZipFile(template) as src, zipfile.ZipFile(out) as dst:
        assert sorted(src.namelist()) == sorted(dst.namelist())
        for name in src.namelist():
            if name != shark.SHEET_XML:
                assert src.read(name) == dst.read(name)


def test_write_workbook_empty_table_still_has_header(tmp_path):
    template = _make_template(tmp_path)
    out = tmp_path / "out.xlsx"
    shark.write_workbook(str(out), [], str(template))
    root = _read_sheet(out)
    assert root.find(f"{NS}dimension").get("ref") == "A1:D1"
    assert len(root.findall(f"{NS}sheetData/{NS}row")) == 1


def test_write_workbook_escapes_special_chars(tmp_path):
    template = _make_template(tmp_path)
    out = tmp_path / "out.xlsx"
    shark.write_workbook(
        str(out),
        [shark.SharkRow(appid="1", key="k", en_us="a & b <c>", zh_cn="甲\x0b乙")],
        str(template),
    )
    cell = _read_sheet(out).find(f"{NS}sheetData/{NS}row[2]")
    values = [c.find(f"{NS}v").text for c in cell]
    assert values[2] == "a & b <c>"     # 转义后能原样读回
    assert values[3] == "甲乙"          # 非法控制字符被丢掉


def test_broken_template_raises(tmp_path):
    bad = tmp_path / "bad.xlsx"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
    with pytest.raises(ValueError):
        shark.write_workbook(str(tmp_path / "out.xlsx"), [], str(bad))
    with pytest.raises(FileNotFoundError):
        shark.write_workbook(str(tmp_path / "out.xlsx"), [], str(tmp_path / "nope.xlsx"))


def test_safe_filename():
    assert shark.safe_filename("换房/升级  (copywriter 1)") == "换房_升级 (copywriter 1)"
    assert shark.safe_filename("a:b*c?") == "a_b_c_"


# ---------------------------------------------------------------------------
# 整体导出与报告
# ---------------------------------------------------------------------------


def test_export_writes_one_file_per_appid(tmp_path):
    template = _make_template(tmp_path)
    content = _content(
        [
            _row("1", "k1", "中文1", "AI1", "Copy1"),
            _row("2", "k2", "中文2", "AI2", "Copy2"),
            _row("3", "k3", "中文3", "AI3", "Copy3"),
        ]
    )
    appid_by_key = _lookup(k1=["100074326"], k2=["100074326"], k3=["100061217"])

    out = shark.export(content, appid_by_key, outdir=str(tmp_path / "dist"), template=str(template))

    assert len(out.results) == 2
    by_appid = {r.appid: r for r in out.results}
    assert by_appid["100074326"].row_count == 2
    assert by_appid["100061217"].row_count == 1
    assert Path(by_appid["100074326"].file_path).name == "100074326_预订-入住 (copywriter 1).xlsx"
    assert Path(by_appid["100074326"].file_path).exists()


def test_export_flags_appid_outside_whitelist(tmp_path):
    template = _make_template(tmp_path)
    content = _content([_row("1", "k1", "中文1", "AI1", "Copy1")])
    out = shark.export(
        content, _lookup(k1=["999999999"]),
        outdir=str(tmp_path / "dist"), template=str(template), whitelist={"100074326"},
    )
    assert out.results[0].in_whitelist is False
    assert "不在白名单" in shark.render_report(out)


def test_export_without_rows_writes_nothing(tmp_path):
    template = _make_template(tmp_path)
    content = _content([_row("1", "k1", "中文1", "AI1", "")])   # 校对值为空
    out = shark.export(content, _lookup(k1=["100074326"]), outdir=str(tmp_path / "dist"), template=str(template))
    assert out.results == []
    assert not (tmp_path / "dist").exists()
    assert "无——没有可导出的行" in shark.render_report(out)


def test_report_and_json_shape(tmp_path):
    template = _make_template(tmp_path)
    content = _content(
        [_row("1", "k1", "中文1", "AI1", "Copy1"), _row("2", "k2", "中文2", "AI2", "Copy2")],
        empty_key_seqs=["7"],
    )
    out = shark.export(content, _lookup(k1=["100074326"]), outdir=str(tmp_path / "dist"), template=str(template))

    report = shark.render_report(out)
    assert "小标题：预订-入住  (copywriter 1)" in report
    assert "100074326" in report
    assert "key 在 i18n 库里查不到 appId" in report

    data = json.loads(json.dumps(shark.output_to_dict(out), ensure_ascii=False))
    assert data["files"][0]["row_count"] == 1
    assert {s["reason"] for s in data["skipped"]} == {"not_found", "empty_key"}


def test_export_without_any_template_file(tmp_path):
    """模板已内嵌，调用方不需要（也不该）传模板文件就能出文件"""
    content = _content([_row("1", "k1", "中文1", "AI1", "Copy1")])
    out = shark.export(content, _lookup(k1=["100074326"]), outdir=str(tmp_path / "dist"))

    path = Path(out.results[0].file_path)
    assert path.exists()
    with zipfile.ZipFile(path) as dst, zipfile.ZipFile(io.BytesIO(shark_template.TEMPLATE_BYTES)) as src:
        assert sorted(dst.namelist()) == sorted(src.namelist())
        for name in src.namelist():
            if name != shark.SHEET_XML:
                assert dst.read(name) == src.read(name)


def test_embedded_template_is_intact():
    """内嵌模板的哈希自检 + 至少是个能解析的 zip"""
    assert shark_template.verify() is True
    with zipfile.ZipFile(io.BytesIO(shark_template.TEMPLATE_BYTES)) as z:
        assert shark.SHEET_XML in z.namelist()


@pytest.mark.skipif(not REPO_TEMPLATE.exists(), reason="仓库里没有 shark 导入模板"
                                                      "（内嵌模板由 tools/embed_shark_template.py 从它生成）")
def test_embedded_matches_repo_template():
    """内嵌模板要和仓库里的真源一致——不一致说明改了模板却忘了重新生成"""
    assert shark_template.TEMPLATE_BYTES == REPO_TEMPLATE.read_bytes()


@pytest.mark.skipif(not REPO_TEMPLATE.exists(), reason="仓库里没有 shark 导入模板")
def test_real_template_parts(tmp_path):
    """显式传真模板时，部件应与它一致（只有 sheet1.xml 被重写）"""
    out = tmp_path / "real.xlsx"
    shark.write_workbook(
        str(out),
        [shark.SharkRow(appid="100074326", key="k", en_us="Copy", zh_cn="中文")],
        str(REPO_TEMPLATE),
    )
    with zipfile.ZipFile(REPO_TEMPLATE) as src, zipfile.ZipFile(out) as dst:
        assert sorted(src.namelist()) == sorted(dst.namelist())

    root = _read_sheet(out)
    assert root.find(f"{NS}dimension").get("ref") == "A1:D2"
    # 模板的第 2-4 行（释义/示例/上传提示）不能被写进去
    values = [c.find(f"{NS}v").text for c in root.find(f"{NS}sheetData/{NS}row[2]")]
    assert values[0] == "k"
