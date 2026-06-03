#!/usr/bin/env python3
"""Mac-side writer: take a captured PROD probe result and apply it to the
'2 Table Status' sheet.

Flow: read RESULTS_JSON (emitted by check_table_status_prod_v1.py) from a
captured Teams card (data/table_status.jsonl) or the mail-watcher stdout
(data/tech_channel.jsonl) -> compute a conflict report vs the current sheet
-> (with --apply) back up the tab once, overwrite the 5 GP-derived stage
columns K/M/O/Q/S for those rows, write 'auto' into each changed stage's
Responsible column, save state + report.

SAFE BY DEFAULT: without --apply it only prints the report (no writes).
Never invents data — it only writes values present in the captured result.

Usage:
  python3 apply_results.py                 # dry: parse latest result + report
  python3 apply_results.py --apply --backup # write to the sheet (backup first)
"""
import argparse
import html
import json
import os
import time

import gspread

CREDENTIALS = "/Users/alexandrgruzdev/Downloads/sheets-tool-498316-6a47b98b256f.json"
SPREADSHEET_ID = "1qoswNdf61-EdNFPF0wgQc2f7cgAeSvkc-CBYQ2rKZis"
SHEET = "2 Table Status"

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHER_DATA = os.path.abspath(os.path.join(HERE, "..", "teams-channel-watcher", "data"))
STATE_DIR = os.path.join(HERE, "state")
MARK_BEGIN, MARK_END = "===RESULTS_JSON_BEGIN===", "===RESULTS_JSON_END==="

# stage code -> (status col letter, 0-based status idx, responsible col letter)
STAGE = {"K": ("K", 10, "L"), "M": ("M", 12, "N"), "O": ("O", 14, "P"),
         "Q": ("Q", 16, "R"), "S": ("S", 18, "T")}
# "no concrete result" — never written to the sheet (left as-is), only reported.
# (not scheduled) = table absent from recon_schedule; Ready = SKIPPED-only verdict.
SKIP_VALUES = {"(not scheduled)", "Ready"}


def find_latest_result(explicit=None):
    """Scan capture JSONL files for the newest line carrying a RESULTS_JSON."""
    files = [explicit] if explicit else [
        os.path.join(WATCHER_DATA, "table_status.jsonl"),
        os.path.join(WATCHER_DATA, "tech_channel.jsonl"),
    ]
    best = None  # (time_str, payload_dict)
    for fp in files:
        if not fp or not os.path.isfile(fp):
            continue
        for line in open(fp, encoding="utf-8", errors="replace"):
            if MARK_BEGIN not in line:
                continue
            try:
                msg = json.loads(line)
                text = msg.get("text", "") or ""
                seg = text.split(MARK_BEGIN, 1)[1].split(MARK_END, 1)[0].strip()
                seg = html.unescape(seg)   # capture HTML-escapes > as &gt; etc.
                payload = json.loads(seg)
                t = msg.get("time", "") or ""
                if best is None or t >= best[0]:
                    best = (t, payload)
            except Exception:
                continue
    return best


def doneness(v):
    v = (v or "").strip().lower()
    return "done" if v == "done" else "no" if v in ("", "not started") else "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write to the sheet")
    ap.add_argument("--backup", action="store_true", help="duplicate the tab before writing")
    ap.add_argument("--from", dest="src", help="explicit jsonl path to read the result from")
    args = ap.parse_args()

    found = find_latest_result(args.src)
    if not found:
        print("No RESULTS_JSON captured yet (is the result card in table_status.jsonl / tech_channel.jsonl?)")
        return
    cap_time, payload = found
    rows = payload.get("rows", [])
    print(f"Loaded result: utc={payload.get('utc')} captured@{cap_time[:19]} rows={len(rows)} "
          f"skippedCanceled={payload.get('skipped_canceled')}")

    gc = gspread.service_account(filename=CREDENTIALS)
    ss = gc.open_by_key(SPREADSHEET_ID)
    ws = ss.worksheet(SHEET)
    vals = ws.get_all_values()

    def cur_cell(r, idx):
        row = vals[r - 1] if r - 1 < len(vals) else []
        return row[idx].strip() if idx < len(row) else ""

    # conflict report vs current sheet
    from collections import Counter
    buckets = {x: Counter() for x in STAGE}
    conflicts = {x: [] for x in STAGE}
    gaps = Counter()
    for res in rows:
        r, prop = res["r"], res["prop"]
        if res.get("gap"):
            gaps[res["gap"]] += 1
        for x, (sl, idx, rl) in STAGE.items():
            p = prop.get(x, "")
            if p == "N/A":
                buckets[x]["N/A"] += 1
                continue
            if p in SKIP_VALUES:
                buckets[x]["no-data(left)"] += 1
                continue
            cv, pv = doneness(cur_cell(r, idx)), doneness(p)
            if cv == pv:
                buckets[x]["AGREE"] += 1
            elif cv == "done" and pv == "no":
                buckets[x]["sheet=Done,GP=no"] += 1
                if len(conflicts[x]) < 12:
                    conflicts[x].append(f"{res['e']}.{res['f']} (sheet={cur_cell(r, idx)!r})")
            elif cv == "no" and pv == "done":
                buckets[x]["GP=Done,sheet=no"] += 1
            else:
                buckets[x]["other"] += 1

    print("\n=== REPORT (proposed vs current sheet) ===")
    for x, (sl, idx, rl) in STAGE.items():
        print(f"  {sl} {payload['stage_cols'].get(x, x):16s}: "
              + "  ".join(f"{k}={v}" for k, v in buckets[x].most_common()))
    if gaps:
        print("  gaps:", dict(gaps))
    for x in STAGE:
        if conflicts[x]:
            print(f"  ! {x} sheet=Done but GP=no:")
            for c in conflicts[x]:
                print("      ", c)

    if not args.apply:
        print("\n(dry run — pass --apply to write; --backup to snapshot the tab first)")
        return

    # ---- backup ----
    if args.backup:
        stamp = time.strftime("%Y%m%d-%H%M", time.gmtime())
        name = f"BAK {SHEET} {stamp}"
        try:
            ss.duplicate_sheet(ws.id, new_sheet_name=name)
            print(f"[backup] duplicated tab -> {name!r}")
        except Exception as e:
            print(f"[backup] FAILED ({e}) — aborting write to stay safe"); return

    # ---- build batch update (status = proposed, responsible = 'auto') ----
    updates = []
    changed = 0
    for res in rows:
        r, prop = res["r"], res["prop"]
        for x, (sl, idx, rl) in STAGE.items():
            p = prop.get(x, "")
            if not p or p in SKIP_VALUES:   # no concrete result -> leave the cell
                continue
            updates.append({"range": f"{sl}{r}", "values": [[p]]})
            if p != "N/A":
                updates.append({"range": f"{rl}{r}", "values": [["auto"]]})
            changed += 1
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    print(f"[write] applied {len(updates)} cells across {len(rows)} rows ({changed} stage values)")

    # ---- state + report ----
    os.makedirs(STATE_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, "table_status_state.json")
    state = {}
    if os.path.isfile(state_path):
        try:
            state = json.load(open(state_path))
        except Exception:
            state = {}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for res in rows:
        key = f"{res['e']}.{res['f']}"
        state[key] = {"row": res["r"], "prop": res["prop"], "checked_at": now,
                      "written": res["prop"]}
    json.dump(state, open(state_path, "w"), indent=0)
    print(f"[state] updated {len(rows)} keys -> {state_path}")


if __name__ == "__main__":
    main()
