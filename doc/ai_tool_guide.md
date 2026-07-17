# happyhappyhappy — AI Tool Guide

You are an AI assistant helping a product manager extract i18n keys from UI screenshots.
This document tells you exactly how to drive the `happyhappyhappy` CLI to complete the full workflow.

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
   - macOS: `./happyhappyhappy`
   - Dev (source): `python main.py`

   All examples below use `happyhappyhappy` as a placeholder.

---

## Step-by-step workflow

### Step 1 — You receive a screenshot

The user sends you a screenshot of a UI page.


### Step 2 — Extract Chinese strings

Identify all visible Chinese text. Output **only** the strings themselves, no labels or explanations.
Produce a `texts.json` file:

```json
{
  "texts": ["金额", "房间号", "确认订单", "取消"]
}
```

**Important: You need to tell user the Chinese you find. It can help user to check the result.**

Write this file to a known path, e.g. `~/Desktop/texts.json`.

### Step 3 — Run the query

```bash
happyhappyhappy --cli full \
  --texts-file ~/Desktop/texts.json \
  --use-saved-creds \
  --output ~/Desktop/i18n_result.csv
```

This performs login + query + CSV export in one step.

### Step 4 — Read the CSV and report back

Open `~/Desktop/i18n_result.csv` (UTF-8 with BOM) and summarize the results to the user:

| status | meaning | action |
|--------|---------|--------|
| `found` | unique match found | ready to use |
| `not_found` | no match in database | inform user, skip row |
| `ambiguous` | multiple candidates | CSV contains all candidates with `note=待研发确认`; tell user to send the CSV to a developer for confirmation |
| `confirmed` | previously confirmed by developer (re-import flow) | ready to use |

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
  [--ambiguous all|skip]   # default: all
```

### `query` — query only, output JSON

```bash
happyhappyhappy --cli query \
  --texts-file <path/to/texts.json> \
  --use-saved-creds \
  --output <path/to/result.json>
```

### `export` — convert query JSON to CSV

Input is the **JSON file** produced by `query`. Do not pass a CSV here.

```bash
happyhappyhappy --cli export \
  --input  <path/to/result.json> \
  --output <path/to/output.csv> \
  [--ambiguous all|skip]   # default: all
```

### `import` — process developer-edited CSV, output clean CSV

Input is a **CSV file** that was previously exported and then edited by a developer
(they deleted the wrong candidate rows, keeping exactly one row per Chinese string).

```bash
happyhappyhappy --cli import \
  --input  <path/to/dev_confirmed.csv> \
  --output <path/to/final.csv>
```

The command prints a summary:
```
Processed 10 entries -> final.csv
  confirmed (dev resolved) : 3
  found                    : 6
  not_found                : 1
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

### JSON result format (from `query`)

```json
{
  "results": [
    {
      "zh_cn": "金额",
      "status": "found",
      "trip_appid": "100074326",
      "key": "pos.amount",
      "en_us": "Amount"
    },
    {
      "zh_cn": "房间号",
      "status": "ambiguous",
      "candidates": [
        {"trip_appid": "100074326", "key": "pos.room.no",    "en_us": "Room No."},
        {"trip_appid": "100061217", "key": "hotel.room.num", "en_us": "Room Number"}
      ]
    },
    {
      "zh_cn": "获取微信实例异常",
      "status": "not_found"
    }
  ]
}
```

---

## Re-import flow (for ambiguous items)

When there are ambiguous rows, follow these steps:

**Step A — export includes all candidates**

The `full` command (or `export`) already writes all candidate rows with `note=待研发确认`.
Tell the user: "请将 CSV 发给研发，研发在 CSV 里删掉每个中文词条多余的候选行，只保留正确的一行，然后发回给你。"

**Step B — developer sends back the edited CSV**

Run the `import` command to process it:

```bash
happyhappyhappy --cli import \
  --input  <path/to/dev_confirmed.csv> \
  --output <path/to/final.csv>
```

This reads the CSV (not JSON), resolves any ambiguous rows where the developer left only one candidate, and writes a clean final CSV ready for the copywriter.

**Do not use `export` for this step** — `export` only accepts JSON input from `query`.

---

## Error handling

| error message | cause | fix |
|---------------|-------|-----|
| `Error: no saved credentials` | credentials file missing | ask user to open app and log in with "记住密码" ticked |
| `Login error: 登录失败` | wrong username/password | ask user to re-login via GUI |
| `Auth error: Session 已过期` | session expired | re-login |
| `Query error: ...` | Archery SQL error | check SQL or ask user to contact developer |
| network timeout | not on intranet | confirm user is connected to VPN / office network |

---

## Example full session

```
User: [sends screenshot of PMS checkout page]

You:
1. Extract Chinese strings from screenshot → texts.json
2. Run: happyhappyhappy --cli full --texts-file texts.json --use-saved-creds --output result.csv
3. Read result.csv
4. Report:
   - 金额 → found: trip_appid=100074326, key=pos.amount, en=Amount
   - 房间号 → ambiguous: 2 candidates, please send CSV to developer
   - 获取微信实例异常 → not_found: no entry in database
```
