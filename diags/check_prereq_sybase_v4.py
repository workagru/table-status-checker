#!/usr/bin/env python3
"""Sybase ASE (SIMAHDWH) via ODBC — v4 (diagnostic, READ-ONLY).

v3 proved SYBDWHUATHQ:5000 is TCP-OPEN from the VDI but no DataDirect ODBC
variant connected — and the per-variant errors went to stdout (tech channel,
not captured). v4 PUTS each variant's full error into the SELF-POST (table
status, reliably captured) and tries a wider keyword matrix (NetworkAddress /
HostName+PortNumber / Host+Port / ServerName, UID|LogonID). On the first
success it reads the catalog and emits col I; otherwise it reports every error.
Injected: WORKLIST_B64, WEBHOOK, MSSQL_CREDS_JSON.
"""
import base64
import json
import os
import socket
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
    print(f"check_prereq_sybase v4 @ {utc}")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    cred = next((c for c in EXTRA if (c.get('dialect') or '').lower() == 'sybase'), None)
    if not cred:
        print("no sybase cred"); return 1
    dbname = cred.get('database', 'SIMAHDWH')
    host = cred['server']; port = cred.get('port', 5000)
    user = cred['user']; pwd = cred['password']
    drows = [r for r in rows if (r.get("db") or "").strip().lower() == dbname.lower()]
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = host
    print(f"target {host}({ip}):{port}/{dbname}  rows={len(drows)}")

    try:
        import pyodbc
    except Exception as e:
        print("pyodbc n/a:", e); return 1
    syb = [d for d in pyodbc.drivers() if 'sybase' in d.lower()]
    print("sybase drivers:", syb)

    def variants(drv):
        return [
            ("NetworkAddr,UID", f"DRIVER={{{drv}}};NetworkAddress={host},{port};Database={dbname};UID={user};PWD={pwd};"),
            ("NetworkAddr,IP", f"DRIVER={{{drv}}};NetworkAddress={ip},{port};Database={dbname};UID={user};PWD={pwd};"),
            ("HostName/PortNumber", f"DRIVER={{{drv}}};HostName={host};PortNumber={port};Database={dbname};UID={user};PWD={pwd};"),
            ("Host/Port", f"DRIVER={{{drv}}};Host={host};Port={port};Database={dbname};UID={user};PWD={pwd};"),
            ("ServerName", f"DRIVER={{{drv}}};ServerName={host};Database={dbname};UID={user};PWD={pwd};"),
            ("NetworkAddr,LogonID,noDB", f"DRIVER={{{drv}}};NetworkAddress={host},{port};LogonID={user};Password={pwd};"),
        ]
    cn = None; used = None; log = []
    for drv in syb:
        for label, cs in variants(drv):
            try:
                cn = pyodbc.connect(cs, timeout=CONNECT_TIMEOUT, autocommit=True)
                used = f"{drv}|{label}"; log.append(f"OK   {label}"); print("OK", label); break
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:90]}"
                log.append(f"FAIL {label}: {msg}"); print("FAIL", label, msg)
        if cn:
            break

    results = []; dist = Counter(); gaps = Counter(); ntab = 0
    if cn:
        try:
            cur = cn.cursor()
            cur.execute("SELECT user_name(uid), name FROM sysobjects WHERE type='U'")
            cat = set();
            for owner, nm in cur.fetchall():
                cat.add(((owner or "").lower().strip(), (nm or "").lower().strip()))
            cur.close(); ntab = len(cat); tn = {t for _, t in cat}
            for r in drows:
                sch = (r.get("d") or "").lower().strip(); tbl = (r.get("f") or "").lower().strip()
                t = (r.get("t") or "").lower().strip()
                if (sch, tbl) in cat or tbl in tn:
                    istat = "Done" if t != "cdc" else "Read granted, but no CDC"
                    dist[istat] += 1; results.append({"r": r["r"], "prop": {"I": istat}, "gap": None})
                else:
                    gaps["MISSING_TABLE_SRC"] += 1; results.append({"r": r["r"], "prop": {}, "gap": "M"})
        except Exception as e:
            print("catalog FAIL:", e)
        cn.close()

    IMAP = {"Done": "D", "Read granted, but no CDC": "C"}
    lines = [f'{r["r"]}:{IMAP.get(r["prop"].get("I",""), "_")}:{"M" if r.get("gap") else "_"}' for r in results]
    payload = json.dumps({"utc": utc, "fmt": "ci", "kind": "prereq_sybase", "chunk": 1, "chunks": 1,
                          "rows_str": "\n".join(lines)}, ensure_ascii=False, separators=(",", ":"))
    print(f"computed {len(results)} I={dict(dist)} gaps={dict(gaps)}")
    if WEBHOOK.startswith("http"):
        head = (f"Sybase SIMAHDWH via ODBC — {'CONNECTED via '+used if used else 'NOT connected'}\n"
                f"drivers={syb}\n" + "\n".join(log))
        body = head + (f"\nI={dict(dist)} tables={ntab}\n{MARK_BEGIN}\n{payload}\n{MARK_END}" if results else "")
        try:
            print("[selfpost]", post_card(f"🔑 prerequisites (Sybase v4) · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    if results:
        print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== prereq sybase v4 done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
