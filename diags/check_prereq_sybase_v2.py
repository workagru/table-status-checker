#!/usr/bin/env python3
"""PRODUCTION Prerequisites — Sybase ASE source (SIMAHDWH) via ODBC (READ-ONLY).

v2: the VDI has NO Sybase JDBC jar, but it DOES have the ODBC driver
'DataDirect 7.1 Sybase Wire Protocol'. So we reach SIMAHDWH over ODBC instead
of JDBC. We auto-pick any installed driver whose name contains 'sybase', try a
few DataDirect connection-string shapes (NetworkAddress vs HOST/PORT, Database
vs DB), read the ASE catalog (sysobjects type='U' + owner), and decide col I
per worklist row (db==SIMAHDWH): non-cdc present -> Done; cdc present ->
'Read granted, but no CDC' (ASE CDC is Rep-Server-side, not checkable here);
missing -> MISSING_TABLE_SRC. Compact 'ci' output + stdout.
Injected: WORKLIST_B64, WEBHOOK, MSSQL_CREDS_JSON.
"""
import base64
import json
import os
import sys
import time
import traceback
from collections import Counter

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

RUNTIME = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\agruzdev\AppData\Local"),
                       "autorecon_runtime")
if os.path.isdir(RUNTIME) and RUNTIME not in sys.path:
    sys.path.insert(0, RUNTIME)

WORKLIST_B64 = "__WORKLIST_B64__"
WEBHOOK = "__WEBHOOK__"
MSSQL_CREDS_JSON = "__MSSQL_CREDS_JSON__"
try:
    EXTRA = json.loads(MSSQL_CREDS_JSON)
except Exception:
    EXTRA = []
CONNECT_TIMEOUT = 8
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
    print(f"check_prereq_sybase v2 (ODBC) @ {utc}")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    cred = next((c for c in EXTRA if (c.get('dialect') or '').lower() == 'sybase'), None)
    if not cred:
        print("no sybase cred"); return 1
    dbname = cred.get('database', 'SIMAHDWH')
    host = cred['server']; port = cred.get('port', 5000)
    user = cred['user']; pwd = cred['password']
    drows = [r for r in rows if (r.get("db") or "").strip().lower() == dbname.lower()]
    print(f"target {host}:{port}/{dbname}  worklist rows={len(drows)}")

    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1
    drivers = list(pyodbc.drivers())
    syb = [d for d in drivers if 'sybase' in d.lower()]
    print("sybase ODBC drivers:", syb or "(none)")
    if not syb:
        print("no Sybase ODBC driver installed");
        if WEBHOOK.startswith("http"):
            try: post_card(f"🔑 prerequisites (Sybase ODBC) · {utc}", f"no Sybase ODBC driver on VDI; drivers={drivers}", WEBHOOK)
            except Exception: pass
        return 2

    def variants(drv):
        return [
            f"DRIVER={{{drv}}};NetworkAddress={host},{port};Database={dbname};UID={user};PWD={pwd};",
            f"DRIVER={{{drv}}};HOST={host};PORT={port};Database={dbname};UID={user};PWD={pwd};",
            f"DRIVER={{{drv}}};HOST={host};PORT={port};DB={dbname};UID={user};PWD={pwd};",
            f"DRIVER={{{drv}}};NetworkAddress={host},{port};DatabaseName={dbname};LogonID={user};Password={pwd};",
            f"DRIVER={{{drv}}};Server={host};Port={port};Database={dbname};UID={user};PWD={pwd};",
        ]
    cn = None; used = None; last = "?"
    for drv in syb:
        for cs in variants(drv):
            try:
                cn = pyodbc.connect(cs, timeout=CONNECT_TIMEOUT, autocommit=True)
                used = cs.split(';UID')[0].split(';LogonID')[0]; break
            except Exception as e:
                last = f"{type(e).__name__}: {str(e)[:130]}"
        if cn:
            break
    if not cn:
        print("Sybase ODBC connect FAILED:", last)
        if WEBHOOK.startswith("http"):
            try: post_card(f"🔑 prerequisites (Sybase ODBC) · {utc}", f"SIMAHDWH connect FAILED via {syb}: {last}", WEBHOOK)
            except Exception: pass
        return 1
    print("connected via", used)

    catalog = set()
    try:
        cur = cn.cursor()
        cur.execute("SELECT user_name(uid), name FROM sysobjects WHERE type='U'")
        for owner, nm in cur.fetchall():
            catalog.add(((owner or "").strip().lower(), (nm or "").strip().lower()))
        cur.close()
    except Exception as e:
        print("catalog read FAILED:", type(e).__name__, e); cn.close(); return 1
    print(f"catalog: {len(catalog)} user tables")
    cn.close()
    tblnames = {t for _, t in catalog}

    results = []; dist = Counter(); gaps = Counter()
    for r in drows:
        sch = (r.get("d") or "").strip().lower(); tbl = (r.get("f") or "").strip().lower()
        t = (r.get("t") or "").strip().lower()
        present = (sch, tbl) in catalog or tbl in tblnames
        if not present:
            gaps["MISSING_TABLE_SRC"] += 1
            results.append({"r": r["r"], "prop": {}, "gap": "MISSING_TABLE_SRC"})
        else:
            istat = "Done" if t != "cdc" else "Read granted, but no CDC"
            dist[istat] += 1
            results.append({"r": r["r"], "prop": {"I": istat}, "gap": None})

    IMAP = {"Done": "D", "Read granted, but no CDC": "C", "Not started": "N"}
    lines = [f'{res["r"]}:{IMAP.get(res["prop"].get("I",""), "_")}:{"M" if res.get("gap") else "_"}'
             for res in results]
    payload = json.dumps({"utc": utc, "fmt": "ci", "kind": "prereq_sybase", "chunk": 1, "chunks": 1,
                          "rows_str": "\n".join(lines)}, ensure_ascii=False, separators=(",", ":"))
    print(f"computed {len(results)} rows  I={dict(dist)}  gaps={dict(gaps)}")
    if WEBHOOK.startswith("http"):
        body = f"Prerequisites (Sybase ODBC) I={dict(dist)} gaps={dict(gaps)} tables={len(catalog)}\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                print("[selfpost]", post_card(f"🔑 prerequisites (Sybase ODBC) · {utc}", body, WEBHOOK))
            except Exception as ex:
                print("[selfpost] FAIL:", ex)
    print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== prereq sybase v2 done (NO sheet writes) ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
