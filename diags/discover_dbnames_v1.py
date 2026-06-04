#!/usr/bin/env python3
"""Discovery (READ-ONLY): list the REAL sys.databases names on every reachable
MSSQL endpoint, then cross-reference the sheet's distinct Source-Database names
to expose name mismatches (e.g. sheet 'SIMAT_B2CEnquiry' vs server
'UAT_B2CEnquiry'). For each sheet DB: FOUND@endpoint (ci) or NOT-FOUND + the
closest real name (difflib). No aliases are applied — this only reports, so the
user/me can decide the mapping. Same endpoint-building + installed 'SQL Server'
driver as the prereq v4 probe. Injected: WORKLIST_B64, WEBHOOK, MSSQL_CREDS_JSON.
"""
import base64
import difflib
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

WORKLIST_B64 = "__WORKLIST_B64__"
WEBHOOK = "__WEBHOOK__"
MSSQL_CREDS_JSON = "__MSSQL_CREDS_JSON__"
try:
    EXTRA = json.loads(MSSQL_CREDS_JSON)
except Exception:
    EXTRA = []
CONNECT_TIMEOUT = 6


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
    print(f"discover_dbnames v1 @ {utc}")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    endpoints = {}; prof_by_host = {}; ref = {}
    try:
        from configs.config_sources import SOURCE_PROFILES
        for nm, p in SOURCE_PROFILES.items():
            if (p.get('dialect') or '').lower() == 'mssql' and p.get('server') and 'CHANGEME' not in str(p.get('server')):
                ep = (p['server'], p.get('port', 1433))
                endpoints.setdefault(ep, dict(p))
                prof_by_host.setdefault(p['server'], {k: p.get(k) for k in ('driver', 'encrypt', 'trust_server_certificate')})
                if not ref and p.get('driver'):
                    ref = {k: p.get(k) for k in ('driver', 'encrypt', 'trust_server_certificate')}
    except Exception as e:
        print("SOURCE_PROFILES WARN:", e)
    DEF = ref.get('driver') or "SQL Server"
    for c in EXTRA:
        if (c.get('dialect') or 'mssql').lower() != 'mssql':
            continue
        ep = (c['server'], c.get('port', 1433))
        spec = dict(c)
        base = prof_by_host.get(c['server']) or ref
        spec['driver'] = base.get('driver', DEF)
        spec['encrypt'] = base.get('encrypt', 'no')
        spec['trust_server_certificate'] = base.get('trust_server_certificate', 'yes')
        endpoints.setdefault(ep, spec)

    ep_dbs = {}; all_dbs = {}     # ep -> [names]; name_lower -> "server:port"
    for ep, spec in endpoints.items():
        cn = None
        for v in (spec, dict(spec, encrypt="no"), dict(spec, encrypt="yes")):
            try:
                cn = pyodbc.connect(conn_str(v, "master"), timeout=CONNECT_TIMEOUT, autocommit=True); break
            except Exception:
                pass
        if not cn:
            print(f"  [{ep[0]}:{ep[1]}] unreachable"); continue
        try:
            cur = cn.cursor(); cur.execute("SELECT name FROM sys.databases WHERE database_id>4 ORDER BY name")
            names = [r[0] for r in cur.fetchall()]; cur.close()
        except Exception as e:
            names = []; print(f"  [{ep[0]}:{ep[1]}] db list FAIL: {e}")
        cn.close()
        tag = f"{ep[0].split('.')[0]}:{ep[1]}"
        ep_dbs[tag] = names
        for n in names:
            all_dbs.setdefault(n.lower(), tag)
        print(f"\n[{tag}] {len(names)} dbs:\n  " + ", ".join(names))

    # cross-reference the sheet's distinct source DBs
    sheet_dbs = {}
    for r in rows:
        d = (r.get("db") or "").strip()
        if d:
            sheet_dbs[d] = sheet_dbs.get(d, 0) + 1
    universe = list(all_dbs)
    found = []; missing = []
    for d, n in sorted(sheet_dbs.items(), key=lambda x: -x[1]):
        if d.lower() in all_dbs:
            found.append(f"{d} -> {all_dbs[d.lower()]} ({n})")
        else:
            sug = difflib.get_close_matches(d.lower(), universe, n=1, cutoff=0.6)
            sugn = f"{sug[0]}@{all_dbs[sug[0]]}" if sug else "—"
            missing.append(f"{d} ({n} rows)  ~closest: {sugn}")

    print("\n=== sheet DB names NOT FOUND on any reachable endpoint (with closest real name) ===")
    for m in missing:
        print("  " + m)
    print(f"\nfound={len(found)} missing={len(missing)} of {len(sheet_dbs)} distinct sheet DBs")

    if WEBHOOK.startswith("http"):
        body = ("DB-name mismatch report (NOT-FOUND sheet DBs -> closest real db)\n"
                + "\n".join(missing[:60])
                + f"\n\nreachable endpoints: {', '.join(sorted(ep_dbs))}")
        try:
            print("[selfpost]", post_card(f"🔎 db-name discovery · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("\n=== discover_dbnames v1 done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
