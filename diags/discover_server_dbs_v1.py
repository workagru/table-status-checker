#!/usr/bin/env python3
"""Coverage discovery — list the databases on every MSSQL server we can reach.

For each distinct MSSQL server (from SOURCE_PROFILES + injected user creds),
connect once and `SELECT name FROM sys.databases`. The Mac then matches the
sheet's 81 source databases against these lists to report covered vs missing.
READ-ONLY. Self-contained (reads SOURCE_PROFILES from the runtime).
Injected: WEBHOOK, MSSQL_CREDS_JSON (extra server creds, no secrets in git).
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

RUNTIME = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\agruzdev\AppData\Local"),
                       "autorecon_runtime")
if os.path.isdir(RUNTIME) and RUNTIME not in sys.path:
    sys.path.insert(0, RUNTIME)

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
              f"try{{Invoke-RestMethod -Uri '{url}' -Method Post -ContentType 'application/json; charset=utf-8' -Body $b|Out-Null;Write-Host 'OK'}}catch{{Write-Host ('FAIL '+$_.Exception.Message);exit 1}}")
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                           capture_output=True, text=True, timeout=60)
        try: os.remove(tmp)
        except Exception: pass
        return (200 if r.returncode == 0 else 599)
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(card).encode(),
                                 headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def conn_str(spec):
    port = spec.get('port', 1433)
    server = f"{spec['server']},{port}" if port else spec['server']
    return ";".join([f"DRIVER={{{spec['driver']}}}", f"SERVER={server}",
                     f"DATABASE={spec.get('database', 'master')}",
                     f"UID={spec['user']}", f"PWD={spec['password']}",
                     f"Encrypt={spec.get('encrypt', 'yes')}",
                     f"TrustServerCertificate={spec.get('trust_server_certificate', 'yes')}",
                     f"Connect Timeout={CONNECT_TIMEOUT}"]) + ";"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"discover_server_dbs v1 @ {utc}")
    default_driver = "ODBC Driver 17 for SQL Server"
    specs = {}    # host -> spec (with driver)
    try:
        from configs.config_sources import SOURCE_PROFILES
        for name, p in SOURCE_PROFILES.items():
            if (p.get('dialect') or '').lower() == 'mssql' and p.get('server') and 'CHANGEME' not in str(p.get('server')):
                specs.setdefault(p['server'], dict(p))
                default_driver = p.get('driver', default_driver)
    except Exception as e:
        print("SOURCE_PROFILES load WARN:", e)
    for c in EXTRA:                          # user-provided creds (fill driver, don't override profiles)
        if c.get('server') and c['server'] not in specs:
            c = dict(c); c.setdefault('driver', default_driver); specs[c['server']] = c
    print(f"servers to probe: {list(specs)}")

    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    result = {}
    for host, spec in specs.items():
        spec = dict(spec); spec.setdefault('driver', default_driver)
        try:
            conn = pyodbc.connect(conn_str(spec), timeout=CONNECT_TIMEOUT, autocommit=True)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY name")
            dbs = [r[0] for r in cur.fetchall()]
            cur.close(); conn.close()
            result[host] = dbs
            print(f"\n[{host}] {len(dbs)} dbs:")
            print("   " + ", ".join(dbs))
        except Exception as e:
            result[host] = {"error": f"{type(e).__name__}: {str(e)[:90]}"}
            print(f"\n[{host}] connect FAILED: {type(e).__name__}: {str(e)[:90]}")

    payload = json.dumps({"utc": utc, "kind": "server_dbs", "servers": result},
                         ensure_ascii=False, separators=(",", ":"))
    posted = False
    if WEBHOOK.startswith("http"):
        body = f"server DB lists\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                posted = post_card(f"🗄 server DB lists · {utc}", body, WEBHOOK) == 200
            except Exception as ex:
                print("[selfpost] FAIL:", ex)
    if not posted:
        print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== server dbs done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
