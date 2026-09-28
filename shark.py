"""
shark.py — 生成 shark 词条管理平台的导入文件

shark 以 appId 区分项目，一份文件只能导进一个项目，所以按 appId 拆成多个 xlsx：
    {appid}_{小标题}.xlsx

导入模板是常量，已经内嵌在 `shark_template.py` 里（字节来自
`doc/PrePublishImportTranslation.xlsx`），所以运行时**不需要模板文件**，调用方只要传参数。
生成方式：按模板的 zip 结构重写 `xl/worksheets/sheet1.xml`，其余部件原样保留，
保证与 shark 要求格式一致；模板自带的释义/示例行不写进去，数据直接从第 2 行开始。

本模块不调用 shark 接口，导入由人工在 shark 上完成。
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field

import shark_template
from archery import APPID_WHITELIST, NUMERIC_PASSES
from models import KeyMatch, SharkExportResult, SharkRow, SkippedKey, TableContent

SHEET_XML = "xl/worksheets/sheet1.xml"
HEADERS = ["TransKey", "Description", "en-US", "zh-CN"]

# 文件名里不允许出现的字符
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')
# XML 1.0 不允许的控制字符
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _escape(text: str) -> str:
    text = _ILLEGAL_XML.sub("", text or "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def safe_filename(part: str) -> str:
    """小标题里的 / \\ : 等字符不能进文件名（如"换房/升级"）"""
    return _ILLEGAL_FILENAME.sub("_", " ".join(part.split())).strip() or "table"


# ---------------------------------------------------------------------------
# 组装数据
# ---------------------------------------------------------------------------


@dataclass
class SharkExportOutput:
    results: list[SharkExportResult] = field(default_factory=list)
    skipped: list[SkippedKey] = field(default_factory=list)
    duplicate_count: int = 0
    title: str = ""
    period: str = ""
    expanded: dict[str, str] = field(default_factory=dict)   # 数字 key：文档里的截断值 → 库里的真身


def build_rows(
    content: TableContent,
    lookup: dict[str, KeyMatch],
) -> tuple[list[SharkRow], list[SkippedKey], dict[str, str]]:
    """
    把文档行组装成待写入 shark 的行。

    规则：
    - `en-US by copywriters` 为空 → 不导出（空值会覆盖 shark 上已有的英文）；
    - key 查不到 appId → 不导出，计入报告；
    - 命中多个 appId → 不导出，计入报告由人工判断；
    - 纯数字 key 文档里是截断值：`lookup` 会带着真身（`real_key`）回来，
      **写进文件的 TransKey 用真身**；LIKE 命中多个真身时同样不导出，计入报告；
    - 同一 key 出现多次取最后一条。

    返回 (行, 跳过的, 数字 key 的真身对照 {文档里的截断值: 真身})。
    """
    rows: list[SharkRow] = []
    skipped: list[SkippedKey] = []
    expanded: dict[str, str] = {}
    by_key: OrderedDict[str, SharkRow] = OrderedDict()

    for seq in content.empty_key_seqs:
        skipped.append(SkippedKey(key="", reason="empty_key", detail=f"文档序号 {seq}"))

    for row in content.rows:
        en = row.en_copywriter.strip()
        if not en:
            skipped.append(SkippedKey(key=row.key, reason="empty_copywriter", detail=f"序号 {row.seq}"))
            continue

        match = lookup.get(row.key) or KeyMatch(doc_key=row.key)

        if match.ambiguous_real_key:
            candidates = "、".join(sorted(match.candidates))
            skipped.append(SkippedKey(
                key=row.key, reason="multi_real_key",
                detail=f"序号 {row.seq}；LIKE 命中多个词条：{candidates}",
            ))
            continue
        if not match.appids:
            detail = f"序号 {row.seq}"
            if row.key.isdigit():
                # 数字 key 是"截断值 + LIKE 多趟找真身"，说清楚已经试过，免得以为是版本旧
                passes = "、".join(label for label, _, _ in NUMERIC_PASSES)
                detail += f"；已按【{passes}】{len(NUMERIC_PASSES)} 趟匹配过，库里确实没有"
            skipped.append(SkippedKey(key=row.key, reason="not_found", detail=detail))
            continue
        if len(match.appids) > 1:
            skipped.append(SkippedKey(
                key=row.key, reason="multi_appid", detail="、".join(match.appids),
            ))
            continue

        real_key = match.real_key or row.key
        if real_key != row.key:
            expanded[row.key] = real_key

        by_key[real_key] = SharkRow(
            appid=match.appids[0],
            key=real_key,          # 写真身：截断值会让 shark 更新到错误的词条
            en_us=en,
            zh_cn=row.zh_cn.strip(),
        )

    rows = list(by_key.values())
    return rows, skipped, expanded


def group_by_appid(rows: list[SharkRow]) -> OrderedDict[str, list[SharkRow]]:
    grouped: OrderedDict[str, list[SharkRow]] = OrderedDict()
    for row in rows:
        grouped.setdefault(row.appid, []).append(row)
    return grouped


# ---------------------------------------------------------------------------
# 写 xlsx
# ---------------------------------------------------------------------------


def _sheet_xml(rows: list[SharkRow]) -> str:
    total = len(rows) + 1  # 表头 + 数据
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        f'<dimension ref="A1:D{total}"/>',
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>',
        "<sheetData>",
    ]

    def cell(ref: str, value: str) -> str:
        return f'<c r="{ref}" t="str"><v>{_escape(value)}</v></c>'

    lines.append(
        '<row r="1">'
        + "".join(cell(f"{col}1", name) for col, name in zip("ABCD", HEADERS))
        + "</row>"
    )
    for i, row in enumerate(rows, start=2):
        lines.append(
            f'<row r="{i}">'
            + cell(f"A{i}", row.key)
            + cell(f"B{i}", row.description)
            + cell(f"C{i}", row.en_us)
            + cell(f"D{i}", row.zh_cn)
            + "</row>"
        )

    lines += [
        "</sheetData>",
        f'<ignoredErrors><ignoredError numberStoredAsText="1" sqref="A1:D{total}"/></ignoredErrors>',
        "</worksheet>",
    ]
    return "".join(lines)


def write_workbook(out_path: str, rows: list[SharkRow], template: str | bytes | None = None) -> None:
    """
    写出一个 shark 导入文件（按模板的 zip 结构重写 sheet1.xml，其余部件原样复制）。

    template 传路径或原始字节可以覆盖内嵌模板，用于模板变更时的对比/调试；默认用内嵌的。
    """
    if isinstance(template, bytes):
        source = template
    elif isinstance(template, str):
        source = open(template, "rb").read()
    else:
        source = shark_template.TEMPLATE_BYTES

    with zipfile.ZipFile(io.BytesIO(source)) as src:
        if SHEET_XML not in src.namelist():
            raise ValueError(f"模板里没有 {SHEET_XML}")
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                data = _sheet_xml(rows).encode("utf-8") if item.filename == SHEET_XML else src.read(item.filename)
                dst.writestr(item, data)


def export(
    content: TableContent,
    appid_by_key: dict[str, KeyMatch],
    outdir: str = "shark_import",
    template: str | bytes | None = None,
    whitelist: set[str] | None = None,
) -> SharkExportOutput:
    """组装 + 按 appId 落盘，返回结果与报告数据（模板默认用内嵌的）"""
    whitelist = APPID_WHITELIST if whitelist is None else whitelist
    rows, skipped, expanded = build_rows(content, appid_by_key)

    output = SharkExportOutput(
        skipped=skipped,
        title=content.meta.title.strip(),
        period=content.meta.period.strip(),
        expanded=expanded,
    )
    output.duplicate_count = len(content.rows) - len({r.key for r in content.rows})

    for appid, group in group_by_appid(rows).items():
        filename = f"{appid}_{safe_filename(content.meta.title)}.xlsx"
        path = os.path.join(outdir, filename)
        write_workbook(path, group, template)
        output.results.append(
            SharkExportResult(
                appid=appid,
                file_path=path,
                row_count=len(group),
                in_whitelist=appid in whitelist,
            )
        )

    return output


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

_REASON_TEXT = {
    "empty_key": "文档里 key 为空",
    "not_found": "key 在 i18n 库里查不到 appId",
    "multi_appid": "key 命中多个 appId",
    "empty_copywriter": "en-US by copywriters 为空",
    "multi_real_key": "数字 key 按前缀匹配到多个完整 key",
}


def render_report(output: SharkExportOutput, tool: str = "") -> str:
    """tool 是调用方给的构建标识（cli.build_stamp），只用于排查版本"""
    lines = [
        "文档取表 → shark 导入文件报告",
        f"程序：{tool}（对不上就说明跑的不是新包）",
        f"小标题：{output.title}（{output.period}）",
        f"输出文件：{len(output.results)} 个"
        + (f"，重复 key 覆盖 {output.duplicate_count} 条" if output.duplicate_count else ""),
        "",
    ]

    if output.results:
        lines.append("【输出文件】appId → 文件 → 行数")
        for r in output.results:
            flag = "" if r.in_whitelist else "  ⚠ 该 appId 不在白名单里，请确认是否要导入"
            lines.append(f"  {r.appid} → {r.file_path}（{r.row_count} 行）{flag}")
    else:
        lines.append("【输出文件】无——没有可导出的行")

    if output.expanded:
        lines.append("")
        lines.append(f"【key 已校正成库里的写法】{len(output.expanded)} 条"
                     "（补全截断的数字 key / 纠正大小写，写进文件的是右边这个）")
        for doc_key, real_key in output.expanded.items():
            lines.append(f"  {doc_key} → {real_key}")

    lines.append("")
    lines.append("【未导出的行】需人工处理")
    if not output.skipped:
        lines.append("  无")
    else:
        grouped: OrderedDict[str, list[SkippedKey]] = OrderedDict()
        for s in output.skipped:
            grouped.setdefault(s.reason, []).append(s)
        for reason, items in grouped.items():
            lines.append(f"  {_REASON_TEXT.get(reason, reason)}（{len(items)} 条）")
            for s in items[:20]:
                key = s.key or "(空)"
                detail = f" — {s.detail}" if s.detail else ""
                lines.append(f"    - {key}{detail}")
            if len(items) > 20:
                lines.append(f"    …… 其余 {len(items) - 20} 条见 JSON 输出")

    lines.append("")
    lines.append("下一步：在 shark 上按项目（appId）选择对应文件导入；未匹配的 key 请在文档里补全后重跑。")
    return "\n".join(lines)


def output_to_dict(output: SharkExportOutput) -> dict:
    """--json 用的结构化输出，供 AI 解析"""
    return {
        "title": output.title,
        "period": output.period,
        "duplicate_count": output.duplicate_count,
        "expanded_keys": output.expanded,
        "files": [
            {
                "appid": r.appid,
                "path": r.file_path,
                "row_count": r.row_count,
                "in_whitelist": r.in_whitelist,
            }
            for r in output.results
        ],
        "skipped": [
            {"key": s.key, "reason": s.reason, "detail": s.detail} for s in output.skipped
        ],
    }
