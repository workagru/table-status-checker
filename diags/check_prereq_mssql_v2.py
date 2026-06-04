#!/usr/bin/env python3
"""PRODUCTION Prerequisites — MSSQL, ALL reachable servers (READ-ONLY).

Generalizes v1: connects to every distinct MSSQL server (SOURCE_PROFILES +
injected user creds), discovers which databases live on each, then for every
worklist row routes to the right server, switches DB with USE, and reads the
whole DB's table catalog (one query) to decide Prerequisites (col I):
  cdc row: Done if table found & is_tracked_by_cdc; Not started if found & not;
  non-cdc: Done if found; gap MISSING_TABLE_SRC if not found; NO_ACCESS_DB if
  the source DB isn't on any reachable server.
Same RESULTS_JSON contract + self-post. Injected: WORKLIST_B64, WEBHOOK,
MSSQL_CREDS_JSON (extra/override server creds).
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


def conn_str(spec, database="master"):
    port = spec.get('port', 1433)
    server = f"{spec['server']},{port}" if port else spec['server']
    return ";".join([f"DRIVER={{{spec['driver']}}}", f"SERVER={server}", f"DATABASE={database}",
                     f"UID={spec['user']}", f"PWD={spec['password']}",
                     f"Encrypt={spec.get('encrypt', 'yes')}",
                     f"TrustServerCertificate={spec.get('trust_server_certificate', 'yes')}",
                     f"Connect Timeout={CONNECT_TIMEOUT}"]) + ";"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_prereq_mssql v2 @ {utc}")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    # ---- build server->spec (profiles, then user creds override/add) ----
    specs = {}; default_driver = "ODBC Driver 17 for SQL Server"
    try:
        from configs.config_sources import SOURCE_PROFILES
        for nm, p in SOURCE_PROFILES.items():
            if (p.get('dialect') or '').lower() == 'mssql' and p.get('server') and 'CHANGEME' not in str(p.get('server')):
                specs[p['server']] = dict(p); default_driver = p.get('driver', default_driver)
    except Exception as e:
        print("SOURCE_PROFILES WARN:", e)
    for c in EXTRA:                          # user creds only ADD new servers
        if c.get('server') and c['server'] not in specs:  # profiles already work for known servers
            c = dict(c); c.setdefault('driver', default_driver); specs[c['server']] = c
    print(f"servers: {list(specs)}")

    # ---- connect each server once, map db_lower -> (host, conn) ----
    conns = {}; db_host = {}
    for host, spec in specs.items():
        spec = dict(spec); spec.setdefault('driver', default_driver)
        try:
            cn = pyodbc.connect(conn_str(spec), timeout=CONNECT_TIMEOUT, autocommit=True)
            conns[host] = cn
            cur = cn.cursor(); cur.execute("SELECT name FROM sys.databases WHERE database_id>4")
            for (nm,) in cur.fetchall():
                db_host.setdefault(nm.lower(), host)
            cur.close()
        except Exception as e:
            print(f"[{host}] connect FAILED: {type(e).__name__}: {str(e)[:70]}")
    print(f"reachable databases: {len(db_host)}")

    # ---- per source-db: USE it, dump table catalog once, match rows ----
    by_db = defaultdict(list)
    for r in rows:
        by_db[(r.get("db") or "").strip()].append(r)

    results = []; dist = Counter(); gaps = Counter()
    for db, drows in by_db.items():
        host = db_host.get(db.lower())
        if not host:
            for r in drows:
                gaps["NO_ACCESS_DB"] += 1
                results.append({"r": r["r"], "f": r.get("f"), "t": (r.get("t") or "").lower(),
                                "prop": {}, "gap": "NO_ACCESS_DB"})
            continue
        catalog = None
        try:
            cur = conns[host].cursor()
            cur.execute(f"USE [{db}]")
            cur.execute("SELECT LOWER(SCHEMA_NAME(schema_id)), LOWER(name), is_tracked_by_cdc FROM sys.tables")
            catalog = {(s, t): bool(c) for s, t, c in cur.fetchall()}
            cur.close()
        except Exception as e:
            for r in drows:
                gaps["DB_ERR"] += 1
                results.append({"r": r["r"], "f": r.get("f"), "t": (r.get("t") or "").lower(),
                                "prop": {}, "gap": "DB_ERR:" + type(e).__name__})
            continue
        for r in drows:
            sch = (r.get("d") or "").strip().lower(); tbl = (r.get("f") or "").strip().lower()
            t = (r.get("t") or "").strip().lower(); istat = None; gap = None
            if (sch, tbl) not in catalog:
                gap = "MISSING_TABLE_SRC"; gaps[gap] += 1
            else:
                tracked = catalog[(sch, tbl)]
                # cdc table reachable but CDC not enabled -> the sheet's special status
                istat = ("Done" if (t != "cdc" or tracked) else "Read granted, but no CDC")
            if istat:
                dist[istat] += 1
                results.append({"r": r["r"], "f": r.get("f"), "t": t, "prop": {"I": istat}, "gap": None})
            else:
                results.append({"r": r["r"], "f": r.get("f"), "t": t, "prop": {}, "gap": gap})

    for cn in conns.values():
        try: cn.close()
        except Exception: pass

    # compact 'ci' encoding: one line per RESULT row 'r:Icode:gapcode'. Rows on
    # unreachable DBs (NO_ACCESS_DB) are only counted, not shipped (col I left).
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
                          "no_access_rows": gaps.get("NO_ACCESS_DB", 0), "rows_str": "\n".join(lines)},
                         ensure_ascii=False, separators=(",", ":"))
    print(f"computed {len(results)} rows  I={dict(dist)}  gaps={dict(gaps)}  shipped={len(lines)}")
    if WEBHOOK.startswith("http"):
        body = f"Prerequisites (MSSQL all servers) I={dict(dist)} gaps={dict(gaps)}\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                print("[selfpost]", post_card(f"🔑 prerequisites (MSSQL all) · {utc}", body, WEBHOOK))
            except Exception as ex:
                print("[selfpost] FAIL:", ex)
    # always emit to stdout too — compact payload is small and tech channel is
    # captured reliably (the self-post to 'table status' depends on the active tab).
    print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== prereq mssql v2 done (NO sheet writes) ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
