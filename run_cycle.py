#!/usr/bin/env python3
"""End-to-end SCHEDULED cycle for the '2 Table Status' checker (stateful delta).

One run: build a minimal work-list (skip stages already confirmed Done both in
the sheet AND in the Mac-side state store; check only the stages each Table type
needs; always re-read recon) -> email the filtered probes to the VDI -> poll the
captured Teams JSONL for the results -> apply to the sheet -> refresh the report.

Designed to run unattended from launchd. SAFE: --dry (default) only prints the
work-list; --run sends + waits + applies. Requires the capture browser open with
the Teams 'table status' tab active (the extension writes data/table_status.jsonl).

Stateful rule (user 2026-06-04): skip (row,stage) when sheet==terminal-OK AND
state==same; otherwise re-check. cdc -> all stages; lookup/inter -> I,M,O only;
recon (S) for cdc is always re-read (data drifts).
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time

import gspread

HERE = os.path.dirname(os.path.abspath(__file__))
CRED = "/Users/alexandrgruzdev/Downloads/sheets-tool-498316-6a47b98b256f.json"
SID = "1qoswNdf61-EdNFPF0wgQc2f7cgAeSvkc-CBYQ2rKZis"
SHEET = "2 Table Status"
STATE_PATH = os.path.join(HERE, "state", "table_status_state.json")
WATCHER_DATA = os.path.abspath(os.path.join(HERE, "..", "teams-channel-watcher", "data"))
GMAIL = os.path.join(HERE, "gmail_send.py")
DIAGS = os.path.join(HERE, "diags")
MARK_BEGIN = "===RESULTS_JSON_BEGIN==="

# 0-based sheet columns
C_DB, C_SRCSCHEMA, C_E, C_F, C_TYPE = 2, 3, 4, 5, 7
STAGE_COL = {"I": 8, "K": 10, "M": 12, "O": 14, "Q": 16, "S": 18}
APPLICABLE = {"cdc": ["I", "K", "M", "O", "Q", "S"],
              "lookup": ["I", "M", "O"], "inter": ["I", "M", "O"]}
GP_STAGES = {"K", "M", "O", "Q"}
TERMINAL_OK = {"done", "n/a", "read granted, but no cdc", "canceled", "(not scheduled)"}


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def norm_type(t):
    t = (t or "").strip().lower()
    return t if t in APPLICABLE else "inter"


def build_worklist():
    gc = gspread.service_account(filename=CRED)
    vals = gc.open_by_key(SID).worksheet(SHEET).get_all_values()
    state = {}
    if os.path.isfile(STATE_PATH):
        try:
            state = json.load(open(STATE_PATH))
        except Exception:
            state = {}

    gp_rows, prereq_rows, all_rows = [], [], []
    skipped_stage = 0; checked_stage = 0
    for i, row in enumerate(vals[1:], start=2):
        g = lambda x: (row[x].strip() if len(row) > x else "")
        e, f, t = g(C_E), g(C_F), norm_type(g(C_TYPE))
        if not (e or f):
            continue
        item = {"r": i, "e": e, "f": f, "t": g(C_TYPE), "d": g(C_SRCSCHEMA),
                "db": g(C_DB), "cur": [g(STAGE_COL[s]) for s in ("I", "K", "M", "O", "Q", "S")]}
        all_rows.append(item)
        key = f"{e}.{f}".lower()
        sprop = (state.get(key) or {}).get("prop", {})
        need = {}
        for st in APPLICABLE[t]:
            sheet_v = g(STAGE_COL[st]).strip().lower()
            state_v = str(sprop.get(st, "")).strip().lower()
            confirmed = (sheet_v in TERMINAL_OK) and (state_v == sheet_v) and sheet_v != ""
            if st == "S" and t == "cdc":
                confirmed = False               # recon always re-read
            need[st] = not confirmed
            skipped_stage += int(confirmed); checked_stage += int(not confirmed)
        if any(need.get(s) for s in GP_STAGES if s in APPLICABLE[t]):
            gp_rows.append(item)
        if need.get("I"):
            prereq_rows.append(item)
    return all_rows, gp_rows, prereq_rows, skipped_stage, checked_stage


def _load_secret(key):
    p = os.path.join(HERE, "secrets.local.json")
    return (json.load(open(p)).get(key) if os.path.isfile(p) else None)


def fill_and_send(template_name, subject, worklist):
    """Inject a filtered work-list (+ webhook/aliases/creds) into a probe and email it."""
    src = open(os.path.join(DIAGS, template_name), encoding="utf-8").read()
    if '"__WORKLIST_B64__"' in src:
        b64 = base64.b64encode(json.dumps(worklist, ensure_ascii=False).encode()).decode()
        src = src.replace('"__WORKLIST_B64__"', json.dumps(b64))
    if '"__WEBHOOK__"' in src:
        src = src.replace('"__WEBHOOK__"', json.dumps(_load_secret("table_status_webhook") or ""))
    if '"__ALIAS_MAP_JSON__"' in src:
        ap = os.path.join(HERE, "schema_aliases.json")
        src = src.replace('"__ALIAS_MAP_JSON__"', json.dumps(json.dumps(json.load(open(ap)) if os.path.isfile(ap) else {})))
    if '"__MSSQL_CREDS_JSON__"' in src:
        src = src.replace('"__MSSQL_CREDS_JSON__"', json.dumps(json.dumps(_load_secret("mssql_creds") or [])))
    if '"__DB_ALIAS_JSON__"' in src:
        dp = os.path.join(HERE, "db_aliases.json")
        dmap = {k: v for k, v in (json.load(open(dp)).items() if os.path.isfile(dp) else []) if not k.startswith("_")}
        src = src.replace('"__DB_ALIAS_JSON__"', json.dumps(json.dumps(dmap)))
    out = f"/tmp/{subject}_filled.py"
    open(out, "w", encoding="utf-8").write(src)
    subprocess.run([sys.executable, GMAIL, out, "--to", "agruzdev@simah.com",
                    "--subject", subject, "--body", "scheduled table-status cycle probe"], check=True)
    log(f"sent {subject} (worklist={len(worklist)} rows)")


def newest_capture_ts():
    newest = ""
    for fn in ("table_status.jsonl", "tech_channel.jsonl"):
        fp = os.path.join(WATCHER_DATA, fn)
        if os.path.isfile(fp):
            for line in open(fp, encoding="utf-8", errors="replace"):
                try:
                    t = json.loads(line).get("time", "")
                    if t > newest:
                        newest = t
                except Exception:
                    pass
    return newest


def wait_for_kind(kind, since_ts, timeout=900, poll=20):
    """Poll the JSONL until a result of `kind` newer than since_ts has ALL its
    chunks (per the payload's own 'chunks' total)."""
    import html
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = {}
        for fn in ("table_status.jsonl", "tech_channel.jsonl"):
            fp = os.path.join(WATCHER_DATA, fn)
            if not os.path.isfile(fp):
                continue
            for line in open(fp, encoding="utf-8", errors="replace"):
                if MARK_BEGIN not in line:
                    continue
                try:
                    m = json.loads(line)
                    if (m.get("time", "") or "") <= since_ts:
                        continue
                    seg = html.unescape(m["text"].split(MARK_BEGIN, 1)[1].split("===RESULTS_JSON_END===", 1)[0].strip())
                    p = json.loads(seg)
                    if p.get("kind") == kind or (kind == "gp" and p.get("fmt") == "c1"):
                        info = runs.setdefault(p.get("utc", ""), {"exp": 1, "got": set()})
                        info["exp"] = p.get("chunks", 1)
                        info["got"].add(p.get("chunk", 1))
                except Exception:
                    pass
        for utc, info in runs.items():
            if len(info["got"]) >= info["exp"]:
                log(f"got {kind} result utc={utc} ({len(info['got'])}/{info['exp']} chunks)")
                return True
        time.sleep(poll)
    log(f"TIMEOUT waiting for {kind}")
    return False


def apply(*flags):
    subprocess.run([sys.executable, os.path.join(HERE, "apply_results.py"), *flags], check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="actually send + wait + apply (else dry work-list only)")
    ap.add_argument("--full", action="store_true", help="ignore state -> re-check every row")
    ap.add_argument("--timeout", type=int, default=1200, help="per-probe wait seconds")
    args = ap.parse_args()

    all_rows, gp_rows, prereq_rows, skipped, checked = build_worklist()
    log(f"work-list: {len(all_rows)} rows total | GP re-check {len(gp_rows)} | prereq re-check {len(prereq_rows)} "
        f"| stages skipped(confirmed)={skipped} checked={checked}")
    if args.full:
        gp_rows, prereq_rows = all_rows, all_rows
        log(f"--full: re-checking all {len(all_rows)} rows")
    if not args.run:
        log("DRY (no send). Pass --run to execute the cycle.")
        return

    since = newest_capture_ts()
    # 1) GP stages K/M/O/Q  2) prereq I  3) recon S (full read)
    if gp_rows:
        fill_and_send("check_table_status_prod_v1.py", "cycle_gp", gp_rows)
    if prereq_rows:
        fill_and_send("check_prereq_mssql_v5.py", "cycle_prereq", prereq_rows)
    fill_and_send("check_recon_v1.py", "cycle_recon", all_rows)

    if gp_rows and wait_for_kind("gp", since, timeout=args.timeout):
        apply("--require", "M", "--stages", "KMOQ", "--apply")
    if prereq_rows and wait_for_kind("prereq_mssql", since, timeout=args.timeout):
        apply("--require", "I", "--apply")
    if wait_for_kind("recon", since, timeout=args.timeout):
        apply("--recon", "--apply")

    subprocess.run([sys.executable, os.path.join(HERE, "make_session_report.py")], check=False)
    log("cycle done.")


if __name__ == "__main__":
    main()
