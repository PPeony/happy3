# happyhappyhappy — AI Tool Guide

You are an AI assistant helping a product manager extract i18n keys from UI screenshots.
This document tells you exactly how to drive the `happyhappyhappy` CLI to complete the full workflow.

**CRITICAL: This is a multi-step workflow. You MUST stop and wait for user confirmation between Step 2 and Step 3. Do NOT proceed to Step 3 automatically.**

---

## What this tool does

Given a list of Chinese UI strings, the tool queries an internal i18n database (Archery) and returns:
- the `trip_appid` and `key` for each string
- the English translation (`en_us`)
- a status: `found` / `not_found` / `ambiguous`

Results are exported as a CSV file ready to hand off to a copywriter or developer.

---

## Prerequisites

1. **Saved credentials** — the user must have logged in via the GUI and ticked "记住密码".
   Credentials are stored at `~/.happyhappyhappy/credentials.json`.
   If missing, ask the user to open the app and log in first.

2. **Executable** — use the platform binary:
   - Windows: `happyhappyhappy.exe`
   - macOS GUI: double-click `happyhappyhappy.app`
   - macOS CLI: `happyhappyhappy.app/Contents/MacOS/happyhappyhappy`
   - Dev (source): `python main.py`

   All CLI examples below use `happyhappyhappy` as a placeholder.
   On macOS replace it with `happyhappyhappy.app/Contents/MacOS/happyhappyhappy`.

---

## Step-by-step workflow

### Step 1 — Receive the screenshot

The user sends you a screenshot of a UI page.

Before extracting, **ask the user one question**:

> 这些文本是否来自同一个接口或同一类数据？（如果是，我可以自动推断重复词条的归属）

If the user says yes → add `--same-interface` flag in Step 3.
If the user says no or unsure → do not add the flag.

### Step 2 — Extract Chinese strings and WAIT for confirmation

Identify all visible Chinese text from the screenshot.

Present the list to the user in the chat like this:

> 我从截图中识别到以下中文文本，请确认是否正确：
> 1. 金额
> 2. 房间号
> 3. 确认订单
> 4. 取消
>
> 如有遗漏或多余，请告诉我，确认无误后我再继续查询。

**STOP HERE. Do NOT proceed to Step 3 until the user explicitly confirms.**

Wait for the user to reply with one of:
- "对" / "没问题" / "确认" → proceed to Step 3 with the current list
- corrections like "少了XX" / "去掉XX" / "改成XX" → update the list, show the revised list, and ask for confirmation again
- a completely new list → use that list and ask for confirmation

Only move to Step 3 after the user has confirmed the list.

### Step 3 — Run the query

Write the confirmed list to `~/Desktop/texts.json`:
```json
{
  "texts": ["金额", "房间号", "确认订单", "取消"]
}
```

**Without** same-interface:
```bash
happyhappyhappy --cli full \
  --texts-file ~/Desktop/texts.json \
  --use-saved-creds \
  --output ~/Desktop/i18n_result.csv
```

**With** same-interface (user confirmed all strings are from the same interface):
```bash
happyhappyhappy --cli full \
  --texts-file ~/Desktop/texts.json \
  --use-saved-creds \
  --same-interface \
  --output ~/Desktop/i18n_result.csv
```

When `--same-interface` is used, the tool automatically resolves ambiguous entries using the trip_appid found in other `found` entries from the same query. See "Same-interface resolution" section below.

### Step 4 — Read the CSV and report back

Open `~/Desktop/i18n_result.csv` (UTF-8 with BOM) and summarize the results to the user in the chat. Do not ask the user to open the file themselves.

Report format:
> 查询完成，共 N 条：
> - ✓ 金额 → key: pos.amount，英文: Amount
> - ✓ 房间号 → key: pos.room.no，英文: Room No.（已自动推断）
> - ✗ 获取微信实例异常 → 未找到
> - ⚠ 确认订单 → 有歧义，已写入 CSV，请发给研发确认

Status meanings:

| status | meaning | action |
|--------|---------|--------|
| `found` | unique match found | ready to use |
| `not_found` | no match in database | inform user, skip row |
| `ambiguous` | multiple candidates | CSV contains all candidates with `note=待研发确认`; tell user to send CSV to developer |
| `confirmed` | auto-resolved or previously confirmed | ready to use |

---

## Same-interface resolution

When the user declares all strings come from the same interface/API, add `--same-interface` to `full` or `query`.

**How it works:**
1. After querying, the tool collects all `trip_appid` values from `found` entries.
2. If all `found` entries share the **same** `trip_appid` → ambiguous entries are filtered to keep only candidates with that appid. If exactly one candidate remains, it is automatically upgraded to `found`.
3. If `found` entries have **different** `trip_appid` values → the tool prints a warning and does NOT auto-resolve:

```
[WARNING] 同一接口返回的 trip_appid 不同：['100061217', '100074326']，无法自动推断，请用户手动确认歧义项。
```

When you see this warning, tell the user:
> ⚠ 警告：查询结果中发现不同的 trip_appid（XXX 和 YYY），无法自动推断歧义项归属。请手动确认，或检查这些文本是否真的来自同一接口。

---

## CLI reference

There are four subcommands. Use them in order:

| step | command | input | output |
|------|---------|-------|--------|
| 1 | `full` | texts.json + credentials | CSV (all-in-one, recommended) |
| 1a | `query` | texts.json + credentials | JSON |
| 1b | `export` | JSON from `query` | CSV |
| 2 | `import` | developer-edited CSV | clean final CSV |

### `full` — query + export in one step (recommended)

```bash
happyhappyhappy --cli full \
  --texts-file <path/to/texts.json> \
  --use-saved-creds \
  --output <path/to/output.csv> \
  [--same-interface] \
  [--ambiguous all|skip]
```

### `query` — query only, output JSON

```bash
happyhappyhappy --cli query \
  --texts-file <path/to/texts.json> \
  --use-saved-creds \
  --output <path/to/result.json> \
  [--same-interface]
```

### `export` — convert query JSON to CSV

Input is the **JSON file** produced by `query`. Do not pass a CSV here.

```bash
happyhappyhappy --cli export \
  --input  <path/to/result.json> \
  --output <path/to/output.csv> \
  [--ambiguous all|skip]
```

### `import` — process developer-edited CSV, output clean CSV

```bash
happyhappyhappy --cli import \
  --input  <path/to/dev_confirmed.csv> \
  --output <path/to/final.csv>
```

### Auth options (same for `query` and `full`)

| option | description |
|--------|-------------|
| `--use-saved-creds` | use `~/.happyhappyhappy/credentials.json` **(recommended)** |
| `--username` / `--password` | pass credentials directly |
| `--cookie` + `--csrf` | pass a live session cookie (advanced) |

---

## Output format

### CSV columns

| column | description |
|--------|-------------|
| `trip_appid` | Ctrip app ID |
| `key` | i18n key |
| `zh_cn` | original Chinese string |
| `en_us` | English translation |
| `image_url` | screenshot URL (empty, not implemented) |
| `status` | `found` / `not_found` / `ambiguous` / `confirmed` |
| `note` | `待研发确认` for ambiguous rows, otherwise empty |

---

## Re-import flow (for ambiguous items)

**Step A** — The `full` command already writes all candidate rows with `note=待研发确认`.
Tell the user: "请将 CSV 发给研发，研发在 CSV 里删掉每个中文词条多余的候选行，只保留正确的一行，然后发回给你。"

**Step B** — When developer sends back the edited CSV, run:

```bash
happyhappyhappy --cli import \
  --input  <path/to/dev_confirmed.csv> \
  --output <path/to/final.csv>
```

**Do not use `export` for this step** — `export` only accepts JSON input from `query`.

---

## Error handling

| error message | cause | fix |
|---------------|-------|-----|
| `Error: no saved credentials` | credentials file missing | ask user to open app and log in with "记住密码" ticked |
| `Login error: 登录失败` | wrong username/password | ask user to re-login via GUI |
| `Auth error: Session 已过期` | session expired | re-login |
| `[WARNING] 同一接口返回的 trip_appid 不同` | same-interface flag used but found entries have different appids | warn user, do not auto-resolve |
| `Query error: ...` | Archery SQL error | ask user to contact developer |
| network timeout | not on intranet | confirm user is on VPN / office network |
