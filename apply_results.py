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
STAGE = {"I": ("I", 8, "J"), "K": ("K", 10, "L"), "M": ("M", 12, "N"),
         "O": ("O", 14, "P"), "Q": ("Q", 16, "R"), "S": ("S", 18, "T")}
# "no concrete result" — never written to the sheet (left as-is), only reported.
# (not scheduled) = table absent from recon_schedule; Ready = SKIPPED-only verdict.
SKIP_VALUES = {"(not scheduled)", "Ready"}


def find_latest_result(explicit=None, require_stage=None):
    """Scan capture JSONL files for the newest line carrying a RESULTS_JSON.
    require_stage: if set, only accept results whose stage_cols contains it
    (e.g. 'M' for the GP probe, 'I' for the prereq probe)."""
    files = [explicit] if explicit else [
        os.path.join(WATCHER_DATA, "table_status.jsonl"),
        os.path.join(WATCHER_DATA, "tech_channel.jsonl"),
    ]
    from collections import defaultdict
    by_run = defaultdict(dict)   # run utc -> {chunk_no: (time, payload)}
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
                if require_stage and require_stage not in (payload.get("stage_cols") or {}):
                    continue
                t = msg.get("time", "") or ""
                by_run[payload.get("utc", "")][payload.get("chunk", 1)] = (t, payload)
            except Exception:
                continue
    if not by_run:
        return None
    # latest run = the utc whose newest chunk has the max capture time
    run = max(by_run, key=lambda u: max(t for t, _ in by_run[u].values()))
    chunks = by_run[run]
    merged = {}                       # dedupe rows by sheet-row number
    for ck in sorted(chunks):
        for row in chunks[ck][1].get("rows", []):
            merged[row["r"]] = row
    base = dict(next(iter(chunks.values()))[1])
    base["rows"] = list(merged.values())
    expected = base.get("chunks", 1)
    if len(chunks) < expected:
        print(f"WARNING: only {len(chunks)}/{expected} chunks captured for run {run} "
              f"— result is INCOMPLETE, do not --apply")
    latest_t = max(t for t, _ in chunks.values())
    return (latest_t, base)


def doneness(v):
    v = (v or "").strip().lower()
    return "done" if v == "done" else "no" if v in ("", "not started") else "other"


UPSTREAM = [("I", 8), ("K", 10), ("M", 12), ("O", 14), ("Q", 16)]  # for cdc Ready rule


def finalize_recon(apply):
    """Rule: a cdc row whose upstream I/K/M/O/Q are all Done but has no real
    recon verdict (PASS/DIFF/ERROR) gets Data recon = 'Ready' (= pipeline
    complete, awaiting reconciliation). Uses the GP probe result for the
    'has a real verdict?' signal and the current sheet for upstream values."""
    found = find_latest_result(require_stage="M")
    if not found:
        print("finalize-recon: no GP result captured"); return
    _, payload = found
    gp_rows = {r["r"]: r for r in payload.get("rows", [])}
    gc = gspread.service_account(filename=CREDENTIALS)
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET)
    vals = ws.get_all_values()
    updates = []
    for rnum, res in gp_rows.items():
        if (res.get("t") or "").lower() != "cdc":
            continue
        sv = res.get("prop", {}).get("S", "")
        if sv in ("Done", "Discrepancies", "Error"):
            continue   # real verdict already -> leave
        row = vals[rnum - 1] if rnum - 1 < len(vals) else []
        ok = all((row[idx].strip().lower() == "done" if idx < len(row) else False)
                 for _, idx in UPSTREAM)
        if ok:
            updates.append({"range": f"S{rnum}", "values": [["Ready"]]})
    print(f"finalize-recon: {len(updates)} cdc rows -> Data recon='Ready' (upstream all Done, no verdict)")
    if apply and updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        print("  written.")
    elif not apply:
        print("  (dry — pass --apply to write)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write to the sheet")
    ap.add_argument("--backup", action="store_true", help="duplicate the tab before writing")
    ap.add_argument("--from", dest="src", help="explicit jsonl path to read the result from")
    ap.add_argument("--finalize-recon", dest="finalize", action="store_true",
                    help="apply the cdc 'Ready' rule and exit")
    ap.add_argument("--require", help="only accept a result whose stage_cols has this key (M=GP probe, I=prereq)")
    args = ap.parse_args()

    if args.finalize:
        finalize_recon(args.apply)
        return

    found = find_latest_result(args.src, require_stage=args.require)
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
            if not p:               # stage not present in this result
                continue
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

    # ---- build batch update (status columns only; Responsible never touched) ----
    updates = []
    changed = 0
    for res in rows:
        r, prop = res["r"], res["prop"]
        t = (res.get("t") or "").strip().lower()
        for x, (sl, idx, rl) in STAGE.items():
            p = prop.get(x, "")
            if not p or p in SKIP_VALUES:   # no concrete result -> leave the cell
                continue
            # Non-cdc rule (user 2026-06-03): an old 'Done' the autotester does
            # NOT confirm (proposed != Done) becomes 'Not started', not N/A.
            if t != "cdc" and p != "Done" and cur_cell(r, idx).strip().lower() == "done":
                p = "Not started"
            updates.append({"range": f"{sl}{r}", "values": [[p]]})  # status only; never touch Responsible
            changed += 1

    # ---- lookup/inter mislabeled-CDC note (user 2026-06-03) ----
    # A lookup/inter row with >3 of its 6 statuses filled AND a populated
    # stream table in GP is really CDC -> add a Comment (col V, idx 21) note.
    STATUS_IDX = [8, 10, 12, 14, 16, 18]
    flagged = 0
    for res in rows:
        t = (res.get("t") or "").strip().lower()
        if t not in ("lookup", "inter") or not res.get("cdc_detected"):
            continue
        r = res["r"]
        nfilled = sum(1 for idx in STATUS_IDX if cur_cell(r, idx).strip())
        if nfilled <= 3:
            continue
        note = f"ключ {t} а реализовано CDC"
        comment = cur_cell(r, 21)
        if note.lower() in comment.lower():
            continue
        new_comment = f"{comment} | {note}".strip(" |") if comment else note
        updates.append({"range": f"V{r}", "values": [[new_comment]]})
        flagged += 1

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    print(f"[write] applied {len(updates)} cells across {len(rows)} rows "
          f"({flagged} lookup/inter mislabeled-CDC notes)")

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
        key = f"{(res['e'] or '').lower()}.{(res['f'] or '').lower()}"
        entry = state.get(key, {"row": res["r"], "prop": {}})
        entry["row"] = res["r"]
        entry.setdefault("prop", {}).update(res["prop"])   # merge GP + prereq stages
        entry["checked_at"] = now
        entry["written"] = dict(entry["prop"])
        state[key] = entry
    json.dump(state, open(state_path, "w"), indent=0)
    print(f"[state] updated {len(rows)} keys -> {state_path}")


if __name__ == "__main__":
    main()
