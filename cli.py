"""
cli.py - command line interface

Subcommands:
  query        -- query Archery, output JSON
  export       -- convert query JSON to CSV
  import       -- process developer-edited CSV, output clean CSV
  full         -- query + export in one step
  docx-titles  -- list / fuzzy-search section titles in the RezenOne docx
  shark-export -- build shark import xlsx files (one per appId) from a docx table
  doc-submit   -- append the check result as a table into an existing Feishu doc

`upload`（上传飞书多维表格）已移除：载体选错了，要求是写进对应格式的飞书文档。
2026-09-28 重启，新入口是 `doc-submit`（docx 块），详见 doc/spec.md 的"飞书结果提交（重启中）"。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from collections import defaultdict
from typing import Literal

import shark
from archery import ArcheryQueryError, ArcherySession, LoginError, SessionExpiredError, login
from credentials import load as creds_load, save as creds_save, cred_path
import feishu_doc
import feishu_table
from docx_table import DocxColumnError, DocxError, TableNotFoundError, load_document
from table_doc import TableDocError
from models import EXPECTED_COLUMNS, Candidate, I18nEntry

def _tool_dir() -> str:
    """工具所在目录：打包后是 exe 所在目录，开发时是本文件所在目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _search_bases() -> list[tuple[str, str]]:
    """
    默认文件的查找位置，按优先级返回 [(说明, 目录)]。

    为什么要找这么多地方：工具会进 PATH、也可能从 dist/ 里被调用，
    而 doc/ 通常在仓库根目录（也就是 exe 的上一级），只按 cwd 解析必然找不到。
    """
    bases: list[tuple[str, str]] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:                                  # --add-data 打进 exe 的文件
        bases.append(("打包内", meipass))

    tool_dir = _tool_dir()
    bases.append(("工具安装目录", tool_dir))

    parent = os.path.dirname(tool_dir)           # exe 在 dist/ 时，doc/ 在它上一级
    if parent and parent != tool_dir:
        bases.append(("安装目录的上一级", parent))

    bases.append(("当前工作目录", os.getcwd()))
    return bases


def candidate_paths(rel_path: str) -> list[str]:
    """要找一个默认文件时，实际会查哪些绝对路径（报错信息里用）"""
    return [os.path.join(base, rel_path) for _, base in _search_bases()]


def _first_existing(rel_path: str) -> str:
    """按 _search_bases() 的顺序找第一个存在的文件；都没有就返回安装目录下的路径（报错时给人看）"""
    for candidate in candidate_paths(rel_path):
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(_tool_dir(), rel_path)


_DOC_REL = os.path.join("doc", "RezenOne翻译需求.docx")

# 词条表有 135MB，放在程序旁边（或用 --doc 指定）；shark 模板是常量，已内嵌在 shark_template.py 里
DEFAULT_DOCX = _first_existing(_DOC_REL)
DEFAULT_OUTDIR = "shark_import"


CSV_FIELDNAMES = ["trip_appid", "key", "zh_cn", "en_us", "image_url", "status", "note"]


# ---------------------------------------------------------------------------
# JSON serialize / deserialize
# ---------------------------------------------------------------------------

def entries_to_json(entries: list[I18nEntry]) -> dict:
    results = []
    for e in entries:
        if e.status in ("found", "confirmed"):
            results.append({
                "zh_cn": e.zh_cn,
                "status": e.status,
                "trip_appid": e.trip_appid,
                "key": e.key,
                "en_us": e.en_us,
            })
        elif e.status == "ambiguous":
            results.append({
                "zh_cn": e.zh_cn,
                "status": "ambiguous",
                "candidates": [
                    {"trip_appid": c.trip_appid, "key": c.key, "en_us": c.en_us}
                    for c in e.candidates
                ],
            })
        else:
            results.append({"zh_cn": e.zh_cn, "status": "not_found"})
    return {"results": results}


def entries_from_json(data: dict) -> list[I18nEntry]:
    entries = []
    for r in data.get("results", []):
        status = r["status"]
        if status in ("found", "confirmed"):
            entries.append(I18nEntry(
                zh_cn=r["zh_cn"], status=status,
                trip_appid=r.get("trip_appid"),
                key=r.get("key"),
                en_us=r.get("en_us"),
            ))
        elif status == "ambiguous":
            candidates = [
                Candidate(trip_appid=c["trip_appid"], key=c["key"], en_us=c.get("en_us"))
                for c in r.get("candidates", [])
            ]
            entries.append(I18nEntry(zh_cn=r["zh_cn"], status="ambiguous", candidates=candidates))
        else:
            entries.append(I18nEntry(zh_cn=r["zh_cn"], status="not_found"))
    return entries


# ---------------------------------------------------------------------------
# CSV export / import
# ---------------------------------------------------------------------------

def entries_to_csv(
    entries: list[I18nEntry],
    output_path: str,
    ambiguous: Literal["skip", "all"] = "all",
) -> int:
    written = 0
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for e in entries:
            if e.status in ("found", "confirmed", "not_found"):
                writer.writerow(e.to_csv_row())
                written += 1
            elif e.status == "ambiguous" and ambiguous == "all":
                for c in e.candidates:
                    writer.writerow({
                        "trip_appid": c.trip_appid,
                        "key": c.key,
                        "zh_cn": e.zh_cn,
                        "en_us": c.en_us or "",
                        "image_url": "",
                        "status": "ambiguous",
                        "note": "待研发确认",
                    })
                    written += 1
    return written


def entries_from_csv(input_path: str) -> list[I18nEntry]:
    """
    Import CSV back to I18nEntry list.

    Rules:
    - found / confirmed / not_found: restore directly (single row)
    - ambiguous, only 1 row left for this zh_cn: upgrade to confirmed
    - ambiguous, still multiple rows: keep as ambiguous with candidates
    """
    rows: list[dict] = []
    with open(input_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rows.append(row)

    groups: dict[str, list[dict]] = defaultdict(list)
    order: list[str] = []
    for row in rows:
        zh = row.get("zh_cn", "")
        if zh not in groups:
            order.append(zh)
        groups[zh].append(row)

    entries: list[I18nEntry] = []
    for zh in order:
        group = groups[zh]
        first = group[0]
        status = first.get("status", "").strip()

        if status in ("found", "confirmed"):
            entries.append(I18nEntry(
                zh_cn=zh, status=status,
                trip_appid=first.get("trip_appid") or None,
                key=first.get("key") or None,
                en_us=first.get("en_us") or None,
            ))
        elif status == "not_found":
            entries.append(I18nEntry(zh_cn=zh, status="not_found"))
        elif status == "ambiguous":
            if len(group) == 1:
                entries.append(I18nEntry(
                    zh_cn=zh, status="confirmed",
                    trip_appid=first.get("trip_appid") or None,
                    key=first.get("key") or None,
                    en_us=first.get("en_us") or None,
                ))
            else:
                candidates = [
                    Candidate(
                        trip_appid=r.get("trip_appid", ""),
                        key=r.get("key", ""),
                        en_us=r.get("en_us") or None,
                    )
                    for r in group
                ]
                entries.append(I18nEntry(zh_cn=zh, status="ambiguous", candidates=candidates))
        else:
            entries.append(I18nEntry(zh_cn=zh, status="not_found"))

    return entries


# ---------------------------------------------------------------------------
# Same-interface hint: resolve ambiguous entries using known appid
# ---------------------------------------------------------------------------

class SameInterfaceConflict(Exception):
    """Raised when found entries have inconsistent trip_appids."""
    pass


def apply_same_interface_hint(entries: list[I18nEntry]) -> list[I18nEntry]:
    """
    When the user declares all strings come from the same interface/API,
    use the trip_appid(s) from `found` entries to narrow down ambiguous ones.

    Rules:
    1. Collect all trip_appids from `found` / `confirmed` entries.
    2. If those appids are NOT all the same → raise SameInterfaceConflict
       (caller should warn the user and skip auto-resolution).
    3. If there is exactly one consistent appid → for each `ambiguous` entry,
       keep only candidates matching that appid.
       - Exactly 1 candidate remains → upgrade to `confirmed`.
       - 0 candidates remain → keep as `ambiguous` (appid not in candidates).
       - More than 1 candidate remains → keep as `ambiguous`.
    4. If there are no `found` entries at all → nothing to infer, return as-is.
    """
    found_appids = {
        e.trip_appid
        for e in entries
        if e.status in ("found", "confirmed") and e.trip_appid
    }

    if not found_appids:
        return entries  # nothing to infer from

    if len(found_appids) > 1:
        raise SameInterfaceConflict(
            f"同一接口返回的 trip_appid 不同：{sorted(found_appids)}，"
            f"无法自动推断，请用户手动确认歧义项。"
        )

    # Exactly one consistent appid
    target_appid = next(iter(found_appids))
    resolved = []
    for e in entries:
        if e.status != "ambiguous":
            resolved.append(e)
            continue

        matching = [c for c in e.candidates if c.trip_appid == target_appid]
        if len(matching) == 1:
            c = matching[0]
            resolved.append(I18nEntry(
                zh_cn=e.zh_cn,
                status="found",
                trip_appid=c.trip_appid,
                key=c.key,
                en_us=c.en_us,
            ))
        else:
            # 0 or still multiple matches under this appid — keep ambiguous
            resolved.append(e)

    return resolved


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _parse_sessionid(cookie_str: str) -> str:
    for part in cookie_str.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == "sessionid":
            return v.strip()
    raise ValueError(f"sessionid not found in cookie: {cookie_str!r}")


def _get_session(args: argparse.Namespace, result_path: str | None = None) -> ArcherySession:
    """登录 Archery。result_path 给了就把登录失败也写进结果文件（stdout/stderr 可能不可用）"""

    def _die(message: str, code: int):
        if result_path:
            _fail(message, result_path, code)
        print(f"Error: {message}", file=sys.stderr)
        sys.exit(code)

    if getattr(args, "cookie", None) and getattr(args, "csrf", None):
        return ArcherySession(
            csrftoken=args.csrf,
            sessionid=_parse_sessionid(args.cookie),
        )

    username = getattr(args, "username", None)
    password = getattr(args, "password", None)

    if not username or not password:
        if getattr(args, "use_saved_creds", False):
            saved = creds_load()
            if not saved:
                _die(f"no saved credentials at {cred_path()}", 1)
            username = saved["username"]
            password = saved["password"]
            print(f"[creds] using saved credentials for user: {username}")
        else:
            _die("provide --cookie/--csrf, --username/--password, or --use-saved-creds", 1)

    try:
        session = login(username, password)
    except LoginError as e:
        _die(f"登录失败：{e}", 2)
    except Exception as e:
        _die(f"网络错误：{e}", 2)

    if getattr(args, "save_creds", False):
        creds_save(username, password)
        print(f"[creds] credentials saved to {cred_path()}")

    return session


def build_stamp() -> str:
    """
    这份程序是什么时候构建（开发时是源码最后修改）的。

    排查"我跑的到底是不是新包"用——源码改了但 exe 没重打包，是反复踩过的坑；
    把构建时间写进结果文件和报告，一眼就能对照。
    """
    target = sys.executable if getattr(sys, "frozen", False) else __file__
    try:
        stamp = datetime.fromtimestamp(os.path.getmtime(target)).strftime("%Y-%m-%d %H:%M")
    except OSError:
        stamp = "未知"
    return f"{os.path.basename(target)}（{stamp}）"


def _result_path(args: argparse.Namespace, kind: str, default_dir: str | None = None) -> str:
    """
    结果文件路径：给了 --output 就用它，否则放在 default_dir（默认当前目录）下的固定名字。

    结果一律写文件，不靠 stdout——见 _write_result 的说明。
    """
    output = getattr(args, "output", None)
    if output:
        return output
    base = default_dir or os.getcwd()
    return os.path.join(base, f"happyhappyhappy-{kind}.json")


def _write_result(path: str, payload: dict) -> str:
    """
    把结果写成 JSON 文件——新子命令**唯一**的输出通道。

    为什么不打印：打包成 Windows GUI 子系统程序时（`flet pack` 的默认行为，也是给产品双击的那个包），
    PyInstaller 会把 sys.stdout / sys.stderr 都置成 None，`print()` 遇到 stdout 为 None 会**静默返回**
    ——命令成功、退出码 0，调用方（尤其是 AI）什么都看不到。老的那些子命令（query / full）真正的产出
    本来就是文件，所以一直没暴露这个问题；新子命令干脆从设计上就走文件，不再依赖 stdout。
    成功与失败都写进同一个文件（`ok` 字段区分），这样调用方只要读文件就能判断结果。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def _fail(message: str, result_path: str, exit_code: int = 1, extra: dict | None = None) -> None:
    """出错：写进结果文件 + 尽力打到 stderr，然后按退出码退出"""
    payload = {"ok": False, "error": message, "exit_code": exit_code, "result_path": result_path}
    if extra:
        payload.update(extra)
    try:
        _write_result(result_path, payload)
    except Exception:
        pass
    print(f"Error: {message}", file=sys.stderr)
    print(f"(结果文件：{result_path})", file=sys.stderr)
    sys.exit(exit_code)


def _load_source(args: argparse.Namespace, result_path: str):
    """
    读词条表，两个数据源选一个：

    - `--feishu-doc <链接>`：直接读飞书**活文档**（不用先导出 docx，也不会读到旧快照）；
    - `--doc <路径>`（或默认位置）：读本地导出的 docx，离线也能用。
    """
    link = getattr(args, "feishu_doc", None)
    if link:
        try:
            document = feishu_table.load_document(link)
        except TableDocError as e:
            _fail(f"{e}", result_path, 1, {"feishu_doc": link})
        for warning in document.warnings:
            print(f"[warn] {warning}", file=sys.stderr)
        return document

    explicit = getattr(args, "doc", None)
    path = explicit or DEFAULT_DOCX
    try:
        return load_document(path)
    except DocxError as e:
        detail = {"doc_path": path}
        if not explicit:
            detail["searched"] = candidate_paths(_DOC_REL)
        _fail(f"{e}（用 --doc 指定词条表路径，或用 --feishu-doc 直接读飞书文档）", result_path, 1, detail)


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_query(args: argparse.Namespace) -> None:
    if args.texts:
        texts = json.loads(args.texts)
    elif args.texts_file:
        with open(args.texts_file, encoding="utf-8") as f:
            texts = json.load(f)["texts"]
    else:
        print("Error: provide --texts or --texts-file", file=sys.stderr)
        sys.exit(1)

    session = _get_session(args)
    try:
        entries = session.query_texts(texts)
    except SessionExpiredError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        sys.exit(2)
    except ArcheryQueryError as e:
        print(f"Query error: {e}", file=sys.stderr)
        sys.exit(3)

    if getattr(args, "same_interface", False):
        try:
            entries = apply_same_interface_hint(entries)
            auto_confirmed = sum(1 for e in entries if e.status == "confirmed")
            if auto_confirmed:
                print(f"[same-interface] auto-confirmed {auto_confirmed} ambiguous entries")
        except SameInterfaceConflict as e:
            print(f"[WARNING] {e}", file=sys.stderr)

    result = entries_to_json(entries)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Written to {args.output}")
    else:
        print(output)


def cmd_export(args: argparse.Namespace) -> None:
    """Convert a query result JSON to CSV."""
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    entries = entries_from_json(data)
    written = entries_to_csv(entries, args.output, ambiguous=args.ambiguous)
    print(f"Exported {written} rows -> {args.output}")


def cmd_import(args: argparse.Namespace) -> None:
    """
    Process a developer-edited CSV and output a clean final CSV.

    The developer receives a CSV where ambiguous rows have note=待研发确认.
    They delete the wrong candidate rows (keeping exactly one row per zh_cn),
    then send the file back. This command re-processes it:
    - ambiguous rows with only 1 row remaining -> upgraded to confirmed
    - ambiguous rows still with multiple rows -> kept as ambiguous
    """
    entries = entries_from_csv(args.input)
    written = entries_to_csv(entries, args.output, ambiguous="all")

    confirmed     = sum(1 for e in entries if e.status == "confirmed")
    found         = sum(1 for e in entries if e.status == "found")
    not_found     = sum(1 for e in entries if e.status == "not_found")
    still_ambig   = sum(1 for e in entries if e.status == "ambiguous")

    print(f"Processed {len(entries)} entries -> {args.output}")
    print(f"  confirmed (dev resolved) : {confirmed}")
    print(f"  found                    : {found}")
    print(f"  not_found                : {not_found}")
    if still_ambig:
        print(f"  still ambiguous          : {still_ambig}  (developer did not resolve these)")


def cmd_full(args: argparse.Namespace) -> None:
    if args.texts:
        texts = json.loads(args.texts)
    elif args.texts_file:
        with open(args.texts_file, encoding="utf-8") as f:
            texts = json.load(f)["texts"]
    else:
        print("Error: provide --texts or --texts-file", file=sys.stderr)
        sys.exit(1)

    session = _get_session(args)
    try:
        entries = session.query_texts(texts)
    except SessionExpiredError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        sys.exit(2)
    except ArcheryQueryError as e:
        print(f"Query error: {e}", file=sys.stderr)
        sys.exit(3)

    if getattr(args, "same_interface", False):
        try:
            entries = apply_same_interface_hint(entries)
            auto_confirmed = sum(1 for e in entries if e.status == "confirmed")
            if auto_confirmed:
                print(f"[same-interface] auto-confirmed {auto_confirmed} ambiguous entries")
        except SameInterfaceConflict as e:
            print(f"[WARNING] {e}", file=sys.stderr)

    written = entries_to_csv(entries, args.output, ambiguous=args.ambiguous)
    print(f"Exported {written} rows -> {args.output}")


def cmd_docx_titles(args: argparse.Namespace) -> None:
    """
    列出/搜索文档里的小标题，供人工或 AI 选择。

    命中多张表时不做取舍，全部列出——由人确认要哪一张，再传给 shark-export。
    结果写到 `--output` 指定的文件（默认当前目录的 happyhappyhappy-docx-titles.json）。
    """
    result_path = _result_path(args, "docx-titles")
    doc = _load_source(args, result_path)
    keyword = getattr(args, "keyword", "") or ""
    metas = doc.search(keyword)

    payload = {
        "ok": True,
        "tool": build_stamp(),
        "source": doc.source,
        "warnings": list(getattr(doc, "warnings", [])),
        "keyword": keyword,
        "count": len(metas),
        "tables": [
            {
                "order": m.order,
                "period": m.period,
                "title": m.title,
                "columns": m.columns,
                "row_count": m.row_count,
                "compliant": m.compliant,
            }
            for m in metas
        ],
        "result_path": result_path,
    }
    if not metas:
        payload["hint"] = "没有匹配的小标题，换个关键词试试"
    elif any(not m.compliant for m in metas):
        payload["hint"] = "列不符的表不能导出，需先让文档维护者改成固定六列：" + "、".join(EXPECTED_COLUMNS)

    _write_result(result_path, payload)
    print(f"Written to {result_path}")


def cmd_doc_submit(args: argparse.Namespace) -> None:
    """
    把校验结果按固定六列写成一张表，插进**已有飞书文档**的指定标题下面。

    截图走两步：先上传拿 file_token，再让 image 块引用它；每行的截图格都插同一张图。
    `--probe` 是最小验证模式：只插一个文本块 + 一张 2×2 小表格（带一张图），
    用来先把块结构在真文档上验一遍。
    """
    result_path = _result_path(args, "doc-submit")

    try:
        if getattr(args, "probe", False):
            out = feishu_doc.probe(
                args.doc_url, args.title, getattr(args, "screenshot", None),
                force_reauth=getattr(args, "reauth", False),
            )
            _write_result(result_path, {"ok": True, "tool": build_stamp(), "probe": True,
                                        "result_path": result_path, **out})
            print(f"Written to {result_path}")
            return

        if not getattr(args, "input", None):
            _fail("非探针模式要传 --input（链路 A 的查询结果 JSON）", result_path, 1)
        with open(args.input, encoding="utf-8") as f:
            entries = entries_from_json(json.load(f))

        rows = []
        for i, e in enumerate(entries, start=1):
            rows.append([
                str(i),
                e.key or "",
                e.zh_cn or "",
                e.en_us or "",
                "",                      # en-US by copywriters：链路 A 没有校对值，留空
                "",                      # 截图列由 build_table 放图片块
            ])

        client = feishu_doc.FeishuDocClient(force_reauth=getattr(args, "reauth", False))
        document_id = client.resolve(args.doc_url)     # 支持 /docx/ 和 /wiki/ 两种链接

        image_path = getattr(args, "screenshot", None)
        image_size_px = feishu_doc.image_size(image_path) if image_path else None

        blocks = client.list_blocks(document_id)
        parent_id, index = feishu_doc.find_insert_position(blocks, args.title)

        # 行多就拆成多张表：一次请求能带的块数有上限，而且太长的表人也不好读
        per_table = int(getattr(args, "rows_per_table", None) or 20)
        chunks = [rows[i:i + per_table] for i in range(0, len(rows), per_table)] or [[]]

        created = 0
        image_cells = 0
        for n, chunk in enumerate(chunks, start=1):
            label = f"{getattr(args, 'label', '') or 'i18n 校验结果'}（{datetime.now():%Y-%m-%d %H:%M}）"
            if len(chunks) > 1:
                label += f" 第 {n}/{len(chunks)} 段"

            # ① 建表：截图列先留空（空文本块占位——单元格必须至少有一个子块）
            nid = feishu_doc._block_id_factory("s")
            label_id = nid()
            table = feishu_doc.build_table(header=feishu_doc.TABLE_HEADER, rows=chunk)
            result = client.create_descendant(
                document_id, parent_id, index + created,
                feishu_doc.TablePayload(
                    children_id=[label_id] + table.children_id,
                    descendants=[feishu_doc.text_block(label, label_id)] + table.descendants,
                ),
            )
            created += 1 + len(table.children_id)

            # ② 回查拿到表格的真实 block_id（建之前不知道），再逐行把截图放进"截图"列
            if not image_path:
                continue
            created_blocks = result.get("children") or []
            table_id = next((b.get("block_id") for b in created_blocks
                             if b.get("block_type") == feishu_doc.BLOCK_TABLE), "")
            if not table_id:
                continue

            columns = len(feishu_doc.TABLE_HEADER)
            cell_ids, _ = feishu_doc.locate_table(client, document_id, table_id)
            for r in range(1, len(chunk) + 1):          # 0 是表头，跳过
                cell_index = r * columns + (columns - 1)   # 最后一列 = 截图列
                if cell_index >= len(cell_ids):
                    continue
                image_cell = cell_ids[cell_index]
                # ③ 单元格里的图片是三步：建空图片块 → 上传素材 → PATCH 换图
                img_block_id = client.create_image_placeholder(document_id, image_cell, 0)
                client.put_image_in_block(document_id, image_path, img_block_id)
                image_cells += 1

        _write_result(result_path, {
            "ok": True, "tool": build_stamp(), "result_path": result_path,
            "document_id": document_id,
            "document_url": f"https://feishu.cn/docx/{document_id}",
            "title": args.title,
            "rows": len(rows),
            "tables": len(chunks),
            "image_cells": image_cells,
        })
        print(f"Written to {result_path}")
    except Exception as e:                      # 网络/接口错误一律落文件，别只在屏幕上
        _fail(f"{type(e).__name__}: {e}", result_path, 1)


def cmd_shark_export(args: argparse.Namespace) -> None:
    """
    从文档里取一张表，按 appId 生成 shark 导入文件。

    产物：`{outdir}/{appid}_{小标题}.xlsx`（每个 appId 一个）+ `{outdir}/report.txt`（人看的）
    + 结果 JSON（AI 看的，`--output` 指定，默认 {outdir}/happyhappyhappy-shark-export.json）。

    典型调用（AI 驱动）：
        happyhappyhappy --cli shark-export \\
            --doc <词条表路径> --title 预订-入住 --use-saved-creds
    """
    outdir = getattr(args, "outdir", None) or DEFAULT_OUTDIR
    result_path = _result_path(args, "shark-export", default_dir=outdir)
    doc = _load_source(args, result_path)

    try:
        content = doc.content(args.title, period=getattr(args, "period", None))
    except (TableNotFoundError, DocxColumnError) as e:
        _fail(str(e), result_path, 1)

    session = _get_session(args, result_path)
    try:
        appid_by_key = session.query_appid_by_keys([r.key for r in content.rows])
    except SessionExpiredError as e:
        _fail(f"Archery session 已过期，请重新登录：{e}", result_path, 2)
    except ArcheryQueryError as e:
        _fail(f"查询失败：{e}", result_path, 3)

    # shark 导入模板是常量，已内嵌在程序里，正常不用传；--template 只是调试时的覆盖入口
    template = getattr(args, "template", None)
    if template and not os.path.isfile(template):
        _fail(f"--template 指定的文件不存在：{template}", result_path, 1)

    output = shark.export(content, appid_by_key, outdir=outdir, template=template)

    report_path = os.path.join(outdir, "report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(shark.render_report(output, tool=build_stamp()) + "\n")

    data = {"ok": True, "tool": build_stamp(), **shark.output_to_dict(output),
            "report_path": report_path, "result_path": result_path}
    _write_result(result_path, data)
    print(f"Written to {result_path}")

    if not output.results:
        sys.exit(4)  # 一行都没导出，调用方需要看出这是失败


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _add_auth_args(p) -> None:
    g = p.add_argument_group("auth (pick one)")
    g.add_argument("--cookie", help="csrftoken=x; sessionid=y")
    g.add_argument("--csrf",   help="csrftoken value (use with --cookie)")
    g.add_argument("--username", "-u")
    g.add_argument("--password", "-p")
    g.add_argument("--use-saved-creds", dest="use_saved_creds", action="store_true",
                   help="use saved credentials from ~/.happyhappyhappy/credentials.json")
    g.add_argument("--save-creds", dest="save_creds", action="store_true",
                   help="save credentials after successful login")


def run_cli(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="happyhappyhappy --cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_query = sub.add_parser("query", help="query i18n keys, output JSON")
    p_query.add_argument("--texts", help="JSON array string, e.g. '[\"金额\",\"房间号\"]'")
    p_query.add_argument("--texts-file", dest="texts_file", help="path to texts.json")
    p_query.add_argument("--output", help="output JSON path (default: stdout)")
    p_query.add_argument("--same-interface", dest="same_interface", action="store_true",
                         help="all strings come from the same interface; auto-resolve ambiguous entries using the consistent trip_appid")
    _add_auth_args(p_query)

    p_export = sub.add_parser("export", help="convert query result JSON to CSV")
    p_export.add_argument("--input",  required=True, help="JSON file from `query`")
    p_export.add_argument("--output", required=True, help="output CSV path")
    p_export.add_argument("--ambiguous", choices=["skip", "all"], default="all",
                          help="how to handle ambiguous entries (default: all)")

    p_import = sub.add_parser("import",
                              help="process developer-edited CSV, output clean CSV")
    p_import.add_argument("--input",  required=True,
                          help="CSV edited by developer (one row per zh_cn kept)")
    p_import.add_argument("--output", required=True, help="output CSV path")

    p_full = sub.add_parser("full", help="query + export CSV in one step")
    p_full.add_argument("--texts", help="JSON array string")
    p_full.add_argument("--texts-file", dest="texts_file", help="path to texts.json")
    p_full.add_argument("--output", required=True, help="output CSV path")
    p_full.add_argument("--same-interface", dest="same_interface", action="store_true",
                        help="all strings come from the same interface; auto-resolve ambiguous entries using the consistent trip_appid")
    p_full.add_argument("--ambiguous", choices=["skip", "all"], default="all")
    _add_auth_args(p_full)

    p_titles = sub.add_parser("docx-titles",
                              help="list / fuzzy-search section titles in the docx")
    p_titles.add_argument("--doc", default=None, help=f"docx 路径（默认 {DEFAULT_DOCX}）")
    p_titles.add_argument("--feishu-doc", dest="feishu_doc", default=None,
                          help="改读飞书活文档：给 /docx/ 或 /wiki/ 链接（与 --doc 二选一）")
    p_titles.add_argument("--keyword", default="",
                          help="小标题关键词；忽略大小写、空格、全半角")
    p_titles.add_argument("--output", default=None,
                          help="结果 JSON 写到这个文件（默认：当前目录的 happyhappyhappy-docx-titles.json）")
    p_titles.add_argument("--json", action="store_true",
                          help=argparse.SUPPRESS)   # 结果本来就是 JSON，这个参数只为兼容旧调用保留

    p_doc = sub.add_parser("doc-submit",
                           help="append the check result as a table into an existing Feishu doc")
    p_doc.add_argument("--doc-url", dest="doc_url", required=True,
                       help="飞书文档链接（.../docx/xxx）或 document_id")
    p_doc.add_argument("--title", required=True,
                       help="插到这个标题下面（文档里标题的完整文字）")
    p_doc.add_argument("--input", default=None, help="链路 A 的查询结果 JSON（query 的输出）")
    p_doc.add_argument("--screenshot", default=None, help="界面截图（每行的截图格都插这张）")
    p_doc.add_argument("--label", default=None, help="批次小标题，默认「i18n 校验结果（时间）」")
    p_doc.add_argument("--rows-per-table", dest="rows_per_table", type=int, default=20,
                       help="一张表最多多少行，超了拆成多张（默认 20）")
    p_doc.add_argument("--probe", action="store_true",
                       help="最小验证：只插一个文本块 + 一张 2×2 小表格，先把块结构验通")
    p_doc.add_argument("--reauth", action="store_true",
                       help="强制重新走一次飞书 OAuth 授权（平时不用加：token 会缓存，"
                            "access_token 2 小时、refresh_token 30 天，自动刷新）")
    p_doc.add_argument("--output", default=None, help="结果 JSON 写到这个文件")
    g_doc = p_doc.add_argument_group("兼容旧调用")
    g_doc.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    p_shark = sub.add_parser("shark-export",
                             help="build shark import xlsx files (one per appId) from a docx table")
    p_shark.add_argument("--doc", default=None, help=f"docx 路径（默认 {DEFAULT_DOCX}）")
    p_shark.add_argument("--feishu-doc", dest="feishu_doc", default=None,
                         help="改读飞书活文档：给 /docx/ 或 /wiki/ 链接（与 --doc 二选一）")
    p_shark.add_argument("--title", required=True,
                         help="小标题关键词，必须唯一命中；命中多张会报错并列出候选")
    p_shark.add_argument("--period", default=None, help="小标题重名时用「期」限定")
    p_shark.add_argument("--template", default=None,
                         help="可选：用指定文件覆盖内置的 shark 导入模板（调试用，正常不用传）")
    p_shark.add_argument("--outdir", default=None,
                         help=f"输出目录（默认 {DEFAULT_OUTDIR}）")
    p_shark.add_argument("--output", default=None,
                         help="结果 JSON 写到这个文件（默认：<outdir>/happyhappyhappy-shark-export.json）")
    p_shark.add_argument("--json", action="store_true",
                         help=argparse.SUPPRESS)   # 结果本来就是 JSON，这个参数只为兼容旧调用保留
    _add_auth_args(p_shark)

    args = parser.parse_args(argv)
    if args.command == "query":
        cmd_query(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "import":
        cmd_import(args)
    elif args.command == "full":
        cmd_full(args)
    elif args.command == "docx-titles":
        cmd_docx_titles(args)
    elif args.command == "shark-export":
        cmd_shark_export(args)
    elif args.command == "doc-submit":
        cmd_doc_submit(args)
