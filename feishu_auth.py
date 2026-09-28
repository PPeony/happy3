"""
feishu_auth.py — 飞书 OAuth2 user_access_token 获取

多维表格那条链路（feishu.py）已废弃，但**这里的 OAuth 被新链路复用**：
写飞书文档（feishu_doc.py）同样要用 user_access_token，所以本模块是活的，不是历史包袱。

流程：
  1. 本地起临时 HTTP server 监听 localhost:19721
  2. 打开浏览器，跳转飞书授权页
  3. 用户在浏览器里点"同意授权"
  4. 飞书重定向到 localhost:19721/callback，server 拿到 code
  5. 先拿 app_access_token，再用它换 user_access_token + refresh_token
  6. token 缓存到 ~/.happyhappyhappy/feishu_user_token.json

有效期：user_access_token 2 小时，refresh_token 30 天（自动刷新）

飞书应用需在开发者后台额外配置：
  安全设置 → 重定向 URL 添加：http://localhost:19721/callback
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

FEISHU_BASE = "https://open.feishu.cn/open-apis"

# 凭据从 feishu.py 统一读取，避免在此重复定义
def _get_app_creds() -> tuple[str, str]:
    from feishu import FEISHU_APP_ID, FEISHU_APP_SECRET
    return FEISHU_APP_ID, FEISHU_APP_SECRET
_TOKEN_FILE = Path.home() / ".happyhappyhappy" / "feishu_user_token.json"
_CALLBACK_PORT = 19721
_REDIRECT_URI  = f"http://localhost:{_CALLBACK_PORT}/callback"


class FeishuAuthError(Exception):
    pass


# ---------------------------------------------------------------------------
# Token 本地持久化
# ---------------------------------------------------------------------------

def _load_token() -> dict:
    if not _TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_token(data: dict) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 获取 app_access_token（用于后续换 user token）
# ---------------------------------------------------------------------------

def _get_app_access_token() -> str:
    app_id, app_secret = _get_app_creds()
    resp = requests.post(
        f"{FEISHU_BASE}/auth/v3/app_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuAuthError(f"获取 app_access_token 失败：{data.get('msg')} (full: {data})")
    return data["app_access_token"]


# ---------------------------------------------------------------------------
# Token 刷新
# ---------------------------------------------------------------------------

def _refresh(refresh_token: str) -> dict:
    app_token = _get_app_access_token()
    resp = requests.post(
        f"{FEISHU_BASE}/authen/v1/refresh_access_token",
        headers={
            "Authorization": f"Bearer {app_token}",
            "Content-Type":  "application/json",
        },
        json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuAuthError(f"刷新 token 失败：{data.get('msg')} (full: {data})")
    return data["data"]


# ---------------------------------------------------------------------------
# 主入口：获取有效的 user_access_token
# ---------------------------------------------------------------------------

def get_user_access_token(
    app_id: str = "",
    app_secret: str = "",
    force_reauth: bool = False,
) -> str:
    """
    返回有效的 user_access_token。
    app_id/app_secret 留空时自动从配置读取。
    """
    if not app_id or not app_secret:
        app_id, app_secret = _get_app_creds()

    if force_reauth:
        # 说一句，免得以为是"授权没生效"——其实是这个参数要求每次重新授权
        print("[feishu] 你指定了 --reauth：强制重新走一次浏览器授权"
              "（去掉这个参数就会复用缓存的 token，不用再弹浏览器）")

    if not force_reauth:
        cached = _load_token()
        if cached:
            left = cached.get("access_token_expire", 0) - time.time()
            if left > 300:
                print(f"[feishu] 用缓存的 token（还剩约 {int(left // 60)} 分钟；"
                      f"想强制重新授权加 --reauth）")
                return cached["access_token"]
            if cached.get("refresh_token_expire", 0) - time.time() > 300:
                try:
                    token_data = _refresh(cached["refresh_token"])
                    now = time.time()
                    cache = {
                        "access_token":         token_data["access_token"],
                        "refresh_token":         token_data.get("refresh_token", cached["refresh_token"]),
                        "access_token_expire":   now + token_data.get("expires_in", 7200),
                        "refresh_token_expire":  now + token_data.get("refresh_token_expires_in", 2592000),
                    }
                    _save_token(cache)
                    print("[feishu] token 已自动刷新")
                    return cache["access_token"]
                except Exception as e:
                    print(f"[feishu] 刷新 token 失败（{e}），重新授权...")

    return _oauth_flow(app_id, app_secret)


# ---------------------------------------------------------------------------
# OAuth 浏览器授权流程
# ---------------------------------------------------------------------------

def _oauth_flow(app_id: str, app_secret: str) -> str:
    """打开浏览器完成授权，返回 user_access_token。"""
    auth_url = (
        "https://open.feishu.cn/open-apis/authen/v1/authorize?"
        + urlencode({
            "app_id":        app_id,
            "redirect_uri":  _REDIRECT_URI,
            "response_type": "code",
            # docx:document 写文档要；wiki:node:read 用于把知识库(wiki)链接换成文档 token
            # （文档挂在知识库里时，链接上那个是节点 token，不是 document_id，必须查一次）
            "scope":         "bitable:app drive:drive docx:document wiki:node:read",
        })
    )

    code_holder: dict = {}
    done_event = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if "code" in params:
                code_holder["code"] = params["code"][0]
                self._respond("授权成功！请回到命令行查看结果。")
            else:
                err = params.get("error_description", params.get("error", ["未知错误"]))[0]
                code_holder["error"] = err
                self._respond(f"授权失败：{err}")
            done_event.set()

        def _respond(self, msg: str):
            body = (
                f'<!DOCTYPE html><html><head><meta charset="utf-8">'
                f'<style>body{{font-family:sans-serif;text-align:center;'
                f'margin-top:80px;font-size:20px}}</style></head>'
                f'<body><p>{msg}</p></body></html>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # 静默 server 日志

    server = HTTPServer(("localhost", _CALLBACK_PORT), CallbackHandler)
    t = threading.Thread(target=lambda: server.serve_forever(), daemon=True)
    t.start()

    print(f"\n[feishu] 正在打开浏览器进行飞书授权...")
    print(f"[feishu] 应用 app_id：{app_id}")
    print(f"[feishu] 回调地址（必须**原样**登记在该应用的"
          f"「安全设置 → 重定向 URL」里，飞书要求精确匹配）：")
    print(f"         {_REDIRECT_URI}")
    print(f"[feishu] 浏览器若报 20029「重定向 URL 有误」，就是这个地址没登记或写法不一致"
          f"（常见错法：只写 http://localhost、少了端口或 /callback）。")
    print(f"[feishu] 如果浏览器未自动打开，请手动访问：\n  {auth_url}\n")
    webbrowser.open(auth_url)

    if not done_event.wait(timeout=120):
        server.shutdown()
        raise FeishuAuthError("授权超时（120s），请重试")

    server.shutdown()

    if "error" in code_holder:
        raise FeishuAuthError(f"授权失败：{code_holder['error']}")

    code = code_holder["code"]
    print(f"[feishu] 已获取授权码，正在换取 token...")

    # Step 1: 拿 app_access_token
    app_token = _get_app_access_token()

    # Step 2: 用 code 换 user_access_token
    resp = requests.post(
        f"{FEISHU_BASE}/authen/v1/access_token",
        headers={
            "Authorization": f"Bearer {app_token}",
            "Content-Type":  "application/json",
        },
        json={"grant_type": "authorization_code", "code": code},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuAuthError(
            f"换取 user token 失败：{data.get('msg')}\n完整响应：{data}"
        )

    token_data = data["data"]
    now = time.time()
    cache = {
        "access_token":          token_data["access_token"],
        "refresh_token":          token_data["refresh_token"],
        "access_token_expire":    now + token_data.get("expires_in", 7200),
        "refresh_token_expire":   now + token_data.get("refresh_token_expires_in", 2592000),
    }
    _save_token(cache)
    print(f"[feishu] 授权成功！token 已缓存到 {_TOKEN_FILE}")
    return cache["access_token"]
