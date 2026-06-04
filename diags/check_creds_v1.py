#!/usr/bin/env python3
"""Credential check — try every MSSQL (server, PORT) endpoint and report
OK (+ db count) or FAIL (+ error) for each. Endpoints come from injected
per-db creds + SOURCE_PROFILES. The per-endpoint status goes into the
SELF-POST card (so it's captured even when the tech tab isn't). READ-ONLY.
Injected: WEBHOOK, MSSQL_CREDS_JSON.
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
CONNECT_TIMEOUT = 6
DEF_DRIVER = "ODBC Driver 17 for SQL Server"


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


def conn_str(spec, database):
    server = f"{spec['server']},{spec['port']}" if spec.get('port') else spec['server']
    return ";".join([f"DRIVER={{{spec['driver']}}}", f"SERVER={server}", f"DATABASE={database}",
                     f"UID={spec['user']}", f"PWD={spec['password']}",
                     f"Encrypt={spec.get('encrypt', 'no')}",
                     f"TrustServerCertificate={spec.get('trust_server_certificate', 'yes')}",
                     f"Connect Timeout={CONNECT_TIMEOUT}"]) + ";"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_creds v1 @ {utc}")
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1
    print("drivers:", [d for d in pyodbc.drivers()])

    endpoints = {}; prof_by_host = {}
    try:
        from configs.config_sources import SOURCE_PROFILES
        for nm, p in SOURCE_PROFILES.items():
            if (p.get('dialect') or '').lower() == 'mssql' and p.get('server') and 'CHANGEME' not in str(p.get('server')):
                ep = (p['server'], p.get('port', 1433))
                endpoints.setdefault(ep, dict(p, sample=p.get('database', 'master'), src='profile:' + nm))
                prof_by_host.setdefault(p['server'], {k: p.get(k) for k in ('driver', 'encrypt', 'trust_server_certificate')})
    except Exception as e:
        print("SOURCE_PROFILES WARN:", e)
    for c in EXTRA:
        if (c.get('dialect') or 'mssql').lower() != 'mssql':
            continue
        ep = (c['server'], c.get('port', 1433))
        spec = dict(c); spec['sample'] = c.get('database', 'master'); spec['src'] = 'user'
        base = prof_by_host.get(c['server'])
        if base and base.get('driver'):
            spec['driver'] = base['driver']
            for k in ('encrypt', 'trust_server_certificate'):
                if base.get(k) is not None:
                    spec[k] = base[k]
        else:
            spec.setdefault('driver', DEF_DRIVER); spec.setdefault('encrypt', 'no'); spec.setdefault('trust_server_certificate', 'yes')
        endpoints[ep] = spec

    lines = []
    for ep in sorted(endpoints, key=lambda e: (e[0], e[1])):
        spec = endpoints[ep]
        variants = [spec,
                    dict(spec, driver=DEF_DRIVER, encrypt="no", trust_server_certificate="yes"),
                    dict(spec, driver="ODBC Driver 18 for SQL Server", encrypt="no", trust_server_certificate="yes")]
        ok = None; err = "?"
        for v in variants:
            try:
                cn = pyodbc.connect(conn_str(v, v.get('sample', 'master')), timeout=CONNECT_TIMEOUT, autocommit=True)
                cur = cn.cursor(); cur.execute("SELECT COUNT(*) FROM sys.databases")
                n = cur.fetchone()[0]; cur.close(); cn.close()
                ok = (n, v['driver'], v.get('encrypt')); break
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:70]}"
        if ok:
            line = f"OK   {ep[0]}:{ep[1]:<5} {ok[0]} dbs  [{spec.get('src')}; {ok[1]} enc={ok[2]}]"
        else:
            line = f"FAIL {ep[0]}:{ep[1]:<5} {err}  [{spec.get('src')}]"
        print(line); lines.append(line)

    body = "credential check (per endpoint)\n" + "\n".join(lines)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔐 cred check · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("\n=== cred check done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
