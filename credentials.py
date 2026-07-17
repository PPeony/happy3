"""
credentials.py - 本地凭据读写

存储路径：~/.happyhappyhappy/credentials.json
格式：{"username": "xxx", "password": "xxx"}

注意：密码以明文存储，仅适用于内网工具。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_CRED_DIR = Path.home() / ".happyhappyhappy"
_CRED_FILE = _CRED_DIR / "credentials.json"


def load() -> dict[str, str]:
    """读取保存的凭据，返回 {"username": ..., "password": ...}，若不存在则返回空 dict。"""
    if not _CRED_FILE.exists():
        return {}
    try:
        with open(_CRED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "username": str(data.get("username", "")),
            "password": str(data.get("password", "")),
        }
    except Exception:
        return {}


def save(username: str, password: str) -> None:
    """保存凭据到本地文件。"""
    _CRED_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CRED_FILE, "w", encoding="utf-8") as f:
        json.dump({"username": username, "password": password}, f, ensure_ascii=False, indent=2)


def clear() -> None:
    """删除保存的凭据。"""
    if _CRED_FILE.exists():
        _CRED_FILE.unlink()


def cred_path() -> str:
    return str(_CRED_FILE)
