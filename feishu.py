"""
DEPRECATED（2026-09-24）：本模块的"上传飞书多维表格"不是需求要的形态，请勿在此基础上加新功能。

需求本体仍然成立——链路 A 的校验结果（key、中文、英文、截图）要提交给飞书 copywriter 系统；
但要求是写进**对应格式的飞书文档**，而不是多维表格（Bitable）。因此这条 Bitable 链路不再使用，
CLI 的 `upload` 子命令与 GUI 入口都已移除。详见 doc/spec.md 的"飞书结果提交（挂起）"。

保留原因：需求重启时，凭据加载、OAuth、上传图片拿 file_token 这几部分可以复用。
下面的内容为历史实现。

feishu.py — 飞书开放平台 API 封装

功能：
  1. 获取 token（支持 tenant_access_token 和 user_access_token 两种模式）
  2. 上传本地截图，返回 file_token
  3. 创建多维表格（Bitable），自动建字段
  4. 批量写入记录（含截图附件）
  5. 返回在线链接 URL

认证模式：
  - user 模式（推荐）：文件所有者是你本人，可直接编辑
    FeishuClient(app_id, app_secret, use_user_token=True)
  - tenant 模式：文件归属应用机器人，需要配置 folder_token 或手动分享
    FeishuClient(app_id, app_secret)

权限要求（飞书开发者后台申请）：
  - bitable:app   — 创建/编辑多维表格、字段、记录
  - drive:drive   — 上传图片拿 file_token

user 模式额外要求：
  - 安全设置 → 重定向 URL 添加：http://localhost:19721/callback（要精确到这个地址，
    只写 http://localhost 会报 20029）
"""
from __future__ import annotations

import os
import warnings
from datetime import datetime
from typing import Any

import requests

from models import I18nEntry

FEISHU_BASE = "https://open.feishu.cn/open-apis"

def _load_app_credentials(env=None, home=None) -> tuple[str, str]:
    """
    读取飞书应用凭据，优先级：
    1. 环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET
    2. <用户目录>/.happyhappyhappy/feishu.json

    读不出来时把**查过哪里、为什么不行**都列出来——以前一律报"未找到飞书应用凭据"，
    文件明明存在也看不出原因，白排查（2026-09-28 踩过）。
    env / home 只是给测试注入用的，正常运行不用传。
    """
    import json
    import os
    from pathlib import Path as _Path

    env = os.environ if env is None else env
    home_dir = _Path.home() if home is None else _Path(home)

    app_id = str(env.get("FEISHU_APP_ID", "") or "").strip()
    app_secret = str(env.get("FEISHU_APP_SECRET", "") or "").strip()
    if app_id and app_secret:
        return app_id, app_secret

    cfg = home_dir / ".happyhappyhappy" / "feishu.json"
    tried = [f"环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET：没设置"]

    if not cfg.exists():
        tried.append(f"配置文件 {cfg}：不存在（用户目录解析为 {home_dir}）")
    else:
        try:
            # 用 utf-8-sig 读：PowerShell 的 Set-Content -Encoding UTF8 会写 BOM，
            # 按 utf-8 读再 json.loads 会直接解析失败
            data = json.loads(cfg.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            tried.append(f"配置文件 {cfg}：JSON 解析失败（{e}）—— 检查文件开头有没有 BOM 或多余字符")
        except Exception as e:
            tried.append(f"配置文件 {cfg}：读取失败（{type(e).__name__}: {e}）")
        else:
            app_id = str(data.get("app_id", "") or "").strip()
            app_secret = str(data.get("app_secret", "") or "").strip()
            if app_id and app_secret:
                return app_id, app_secret
            tried.append(f"配置文件 {cfg}：内容能解析，但没有 app_id / app_secret"
                         f"（文件里的字段是 {sorted(data)}）")

    raise RuntimeError(
        "没拿到飞书应用凭据，查过的地方：\n  - " + "\n  - ".join(tried) +
        "\n修法：把 app_id / app_secret 写进上面那个 feishu.json，或设成环境变量。"
    )


FEISHU_APP_ID, FEISHU_APP_SECRET = _load_app_credentials()

# 多维表格字段定义（顺序即列顺序）
# type 数字含义：1=文本 3=单选 13=电话（不用）
# 我们全用文本(1)和附件(17)
_FIELD_DEFS = [
    {"field_name": "zh_cn",      "type": 1},   # 多行文本
    {"field_name": "key",        "type": 1},
    {"field_name": "trip_appid", "type": 1},
    {"field_name": "en_us",      "type": 1},
    {"field_name": "status",     "type": 1},
    {"field_name": "note",       "type": 1},
    {"field_name": "截图",        "type": 17},  # 附件
]


class FeishuError(Exception):
    """飞书 API 调用失败"""
    pass


class FeishuClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        timeout: int = 15,
        use_user_token: bool = False,
        force_reauth: bool = False,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout = timeout
        self.use_user_token = use_user_token
        self.force_reauth = force_reauth
        self._token: str | None = None
        warnings.warn(
            "feishu.py 已废弃：上传多维表格不是需求要的形态（要写进指定格式的飞书文档），"
            "见 doc/spec.md「飞书结果提交（挂起）」",
            DeprecationWarning,
            stacklevel=2,
        )

    # ------------------------------------------------------------------
    # Step 0: token
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """
        获取有效 token，运行期间缓存。
        use_user_token=True  → user_access_token（文件归你本人）
        use_user_token=False → tenant_access_token（文件归机器人）
        """
        if self._token:
            return self._token
        if self.use_user_token:
            from feishu_auth import get_user_access_token
            self._token = get_user_access_token(
                self.app_id, self.app_secret,
                force_reauth=self.force_reauth,
            )
        else:
            url = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
            resp = requests.post(
                url,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise FeishuError(f"获取 token 失败：{data.get('msg')}")
            self._token = data["tenant_access_token"]
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _post(self, url: str, **kwargs) -> dict:
        """POST 并统一打印响应，方便调试。"""
        resp = requests.post(url, headers=self._auth_headers(), timeout=self.timeout, **kwargs)
        if not resp.ok:
            raise FeishuError(
                f"HTTP {resp.status_code} {resp.reason} — {url}\n"
                f"Response: {resp.text[:800]}"
            )
        return resp.json()

    def _get_json(self, url: str) -> dict:
        resp = requests.get(url, headers=self._auth_headers(), timeout=self.timeout)
        if not resp.ok:
            raise FeishuError(
                f"HTTP {resp.status_code} {resp.reason} — {url}\n"
                f"Response: {resp.text[:800]}"
            )
        return resp.json()

    def _delete(self, url: str) -> dict:
        resp = requests.delete(url, headers=self._auth_headers(), timeout=self.timeout)
        if not resp.ok:
            raise FeishuError(
                f"HTTP {resp.status_code} {resp.reason} — {url}\n"
                f"Response: {resp.text[:800]}"
            )
        return resp.json()

    def _patch(self, url: str, **kwargs) -> dict:
        resp = requests.patch(url, headers=self._auth_headers(), timeout=self.timeout, **kwargs)
        if not resp.ok:
            raise FeishuError(
                f"HTTP {resp.status_code} {resp.reason} — {url}\n"
                f"Response: {resp.text[:800]}"
            )
        return resp.json()

    def _check(self, data: dict, label: str) -> dict:
        if data.get("code") != 0:
            raise FeishuError(f"{label} 失败：code={data.get('code')} msg={data.get('msg')}")
        return data

    # ------------------------------------------------------------------
    # Step 1: 上传截图
    # ------------------------------------------------------------------

    def upload_image(self, screenshot_path: str, app_token: str) -> str:
        """
        上传本地截图到飞书，返回 file_token。

        parent_type = "bitable_image"  表示图片归属到某个多维表格，
        parent_node = app_token        即该表格的 token。
        """
        url = f"{FEISHU_BASE}/drive/v1/medias/upload_all"
        file_size = os.path.getsize(screenshot_path)
        file_name = os.path.basename(screenshot_path)

        with open(screenshot_path, "rb") as f:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {self._get_token()}"},
                data={
                    "file_name":   file_name,
                    "parent_type": "bitable_image",
                    "parent_node": app_token,
                    "size":        str(file_size),
                },
                files={"file": (file_name, f)},
                timeout=self.timeout,
            )
        if not resp.ok:
            raise FeishuError(
                f"HTTP {resp.status_code} 上传图片失败 — {url}\n"
                f"Response: {resp.text[:800]}"
            )
        data = self._check(resp.json(), "上传图片")
        return data["data"]["file_token"]

    # ------------------------------------------------------------------
    # Step 2: 创建多维表格
    # ------------------------------------------------------------------

    def create_bitable(self, title: str, folder_token: str = "") -> tuple[str, str, str]:
        """
        创建多维表格，返回 (app_token, table_id, url)。

        folder_token: 飞书云空间目标文件夹 token（强烈建议填写）。
          - 填写后，文件创建在你的文件夹里，你是文件拥有者，可以直接编辑和分享。
          - 不填写，文件创建在应用的机器人空间里，只有机器人能编辑，需要手动转移权限。
          - 从文件夹 URL 获取：https://xxx.feishu.cn/drive/folder/fldcnXXX -> fldcnXXX
        """
        url = f"{FEISHU_BASE}/bitable/v1/apps"
        body: dict[str, str] = {"name": title}
        if folder_token:
            body["folder_token"] = folder_token
        data = self._check(self._post(url, json=body), "创建多维表格")
        app = data["data"]["app"]
        app_token = app["app_token"]
        table_id = self._get_default_table_id(app_token)
        bitable_url = app.get("url") or f"https://feishu.cn/base/{app_token}"
        return app_token, table_id, bitable_url

    def _get_default_table_id(self, app_token: str) -> str:
        """拿新建 Bitable 的默认表 ID。"""
        url = f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables"
        data = self._check(self._get_json(url), "获取表列表")
        items = data["data"]["items"]
        if not items:
            raise FeishuError("多维表格创建后没有找到默认数据表")
        return items[0]["table_id"]

    # ------------------------------------------------------------------
    # Step 3: 清除默认字段和空行，建我们自己的字段
    # ------------------------------------------------------------------

    def setup_fields(self, app_token: str, table_id: str) -> None:
        """
        1. 删除飞书自带的默认字段（文本/单选/日期/附件等）
        2. 删除模板自带的空记录
        3. 按 _FIELD_DEFS 顺序创建我们需要的字段
        """
        base = f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}"

        # --- 删除默认字段 ---
        fields_data = self._check(self._get_json(f"{base}/fields"), "列出字段")
        for field in fields_data["data"].get("items", []):
            fid = field["field_id"]
            try:
                self._delete(f"{base}/fields/{fid}")
            except FeishuError:
                pass  # 部分字段不可删（如主键），忽略

        # --- 删除默认空记录 ---
        records_data = self._check(self._get_json(f"{base}/records?page_size=100"), "列出记录")
        record_ids = [r["record_id"] for r in records_data["data"].get("items", [])]
        if record_ids:
            # 批量删除：POST batch_delete
            del_url = f"{base}/records/batch_delete"
            self._check(self._post(del_url, json={"records": record_ids}), "删除默认记录")

        # --- 创建我们的字段 ---
        for field in _FIELD_DEFS:
            data = self._post(f"{base}/fields", json=field)
            self._check(data, f"创建字段 {field['field_name']}")

    # ------------------------------------------------------------------
    # Step 4: 批量写入记录
    # ------------------------------------------------------------------

    def batch_write_records(
        self,
        app_token: str,
        table_id: str,
        entries: list[I18nEntry],
        file_token: str | None = None,
        batch_size: int = 500,
    ) -> int:
        """
        批量写入词条记录，返回实际写入行数。

        file_token: 若传入，每条记录的"截图"附件字段都指向同一张截图。
        batch_size: 飞书单次限制 500 条。
        """
        url = f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"

        def _build_record(entry: I18nEntry) -> dict[str, Any]:
            fields: dict[str, Any] = {
                "zh_cn":      entry.zh_cn,
                "key":        entry.key or "",
                "trip_appid": entry.trip_appid or "",
                "en_us":      entry.en_us or "",
                "status":     entry.status,
                "note":       _note(entry),
            }
            if file_token:
                fields["截图"] = [{"file_token": file_token}]
            return {"fields": fields}

        # 歧义条目展开为多行（与 CSV 导出逻辑一致）
        records = []
        for entry in entries:
            if entry.status == "ambiguous":
                for c in entry.candidates:
                    from models import I18nEntry as _E
                    candidate_entry = _E(
                        zh_cn=entry.zh_cn,
                        status="ambiguous",
                        trip_appid=c.trip_appid,
                        key=c.key,
                        en_us=c.en_us,
                    )
                    records.append(_build_record(candidate_entry))
            else:
                records.append(_build_record(entry))

        # 分批写入
        written = 0
        for i in range(0, len(records), batch_size):
            batch = records[i: i + batch_size]
            data = self._post(url, json={"records": batch})
            self._check(data, "批量写入记录")
            written += len(batch)

        return written

    # ------------------------------------------------------------------
    # 一步完成：创建表格 + 上传图片 + 写入记录
    # ------------------------------------------------------------------

    def create_and_upload(
        self,
        entries: list[I18nEntry],
        title: str | None = None,
        screenshot_path: str | None = None,
        folder_token: str = "",
    ) -> str:
        """
        主入口：一步完成所有操作，返回飞书多维表格的在线链接。

        folder_token: 目标文件夹 token（强烈建议填写，否则文件归属机器人）。
          从文件夹 URL 获取：https://xxx.feishu.cn/drive/folder/fldcnXXX
        screenshot_path: 本地截图文件路径（可选），所有记录共享同一张截图。
        title: 表格标题，默认用当前日期时间。
        """
        if title is None:
            title = f"i18n词条校验_{datetime.now().strftime('%Y%m%d_%H%M')}"

        # 1. 建表（传入 folder_token，文件创建在用户自己的文件夹里）
        app_token, table_id, bitable_url = self.create_bitable(title, folder_token=folder_token)

        # 2. 清默认字段/空行，建我们的字段
        self.setup_fields(app_token, table_id)

        # 3. 上传截图（可选）
        file_token: str | None = None
        if screenshot_path:
            file_token = self.upload_image(screenshot_path, app_token)

        # 4. 写记录
        self.batch_write_records(app_token, table_id, entries, file_token=file_token)

        return bitable_url


def _note(entry: I18nEntry) -> str:
    if entry.status == "ambiguous":
        return "待研发确认"
    return ""


def upload_to_feishu(
    entries: list[I18nEntry],
    title: str | None = None,
    screenshot_path: str | None = None,
) -> str:
    """
    便捷入口：使用内置 app_id/app_secret + user_access_token 上传到飞书。
    无需外部传凭据，打包后直接调用。
    返回飞书多维表格在线链接。
    """
    warnings.warn(
        "upload_to_feishu 已废弃：上传多维表格不是需求要的形态，见 doc/spec.md",
        DeprecationWarning,
        stacklevel=2,
    )
    client = FeishuClient(
        app_id=FEISHU_APP_ID,
        app_secret=FEISHU_APP_SECRET,
        use_user_token=True,
    )
    return client.create_and_upload(entries, title=title, screenshot_path=screenshot_path)
