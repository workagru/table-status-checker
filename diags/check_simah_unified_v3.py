#!/usr/bin/env python3
"""SIMAH_UNIFIED probe v3 — REAL password this time (taken from injected
MSSQL_CREDS_JSON, not hard-coded). Tries DATABASE=SIMAH_UNIFIED then master
on DBMSTRUAT:1450 via the legacy '{SQL Server}' driver, captures full error.
Injected: WEBHOOK, MSSQL_CREDS_JSON. READ-ONLY.
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
MSSQL_CREDS_JSON = "__MSSQL_CREDS_JSON__"
try:
    EXTRA = json.loads(MSSQL_CREDS_JSON)
except Exception:
    EXTRA = []
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


def find_cred():
    for c in EXTRA:
        sv = (c.get('server', '') or '').lower()
        db = (c.get('database', '') or '').lower()
        if 'dbmstruat' in sv and db == 'simah_unified':
            return c
    return None


def try_connect(label, server, port, user, pwd, database):
    import pyodbc
    parts = ["DRIVER={SQL Server}", f"SERVER={server},{port}",
             f"UID={user}", f"PWD={pwd}", "Network=DBMSSOCN"]
    if database is not None:
        parts.append(f"DATABASE={database}")
    cs = ";".join(parts) + ";"
    t0 = time.time()
    try:
        cn = pyodbc.connect(cs, timeout=TIMEOUT, autocommit=True)
        cur = cn.cursor()
        cur.execute("SELECT DB_NAME(), SUSER_SNAME(), @@SERVERNAME, @@VERSION")
        row = cur.fetchone(); cur.close(); cn.close()
        return (f"OK   [{label:28}] {int((time.time()-t0)*1000)}ms "
                f"-> db={row[0]} login={row[1]} server={row[2]} ver={(row[3] or '')[:50]}")
    except Exception as e:
        args = list(getattr(e, "args", []) or [])
        state = args[0] if (args and isinstance(args[0], str) and len(args[0]) == 5) else "?"
        msg = args[1] if len(args) > 1 else str(e)
        import re
        m = re.search(r"\((\d{4,6})\)", str(msg))
        native = m.group(1) if m else "?"
        return (f"FAIL [{label:28}] {int((time.time()-t0)*1000)}ms state={state} native={native} "
                f"{str(msg)[:220]}")


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_simah_unified v3 (REAL pwd) @ {utc}")
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    cred = find_cred()
    if not cred:
        print("FATAL: DBMSTRUAT/SIMAH_UNIFIED cred not in injected MSSQL_CREDS_JSON")
        return 1
    server = cred['server']; port = cred.get('port', 1450)
    user = cred['user']; pwd = cred['password']
    # show the password tail (last 4) so the user can verify on their side w/o leaking
    pwd_tag = f"len={len(pwd)} tail=...{pwd[-4:]}"
    print(f"using server={server}:{port} user={user} pwd_tag={pwd_tag}")

    out = [f"target {server}:{port}  user={user}  pwd_tag={pwd_tag}"]
    for label, db in [("DB=SIMAH_UNIFIED", "SIMAH_UNIFIED"),
                      ("DB=master       ", "master"),
                      ("DB=<default>    ", None)]:
        line = try_connect(label, server, port, user, pwd, db)
        print(line); out.append(line)

    body = "\n".join(out)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔐 SIMAH_UNIFIED probe v3 (real pwd) · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
