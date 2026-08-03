"""
tests/test_feishu.py

单元测试：mock 所有 HTTP，验证飞书 API 封装逻辑。

运行：
    pytest tests/test_feishu.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from feishu import FeishuClient, FeishuError, _note
from models import Candidate, I18nEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_found(zh="金额", appid="100074326", key="pos.amount", en="Amount"):
    return I18nEntry(zh_cn=zh, status="found", trip_appid=appid, key=key, en_us=en)

def make_not_found(zh="未知词"):
    return I18nEntry(zh_cn=zh, status="not_found")

def make_ambiguous(zh="房间号"):
    return I18nEntry(
        zh_cn=zh, status="ambiguous",
        candidates=[
            Candidate(trip_appid="100074326", key="pos.room.no",    en_us="Room No."),
            Candidate(trip_appid="100061217", key="hotel.room.num", en_us="Room Number"),
        ],
    )

def make_confirmed(zh="房间号", appid="100074326", key="pos.room.no", en="Room No."):
    return I18nEntry(zh_cn=zh, status="confirmed", trip_appid=appid, key=key, en_us=en)


def _ok(data: dict) -> MagicMock:
    """构造成功的 requests.Response mock。"""
    m = MagicMock()
    m.raise_for_status = MagicMock()
    m.json.return_value = {"code": 0, **data}
    return m


# ---------------------------------------------------------------------------
# get_token
# ---------------------------------------------------------------------------

class TestGetToken:
    def test_success(self):
        client = FeishuClient("aid", "asec")
        with patch("feishu.requests.post", return_value=_ok({"tenant_access_token": "t-abc"})):
            token = client._get_token()
        assert token == "t-abc"

    def test_cached(self):
        client = FeishuClient("aid", "asec")
        with patch("feishu.requests.post", return_value=_ok({"tenant_access_token": "t-abc"})) as mock_post:
            client._get_token()
            client._get_token()
        assert mock_post.call_count == 1  # 第二次不发请求

    def test_failure_raises(self):
        client = FeishuClient("aid", "asec")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"code": 99, "msg": "invalid app_secret"}
        with patch("feishu.requests.post", return_value=resp):
            with pytest.raises(FeishuError, match="获取 token 失败"):
                client._get_token()


# ---------------------------------------------------------------------------
# create_bitable
# ---------------------------------------------------------------------------

class TestCreateBitable:
    def _mock_client(self) -> FeishuClient:
        c = FeishuClient("aid", "asec")
        c._token = "t-fake"
        return c

    def test_returns_token_and_url(self):
        client = self._mock_client()
        create_resp = _ok({
            "data": {"app": {"app_token": "bascnABC", "url": "https://feishu.cn/base/bascnABC"}}
        })
        list_resp = _ok({
            "data": {"items": [{"table_id": "tbl001", "name": "数据表"}]}
        })
        with patch("feishu.requests.post", return_value=create_resp), \
             patch("feishu.requests.get", return_value=list_resp):
            app_token, table_id, url = client.create_bitable("测试表格")

        assert app_token == "bascnABC"
        assert table_id  == "tbl001"
        assert "bascnABC" in url

    def test_no_tables_raises(self):
        client = self._mock_client()
        create_resp = _ok({
            "data": {"app": {"app_token": "bascnABC", "url": "https://feishu.cn/base/bascnABC"}}
        })
        list_resp = _ok({"data": {"items": []}})
        with patch("feishu.requests.post", return_value=create_resp), \
             patch("feishu.requests.get",  return_value=list_resp):
            with pytest.raises(FeishuError, match="默认数据表"):
                client.create_bitable("测试表格")


# ---------------------------------------------------------------------------
# setup_fields
# ---------------------------------------------------------------------------

class TestSetupFields:
    def test_creates_all_fields(self):
        client = FeishuClient("aid", "asec")
        client._token = "t-fake"
        field_resp = _ok({"data": {"field": {"field_id": "fldXXX"}}})
        with patch("feishu.requests.post", return_value=field_resp) as mock_post:
            client.setup_fields("bascnABC", "tbl001")
        # _FIELD_DEFS 共 7 个字段
        assert mock_post.call_count == 7


# ---------------------------------------------------------------------------
# upload_image
# ---------------------------------------------------------------------------

class TestUploadImage:
    def test_returns_file_token(self, tmp_path):
        img = tmp_path / "screen.png"
        img.write_bytes(b"\x89PNG\r\n" + b"\x00" * 100)  # fake PNG

        client = FeishuClient("aid", "asec")
        client._token = "t-fake"
        upload_resp = _ok({"data": {"file_token": "boxbcXXX"}})
        with patch("feishu.requests.post", return_value=upload_resp):
            ft = client.upload_image(str(img), "bascnABC")
        assert ft == "boxbcXXX"

    def test_api_error_raises(self, tmp_path):
        img = tmp_path / "screen.png"
        img.write_bytes(b"\x00" * 10)
        client = FeishuClient("aid", "asec")
        client._token = "t-fake"
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"code": 1061073, "msg": "no scope auth"}
        with patch("feishu.requests.post", return_value=resp):
            with pytest.raises(FeishuError, match="上传图片"):
                client.upload_image(str(img), "bascnABC")


# ---------------------------------------------------------------------------
# batch_write_records
# ---------------------------------------------------------------------------

class TestBatchWriteRecords:
    def _client(self):
        c = FeishuClient("aid", "asec")
        c._token = "t-fake"
        return c

    def _batch_resp(self):
        return _ok({"data": {"records": []}})

    def test_found_entry_written(self):
        client = self._client()
        entries = [make_found()]
        with patch("feishu.requests.post", return_value=self._batch_resp()) as mock_post:
            written = client.batch_write_records("bascnABC", "tbl001", entries)
        assert written == 1
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        rec = payload["records"][0]["fields"]
        assert rec["zh_cn"]      == "金额"
        assert rec["key"]        == "pos.amount"
        assert rec["trip_appid"] == "100074326"
        assert rec["status"]     == "found"
        assert "截图" not in rec  # 没传 file_token

    def test_ambiguous_expands_to_multiple_rows(self):
        client = self._client()
        entries = [make_ambiguous()]
        with patch("feishu.requests.post", return_value=self._batch_resp()) as mock_post:
            written = client.batch_write_records("bascnABC", "tbl001", entries)
        assert written == 2  # 2 个候选展开为 2 行
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        notes = [r["fields"]["note"] for r in payload["records"]]
        assert all(n == "待研发确认" for n in notes)

    def test_screenshot_attached_when_file_token_given(self):
        client = self._client()
        entries = [make_found()]
        with patch("feishu.requests.post", return_value=self._batch_resp()) as mock_post:
            client.batch_write_records("bascnABC", "tbl001", entries, file_token="boxbcXXX")
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        rec = payload["records"][0]["fields"]
        assert rec["截图"] == [{"file_token": "boxbcXXX"}]

    def test_not_found_written_correctly(self):
        client = self._client()
        entries = [make_not_found()]
        with patch("feishu.requests.post", return_value=self._batch_resp()) as mock_post:
            written = client.batch_write_records("bascnABC", "tbl001", entries)
        assert written == 1
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        rec = payload["records"][0]["fields"]
        assert rec["status"]  == "not_found"
        assert rec["note"]    == ""
        assert rec["key"]     == ""

    def test_batch_split_on_large_input(self):
        """超过 500 条时应分多次 POST。"""
        client = self._client()
        entries = [make_found(zh=f"词{i}") for i in range(1050)]
        with patch("feishu.requests.post", return_value=self._batch_resp()) as mock_post:
            written = client.batch_write_records("bascnABC", "tbl001", entries, batch_size=500)
        assert written == 1050
        assert mock_post.call_count == 3  # 500 + 500 + 50


# ---------------------------------------------------------------------------
# _note helper
# ---------------------------------------------------------------------------

class TestNoteHelper:
    def test_ambiguous_note(self):
        assert _note(make_ambiguous()) == "待研发确认"

    def test_found_note_empty(self):
        assert _note(make_found()) == ""

    def test_not_found_note_empty(self):
        assert _note(make_not_found()) == ""

    def test_confirmed_note_empty(self):
        assert _note(make_confirmed()) == ""


# ---------------------------------------------------------------------------
# create_and_upload (integration of all steps, all mocked)
# ---------------------------------------------------------------------------

class TestCreateAndUpload:
    def test_full_flow_without_screenshot(self):
        client = FeishuClient("aid", "asec")
        client._token = "t-fake"

        create_resp  = _ok({"data": {"app": {"app_token": "bascnABC",
                                             "url": "https://feishu.cn/base/bascnABC"}}})
        list_resp    = _ok({"data": {"items": [{"table_id": "tbl001"}]}})
        field_resp   = _ok({"data": {"field": {}}})
        records_resp = _ok({"data": {"records": []}})

        post_responses = [create_resp] + [field_resp] * 7 + [records_resp]

        with patch("feishu.requests.post", side_effect=post_responses), \
             patch("feishu.requests.get",  return_value=list_resp):
            url = client.create_and_upload([make_found()], title="测试")

        assert "bascnABC" in url

    def test_full_flow_with_screenshot(self, tmp_path):
        img = tmp_path / "ui.png"
        img.write_bytes(b"\x89PNG\r\n" + b"\x00" * 50)

        client = FeishuClient("aid", "asec")
        client._token = "t-fake"

        create_resp  = _ok({"data": {"app": {"app_token": "bascnABC",
                                             "url": "https://feishu.cn/base/bascnABC"}}})
        list_resp    = _ok({"data": {"items": [{"table_id": "tbl001"}]}})
        field_resp   = _ok({"data": {"field": {}}})
        upload_resp  = _ok({"data": {"file_token": "boxbcXXX"}})
        records_resp = _ok({"data": {"records": []}})

        # 顺序：create_bitable POST, setup_fields x7, upload_image POST, batch_write POST
        post_responses = [create_resp] + [field_resp] * 7 + [upload_resp, records_resp]

        with patch("feishu.requests.post", side_effect=post_responses), \
             patch("feishu.requests.get",  return_value=list_resp):
            url = client.create_and_upload(
                [make_found(), make_ambiguous()],
                title="截图测试",
                screenshot_path=str(img),
            )

        assert "bascnABC" in url
