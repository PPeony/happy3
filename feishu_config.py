"""
feishu_config.py — 飞书应用凭据读写

存储路径：~/.happyhappyhappy/feishu.json
格式：{"app_id": "xxx", "app_secret": "xxx", "folder_token": "fldcnXXX"}

folder_token：飞书云空间目标文件夹的 token，从文件夹 URL 中获取：
  https://xxx.feishu.cn/drive/folder/fldcnXXXXXXXX
                                     ^^^^^^^^^^^^^^ 这一段就是 folder_token

注意：明文存储，仅适用于内网工具。
"""
from __future__ import annotations

import json
from pathlib import Path

_CONFIG_DIR = Path.home() / ".happyhappyhappy"
_CONFIG_FILE = _CONFIG_DIR / "feishu.json"


def load() -> dict[str, str]:
    """读取保存的飞书凭据，若不存在则返回空 dict。"""
    if not _CONFIG_FILE.exists():
        return {}
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "app_id":       str(data.get("app_id", "")),
            "app_secret":   str(data.get("app_secret", "")),
            "folder_token": str(data.get("folder_token", "")),
        }
    except Exception:
        return {}


def save(app_id: str, app_secret: str, folder_token: str = "") -> None:
    """保存飞书凭据到本地文件。"""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"app_id": app_id, "app_secret": app_secret, "folder_token": folder_token},
            f, ensure_ascii=False, indent=2,
        )


def config_path() -> str:
    return str(_CONFIG_FILE)
