#!/usr/bin/env python3
"""Data-reconciliation check — read recon_meta verdicts for every row.

Lightweight GP-only probe: for each worklist row, look up its gp_table in
recon_meta (latest run) and emit a one-char verdict code:
  D = has a DIFF (discrepancy)   P = has a PASS (t1/t2/t3 passed, any delta)
  E = only ERROR                 _ = not in recon / only SKIPPED
The Mac writer turns these into the S column via the decision tree
(DIFF->Discrepancies, PASS->Done, else all-upstream-Done->Ready, else Not started).
Applies to ALL table types (non-cdc included). READ-ONLY.
Injected: WORKLIST_B64, WEBHOOK, ALIAS_MAP_JSON.
"""
import base64
import json
import os
import sys
import time
import traceback
from collections import defaultdict, Counter

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

GP = dict(host="grnplumvipuat.ksacb.com.sa", port=5442, dbname="simah_test",
          user="gpadmin", password="gpadmin", connect_timeout=15)
WORKLIST_B64 = "__WORKLIST_B64__"
WEBHOOK = "__WEBHOOK__"
ALIAS_MAP_JSON = "__ALIAS_MAP_JSON__"
try:
    ALIAS_MAP = json.loads(ALIAS_MAP_JSON)
except Exception:
    ALIAS_MAP = {}
MARK_BEGIN, MARK_END = "===RESULTS_JSON_BEGIN===", "===RESULTS_JSON_END==="


def post_card(title, body, url):
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4",
                    "body": [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True},
                             {"type": "TextBlock", "text": body, "wrap": True, "fontType": "Monospace"}]}}]}
    if sys.platform == 'win32':
        import subprocess
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'wb') as f:
            f.write(json.dumps(card, ensure_ascii=False).encode('utf-8'))
        ps = ("$proxy=[System.Net.WebRequest]::GetSystemWebProxy();"
              "$proxy.Credentials=[System.Net.CredentialCache]::DefaultNetworkCredentials;"
              "[System.Net.WebRequest]::DefaultWebProxy=$proxy;"
              f"$b=[System.IO.File]::ReadAllBytes('{tmp}');"
              f"try{{Invoke-RestMethod -Uri '{url}' -Method Post -ContentType 'application/json; charset=utf-8' -Body $b|Out-Null;Write-Host 'OK'}}catch{{Write-Host 'FAIL';exit 1}}")
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                           capture_output=True, text=True, timeout=60)
        try: os.remove(tmp)
        except Exception: pass
        return 200 if r.returncode == 0 else 599
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(card).encode(),
                                 headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_recon v1 @ {utc}")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    try:
        import psycopg2
        conn = psycopg2.connect(**GP)
    except Exception as e:
        print("GP connect FAILED:", e); return 1
    conn.autocommit = True
    cur = conn.cursor()
    # Per gp_table, take the LATEST RUN (by max started_at) and union ALL its
    # test verdicts (t1/t2/t3). Grouping by single-row started_at lost a T1 DIFF
    # when T2/T3 SKIPPED started later. enabled=1 = active schedule rows.
    cur.execute("""SELECT s.gp_table, r.run_id, r.verdict, r.started_at
                   FROM recon_meta.recon_results r
                   JOIN recon_meta.recon_schedule s ON s.id=r.schedule_id
                   WHERE s.enabled = 1""")
    runv = defaultdict(set)          # (gp_table, run_id) -> verdicts
    latest = {}                      # gp_table -> (max_started, run_id)
    for gp_table, run_id, verdict, started in cur.fetchall():
        k = (gp_table or "").lower()
        runv[(k, run_id)].add(verdict)
        if k not in latest or (started and started > latest[k][0]):
            latest[k] = (started, run_id)
    cur.close(); conn.close()

    code = {}; diff_tables = []
    for k, (_, run_id) in latest.items():
        vs = runv[(k, run_id)]
        if "DIFF" in vs:
            code[k] = "D"; diff_tables.append(k)
        elif "PASS" in vs:
            code[k] = "P"
        elif "ERROR" in vs:
            code[k] = "E"
        else:
            code[k] = "_"
    print(f"active scheduled tables: {len(latest)}; DIFF (mismatch): {len(diff_tables)}")
    for d in sorted(diff_tables):
        print("   DIFF:", d)

    lines = []; dist = Counter()
    for r in rows:
        e = (r["e"] or "").strip().lower(); f = (r["f"] or "").strip().lower()
        psa = ALIAS_MAP.get(e, e)
        c = code.get(f"{psa}.{f}", "_")
        dist[c] += 1
        if c != "_":                         # only ship rows that ARE in recon
            lines.append(f'{r["r"]}:{c}')
    payload = json.dumps({"utc": utc, "fmt": "rv", "kind": "recon", "chunk": 1, "chunks": 1,
                          "rows_str": "\n".join(lines)}, ensure_ascii=False, separators=(",", ":"))
    print(f"recon coverage: {len(latest)} gp_tables; per-row codes {dict(dist)}; shipped {len(lines)}")
    if WEBHOOK.startswith("http"):
        body = f"Data reconciliation codes {dict(dist)}\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                print("[selfpost]", post_card(f"📊 recon check · {utc}", body, WEBHOOK))
            except Exception as ex:
                print("[selfpost] FAIL:", ex)
    print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== recon check done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
