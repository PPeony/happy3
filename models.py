from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Candidate:
    """一条候选词条（歧义时有多个）"""
    trip_appid: str
    key: str
    en_us: str | None


@dataclass
class I18nEntry:
    """一条中文文本的查询结果"""
    zh_cn: str
    status: Literal["found", "not_found", "ambiguous", "confirmed"]
    trip_appid: str | None = None
    key: str | None = None
    en_us: str | None = None
    candidates: list[Candidate] = field(default_factory=list)

    def to_csv_row(self) -> dict:
        return {
            "trip_appid": self.trip_appid or "",
            "key": self.key or "",
            "zh_cn": self.zh_cn,
            "en_us": self.en_us or "",
            "image_url": "",
            "status": self.status,
            "note": "",
        }
