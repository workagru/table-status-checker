#!/usr/bin/env python3
"""PRODUCTION Prerequisites checker — MSSQL source (READ-ONLY).

Fills Prerequisites (sheet col I) for MSSQL source tables: access =
HAS_PERMS_BY_NAME(schema.table,'SELECT'); CDC-on = sys.tables.is_tracked_by_cdc.
Per cdc row: I='Done' if found & has-perms & tracked; 'Not started' if found
& has-perms & not tracked; gap (MISSING_TABLE_SRC / NO_ACCESS) and I unset
otherwise. Same RESULTS_JSON contract + self-post as the other probes.

Connection: a base MSSQL profile (BASE_PROFILE) supplies server + creds;
the DATABASE is overridden per row to the sheet's Source Database Name, so
sibling DBs on the same server (e.g. MOLIM_IDENTITY next to molim_finance)
are reachable without a dedicated profile. DBs/servers we can't reach -> gap.

Self-contained: reads SOURCE_PROFILES from the autorecon runtime.
Injected: WORKLIST_B64, WEBHOOK.
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
BASE_PROFILE = "molim_finance"   # supplies server+creds; database overridden per row
CONNECT_TIMEOUT = 10
MARK_BEGIN, MARK_END = "===RESULTS_JSON_BEGIN===", "===RESULTS_JSON_END==="


def _post_ps(card, url):
    import subprocess
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix='.json')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(json.dumps(card, ensure_ascii=False).encode('utf-8'))
        ps = ("$proxy=[System.Net.WebRequest]::GetSystemWebProxy();"
              "$proxy.Credentials=[System.Net.CredentialCache]::DefaultNetworkCredentials;"
              "[System.Net.WebRequest]::DefaultWebProxy=$proxy;"
              f"$b=[System.IO.File]::ReadAllBytes('{tmp}');"
              f"try{{Invoke-RestMethod -Uri '{url}' -Method Post -ContentType 'application/json; charset=utf-8' -Body $b|Out-Null;Write-Host '[teams-ps] OK'}}"
              "catch{Write-Host ('[teams-ps] FAIL: '+$_.Exception.Message);exit 1}")
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                           capture_output=True, text=True, timeout=60)
        return (200 if r.returncode == 0 else 599), (r.stdout or '').strip()
    finally:
        try: os.remove(tmp)
        except Exception: pass


def post_card(title, body, url):
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4",
                    "body": [{"type": "TextBlock", "text": title, "weight": "Bolder",
                              "size": "Medium", "wrap": True},
                             {"type": "TextBlock", "text": body, "wrap": True,
                              "fontType": "Monospace"}]}}]}
    if sys.platform == 'win32':
        return _post_ps(card, url)
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(card).encode(),
                                 headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, str(resp.status)


def conn_str(base, database):
    port = base.get('port', 1433)
    server = f"{base['server']},{port}" if port else base['server']
    return ";".join([
        f"DRIVER={{{base['driver']}}}", f"SERVER={server}", f"DATABASE={database}",
        f"UID={base['user']}", f"PWD={base['password']}",
        f"Encrypt={base.get('encrypt', 'yes')}",
        f"TrustServerCertificate={base.get('trust_server_certificate', 'no')}",
        f"Connect Timeout={CONNECT_TIMEOUT}",
    ]) + ";"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_prereq_mssql v1 @ {utc}")
    try:
        rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    except Exception as e:
        print("worklist decode FAILED:", e); return 1
    try:
        from configs.config_sources import SOURCE_PROFILES
        base = dict(SOURCE_PROFILES)[BASE_PROFILE]
    except Exception as e:
        print(f"base profile {BASE_PROFILE!r} load FAILED:", e); return 1
    print(f"base={BASE_PROFILE} server={base.get('server')}:{base.get('port')}  rows={len(rows)}")

    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    by_db = defaultdict(list)
    for r in rows:
        by_db[(r.get("db") or "").strip()].append(r)

    results = []; dist = Counter(); errors = Counter()
    for db, drows in by_db.items():
        conn = None
        try:
            conn = pyodbc.connect(conn_str(base, db), timeout=CONNECT_TIMEOUT, autocommit=True)
            cur = conn.cursor()
        except Exception as e:
            print(f"  [db {db}] connect FAILED: {type(e).__name__}: {str(e)[:80]}")
            for r in drows:
                errors["NO_ACCESS_DB"] += 1
                results.append({"r": r["r"], "e": r.get("e"), "f": r.get("f"),
                                "t": (r.get("t") or "").lower(), "prop": {}, "gap": "NO_ACCESS_DB"})
            continue
        for r in drows:
            sch = (r.get("d") or "").strip(); tbl = (r.get("f") or "").strip()
            t = (r.get("t") or "").strip().lower(); gap = None; istat = None
            try:
                cur.execute("SELECT t.is_tracked_by_cdc FROM sys.tables t "
                            "JOIN sys.schemas s ON s.schema_id=t.schema_id "
                            "WHERE LOWER(s.name)=LOWER(?) AND LOWER(t.name)=LOWER(?)", (sch, tbl))
                row = cur.fetchone()
                if row is None:
                    gap = "MISSING_TABLE_SRC"; errors[gap] += 1
                else:
                    tracked = bool(row[0])
                    cur.execute("SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'SELECT')", (f"{sch}.{tbl}",))
                    has = bool(cur.fetchone()[0])
                    if not has:
                        gap = "NO_ACCESS"; errors[gap] += 1
                    elif t == "cdc":
                        istat = "Done" if tracked else "Not started"
                    else:
                        istat = "Done"
            except Exception as e:
                gap = "ERR:" + type(e).__name__; errors[gap[:40]] += 1
            if istat:
                dist[istat] += 1
                results.append({"r": r["r"], "e": r.get("e"), "f": r.get("f"),
                                "t": t, "prop": {"I": istat}, "gap": None})
            else:
                results.append({"r": r["r"], "e": r.get("e"), "f": r.get("f"),
                                "t": t, "prop": {}, "gap": gap})
        try: conn.close()
        except Exception: pass

    payload = json.dumps({"utc": utc, "stage_cols": {"I": "Prerequisites"},
                          "skipped_canceled": 0, "rows": results},
                         ensure_ascii=False, separators=(",", ":"))
    print(f"computed {len(results)} rows  I-dist={dict(dist)}  gaps={dict(errors)}")
    posted_ok = False
    if WEBHOOK.startswith("http"):
        body = f"Prerequisites (MSSQL) rows={len(results)} I={dict(dist)} gaps={dict(errors)}\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                st, resp = post_card(f"🔑 prerequisites check (MSSQL) · {utc}", body, WEBHOOK)
                posted_ok = (st == 200)
                print(f"[selfpost] status={st} {resp}")
            except Exception as ex:
                print(f"[selfpost] FAILED: {type(ex).__name__}: {ex}")
    if not posted_ok:
        print("[fallback] emitting RESULTS_JSON to stdout:")
        print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== prereq mssql v1 done (NO sheet writes) ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
