#!/usr/bin/env python3
"""Mac-side generator: read the "2 Table Status" sheet, embed a work-list
into the GP dry-run probe, and email it to the VDI.

Runs on the Mac (has Google Sheets access). Produces a filled, self-
contained probe with the row list baked in as base64, then sends it via
gmail_send.py. The probe itself is read-only and never writes the sheet.
"""
import base64
import json
import os
import subprocess
import sys

import gspread

CREDENTIALS = "/Users/alexandrgruzdev/Downloads/sheets-tool-498316-6a47b98b256f.json"
SPREADSHEET_ID = "1qoswNdf61-EdNFPF0wgQc2f7cgAeSvkc-CBYQ2rKZis"
SHEET = "2 Table Status"

HERE = os.path.dirname(os.path.abspath(__file__))
# probe template + email subject can be overridden by argv (default v1)
PROBE = sys.argv[1] if len(sys.argv) > 1 else "check_table_status_dryrun_v2.py"
SUBJECT = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(os.path.basename(PROBE))[0]


def _resolve(p):
    cands = ([p] if os.path.isabs(p) else
             [os.path.join(os.getcwd(), p), os.path.join(HERE, p),
              os.path.join(HERE, os.path.basename(p))])
    for c in cands:
        if os.path.isfile(c):
            return c
    sys.exit(f"probe template not found: {p}")


TEMPLATE = _resolve(PROBE)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
GMAIL_SEND = os.path.join(REPO_ROOT, "gmail_send.py")  # repo-local (gitignored, has app pw)

# 0-based column indices in the sheet
C_DB, C_E, C_F, C_TYPE = 2, 4, 5, 7   # Source Database Name, GP schema, table, type
STAGE_COLS = [8, 10, 12, 14, 16, 18]   # I,K,M,O,Q,S

# optional argv[3] scope filter, e.g. "db=B7031210" or "type=cdc"
SCOPE = sys.argv[3] if len(sys.argv) > 3 else ""


def _in_scope(cell):
    if not SCOPE or "=" not in SCOPE:
        return True
    key, val = SCOPE.split("=", 1)
    key, val = key.strip().lower(), val.strip().lower()
    if key == "db":
        return cell(C_DB).lower() == val
    if key == "type":
        return cell(C_TYPE).lower() == val
    if key == "schema":
        return cell(C_E).lower() == val
    return True


def build_worklist():
    gc = gspread.service_account(filename=CREDENTIALS)
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET)
    vals = ws.get_all_values()
    work = []
    for i, row in enumerate(vals[1:], start=2):   # data starts at sheet row 2
        def cell(idx):
            return row[idx].strip() if idx < len(row) else ""
        if not _in_scope(cell):
            continue
        e, f, t = cell(C_E), cell(C_F), cell(C_TYPE)
        if not (e or f):
            continue
        # d=source schema/library, db=source database — needed for source-side checks
        work.append({"r": i, "e": e, "f": f, "t": t,
                     "d": cell(3), "db": cell(C_DB),
                     "cur": [cell(c) for c in STAGE_COLS]})
    return work


def load_secret(key):
    path = os.path.join(REPO_ROOT, "secrets.local.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get(key)


def main():
    with open(TEMPLATE, encoding="utf-8") as fh:
        src = fh.read()
    filled = src

    # 1) embed the sheet work-list (only if the probe expects it)
    if '"__WORKLIST_B64__"' in filled:
        work = build_worklist()
        from collections import Counter
        print(f"worklist rows: {len(work)}  by type:",
              dict(Counter((w["t"] or "?").lower() for w in work)))
        payload = base64.b64encode(json.dumps(work, ensure_ascii=False).encode("utf-8")).decode("ascii")
        print(f"payload: {len(payload)} b64 chars (~{len(payload)//1024} KB)")
        filled = filled.replace('"__WORKLIST_B64__"', json.dumps(payload))

    # 2) inject the table-status webhook (only if the probe self-posts)
    if '"__WEBHOOK__"' in filled:
        wh = load_secret("table_status_webhook")
        if not wh:
            sys.exit("probe needs a webhook but secrets.local.json/table_status_webhook is missing")
        filled = filled.replace('"__WEBHOOK__"', json.dumps(wh))
        print("injected table_status webhook")

    # 3) inject schema aliases (only if the probe expects them)
    if '"__ALIAS_MAP_JSON__"' in filled:
        ap = os.path.join(REPO_ROOT, "schema_aliases.json")
        aliases = json.load(open(ap)) if os.path.isfile(ap) else {}
        filled = filled.replace('"__ALIAS_MAP_JSON__"', json.dumps(json.dumps(aliases)))
        print(f"injected {len(aliases)} schema aliases")

    out = f"/tmp/{SUBJECT}_filled.py"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(filled)
    print("wrote", out)

    # send
    cmd = [sys.executable, GMAIL_SEND, out, "--to", "agruzdev@simah.com",
           "--subject", SUBJECT,
           "--body", "GP-side DRY-RUN status checker (read-only). Prints proposed statuses, AGREE/CONFLICT buckets + gap report. No sheet writes."]
    print("sending:", " ".join(cmd))
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
