#!/usr/bin/env python3
"""PRODUCTION status checker v1 — GP side (READ-ONLY on GP; no sheet writes).

Computes the proposed pipeline status per sheet row from Greenplum and
returns a machine-readable result the Mac writer applies to the sheet.
Covers the 5 GP-derivable stages: K DBZ->RMQ, M Create GP table,
O IPC init load, Q RMQ->GPSS, S Data reconciliation. Prerequisites (I) is
source-side and added in a later increment.

Return paths (both, for resilience — whichever Teams tab is captured wins):
  - stdout: a RESULTS_JSON block (mail-watcher -> tech channel),
  - self-POST: an Adaptive Card carrying the same block -> 'table status'.
For a single-DB pilot the result easily fits one card / 20 KB stdout.

Injected by the Mac generator: WORKLIST_B64, ALIAS_MAP, WEBHOOK.
"""
import base64
import json
import os
import sys
import time
import traceback
from collections import Counter, defaultdict

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

GP = dict(host="grnplumvipuat.ksacb.com.sa", port=5442, dbname="simah_test",
          user="gpadmin", password="gpadmin", connect_timeout=15)

WORKLIST_B64 = "__WORKLIST_B64__"
ALIAS_MAP = {}            # {sheet_schema_lower: real_gp_schema_lower}
WEBHOOK = "__WEBHOOK__"   # self-post target; guard checks startswith('http')

# stage -> sheet column letter (writer maps letter -> column index)
STAGE_COL = {"K": "DBZ->RMQ", "M": "Create GP table", "O": "IPC init load",
             "Q": "RMQ->GPSS", "S": "Data recon"}
MARK_BEGIN = "===RESULTS_JSON_BEGIN==="
MARK_END = "===RESULTS_JSON_END==="


def stream_schema(psa):
    return "lz_stream_" + psa[len("lz_psa_"):] if psa.startswith("lz_psa_") else None


# ---- self-post (PowerShell-proxy on Windows, urllib elsewhere) ----
def _post_ps(card, url):
    import subprocess
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix='.json')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(json.dumps(card, ensure_ascii=False).encode('utf-8'))
        ps = ("$proxy=[System.Net.WebRequest]::GetSystemWebProxy();"
              "$proxy.Credentials=[System.Net.CredentialCache]::DefaultNetworkCredentials;"
              "[System.Net.WebRequest]::DefaultWebProxy=$proxy;"
              f"$b=[System.IO.File]::ReadAllBytes('{tmp}');"
              f"try{{Invoke-RestMethod -Uri '{url}' -Method Post -ContentType 'application/json; charset=utf-8' -Body $b|Out-Null;Write-Host '[teams-ps] OK'}}"
              "catch{Write-Host ('[teams-ps] FAIL: '+$_.Exception.Message);exit 1}")
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                           capture_output=True, text=True, timeout=60)
        return (200 if r.returncode == 0 else 599), (r.stdout or '').strip()
    finally:
        try: os.remove(tmp)
        except Exception: pass


def post_card(title, body, url):
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4",
                    "body": [{"type": "TextBlock", "text": title, "weight": "Bolder",
                              "size": "Medium", "wrap": True},
                             {"type": "TextBlock", "text": body, "wrap": True,
                              "fontType": "Monospace"}]}}]}
    if sys.platform == 'win32':
        return _post_ps(card, url)
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(card).encode(),
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, str(resp.status)


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_table_status PROD v1 (GP side) @ {utc}")
    try:
        rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    except Exception as e:
        print("worklist decode FAILED:", e); return 1
    print(f"worklist rows: {len(rows)}  aliases: {len(ALIAS_MAP)}")

    try:
        import psycopg2
        conn = psycopg2.connect(**GP)
    except Exception as e:
        print("GP connect FAILED:", e); return 1
    conn.autocommit = True
    cur = conn.cursor()

    exists = set(); schema_tables = defaultdict(set)
    cur.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_type='BASE TABLE'")
    for s, t in cur.fetchall():
        s, t = s.lower(), t.lower(); exists.add((s, t)); schema_tables[s].add(t)
    reltuples = {}
    cur.execute("SELECT n.nspname,c.relname,c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r'")
    for s, t, n in cur.fetchall():
        reltuples[(s.lower(), t.lower())] = n or 0
    partsum = defaultdict(int)
    try:
        cur.execute("SELECT pn.nspname,pc.relname,SUM(cc.reltuples)::bigint FROM pg_inherits i JOIN pg_class pc ON pc.oid=i.inhparent JOIN pg_namespace pn ON pn.oid=pc.relnamespace JOIN pg_class cc ON cc.oid=i.inhrelid GROUP BY 1,2")
        for s, t, n in cur.fetchall():
            partsum[(s.lower(), t.lower())] = n or 0
    except Exception as e:
        print("partition sum WARN:", e)

    def eff(k):
        return max(reltuples.get(k, 0), partsum.get(k, 0))

    recon = {}
    try:
        cur.execute("SELECT s.gp_table,r.verdict,r.started_at FROM recon_meta.recon_results r JOIN recon_meta.recon_schedule s ON s.id=r.schedule_id")
        latest = {}
        for gp_table, verdict, started in cur.fetchall():
            k = (gp_table or "").lower(); at, vs = latest.get(k, (None, []))
            if at is None or (started and started > at):
                at, vs = started, [verdict]
            elif started == at:
                vs.append(verdict)
            latest[k] = (at, vs)
        for k, (_, vs) in latest.items():
            recon[k] = ("Error" if "ERROR" in vs else "Discrepancies" if "DIFF" in vs
                        else "Done" if "PASS" in vs else "Ready")
    except Exception as e:
        print("recon read WARN:", e)

    cur.close(); conn.close()

    NA = "N/A"; results = []; dist = Counter(); skipped = 0
    for r in rows:
        e = (r["e"] or "").strip().lower(); f = (r["f"] or "").strip().lower()
        t = (r["t"] or "").strip().lower(); curv = r.get("cur", [])
        if any((c or "").strip().lower() == "canceled" for c in curv):
            skipped += 1; continue
        psa = ALIAS_MAP.get(e, e)
        pkey = (psa, f); pexists = pkey in exists; prows = eff(pkey)
        ss = stream_schema(psa); skey = (ss, f) if ss else None
        sexists = bool(skey and skey in exists); srows = eff(skey) if skey else 0
        gp_table = f"{psa}.{f}"
        resolved = psa in schema_tables

        prop = {}
        prop["M"] = "Done" if pexists else "Not started"
        if t == "inter":
            prop["O"] = "Done" if (pexists and prows > 0) else ("Not started" if pexists else NA)
        else:
            prop["O"] = "Done" if (pexists and prows > 0) else "Not started"
        if t == "cdc":
            sv = "Done" if (sexists and srows > 0) else "Not started"
            prop["K"] = sv; prop["Q"] = sv
            prop["S"] = recon.get(gp_table, "(not scheduled)")
        else:
            prop["K"] = prop["Q"] = prop["S"] = NA
        gap = None if (pexists or resolved) else ("NO_TABLE_GP/unresolved_schema" if not resolved else "NO_TABLE_GP")
        for v in prop.values():
            dist[v] += 1
        results.append({"r": r["r"], "e": e, "f": f, "t": t, "prop": prop, "gap": gap})

    payload = json.dumps({"utc": utc, "stage_cols": STAGE_COL,
                          "skipped_canceled": skipped, "rows": results},
                         ensure_ascii=False, separators=(",", ":"))

    print(f"computed {len(results)} rows (skipped Canceled {skipped})")
    print("value distribution:", dict(dist.most_common()))
    print(MARK_BEGIN)
    print(payload)
    print(MARK_END)

    # self-post (resilient second capture path)
    if WEBHOOK.startswith("http"):
        summary = (f"rows={len(results)} skippedCanceled={skipped}\n"
                   f"values={dict(dist.most_common())}\n"
                   f"{MARK_BEGIN}\n{payload}\n{MARK_END}")
        if len(summary) > 17000:
            summary = (f"rows={len(results)} (payload {len(payload)}B > card cap — "
                       f"read RESULTS_JSON from tech channel stdout instead)")
        try:
            st, resp = post_card(f"📋 table-status check · {utc}", summary, WEBHOOK)
            print(f"[selfpost] status={st} {resp}")
        except Exception as ex:
            print(f"[selfpost] FAILED: {type(ex).__name__}: {ex}")
    print("\n=== prod v1 done (NO sheet writes) ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
