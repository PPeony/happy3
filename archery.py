"""
archery.py — Archery 平台查询封装

认证：直接 POST 账号密码到 Archery 登录接口，拿到 csrftoken + sessionid。
查询：一次 self-join 批量拿到 zh-CN 和 en-US，减少请求次数。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import requests

from models import Candidate, I18nEntry, KeyMatch

ARCHERY_BASE = "http://archery.rezen.work"
LOGIN_URL = f"{ARCHERY_BASE}/login/"
QUERY_URL = f"{ARCHERY_BASE}/query/"
QUERY_INSTANCE = "OL_SET_PMS_RO"
QUERY_DB = "pms_i18n"
LIMIT = 1000

# key 反查 appId 时每条 SQL 带多少个 key（IN 列表别太长）
KEY_BATCH = 500

# 纯数字 key 用 LIKE 找真身，一条 SQL 里的 OR 数量
NUMERIC_BATCH = 100

# 纯数字 key 的匹配顺序（真身形态已确认：形如 reception_error_10194060，数字在末尾、前面有下划线）。
# 按可能性从高到低依次试，前一趟没命中的 key 才进下一趟。
#   SQL 模式里 % 是通配符，\_ 才是字面下划线（要和 Python 侧的判定一致）
NUMERIC_PASSES = (
    ("带下划线的后缀", "%\\_{key}", lambda doc_key, real_key: real_key.endswith("_" + doc_key)),
    ("任意字符的后缀", "%{key}", lambda doc_key, real_key: real_key.endswith(doc_key)),
    ("包含", "%{key}%", lambda doc_key, real_key: doc_key in real_key),
)

# trip_appid 白名单，只保留在此列表中的词条
APPID_WHITELIST = {
    "100074326", "100074328", "100061217", "100061219", "100074330",
    "100074332", "100074334", "100074336", "100061221", "100061223",
    "100061225", "100061227", "100074338", "100074340", "100074342",
    "100074344", "100074346", "100074348", "100074350", "100074352",
    "100074354", "100074356", "100074434", "100074358", "100074360",
    "100074362", "100074364", "100074368", "100074830", "100061031",
    "100061083", "100075012", "100061667", "100061683", "100061685",
    "100061687", "100061689", "100061691", "100061693", "100061695",
    "100075114", "100075116", "100061703", "100061705", "100075122",
    "100075124", "100075126", "100075128", "100075132", "100075136",
    "100075138", "100075140", "100075142", "100075144", "100061645",
    "100074950", "100075056", "100061737", "100075284", "100075286",
    "100061861", "100062161", "100062163", "100062165", "100062167",
    "100062169", "100075600", "100075602", "100075604", "100075606",
    "100075608", "100075610", "100062171", "100075612", "100075614",
    "100075616", "100075618",
}


def login(username: str, password: str, timeout: int = 10) -> "ArcherySession":
    """
    Archery 登录流程（根据页面 JS 逆向）：
    1. GET /login/  → 拿 csrftoken cookie
    2. POST /authenticate/  → AJAX 接口，返回 JSON {status:0} 表示成功，同时 Set-Cookie: sessionid
    """
    import urllib3
    urllib3.disable_warnings()

    s = requests.Session()
    s.verify = False

    # Step1: GET /login/ 拿 csrftoken
    print(f"[login] GET {LOGIN_URL}")
    get_resp = s.get(LOGIN_URL, timeout=timeout, allow_redirects=True)
    csrf = s.cookies.get("csrftoken", "")
    print(f"[login] csrftoken={csrf[:16]}...")

    if not csrf:
        raise LoginError("无法获取 csrftoken，请检查网络或 Archery 地址")

    # Step2: POST /authenticate/
    auth_url = f"{ARCHERY_BASE}/authenticate/"
    print(f"[login] POST {auth_url}")
    auth_resp = s.post(
        auth_url,
        data={"username": username, "password": password},
        headers={
            "X-CSRFToken": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_URL,
        },
        timeout=timeout,
    )
    print(f"[login] POST status={auth_resp.status_code}")

    try:
        result = auth_resp.json()
    except Exception:
        raise LoginError(f"认证接口返回非 JSON，状态码={auth_resp.status_code}")

    print(f"[login] authenticate response: {result}")

    if result.get("status") != 0:
        msg = result.get("msg", "未知错误")
        raise LoginError(f"登录失败：{msg}")

    sessionid = s.cookies.get("sessionid", "")
    new_csrf = s.cookies.get("csrftoken", csrf)

    if not sessionid:
        raise LoginError("认证成功但未收到 sessionid cookie")

    print(f"[login] SUCCESS sessionid={sessionid[:8]}...")
    return ArcherySession(csrftoken=new_csrf, sessionid=sessionid)


class ArcherySession:
    def __init__(self, csrftoken: str, sessionid: str) -> None:
        self.csrftoken = csrftoken
        self.sessionid = sessionid

    @property
    def cookie_str(self) -> str:
        return f"csrftoken={self.csrftoken}; sessionid={self.sessionid}"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Cookie": self.cookie_str,
            "X-CSRFToken": self.csrftoken,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{ARCHERY_BASE}/sqlquery/",
            "Origin": ARCHERY_BASE,
        }

    def _post_sql(self, sql: str, timeout: int = 10) -> dict[str, Any]:
        """向 Archery 发送 SQL 查询，返回原始 JSON 响应。"""
        resp = requests.post(
            QUERY_URL,
            headers=self.headers,
            data={
                "instance_name": QUERY_INSTANCE,
                "db_name": QUERY_DB,
                "schema_name": "",
                "tb_name": "",
                "sql_content": sql,
                "limit_num": LIMIT,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        # 如果被重定向到登录页，raise
        if "login" in resp.url:
            raise SessionExpiredError("Session 已过期，请重新登录")
        return resp.json()

    @staticmethod
    def _escape(text: str) -> str:
        """对 SQL IN 列表中的字符串做最小转义（单引号 → ''）。"""
        return text.replace("'", "''")

    @staticmethod
    def _parse_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Archery 返回格式：
        {
          "data": {
            "column_list": ["col1", "col2", ...],
            "rows": [[val1, val2, ...], ...]
          }
        }
        转成 list[dict]。
        """
        if data.get("status") != 0:
            raise ArcheryQueryError(data.get("msg", "查询失败"))
        inner = data.get("data", {})
        columns = inner.get("column_list", [])
        rows = inner.get("rows", [])
        return [dict(zip(columns, row)) for row in rows]

    def query_texts(self, texts: list[str]) -> list[I18nEntry]:
        """
        批量查询中文文本对应的 i18n 词条。

        策略：一次 self-join SQL，同时拿到 zh-CN 和 en-US 行，
        只发 1 次 HTTP 请求。
        """
        if not texts:
            return []

        in_clause = ", ".join(f"'{self._escape(t)}'" for t in texts)
        sql = f"""
SELECT
    zh.trip_appid,
    zh.`key`,
    zh.value   AS zh_cn,
    en.value   AS en_us
FROM i18n_translated_message zh
LEFT JOIN i18n_translated_message en
    ON  zh.trip_appid   = en.trip_appid
    AND zh.`key`        = en.`key`
    AND en.language_cd  = 'en-US'
WHERE zh.language_cd = 'zh-CN'
    AND zh.value IN ({in_clause})
""".strip()

        raw = self._post_sql(sql)
        rows = self._parse_rows(raw)

        # 白名单过滤：只保留 trip_appid 在白名单中的行
        rows = [r for r in rows if str(r.get("trip_appid", "")) in APPID_WHITELIST]

        # 按 zh_cn 分组
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[row["zh_cn"]].append(row)

        results: list[I18nEntry] = []
        for text in texts:
            hits = grouped.get(text, [])
            if len(hits) == 0:
                results.append(I18nEntry(zh_cn=text, status="not_found"))
            elif len(hits) == 1:
                h = hits[0]
                results.append(I18nEntry(
                    zh_cn=text,
                    status="found",
                    trip_appid=h["trip_appid"],
                    key=h["key"],
                    en_us=h.get("en_us"),
                ))
            else:
                candidates = [
                    Candidate(
                        trip_appid=h["trip_appid"],
                        key=h["key"],
                        en_us=h.get("en_us"),
                    )
                    for h in hits
                ]
                results.append(I18nEntry(
                    zh_cn=text,
                    status="ambiguous",
                    candidates=candidates,
                ))

        return results

    def query_appid_by_keys(self, keys: list[str]) -> dict[str, KeyMatch]:
        """
        用 key 批量反查 trip_appid（链路 B），返回 {文档里的 key: KeyMatch}。

        两种 key 走两条路：

        1. **普通 key**：精确匹配（`key IN (...)`）；
        2. **纯数字 key**：文档里写的是**截断过的前缀**（错误码那种长 key 只留了前几位），
           所以用 `LIKE '前缀%'` 找真身，并把真身 full key 记在 `KeyMatch.real_key` 里——
           写进 shark 导入文件的必须是真身，写截断值会更新到错误的词条上。
           前缀里不会有 `%` / `_`（都是数字），所以不用额外转义通配符。

        两种情况都保留"不自动选"的原则：命中多个 appId（或数字 key 命中多个真身）时
        只记录候选，由调用方计入报告交人工判断。`KeyMatch` 一定会有条目返回（哪怕 appId 为空）。
        """
        wanted = list(dict.fromkeys(k.strip() for k in keys if k and k.strip()))
        matches: dict[str, KeyMatch] = {k: KeyMatch(doc_key=k) for k in wanted}
        numeric = [k for k in wanted if k.isdigit()]
        plain = [k for k in wanted if not k.isdigit()]

        # 库里的写法可能和文档不同（大小写差异，MySQL 默认大小写不敏感所以照样能查到），
        # 所以要能把"库里返回的 key"映射回"文档里的 key"，并把库里的写法记成 real_key。
        by_lower: dict[str, str] = {}
        for k in wanted:
            by_lower.setdefault(k.lower(), k)

        # 1) 普通 key：精确匹配。
        #    不按 language_cd 过滤——appId 跟语种无关，而 DISTINCT trip_appid, key 之后结果集大小一样；
        #    加上语种过滤反而会让"只有非 zh-CN 行"的词条被漏掉（这类 key 同样要能导出去）。
        for start in range(0, len(plain), KEY_BATCH):
            batch = plain[start:start + KEY_BATCH]
            in_clause = ", ".join(f"'{self._escape(k)}'" for k in batch)
            sql = f"""
SELECT DISTINCT trip_appid, `key`
FROM i18n_translated_message
WHERE `key` IN ({in_clause})
""".strip()

            for row in self._parse_rows(self._post_sql(sql)):
                returned_key = str(row.get("key") or "").strip()
                appid = str(row.get("trip_appid") or "").strip()
                if not returned_key or not appid:
                    continue
                doc_key = by_lower.get(returned_key.lower())
                match = matches.get(doc_key) if doc_key else None
                if match is None:
                    continue
                if appid not in match.appids:
                    match.appids.append(appid)
                match.real_key = returned_key        # 库里真正的写法

        # 2) 纯数字 key：文档里只留了截断后的一段数字，按 NUMERIC_PASSES 的顺序找真身
        for _label, pattern, hit in NUMERIC_PASSES:
            missed = [k for k in numeric if not matches[k].candidates]
            if not missed:
                break
            for start in range(0, len(missed), NUMERIC_BATCH):
                batch = missed[start:start + NUMERIC_BATCH]
                where = " OR ".join(
                    f"`key` LIKE '{pattern.format(key=self._escape(k))}'" for k in batch
                )
                sql = f"""
SELECT DISTINCT trip_appid, `key`
FROM i18n_translated_message
WHERE ({where})
""".strip()

                for row in self._parse_rows(self._post_sql(sql)):
                    real_key = str(row.get("key") or "").strip()
                    appid = str(row.get("trip_appid") or "").strip()
                    if not real_key or not appid:
                        continue
                    for doc_key in batch:
                        if not hit(doc_key, real_key):
                            continue
                        match = matches[doc_key]
                        appids = match.candidates.setdefault(real_key, [])
                        if appid not in appids:
                            appids.append(appid)

        # 数字 key 收敛：只有一个真身才算数，多个真身留给人工判断
        for doc_key in numeric:
            match = matches[doc_key]
            if len(match.candidates) == 1:
                real_key, appids = next(iter(match.candidates.items()))
                match.real_key = real_key
                match.appids = list(appids)

        return matches


class SessionExpiredError(Exception):
    pass


class ArcheryQueryError(Exception):
    pass


class LoginError(Exception):
    pass
