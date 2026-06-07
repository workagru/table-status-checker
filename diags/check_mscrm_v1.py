#!/usr/bin/env python3
"""Diagnose what's wrong with SIMAH_MSCRM (server name? db name? both?).

Two passes:
  A) Explicit connect to DBUATCJ2:1450 with DATABASE=SIMAH_MSCRM (capture
     the exact error -> 4060 means the login can reach the server but the
     DB does not exist; 18456 means a login problem).
  B) Wide LIKE '%crm%' / '%mscrm%' search across sys.databases on every
     reachable MSSQL endpoint listed in injected MSSQL_CREDS_JSON, using
     the legacy '{SQL Server}' driver. Reports all matches.

Posts a single Adaptive Card to the table_status webhook. Injected:
WEBHOOK, MSSQL_CREDS_JSON. READ-ONLY.
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
TIMEOUT = 6


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


def build_endpoints():
    """One usable spec per (server, port). Prefer profile-style creds. Use legacy
    '{SQL Server}' as the driver (the only one installed on the VDI)."""
    by_host = {}
    try:
        from configs.config_sources import SOURCE_PROFILES
        for nm, p in SOURCE_PROFILES.items():
            if (p.get('dialect') or '').lower() != 'mssql': continue
            if 'CHANGEME' in str(p.get('server', '')): continue
            ep = (p['server'], p.get('port', 1433))
            spec = dict(p)
            spec['driver'] = 'SQL Server'
            spec['sample'] = p.get('database', 'master')
            spec['src'] = 'profile:' + nm
            by_host.setdefault(ep, spec)
    except Exception as e:
        print("WARN SOURCE_PROFILES:", e)
    for c in EXTRA:
        if (c.get('dialect') or 'mssql').lower() != 'mssql': continue
        ep = (c['server'], c.get('port', 1433))
        if ep in by_host: continue
        spec = dict(c); spec['driver'] = 'SQL Server'
        spec['sample'] = c.get('database', 'master'); spec['src'] = 'user'
        by_host[ep] = spec
    return by_host


def connect(spec, database):
    import pyodbc
    server = f"{spec['server']},{spec['port']}" if spec.get('port') else spec['server']
    cs = (f"DRIVER={{{spec['driver']}}};SERVER={server};DATABASE={database};"
          f"UID={spec['user']};PWD={spec['password']};Network=DBMSSOCN;")
    return pyodbc.connect(cs, timeout=TIMEOUT, autocommit=True)


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_mscrm v1 @ {utc}")
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    endpoints = build_endpoints()
    out = [f"== A) explicit connect to DBUATCJ2:1450 with DATABASE=SIMAH_MSCRM =="]

    # find a working cred for DBUATCJ2:1450 to use for the explicit connect
    dbuat = None
    for (server, port), spec in endpoints.items():
        if 'dbuatcj2' in server.lower() and port == 1450:
            dbuat = spec; break
    if not dbuat:
        out.append("no DBUATCJ2:1450 cred in profiles/creds")
    else:
        for db in ("SIMAH_MSCRM", "MSCRM", "CRM", "SIMAH_CRM", "master"):
            t0 = time.time()
            try:
                cn = connect(dbuat, db)
                cur = cn.cursor(); cur.execute("SELECT DB_NAME()")
                v = cur.fetchone()[0]; cur.close(); cn.close()
                out.append(f"  OK   db={db:18} [{int((time.time()-t0)*1000)}ms] -> server returned db={v}")
            except Exception as e:
                args = list(getattr(e, "args", []) or [])
                state = args[0] if (args and isinstance(args[0], str) and len(args[0]) == 5) else "?"
                msg = (args[1] if len(args) > 1 else str(e))
                # extract native error
                import re
                m = re.search(r"\((\d{4,6})\)", str(msg))
                native = m.group(1) if m else "?"
                out.append(f"  FAIL db={db:18} [{int((time.time()-t0)*1000)}ms] state={state} native={native} {str(msg)[:170]}")

    out.append("")
    out.append("== B) any DB on reachable servers whose name contains 'crm' or 'mscrm' ==")
    found_any = False
    for (server, port), spec in sorted(endpoints.items()):
        try:
            cn = connect(spec, spec.get('sample', 'master'))
            cur = cn.cursor()
            cur.execute("SELECT name FROM sys.databases WHERE name LIKE '%crm%' OR name LIKE '%mscrm%' ORDER BY name")
            names = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) FROM sys.databases")
            total = cur.fetchone()[0]
            cur.close(); cn.close()
            if names:
                found_any = True
                out.append(f"  {server}:{port}  total={total}  matches: {', '.join(names)}")
            else:
                out.append(f"  {server}:{port}  total={total}  matches: (none)")
        except Exception as e:
            args = list(getattr(e, "args", []) or [])
            state = args[0] if (args and isinstance(args[0], str) and len(args[0]) == 5) else "?"
            msg = (args[1] if len(args) > 1 else str(e))
            out.append(f"  {server}:{port}  CONNECT FAIL  state={state}  {str(msg)[:120]}")
    if not found_any:
        out.append("")
        out.append("VERDICT: no MSSQL endpoint we can reach has a DB whose name contains 'crm' or 'mscrm'.")

    body = "\n".join(out)
    print(body)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔍 MSCRM probe · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
