"""
tests/test_cli.py

两类用例：
1. 单元测试 — mock 网络，验证纯逻辑（序列化、CSV 导入导出、白名单过滤、SQL 拼接、参数解析）
2. 集成测试（@pytest.mark.integration）— 真实调用 Archery，使用本地保存的凭据

运行单元测试：
    pytest tests/test_cli.py -v

运行集成测试（需内网 + 已保存凭据）：
    pytest tests/test_cli.py -m integration -v
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from archery import APPID_WHITELIST, ArcherySession
from cli import (
    CSV_FIELDNAMES,
    entries_from_csv,
    entries_from_json,
    entries_to_csv,
    entries_to_json,
    run_cli,
)
from models import Candidate, I18nEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_found(zh="金额", appid="100074326", key="pos.amount", en="Amount"):
    return I18nEntry(zh_cn=zh, status="found", trip_appid=appid, key=key, en_us=en)

def make_not_found(zh="未知词"):
    return I18nEntry(zh_cn=zh, status="not_found")

def make_ambiguous(zh="房间号", candidates=None):
    if candidates is None:
        candidates = [
            Candidate(trip_appid="100074326", key="pos.room.no",    en_us="Room No."),
            Candidate(trip_appid="100061217", key="hotel.room.num", en_us="Room Number"),
        ]
    return I18nEntry(zh_cn=zh, status="ambiguous", candidates=candidates)

def make_confirmed(zh="房间号", appid="100074326", key="pos.room.no", en="Room No."):
    return I18nEntry(zh_cn=zh, status="confirmed", trip_appid=appid, key=key, en_us=en)


# ---------------------------------------------------------------------------
# JSON serialize / deserialize
# ---------------------------------------------------------------------------

class TestJsonSerialization:
    def test_found_roundtrip(self):
        e = make_found()
        r = entries_from_json(entries_to_json([e]))[0]
        assert r.status == "found"
        assert r.trip_appid == e.trip_appid
        assert r.key == e.key
        assert r.en_us == e.en_us

    def test_not_found_roundtrip(self):
        r = entries_from_json(entries_to_json([make_not_found()]))[0]
        assert r.status == "not_found"

    def test_ambiguous_roundtrip(self):
        e = make_ambiguous()
        r = entries_from_json(entries_to_json([e]))[0]
        assert r.status == "ambiguous"
        assert len(r.candidates) == 2
        assert r.candidates[0].trip_appid == "100074326"
        assert r.candidates[1].key == "hotel.room.num"

    def test_confirmed_roundtrip(self):
        r = entries_from_json(entries_to_json([make_confirmed()]))[0]
        assert r.status == "confirmed"
        assert r.key == "pos.room.no"

    def test_order_preserved(self):
        entries = [make_found("金额"), make_not_found("未知词"), make_ambiguous("房间号")]
        restored = entries_from_json(entries_to_json(entries))
        assert [r.zh_cn for r in restored] == ["金额", "未知词", "房间号"]


# ---------------------------------------------------------------------------
# CSV export / import
# ---------------------------------------------------------------------------

class TestCsvRoundtrip:
    def _write_read(self, entries, ambiguous="all"):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            entries_to_csv(entries, path, ambiguous=ambiguous)
            return entries_from_csv(path)
        finally:
            os.unlink(path)

    def test_found_roundtrip(self):
        r = self._write_read([make_found()])[0]
        assert r.status == "found"
        assert r.key == "pos.amount"

    def test_not_found_roundtrip(self):
        r = self._write_read([make_not_found()])[0]
        assert r.status == "not_found"

    def test_ambiguous_all_writes_multiple_rows(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            count = entries_to_csv([make_ambiguous()], path, ambiguous="all")
            assert count == 2
            rows = list(csv.DictReader(open(path, encoding="utf-8-sig", newline="")))
            assert all(r["status"] == "ambiguous" for r in rows)
            assert all(r["note"] == "待研发确认" for r in rows)
        finally:
            os.unlink(path)

    def test_ambiguous_skip_writes_nothing(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            assert entries_to_csv([make_ambiguous()], path, ambiguous="skip") == 0
        finally:
            os.unlink(path)

    def test_ambiguous_one_row_becomes_confirmed(self):
        """研发删掉多余行只留一行 → 导入后升级为 confirmed"""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            entries_to_csv([make_ambiguous()], path, ambiguous="all")
            # 只保留第一行
            rows = list(csv.DictReader(open(path, encoding="utf-8-sig", newline="")))
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
                w.writeheader()
                w.writerow(rows[0])
            r = entries_from_csv(path)[0]
            assert r.status == "confirmed"
            assert r.trip_appid == "100074326"
            assert r.key == "pos.room.no"
        finally:
            os.unlink(path)

    def test_ambiguous_multiple_rows_stays_ambiguous(self):
        r = self._write_read([make_ambiguous()])[0]
        assert r.status == "ambiguous"
        assert len(r.candidates) == 2

    def test_found_note_is_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            entries_to_csv([make_found()], path)
            rows = list(csv.DictReader(open(path, encoding="utf-8-sig", newline="")))
            assert rows[0]["note"] == ""
        finally:
            os.unlink(path)

    def test_csv_has_note_column(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            entries_to_csv([make_found()], path)
            header = next(csv.reader(open(path, encoding="utf-8-sig", newline="")))
            assert "note" in header
        finally:
            os.unlink(path)

    def test_order_preserved(self):
        entries = [make_found("金额"), make_not_found("未知词"), make_confirmed("房间号")]
        restored = self._write_read(entries)
        assert [r.zh_cn for r in restored] == ["金额", "未知词", "房间号"]


# ---------------------------------------------------------------------------
# Whitelist filtering
# ---------------------------------------------------------------------------

class TestWhitelist:
    def test_known_appids_in_whitelist(self):
        for appid in ["100074326", "100061217", "100074830", "100075600"]:
            assert appid in APPID_WHITELIST, f"{appid} should be in whitelist"

    def test_unknown_appid_not_in_whitelist(self):
        assert "999999999" not in APPID_WHITELIST
        assert "100000001" not in APPID_WHITELIST

    def test_query_filters_non_whitelist_rows(self):
        session = ArcherySession(csrftoken="x", sessionid="y")
        with patch.object(session, "_post_sql") as mock_sql:
            mock_sql.return_value = {
                "status": 0,
                "data": {
                    "column_list": ["zh_cn", "trip_appid", "key", "en_us"],
                    "rows": [
                        ["金额", "100074326", "pos.amount", "Amount"],   # 白名单内
                        ["金额", "999999999", "bad.key",    "Amount"],   # 非白名单
                    ],
                }
            }
            results = session.query_texts(["金额"])
        assert results[0].status == "found"
        assert results[0].trip_appid == "100074326"

    def test_all_filtered_becomes_not_found(self):
        session = ArcherySession(csrftoken="x", sessionid="y")
        with patch.object(session, "_post_sql") as mock_sql:
            mock_sql.return_value = {
                "status": 0,
                "data": {
                    "column_list": ["zh_cn", "trip_appid", "key", "en_us"],
                    "rows": [["金额", "999999999", "bad.key", "Amount"]],
                }
            }
            results = session.query_texts(["金额"])
        assert results[0].status == "not_found"

    def test_multiple_whitelist_hits_become_ambiguous(self):
        session = ArcherySession(csrftoken="x", sessionid="y")
        with patch.object(session, "_post_sql") as mock_sql:
            mock_sql.return_value = {
                "status": 0,
                "data": {
                    "column_list": ["zh_cn", "trip_appid", "key", "en_us"],
                    "rows": [
                        ["房间号", "100074326", "pos.room.no",    "Room No."],
                        ["房间号", "100061217", "hotel.room.num", "Room Number"],
                    ],
                }
            }
            results = session.query_texts(["房间号"])
        assert results[0].status == "ambiguous"
        assert len(results[0].candidates) == 2


# ---------------------------------------------------------------------------
# SQL building
# ---------------------------------------------------------------------------

class TestSqlBuilding:
    def test_single_quote_escaped(self):
        s = ArcherySession(csrftoken="x", sessionid="y")
        assert s._escape("it's") == "it''s"

    def test_query_uses_in_and_self_join(self):
        texts = ["金额", "房间号"]
        session = ArcherySession(csrftoken="x", sessionid="y")
        captured = []

        def fake_post(sql, **kw):
            captured.append(sql)
            return {"status": 0, "data": {"column_list": ["zh_cn","trip_appid","key","en_us"], "rows": []}}

        with patch.object(session, "_post_sql", side_effect=fake_post):
            session.query_texts(texts)

        assert len(captured) == 1
        sql = captured[0].upper()
        assert "IN" in sql
        assert "LEFT JOIN" in sql
        assert "'金额'" in captured[0]
        assert "'房间号'" in captured[0]

    def test_only_one_request_for_multiple_texts(self):
        session = ArcherySession(csrftoken="x", sessionid="y")
        call_count = [0]

        def fake_post(sql, **kw):
            call_count[0] += 1
            return {"status": 0, "data": {"column_list": ["zh_cn","trip_appid","key","en_us"], "rows": []}}

        with patch.object(session, "_post_sql", side_effect=fake_post):
            session.query_texts(["金额", "房间号", "获取微信实例异常"])

        assert call_count[0] == 1, "should only make 1 HTTP request for batch query"


# ---------------------------------------------------------------------------
# CLI argument parsing (mock _get_session)
# ---------------------------------------------------------------------------

class TestCliArgParsing:
    def _mock_session(self, entries):
        s = MagicMock()
        s.query_texts.return_value = entries
        return s

    def test_query_stdout(self, capsys):
        with patch("cli._get_session", return_value=self._mock_session([make_found("金额")])):
            run_cli(["query", "--texts", '["金额"]', "--use-saved-creds"])
        data = json.loads(capsys.readouterr().out)
        assert data["results"][0]["zh_cn"] == "金额"
        assert data["results"][0]["status"] == "found"

    def test_query_to_file(self, tmp_path):
        out = str(tmp_path / "r.json")
        with patch("cli._get_session", return_value=self._mock_session([make_found("金额")])):
            run_cli(["query", "--texts", '["金额"]', "--use-saved-creds", "--output", out])
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        assert data["results"][0]["key"] == "pos.amount"

    def test_full_outputs_csv(self, tmp_path):
        out = str(tmp_path / "out.csv")
        entries = [make_found("金额"), make_not_found("未知词")]
        with patch("cli._get_session", return_value=self._mock_session(entries)):
            run_cli(["full", "--texts", '["金额","未知词"]', "--use-saved-creds", "--output", out])
        rows = list(csv.DictReader(open(out, encoding="utf-8-sig", newline="")))
        assert len(rows) == 2
        status_map = {r["zh_cn"]: r["status"] for r in rows}
        assert status_map["金额"] == "found"
        assert status_map["未知词"] == "not_found"

    def test_export_from_json_default_all(self, tmp_path):
        jpath = str(tmp_path / "r.json")
        cpath = str(tmp_path / "out.csv")
        Path(jpath).write_text(
            json.dumps(entries_to_json([make_ambiguous("房间号")]), ensure_ascii=False),
            encoding="utf-8"
        )
        run_cli(["export", "--input", jpath, "--output", cpath])
        rows = list(csv.DictReader(open(cpath, encoding="utf-8-sig", newline="")))
        assert len(rows) == 2
        assert all(r["note"] == "待研发确认" for r in rows)

    def test_export_ambiguous_skip(self, tmp_path):
        jpath = str(tmp_path / "r.json")
        cpath = str(tmp_path / "out.csv")
        Path(jpath).write_text(
            json.dumps(entries_to_json([make_ambiguous("房间号")]), ensure_ascii=False),
            encoding="utf-8"
        )
        run_cli(["export", "--input", jpath, "--output", cpath, "--ambiguous", "skip"])
        rows = list(csv.DictReader(open(cpath, encoding="utf-8-sig", newline="")))
        assert len(rows) == 0

    def test_texts_file_input(self, tmp_path):
        tpath = str(tmp_path / "texts.json")
        opath = str(tmp_path / "out.json")
        Path(tpath).write_text(
            json.dumps({"texts": ["金额", "房间号"]}, ensure_ascii=False),
            encoding="utf-8"
        )
        entries = [make_found("金额"), make_ambiguous("房间号")]
        with patch("cli._get_session", return_value=self._mock_session(entries)):
            run_cli(["query", "--texts-file", tpath, "--use-saved-creds", "--output", opath])
        data = json.loads(Path(opath).read_text(encoding="utf-8"))
        zhs = [r["zh_cn"] for r in data["results"]]
        assert "金额" in zhs and "房间号" in zhs

    def test_missing_auth_exits(self):
        with pytest.raises(SystemExit):
            run_cli(["query", "--texts", '["金额"]'])  # no auth args


# ---------------------------------------------------------------------------
# Integration tests — real network, saved credentials
# ---------------------------------------------------------------------------

TEXTS = ["金额", "房间号", "获取微信实例异常"]
TEXTS_JSON = json.dumps(TEXTS)


@pytest.mark.integration
class TestIntegration:
    """
    前置条件：
    - 内网可访问 archery.rezen.work
    - ~/.happyhappyhappy/credentials.json 中有有效账密

    运行：pytest tests/test_cli.py -m integration -v
    """

    def test_query_returns_all_texts(self, tmp_path):
        out = str(tmp_path / "r.json")
        run_cli(["query", "--texts", TEXTS_JSON, "--use-saved-creds", "--output", out])
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        zhs = [r["zh_cn"] for r in data["results"]]
        for t in TEXTS:
            assert t in zhs, f"missing: {t}"

    def test_query_all_statuses_valid(self, tmp_path):
        out = str(tmp_path / "r.json")
        run_cli(["query", "--texts", TEXTS_JSON, "--use-saved-creds", "--output", out])
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        valid = {"found", "not_found", "ambiguous", "confirmed"}
        for r in data["results"]:
            assert r["status"] in valid, f"bad status: {r}"

    def test_query_found_has_appid_and_key(self, tmp_path):
        out = str(tmp_path / "r.json")
        run_cli(["query", "--texts", TEXTS_JSON, "--use-saved-creds", "--output", out])
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        for r in data["results"]:
            if r["status"] == "found":
                assert r.get("trip_appid"), f"found but no trip_appid: {r}"
                assert r.get("key"),        f"found but no key: {r}"

    def test_query_appid_in_whitelist(self, tmp_path):
        out = str(tmp_path / "r.json")
        run_cli(["query", "--texts", TEXTS_JSON, "--use-saved-creds", "--output", out])
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        for r in data["results"]:
            if r["status"] in ("found", "confirmed"):
                assert r["trip_appid"] in APPID_WHITELIST, \
                    f"appid {r['trip_appid']} not in whitelist"

    def test_full_csv_exists_and_has_rows(self, tmp_path):
        out = str(tmp_path / "out.csv")
        run_cli(["full", "--texts", TEXTS_JSON, "--use-saved-creds", "--output", out])
        assert Path(out).exists()
        rows = list(csv.DictReader(open(out, encoding="utf-8-sig", newline="")))
        assert len(rows) >= len(TEXTS)

    def test_full_csv_columns_complete(self, tmp_path):
        out = str(tmp_path / "out.csv")
        run_cli(["full", "--texts", TEXTS_JSON, "--use-saved-creds", "--output", out])
        header = next(csv.reader(open(out, encoding="utf-8-sig", newline="")))
        for col in ["trip_appid", "key", "zh_cn", "en_us", "status", "note"]:
            assert col in header, f"missing column: {col}"

    def test_full_csv_zh_cn_matches_input(self, tmp_path):
        out = str(tmp_path / "out.csv")
        run_cli(["full", "--texts", TEXTS_JSON, "--use-saved-creds", "--output", out])
        rows = list(csv.DictReader(open(out, encoding="utf-8-sig", newline="")))
        for row in rows:
            assert row["zh_cn"] in TEXTS, f"unexpected zh_cn: {row['zh_cn']}"

    def test_full_then_reimport_roundtrip(self, tmp_path):
        out = str(tmp_path / "out.csv")
        run_cli(["full", "--texts", TEXTS_JSON, "--use-saved-creds", "--output", out])
        restored = entries_from_csv(out)
        valid = {"found", "not_found", "ambiguous", "confirmed"}
        for e in restored:
            assert e.status in valid
            assert e.zh_cn in TEXTS
