#!/usr/bin/env python3
"""One-shot connect probe for SIMAH_UNIFIED on DBMSTRUAT:1450 — uses the
legacy '{SQL Server}' driver that's installed on the VDI. Tries the real
target DB, then 'master' on the same instance to localize the failure
(server vs. DB). Posts the per-attempt verdict to the table_status
Teams channel via the self-post webhook.

Injected by the Mac generator: WEBHOOK.
READ-ONLY.
"""
import json
import os
import sys
import time
import traceback

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

WEBHOOK = "__WEBHOOK__"
SERVER = "DBMSTRUAT.ksacb.com.sa"
PORT = 1450
USER = "gpuatsrvusr"
PASSWORD = "QwertyEr45"   # from secrets.local.json (gitignored on Mac)
TIMEOUT = 8


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
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def attempt(database, driver):
    import pyodbc
    cs = (f"DRIVER={{{driver}}};SERVER={SERVER},{PORT};DATABASE={database};"
          f"UID={USER};PWD={PASSWORD};Encrypt=no;TrustServerCertificate=yes;"
          f"Connect Timeout={TIMEOUT};")
    t0 = time.time()
    try:
        cn = pyodbc.connect(cs, timeout=TIMEOUT, autocommit=True)
        cur = cn.cursor()
        cur.execute("SELECT DB_NAME(), SUSER_SNAME(), @@VERSION")
        row = cur.fetchone()
        cur.close(); cn.close()
        return f"OK   db={database:18} driver={driver:14} [{int((time.time()-t0)*1000)}ms] -> db={row[0]} login={row[1]} version={(row[2] or '')[:60]}"
    except Exception as e:
        # capture sqlstate + native error if pyodbc Error
        sqlstate, native, msg = "?", "?", str(e)
        if hasattr(e, "args") and e.args:
            a0 = e.args[0]
            if isinstance(a0, str) and len(a0) == 5: sqlstate = a0
            if len(e.args) > 1: msg = str(e.args[1])
        return f"FAIL db={database:18} driver={driver:14} [{int((time.time()-t0)*1000)}ms] SQLSTATE={sqlstate}  {msg[:280]}"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_simah_unified v1 @ {utc}")
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1
    drivers = list(pyodbc.drivers())
    print("installed drivers:", drivers)

    # try the user-target DB then master on the same server (using the driver
    # that the working profiles use — '{SQL Server}' on this VDI)
    plan = []
    for drv in ["SQL Server", "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"]:
        if drv not in drivers:
            continue
        for db in ("SIMAH_UNIFIED", "master"):
            plan.append((db, drv))

    results = []
    for db, drv in plan:
        line = attempt(db, drv)
        print(line); results.append(line)

    body = f"target {SERVER}:{PORT}  user={USER}\n" + "\n".join(results)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔐 SIMAH_UNIFIED probe · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("\n=== done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
