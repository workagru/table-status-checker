#!/usr/bin/env python3
"""PRODUCTION Prerequisites checker — DB2 for i source (READ-ONLY).

Fills the Prerequisites stage (sheet column I) for DB2i source tables:
access = the object exists/visible in its library, and (for cdc tables)
CDC-on = the file is journaled (Debezium for i reads journals). Uses
QSYS2.OBJECT_STATISTICS(library, '*FILE', table) -> JOURNALED.

Per row: I = 'Done' if found and (journaled OR non-cdc); 'Not started' if
found but cdc-and-not-journaled; gap (MISSING_TABLE_SRC) and I left unset if
not found / no access. Emits the same RESULTS_JSON the Mac writer parses,
via stdout + self-post to 'table status'.

Self-contained: reads SOURCE_PROFILES from the autorecon runtime, connects
with the heartbeat_v4 jt400/JVM pattern. Injected: WORKLIST_B64, WEBHOOK.
"""
import base64
import json
import os
import sys
import time
import traceback
from collections import Counter

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


def load_db2i_profile():
    from configs.config_sources import SOURCE_PROFILES
    for name, p in SOURCE_PROFILES.items():
        if (p.get('dialect') or '').lower() == 'db2i':
            return name, p
    return None, None


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_prereq_db2i v1 @ {utc}")
    try:
        rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    except Exception as e:
        print("worklist decode FAILED:", e); return 1
    print(f"worklist rows: {len(rows)}")

    try:
        name, prof = load_db2i_profile()
    except Exception as e:
        print("SOURCE_PROFILES load FAILED:", e); return 1
    if not prof:
        print("no db2i profile found"); return 1
    jar = prof.get('jt400_jar_path', '')
    print(f"profile={name} host={prof.get('host')}:{prof.get('port')} jar={'ok' if os.path.exists(jar) else 'MISSING'}")

    try:
        import jaydebeapi, jpype
        if not jpype.isJVMStarted():
            jpype.startJVM(jpype.getDefaultJVMPath(),
                           '--add-opens=java.base/java.nio=ALL-UNNAMED',
                           '--add-opens=java.base/java.util=ALL-UNNAMED',
                           '--add-opens=java.base/java.lang=ALL-UNNAMED',
                           '--add-opens=java.base/java.util.concurrent=ALL-UNNAMED',
                           '--add-opens=java.base/sun.nio.ch=ALL-UNNAMED',
                           classpath=[jar])
        host = prof['host']; port = prof.get('port', 8470)
        url = f"jdbc:as400://{host}:{port}" if port and port != 8470 else f"jdbc:as400://{host}"
        conn = jaydebeapi.connect("com.ibm.as400.access.AS400JDBCDriver", url,
                                  [prof['user'], prof['password']], jar)
    except Exception as e:
        print("DB2i connect FAILED:", type(e).__name__, e); return 1

    results = []; dist = Counter(); errors = Counter()
    for r in rows:
        lib = (r.get("d") or "").strip().upper()
        tbl = (r.get("f") or "").strip().upper()
        t = (r.get("t") or "").strip().lower()
        gap = None; istat = None
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM TABLE(QSYS2.OBJECT_STATISTICS(?, '*FILE', ?)) X "
                        "FETCH FIRST 1 ROW ONLY", (lib, tbl))
            cols = [str(d[0]).upper() for d in cur.description]
            row = cur.fetchone()
            cur.close()
            if not row:
                gap = "MISSING_TABLE_SRC"; errors[gap] += 1
            else:
                rec = dict(zip(cols, row))
                jv = str(rec.get("JOURNALED", "")).strip().upper()
                journaled = jv in ("YES", "Y", "1", "TRUE")
                if t == "cdc":
                    istat = "Done" if journaled else "Read granted, but no CDC"
                else:
                    istat = "Done"   # non-cdc: access is enough
        except Exception as e:
            gap = "NO_ACCESS:" + type(e).__name__; errors[gap[:40]] += 1
        if istat:
            dist[istat] += 1
            results.append({"r": r["r"], "e": r.get("e"), "f": r.get("f"),
                            "t": t, "prop": {"I": istat}, "gap": None})
        else:
            results.append({"r": r["r"], "e": r.get("e"), "f": r.get("f"),
                            "t": t, "prop": {}, "gap": gap})
    conn.close()

    payload = json.dumps({"utc": utc, "stage_cols": {"I": "Prerequisites"},
                          "skipped_canceled": 0, "rows": results},
                         ensure_ascii=False, separators=(",", ":"))
    print(f"computed {len(results)} rows  I-dist={dict(dist)}  gaps={dict(errors)}")
    # full JSON only to 'table status'; stdout fallback if self-post fails
    posted_ok = False
    if WEBHOOK.startswith("http"):
        body = f"Prerequisites (DB2i) rows={len(results)} I={dict(dist)} gaps={dict(errors)}\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                st, resp = post_card(f"🔑 prerequisites check (DB2i) · {utc}", body, WEBHOOK)
                posted_ok = (st == 200)
                print(f"[selfpost] status={st} {resp}")
            except Exception as ex:
                print(f"[selfpost] FAILED: {type(ex).__name__}: {ex}")
    if not posted_ok:
        print("[fallback] emitting RESULTS_JSON to stdout:")
        print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== prereq db2i v1 done (NO sheet writes) ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
