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
import re
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

# compact-encoding (fmt=c1) inverse maps — see check_table_status_prod_v1.py
RVMAP = {"D": "Done", "N": "Not started", "A": "N/A", "R": "Ready",
         "S": "(not scheduled)", "X": "Discrepancies", "E": "Error", "_": ""}
RTMAP = {"c": "cdc", "l": "lookup", "i": "inter", "o": ""}


def decode_line(ln, order):
    """'23:DDDDR:c:0:1' -> {r, prop{K..S}, t, cdc_detected, gap}."""
    parts = ln.split(":")
    r = int(parts[0]); codes = parts[1]
    prop = {order[i]: RVMAP.get(codes[i], "") for i in range(min(len(order), len(codes)))}
    return {"r": r, "t": RTMAP.get(parts[2] if len(parts) > 2 else "", ""),
            "prop": prop, "cdc_detected": len(parts) > 3 and parts[3] == "1",
            "gap": "NO_TABLE_GP" if (len(parts) > 4 and parts[4] == "1") else None}


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
                if require_stage:
                    sc = payload.get("stage_cols") or {}
                    so = payload.get("stage_order") or ""   # compact (fmt=c1) GP probe
                    fs = {"c1": "KMOQS", "ci": "I"}.get(payload.get("fmt", ""), "")
                    if require_stage not in sc and require_stage not in so and require_stage not in fs:
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
    base = dict(next(iter(chunks.values()))[1])
    if base.get("fmt") == "c1":       # compact encoding -> decode lines
        order = base.get("stage_order", "KMOQS")
        lines = {}
        for ck in sorted(chunks):
            for ln in (chunks[ck][1].get("rows_str") or "").split("\n"):
                ln = ln.strip()
                if ln:
                    try:
                        lines[int(ln.split(":", 1)[0])] = ln
                    except Exception:
                        pass
        base["rows"] = [decode_line(ln, order) for ln in lines.values()]
        base["stage_cols"] = {"K": "DBZ->RMQ", "M": "Create GP table",
                              "O": "IPC init load", "Q": "RMQ->GPSS", "S": "Data recon"}
    elif base.get("fmt") == "ci":     # compact Prerequisites (col I)
        RIMAP = {"D": "Done", "C": "Read granted, but no CDC", "N": "Not started", "_": ""}
        RGMAP = {"M": "MISSING_TABLE_SRC", "E": "DB_ERR", "_": None}
        lines = {}
        for ck in sorted(chunks):
            for ln in (chunks[ck][1].get("rows_str") or "").split("\n"):
                ln = ln.strip()
                if ln:
                    try:
                        lines[int(ln.split(":", 1)[0])] = ln
                    except Exception:
                        pass
        out = []
        for ln in lines.values():
            parts = ln.split(":")
            iv = RIMAP.get(parts[1] if len(parts) > 1 else "_", "")
            out.append({"r": int(parts[0]), "prop": ({"I": iv} if iv else {}),
                        "gap": RGMAP.get(parts[2] if len(parts) > 2 else "_")})
        base["rows"] = out
        base["stage_cols"] = {"I": "Prerequisites"}
    else:                             # verbose rows -> dedupe by sheet-row number
        merged = {}
        for ck in sorted(chunks):
            for row in chunks[ck][1].get("rows", []):
                merged[row["r"]] = row
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


def coverage_missing(vals):
    """Parse the latest discover_server_dbs card -> reachable DB names, then
    return the sheet's source DBs we have NO access to: [(db, rows, note)]."""
    reachable = set()
    for fp in (os.path.join(WATCHER_DATA, "tech_channel.jsonl"),
               os.path.join(WATCHER_DATA, "table_status.jsonl")):
        if not os.path.isfile(fp):
            continue
        for line in open(fp, encoding="utf-8", errors="replace"):
            if "discover_server_dbs" not in line and "server DB lists" not in line:
                continue
            try:
                t = json.loads(line).get("text", "") or ""
                for mm in re.finditer(r"\][^\n]*\d+ dbs:\s*\n([^\[]+)", t):
                    for d in mm.group(1).replace("\n", " ").split(","):
                        d = d.strip()
                        if d and "===" not in d and "watcher" not in d:
                            reachable.add(d.lower())
            except Exception:
                pass
    from collections import Counter
    cnt = Counter(); sysn = {}
    for row in vals[1:]:
        c = row[2].strip() if len(row) > 2 else ""
        if c:
            cnt[c] += 1; sysn[c] = row[0].strip() if row else ""
    miss = []
    for db, n in cnt.items():
        s = sysn.get(db, "")
        if "DB2" in s or db == "B7031210":
            continue
        if "SAP IQ" in s:
            miss.append((db, n, "SAP IQ (not MSSQL)")); continue
        if db.lower() not in reachable:
            miss.append((db, n, ""))
    return sorted(miss, key=lambda x: -x[1])


def recon_finalize(apply):
    """Re-derive Data reconciliation (col S) for EVERY row from the latest
    recon check (fmt=rv) + the sheet's upstream stages, by the decision tree:
      DIFF -> Discrepancies ; PASS -> Done ; else all-upstream(I/K/M/O/Q
      non-N/A)-Done -> Ready ; else Not started.  Applies to all table types.
      Canceled rows are left untouched."""
    # find the latest recon (kind=recon) payload(s), merge chunks by utc
    from collections import defaultdict, Counter
    by_run = defaultdict(dict)
    for fp in (os.path.join(WATCHER_DATA, "table_status.jsonl"),
               os.path.join(WATCHER_DATA, "tech_channel.jsonl")):
        if not os.path.isfile(fp):
            continue
        for line in open(fp, encoding="utf-8", errors="replace"):
            if MARK_BEGIN not in line:
                continue
            try:
                msg = json.loads(line)
                seg = html.unescape(msg["text"].split(MARK_BEGIN, 1)[1].split(MARK_END, 1)[0].strip())
                p = json.loads(seg)
                if p.get("kind") != "recon":
                    continue
                by_run[p.get("utc", "")][p.get("chunk", 1)] = (msg.get("time", ""), p)
            except Exception:
                continue
    if not by_run:
        print("recon-finalize: no recon result captured"); return
    run = max(by_run, key=lambda u: max(t for t, _ in by_run[u].values()))
    codes = {}
    for ck in sorted(by_run[run]):
        for ln in (by_run[run][ck][1].get("rows_str") or "").split("\n"):
            ln = ln.strip()
            if ln and ":" in ln:
                r, c = ln.split(":", 1); codes[int(r)] = c

    gc = gspread.service_account(filename=CREDENTIALS)
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET)
    vals = ws.get_all_values()
    UP = [8, 10, 12, 14, 16]   # I,K,M,O,Q
    updates = []; dist = Counter()
    for i, row in enumerate(vals[1:], start=2):
        def g(idx):
            return row[idx].strip() if idx < len(row) else ""
        stages = [g(x) for x in (8, 10, 12, 14, 16, 18)]
        if any(v.lower() == "canceled" for v in stages):
            continue
        c = codes.get(i)
        if c == "D":
            s = "Discrepancies"
        elif c == "P":
            s = "Done"
        else:
            nn = [g(idx) for idx in UP if g(idx).lower() != "n/a"]
            s = "Ready" if (nn and all(v.lower() == "done" for v in nn)) else "Not started"
        if s != g(18):
            updates.append({"range": f"S{i}", "values": [[s]]})
        dist[s] += 1
    print(f"recon-finalize: target S distribution {dict(dist)}; {len(updates)} cells to change")
    if apply and updates:
        ws.batch_update(updates, value_input_option="RAW")
        print("  written.")
    elif not apply:
        print("  (dry — pass --apply to write)")


def make_report(apply):
    """Write an 'Auto-check report' tab from the latest GP result: a summary
    + SHEET_AHEAD findings + no-access source DBs + tables missing in source."""
    found = find_latest_result(require_stage="M")
    if not found:
        print("report: no GP result"); return
    _, payload = found
    gc = gspread.service_account(filename=CREDENTIALS)
    ss = gc.open_by_key(SPREADSHEET_ID)
    # baseline = the most recent BAK snapshot (the pre-apply 'before' state),
    # so findings survive even though the live sheet is already overwritten.
    baks = sorted((w.title for w in ss.worksheets() if w.title.startswith("BAK ")), reverse=True)
    base_tab = baks[0] if baks else SHEET
    print(f"report baseline tab: {base_tab!r}")
    vals = ss.worksheet(base_tab).get_all_values()
    live = ss.worksheet(SHEET).get_all_values()

    def cell(r, idx):
        row = vals[r - 1] if r - 1 < len(vals) else []
        return row[idx].strip() if idx < len(row) else ""

    def lcell(r, idx):
        row = live[r - 1] if r - 1 < len(live) else []
        return row[idx].strip() if idx < len(row) else ""

    from collections import Counter
    cats = Counter(); findings = []
    for res in payload.get("rows", []):
        r, prop, t = res["r"], res["prop"], (res.get("t") or "")
        if res.get("gap"):
            cats["NO_TABLE_GP"] += 1
        for x, (sl, idx, rl) in STAGE.items():
            p = prop.get(x, "")
            if not p or p == "N/A" or p in SKIP_VALUES:
                continue
            cv = cell(r, idx)
            if doneness(cv) == "done" and doneness(p) == "no":
                cats["SHEET_AHEAD"] += 1
                findings.append([f"{cell(r,4)}.{cell(r,5)}", t, STAGE_COLS_NAME.get(x, x), cv, p])
            elif doneness(cv) == "no" and doneness(p) == "done":
                cats["GP_AHEAD"] += 1

    # --- tables with DB access but no source table (from prereq result) ---
    no_table_src = []
    pre = find_latest_result(require_stage="I")
    if pre:
        for res in pre[1].get("rows", []):
            if (res.get("gap") or "").startswith("MISSING_TABLE_SRC"):
                rr = res["r"]
                no_table_src.append([cell(rr, 2), f"{cell(rr,3)}.{cell(rr,5)}"])
    # --- source DBs we have no access to (from coverage) ---
    no_access = coverage_missing(vals)

    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    out = [["Auto-check report", stamp],
           ["rows checked", str(len(payload.get("rows", [])))],
           ["SHEET_AHEAD (sheet=Done but GP=no — investigate)", str(cats["SHEET_AHEAD"])],
           ["GP_AHEAD (GP=Done, sheet was behind — auto-corrected)", str(cats["GP_AHEAD"])],
           ["NO_TABLE_GP (not created in GP — backlog)", str(cats["NO_TABLE_GP"])],
           ["NO DB ACCESS (source DBs without creds)", str(len(no_access))],
           ["DB OK but TABLE MISSING in source", str(len(no_table_src))],
           [], ["== SHEET_AHEAD findings =="], ["table", "type", "stage", "sheet", "GP (autotester)"]]
    out += findings
    out += [[], ["== NO DB ACCESS (need creds for these source databases) =="],
            ["source database", "rows", "note"]]
    out += [[db, str(n), note] for db, n, note in no_access]
    out += [[], ["== DB reachable but TABLE not found in source =="],
            ["source database", "schema.table"]]
    out += no_table_src
    if not apply:
        print(f"report (dry): SHEET_AHEAD={cats['SHEET_AHEAD']} GP_AHEAD={cats['GP_AHEAD']} "
              f"NO_TABLE_GP={cats['NO_TABLE_GP']} NO_DB_ACCESS={len(no_access)} "
              f"TABLE_MISSING_SRC={len(no_table_src)} (pass --apply to write the tab)")
        return
    title = "Auto-check report"
    try:
        rep = ss.worksheet(title); rep.clear()
    except Exception:
        rep = ss.add_worksheet(title=title, rows=max(50, len(out) + 5), cols=6)
    rep.update(out, value_input_option="RAW")   # RAW so '== ..' / '=' aren't treated as formulas
    print(f"report: wrote {len(findings)} findings to tab {title!r}")


STAGE_COLS_NAME = {"I": "Prerequisites", "K": "DBZ->RMQ", "M": "Create GP table",
                   "O": "IPC init load", "Q": "RMQ->GPSS", "S": "Data recon"}


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
    ap.add_argument("--report", action="store_true", help="write the Auto-check report tab and exit")
    ap.add_argument("--recon", action="store_true", help="re-derive Data reconciliation (col S) by the tree and exit")
    args = ap.parse_args()

    if args.finalize:
        finalize_recon(args.apply)
        return
    if args.recon:
        recon_finalize(args.apply)
        return
    if args.report:
        make_report(args.apply)
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
                    conflicts[x].append(f"{cur_cell(r, 4)}.{cur_cell(r, 5)} (sheet={cur_cell(r, idx)!r})")
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
        r = res["r"]
        # e/f from the result if present (prereq probes), else from the sheet (compact GP)
        e = (res.get("e") or cur_cell(r, 4)).lower()
        f = (res.get("f") or cur_cell(r, 5)).lower()
        key = f"{e}.{f}"
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
