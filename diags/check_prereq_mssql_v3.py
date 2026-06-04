#!/usr/bin/env python3
"""PRODUCTION Prerequisites — MSSQL v3, routes by (server, PORT) (READ-ONLY).

v3 over v2: databases live on different PORTS of the same host (DBUATCJ2 has
instances on 1450/1451/1452/1453), so we key endpoints by (server, port), not
host. Endpoints come from the injected per-db creds + SOURCE_PROFILES. We
connect each endpoint once, read sys.databases, and also honour the explicit
db->endpoint mapping from the creds. Then per source DB: USE it, read the table
catalog once, decide col I (Done / 'Read granted, but no CDC' / gap). Sybase
endpoints are skipped (ODBC SQL Server driver can't talk TDS-Sybase).
Compact 'ci' output + stdout. Injected: WORKLIST_B64, WEBHOOK, MSSQL_CREDS_JSON.
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
CONNECT_TIMEOUT = 10
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


def conn_str(spec, database):
    server = f"{spec['server']},{spec['port']}" if spec.get('port') else spec['server']
    return ";".join([f"DRIVER={{{spec['driver']}}}", f"SERVER={server}", f"DATABASE={database}",
                     f"UID={spec['user']}", f"PWD={spec['password']}",
                     f"Encrypt={spec.get('encrypt', 'yes')}",
                     f"TrustServerCertificate={spec.get('trust_server_certificate', 'yes')}",
                     f"Connect Timeout={CONNECT_TIMEOUT}"]) + ";"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_prereq_mssql v3 @ {utc}")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    default_driver = "ODBC Driver 17 for SQL Server"
    endpoints = {}        # (server, port) -> spec (driver+user+pass+sample db)
    db_explicit = {}      # db_lower -> (server, port)
    try:
        from configs.config_sources import SOURCE_PROFILES
        for nm, p in SOURCE_PROFILES.items():
            if (p.get('dialect') or '').lower() == 'mssql' and p.get('server') and 'CHANGEME' not in str(p.get('server')):
                ep = (p['server'], p.get('port', 1433))
                endpoints.setdefault(ep, dict(p, sample=p.get('database', 'master')))
                default_driver = p.get('driver', default_driver)
    except Exception as e:
        print("SOURCE_PROFILES WARN:", e)
    for c in EXTRA:
        if (c.get('dialect') or 'mssql').lower() != 'mssql':
            continue                              # skip Sybase etc.
        ep = (c['server'], c.get('port', 1433))
        spec = dict(c); spec.setdefault('driver', default_driver); spec['sample'] = c.get('database', 'master')
        endpoints[ep] = spec                       # explicit creds win for this endpoint
        if c.get('database'):
            db_explicit[c['database'].strip().lower()] = ep
    print(f"endpoints: {list(endpoints)}")

    # connect each endpoint, list its databases
    conns = {}; db_ep = {}
    for ep, spec in endpoints.items():
        for db0 in (spec.get('sample', 'master'), 'master'):
            try:
                cn = pyodbc.connect(conn_str(spec, db0), timeout=CONNECT_TIMEOUT, autocommit=True)
                conns[ep] = cn
                cur = cn.cursor(); cur.execute("SELECT name FROM sys.databases WHERE database_id>4")
                for (nm,) in cur.fetchall():
                    db_ep.setdefault(nm.lower(), ep)
                cur.close()
                break
            except Exception as e:
                last = f"{type(e).__name__}: {str(e)[:60]}"
        else:
            print(f"  [{ep}] connect FAILED: {last}")
    for db, ep in db_explicit.items():            # explicit mapping overrides discovery
        if ep in conns:
            db_ep[db] = ep
    print(f"reachable databases: {len(db_ep)}; connected endpoints: {len(conns)}")

    by_db = defaultdict(list)
    for r in rows:
        by_db[(r.get("db") or "").strip()].append(r)

    results = []; dist = Counter(); gaps = Counter(); noacc = set()
    for db, drows in by_db.items():
        ep = db_ep.get(db.lower())
        if not ep or ep not in conns:
            noacc.add(db)
            for r in drows:
                gaps["NO_ACCESS_DB"] += 1
                results.append({"r": r["r"], "t": (r.get("t") or "").lower(), "prop": {}, "gap": "NO_ACCESS_DB"})
            continue
        try:
            cur = conns[ep].cursor()
            cur.execute(f"USE [{db}]")
            cur.execute("SELECT LOWER(SCHEMA_NAME(schema_id)), LOWER(name), is_tracked_by_cdc FROM sys.tables")
            catalog = {(s, t): bool(c) for s, t, c in cur.fetchall()}
            cur.close()
        except Exception as e:
            for r in drows:
                gaps["DB_ERR"] += 1
                results.append({"r": r["r"], "t": (r.get("t") or "").lower(), "prop": {}, "gap": "DB_ERR:" + type(e).__name__})
            continue
        for r in drows:
            sch = (r.get("d") or "").strip().lower(); tbl = (r.get("f") or "").strip().lower()
            t = (r.get("t") or "").strip().lower(); istat = None; gap = None
            if (sch, tbl) not in catalog:
                gap = "MISSING_TABLE_SRC"; gaps[gap] += 1
            else:
                istat = ("Done" if (t != "cdc" or catalog[(sch, tbl)]) else "Read granted, but no CDC")
            if istat:
                dist[istat] += 1
                results.append({"r": r["r"], "t": t, "prop": {"I": istat}, "gap": None})
            else:
                results.append({"r": r["r"], "t": t, "prop": {}, "gap": gap})
    for cn in conns.values():
        try: cn.close()
        except Exception: pass

    IMAP = {"Done": "D", "Read granted, but no CDC": "C", "Not started": "N"}
    GMAP = {"MISSING_TABLE_SRC": "M", "DB_ERR": "E"}
    lines = []
    for res in results:
        g = res.get("gap") or ""
        if g.startswith("NO_ACCESS_DB"):
            continue
        gc = GMAP.get(g.split(":")[0], "E" if g else "_")
        lines.append(f'{res["r"]}:{IMAP.get(res["prop"].get("I",""), "_")}:{gc}')
    payload = json.dumps({"utc": utc, "fmt": "ci", "kind": "prereq_mssql", "chunk": 1, "chunks": 1,
                          "rows_str": "\n".join(lines)}, ensure_ascii=False, separators=(",", ":"))
    print(f"computed {len(results)} rows  I={dict(dist)}  gaps={dict(gaps)}  no_access_dbs={len(noacc)} shipped={len(lines)}")
    if WEBHOOK.startswith("http"):
        body = f"Prerequisites (MSSQL v3) I={dict(dist)} gaps={dict(gaps)}\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                print("[selfpost]", post_card(f"🔑 prerequisites (MSSQL v3) · {utc}", body, WEBHOOK))
            except Exception as ex:
                print("[selfpost] FAIL:", ex)
    print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== prereq mssql v3 done (NO sheet writes) ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
