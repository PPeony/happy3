"""
tests/test_archery_lookup.py

覆盖 archery.ArcherySession.query_appid_by_keys：
  - 普通 key：精确匹配（IN），不按语种过滤；库里写法与文档不同（大小写）时映射回文档的 key
  - 纯数字 key：文档里只剩截断后的一段数字，按「带下划线的后缀 → 任意字符的后缀 → 包含」
    三趟 LIKE 找回完整 key（真身形态已确认：形如 reception_error_10194060）

不连网：把 _post_sql 换掉，只检查生成的 SQL 和解析结果。

运行：pytest tests/test_archery_lookup.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from archery import NUMERIC_PASSES, ArcherySession

# 三趟在 SQL 里长这样（\ 是给 LIKE 转义下划线用的，不然 _ 会被当成单字符通配符）
PRECISE_SUFFIX = r"LIKE '%\_10194060'"
LOOSE_SUFFIX = r"LIKE '%10194060'"
CONTAINS = r"LIKE '%10194060%'"


def _rows(*pairs) -> dict:
    """造一个 Archery 风格的返回体"""
    return {
        "status": 0,
        "data": {
            "column_list": ["trip_appid", "key"],
            "rows": [list(p) for p in pairs],
        },
    }


class FakeArchery(ArcherySession):
    """按顺序消费预设返回体，并把每次发出的 SQL 记下来"""

    def __init__(self, responses):
        super().__init__(csrftoken="csrf", sessionid="sid")
        self.responses = list(responses)
        self.sqls: list[str] = []

    def _post_sql(self, sql, timeout=10):
        self.sqls.append(sql)
        return self.responses.pop(0) if self.responses else _rows()


class PatternArchery(ArcherySession):
    """按 SQL 里的 LIKE 模式决定返回什么——数字 key 那几趟用这个"""

    def __init__(self, by_pattern):
        super().__init__(csrftoken="csrf", sessionid="sid")
        self.by_pattern = by_pattern
        self.sqls: list[str] = []

    def _post_sql(self, sql, timeout=10):
        self.sqls.append(sql)
        for pattern, rows in self.by_pattern.items():
            if pattern in sql:
                return _rows(*rows)
        return _rows()


# ---------------------------------------------------------------------------
# 三趟的定义
# ---------------------------------------------------------------------------


def test_numeric_passes_are_ordered_specific_first():
    labels = [label for label, _, _ in NUMERIC_PASSES]
    assert labels == ["带下划线的后缀", "任意字符的后缀", "包含"]
    # 第一趟必须匹配"字面下划线"（\_），否则 abc10194060 也会被当成命中
    assert NUMERIC_PASSES[0][1].format(key="10194060") == r"%\_10194060"
    assert NUMERIC_PASSES[0][2]("10194060", "reception_error_10194060") is True
    assert NUMERIC_PASSES[0][2]("10194060", "abc10194060") is False


# ---------------------------------------------------------------------------
# 普通 key
# ---------------------------------------------------------------------------


def test_plain_keys_use_exact_match_and_no_language_filter():
    session = FakeArchery([_rows(("100074326", "reception_checkIn_success"))])

    result = session.query_appid_by_keys(["reception_checkIn_success", "k_absent"])

    assert "IN ('reception_checkIn_success', 'k_absent')" in session.sqls[0]
    assert "LIKE" not in session.sqls[0]
    # 不能再按语种过滤：appId 跟语种无关，过滤会把"只有非 zh-CN 行"的词条漏掉
    assert "language_cd" not in session.sqls[0]
    assert result["reception_checkIn_success"].appids == ["100074326"]
    assert result["reception_checkIn_success"].real_key == "reception_checkIn_success"
    assert result["k_absent"].appids == []          # 查不到也要有条目返回
    assert result["k_absent"].real_key is None


def test_case_difference_is_matched_back_to_doc_key():
    """库里是 reception_register_guest_info（小写 r），文档里写的是 Register（大写 R）。

    MySQL 默认大小写不敏感，所以 SQL 能查到，但返回的是库里的拼写——
    必须能映射回文档里的 key，否则这一行会被静默丢掉、最后误报成 not_found。
    """
    session = FakeArchery([_rows(("100074246", "reception_register_guest_info"))])

    result = session.query_appid_by_keys(["reception_Register_guest_info"])

    match = result["reception_Register_guest_info"]
    assert match.appids == ["100074246"]
    assert match.real_key == "reception_register_guest_info"   # 写真身，用库里的拼写
    assert match.real_key != match.doc_key


def test_unrelated_returned_key_is_ignored():
    """库里返回的 key 跟请求的完全无关时不能张冠李戴"""
    session = FakeArchery([_rows(("100074326", "some_other_key"))])

    assert session.query_appid_by_keys(["k1"])["k1"].appids == []


# ---------------------------------------------------------------------------
# 纯数字 key：第一趟「带下划线的后缀」
# ---------------------------------------------------------------------------


def test_numeric_key_matches_precise_suffix_in_one_query():
    session = PatternArchery({PRECISE_SUFFIX: [("100074262", "reception_error_10194060")]})

    match = session.query_appid_by_keys(["10194060"])["10194060"]

    assert len(session.sqls) == 1                    # 第一趟就命中
    assert PRECISE_SUFFIX in session.sqls[0]
    assert match.real_key == "reception_error_10194060"
    assert match.appids == ["100074262"]


def test_numeric_key_falls_back_to_loose_suffix():
    """下划线形态没命中 → 放宽成"任意字符结尾"（abc10194060 这种）"""
    session = PatternArchery({LOOSE_SUFFIX: [("100074326", "abc10194060")]})

    match = session.query_appid_by_keys(["10194060"])["10194060"]

    assert len(session.sqls) == 2
    assert PRECISE_SUFFIX in session.sqls[0]         # 第一趟：带下划线
    assert "LIKE '%10194060'" in session.sqls[1]     # 第二趟：放宽
    assert match.real_key == "abc10194060"


def test_numeric_key_falls_back_to_contains():
    """后缀都没命中 → 兜底"包含"（数字在中间或开头，如 1019406012 / err_10194060_code）"""
    session = PatternArchery({CONTAINS: [("100074262", "err_10194060_code")]})

    match = session.query_appid_by_keys(["10194060"])["10194060"]

    assert len(session.sqls) == 3
    assert CONTAINS in session.sqls[2]
    assert match.real_key == "err_10194060_code"
    assert match.appids == ["100074262"]


def test_numeric_key_matching_several_real_keys_is_ambiguous():
    """一趟里命中多个真身：只记候选，不给 appId（交人工判断）"""
    session = PatternArchery({PRECISE_SUFFIX: [
        ("100074326", "reception_error_10194060"),
        ("100074328", "pay_error_10194060"),
    ]})

    match = session.query_appid_by_keys(["10194060"])["10194060"]

    assert match.ambiguous_real_key is True
    assert match.appids == []
    assert match.real_key is None
    assert match.candidates == {
        "reception_error_10194060": ["100074326"],
        "pay_error_10194060": ["100074328"],
    }


def test_numeric_key_same_real_key_multiple_appids_keeps_both():
    """同一个真身挂多个 appId：仍交给 multi_appid 那套规则处理"""
    session = PatternArchery({PRECISE_SUFFIX: [
        ("100074326", "error_10194060"),
        ("100061217", "error_10194060"),
    ]})

    match = session.query_appid_by_keys(["10194060"])["10194060"]

    assert match.ambiguous_real_key is False
    assert match.real_key == "error_10194060"
    assert sorted(match.appids) == ["100061217", "100074326"]


# ---------------------------------------------------------------------------
# 混合与去重
# ---------------------------------------------------------------------------


def test_mixed_keys_issue_two_queries():
    session = FakeArchery([
        _rows(("100074326", "reception_total_price")),        # 精确匹配那批
        _rows(("100074262", "reception_error_10194060")),     # 数字 key 的第一趟
    ])

    result = session.query_appid_by_keys(["reception_total_price", "10194060"])

    assert len(session.sqls) == 2
    assert "IN ('reception_total_price')" in session.sqls[0]
    assert PRECISE_SUFFIX in session.sqls[1]
    assert result["reception_total_price"].real_key == "reception_total_price"
    assert result["10194060"].real_key == "reception_error_10194060"


def test_duplicate_and_blank_keys_are_deduped():
    session = FakeArchery([_rows(("100074326", "k1"))])

    result = session.query_appid_by_keys(["k1", "k1", "  ", ""])

    assert "IN ('k1')" in session.sqls[0]
    assert list(result) == ["k1"]
