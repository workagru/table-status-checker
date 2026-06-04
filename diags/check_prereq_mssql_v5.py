#!/usr/bin/env python3
"""PRODUCTION Prerequisites — MSSQL v5, routes by (server, PORT) (READ-ONLY).

v5 over v4:
  * DB-name aliases (DB_ALIAS_JSON): the sheet's Source-Database (col C) can
    differ from the real db on the server (sheet 'SIMAT_B2CEnquiry' == server
    'UAT_B2CEnquiry' on DBUATCJ2:1450). We USE the REAL db, keyed by the sheet
    name. Self-validating: a wrong alias just yields MISSING_TABLE_SRC.
  * Better gap taxonomy: when USE fails we tell PERM (db visible in
    sys.databases but no access) from NOTFOUND_DB (db absent under that name).
v4 kept: installed 'SQL Server' driver for every endpoint, connect to 'master'.
Compact 'ci' output + stdout. Injected: WORKLIST_B64, WEBHOOK, MSSQL_CREDS_JSON,
DB_ALIAS_JSON.
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
DB_ALIAS_JSON = "__DB_ALIAS_JSON__"
try:
    EXTRA = json.loads(MSSQL_CREDS_JSON)
except Exception:
    EXTRA = []
try:
    DB_ALIAS = {k.lower(): v for k, v in json.loads(DB_ALIAS_JSON).items()}
except Exception:
    DB_ALIAS = {}
CONNECT_TIMEOUT = 6
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
                     f"Encrypt={spec.get('encrypt', 'no')}",
                     f"TrustServerCertificate={spec.get('trust_server_certificate', 'yes')}",
                     f"Connect Timeout={CONNECT_TIMEOUT}"]) + ";"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_prereq_mssql v5 @ {utc}  (db_aliases={len(DB_ALIAS)})")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    endpoints = {}; db_explicit = {}; prof_by_host = {}; ref = {}
    try:
        from configs.config_sources import SOURCE_PROFILES
        for nm, p in SOURCE_PROFILES.items():
            if (p.get('dialect') or '').lower() == 'mssql' and p.get('server') and 'CHANGEME' not in str(p.get('server')):
                ep = (p['server'], p.get('port', 1433))
                endpoints.setdefault(ep, dict(p, sample=p.get('database', 'master')))
                prof_by_host.setdefault(p['server'], {k: p.get(k) for k in ('driver', 'encrypt', 'trust_server_certificate')})
                if not ref and p.get('driver'):
                    ref = {k: p.get(k) for k in ('driver', 'encrypt', 'trust_server_certificate')}
    except Exception as e:
        print("SOURCE_PROFILES WARN:", e)
    DEF_DRIVER = ref.get('driver') or "SQL Server"
    for c in EXTRA:
        if (c.get('dialect') or 'mssql').lower() != 'mssql':
            continue
        ep = (c['server'], c.get('port', 1433))
        spec = dict(c); spec['sample'] = c.get('database', 'master')
        base = prof_by_host.get(c['server']) or ref
        spec['driver'] = base.get('driver', DEF_DRIVER)
        spec['encrypt'] = base.get('encrypt', 'no')
        spec['trust_server_certificate'] = base.get('trust_server_certificate', 'yes')
        endpoints[ep] = spec
        if c.get('database'):
            db_explicit[c['database'].strip().lower()] = ep
    print(f"ref driver: {DEF_DRIVER!r}; endpoints: {len(endpoints)}")

    conns = {}; db_ep = {}; ep_dbset = defaultdict(set); ep_ok = []; ep_fail = []
    for ep, spec in endpoints.items():
        cn = None; last = "?"
        for v in (spec, dict(spec, encrypt="no", trust_server_certificate="yes"),
                  dict(spec, encrypt="yes", trust_server_certificate="yes")):
            try:
                cn = pyodbc.connect(conn_str(v, "master"), timeout=CONNECT_TIMEOUT, autocommit=True); break
            except Exception as e:
                last = f"{type(e).__name__}: {str(e)[:55]}"
        if not cn:
            ep_fail.append((ep, last)); continue
        conns[ep] = cn; ep_ok.append(ep)
        try:
            cur = cn.cursor(); cur.execute("SELECT name FROM sys.databases WHERE database_id>4")
            for (nm,) in cur.fetchall():
                db_ep.setdefault(nm.lower(), ep); ep_dbset[ep].add(nm.lower())
            cur.close()
        except Exception:
            pass
    for db, ep in db_explicit.items():
        if ep in conns:
            db_ep.setdefault(db, ep)
    print(f"endpoints connected: {len(ep_ok)}/{len(endpoints)}")
    for ep, err in ep_fail:
        print(f"  FAIL {ep[0]}:{ep[1]} -> {err}")
    print(f"reachable databases: {len(db_ep)}")

    by_db = defaultdict(list)
    for r in rows:
        by_db[(r.get("db") or "").strip()].append(r)

    results = []; dist = Counter(); gaps = Counter()
    for db, drows in by_db.items():
        real = DB_ALIAS.get(db.lower(), db)                 # sheet name -> real db
        ep = db_ep.get(real.lower()) or db_ep.get(db.lower()) or db_explicit.get(db.lower())
        if not ep or ep not in conns:
            for r in drows:
                gaps["NO_ACCESS_DB"] += 1
                results.append({"r": r["r"], "t": (r.get("t") or "").lower(), "prop": {}, "gap": "NO_ACCESS_DB"})
            continue
        try:
            cur = conns[ep].cursor()
            cur.execute(f"USE [{real}]")
            cur.execute("SELECT LOWER(SCHEMA_NAME(schema_id)), LOWER(name), is_tracked_by_cdc FROM sys.tables")
            catalog = {(s, t): bool(c) for s, t, c in cur.fetchall()}
            cur.close()
        except Exception as e:
            known = real.lower() in ep_dbset.get(ep, set())
            code = "PERM" if known else "NOTFOUND_DB"
            for r in drows:
                gaps[code] += 1
                results.append({"r": r["r"], "t": (r.get("t") or "").lower(), "prop": {}, "gap": code})
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
    GMAP = {"MISSING_TABLE_SRC": "M"}     # PERM/NOTFOUND_DB/DB_ERR -> 'E' (leave I blank)
    lines = []
    for res in results:
        g = res.get("gap") or ""
        if g == "NO_ACCESS_DB":
            continue
        gc = GMAP.get(g, "E" if g else "_")
        lines.append(f'{res["r"]}:{IMAP.get(res["prop"].get("I",""), "_")}:{gc}')
    payload = json.dumps({"utc": utc, "fmt": "ci", "kind": "prereq_mssql", "chunk": 1, "chunks": 1,
                          "rows_str": "\n".join(lines)}, ensure_ascii=False, separators=(",", ":"))
    print(f"computed {len(results)} rows  I={dict(dist)}  gaps={dict(gaps)} shipped={len(lines)}")
    if WEBHOOK.startswith("http"):
        body = f"Prerequisites (MSSQL v5) I={dict(dist)} gaps={dict(gaps)} eps={len(ep_ok)}/{len(endpoints)}\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                print("[selfpost]", post_card(f"🔑 prerequisites (MSSQL v5) · {utc}", body, WEBHOOK))
            except Exception as ex:
                print("[selfpost] FAIL:", ex)
    print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== prereq mssql v5 done (NO sheet writes) ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
