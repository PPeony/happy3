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


# ---------------------------------------------------------------------------
# 链路 B：文档取表 → 生成 shark 导入文件
# ---------------------------------------------------------------------------


@dataclass
class TableMeta:
    """文档里一张表的定位信息（不做列校验，用于列表与模糊匹配）"""
    period: str                 # 所属"期"，如 "第二期： ddl 3 Jul (complete)"
    title: str                  # 小标题，如 "预订-入住  (copywriter 1)"
    columns: list[str]          # 实际表头
    row_count: int              # 数据行数（不含表头）
    order: int                  # 第几张表（1 起）

    @property
    def compliant(self) -> bool:
        """表头是否符合六列契约"""
        return self.columns == EXPECTED_COLUMNS


@dataclass
class DocRow:
    """文档表格里的一行数据"""
    seq: str                    # 序号（仅人工核对，不导出）
    key: str                    # key（已去首尾空格）
    zh_cn: str                  # 中文
    en_us: str                  # 英文（AI/原始译文，不导出）
    en_copywriter: str          # en-US by copywriters（校对后的英文，导出的就是它）


@dataclass
class TableContent:
    """取到的一张表的完整内容"""
    meta: TableMeta
    rows: list[DocRow] = field(default_factory=list)          # key 非空的行
    empty_key_seqs: list[str] = field(default_factory=list)   # key 为空被跳过的序号


@dataclass
class SkippedKey:
    """没有写进 shark 文件的 key，以及原因"""
    key: str
    reason: Literal["empty_key", "not_found", "multi_appid", "empty_copywriter", "multi_real_key"]
    detail: str = ""


@dataclass
class KeyMatch:
    """
    一个 key 的反查结果。

    纯数字的 key 在文档里是**截断过的**（只留了前缀），所以要走 `LIKE '前缀%'` 找真身：
    - `real_key`：库里真正的 key，写进 shark 导入文件的就是它（不是文档里那个截断值）；
    - `candidates`：LIKE 命中的多个真身（真身 → appId 列表），多于一个时不自动选，交人工。
    """
    doc_key: str
    real_key: str | None = None
    appids: list[str] = field(default_factory=list)
    candidates: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ambiguous_real_key(self) -> bool:
        """LIKE 命中了多个真身，无法确定该更新哪一个"""
        return len(self.candidates) > 1


@dataclass
class SharkRow:
    """待写入 shark 导入文件的一行"""
    appid: str
    key: str                    # → TransKey
    en_us: str                  # → en-US
    zh_cn: str                  # → zh-CN
    description: str = ""       # → Description，固定留空


@dataclass
class SharkExportResult:
    """一个 appId 一个输出文件"""
    appid: str
    file_path: str
    row_count: int
    in_whitelist: bool = True   # 不在 APPID_WHITELIST 里时由报告标注出来


# 文档表格的固定列契约，列名与顺序都不允许变
EXPECTED_COLUMNS = ["序号", "key", "中文", "英文", "en-US by copywriters", "截图"]

