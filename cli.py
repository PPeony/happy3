"""
cli.py - command line interface

Subcommands:
  query   -- query Archery, output JSON
  export  -- convert query JSON to CSV
  import  -- process developer-edited CSV, output clean CSV
  full    -- query + export in one step
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from typing import Literal

from archery import ArcheryQueryError, ArcherySession, LoginError, SessionExpiredError, login
from credentials import load as creds_load, save as creds_save, cred_path
from models import Candidate, I18nEntry

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


def _get_session(args: argparse.Namespace) -> ArcherySession:
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
                print(f"Error: no saved credentials at {cred_path()}", file=sys.stderr)
                sys.exit(1)
            username = saved["username"]
            password = saved["password"]
            print(f"[creds] using saved credentials for user: {username}")
        else:
            print("Error: provide --cookie/--csrf, --username/--password, or --use-saved-creds",
                  file=sys.stderr)
            sys.exit(1)

    try:
        session = login(username, password)
    except LoginError as e:
        print(f"Login error: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Network error: {e}", file=sys.stderr)
        sys.exit(2)

    if getattr(args, "save_creds", False):
        creds_save(username, password)
        print(f"[creds] credentials saved to {cred_path()}")

    return session


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

    args = parser.parse_args(argv)
    if args.command == "query":
        cmd_query(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "import":
        cmd_import(args)
    elif args.command == "full":
        cmd_full(args)
