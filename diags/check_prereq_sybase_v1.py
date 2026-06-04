#!/usr/bin/env python3
"""PRODUCTION Prerequisites checker — Sybase ASE source (SIMAHDWH) READ-ONLY.

The ODBC MSSQL probe skips Sybase (TDS dialect differs). SIMAHDWH lives on
SYBDWHUATHQ:5000 and needs a JDBC driver. This probe:
  1) finds a Sybase JDBC jar on the VDI (jConnect jconn*.jar or jTDS jtds*.jar)
     with a BOUNDED search (curated dirs + shallow scan — never a full C:\\ walk),
  2) connects via jaydebeapi (jConnect first, then jTDS URL forms),
  3) reads the SIMAHDWH table catalog once (sysobjects type='U'),
  4) per worklist row (db==SIMAHDWH): col I = 'Done' if the (owner.table)
     exists and non-cdc; cdc tables -> 'Read granted, but no CDC' (ASE CDC is
     Rep-Server-side, not checkable here); missing -> MISSING_TABLE_SRC gap.

If no jar is found it reports that clearly (so we know to drop one on the VDI)
without hanging. Compact 'ci' output + stdout, same shape the Mac writer parses.
Injected: WORKLIST_B64, WEBHOOK, MSSQL_CREDS_JSON (the sybase entry is used).
"""
import base64
import glob
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


def find_sybase_jar():
    """Bounded search for a Sybase JDBC jar. Returns (path, kind) or (None, None).
    kind in {'jconnect','jtds'}. Never walks all of C:\\ — curated dirs + shallow."""
    dirs = [RUNTIME, os.path.join(RUNTIME, "libs"), os.path.join(RUNTIME, "lib"),
            os.path.join(RUNTIME, "drivers"), os.path.join(RUNTIME, "jars"),
            os.path.join(RUNTIME, "jdbc")]
    # the DB2i profile's jar dir is the most likely place a JDBC jar lives
    try:
        from configs.config_sources import SOURCE_PROFILES
        for _, p in SOURCE_PROFILES.items():
            jp = p.get('jt400_jar_path') or p.get('jar') or ''
            if jp:
                dirs.append(os.path.dirname(jp))
    except Exception:
        pass
    home = os.environ.get("USERPROFILE", r"C:\Users\agruzdev")
    dirs += [os.path.join(home, "Documents"), os.path.join(home, "Downloads"),
             r"C:\jdbc", r"C:\drivers", r"C:\sybase"]
    seen = set(); cand = []
    for d in dirs:
        if not d or d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        for pat in ("jconn*.jar", "jConnect*.jar", "jconnect*.jar", "jtds*.jar", "*sybase*.jar", "*jconn*.jar"):
            try:
                cand += glob.glob(os.path.join(d, pat))
            except Exception:
                pass
    cand = sorted(set(cand))
    for c in cand:
        b = os.path.basename(c).lower()
        if "jtds" in b:
            return c, "jtds"
    for c in cand:
        return c, "jconnect"
    print(f"  searched dirs: {[d for d in seen]}")
    return None, None


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_prereq_sybase v1 @ {utc}")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    cred = next((c for c in EXTRA if (c.get('dialect') or '').lower() == 'sybase'), None)
    if not cred:
        print("no sybase cred in MSSQL_CREDS_JSON"); return 1
    dbname = cred.get('database', 'SIMAHDWH')
    drows = [r for r in rows if (r.get("db") or "").strip().lower() == dbname.lower()]
    print(f"target db={dbname} host={cred.get('server')}:{cred.get('port')} worklist rows={len(drows)}")

    try:
        import jaydebeapi, jpype
    except Exception as e:
        print("jaydebeapi/jpype not available:", e); return 1

    jar, kind = find_sybase_jar()
    if not jar:
        print("NO Sybase JDBC jar found on the VDI — drop jconn4.jar (jConnect) or "
              "jtds-1.3.x.jar into the autorecon_runtime dir and re-run.")
        if WEBHOOK.startswith("http"):
            try:
                post_card(f"🔑 prerequisites (Sybase) · {utc}",
                          f"SIMAHDWH: no Sybase JDBC jar on VDI (searched runtime/libs/Documents). "
                          f"Need jconn4.jar or jtds-*.jar. rows pending={len(drows)}", WEBHOOK)
            except Exception:
                pass
        return 2
    print(f"jar={jar} kind={kind}")

    try:
        if not jpype.isJVMStarted():
            jpype.startJVM(jpype.getDefaultJVMPath(),
                           '--add-opens=java.base/java.nio=ALL-UNNAMED',
                           '--add-opens=java.base/java.lang=ALL-UNNAMED',
                           classpath=[jar])
    except Exception as e:
        print("JVM start FAILED:", type(e).__name__, e); return 1

    host = cred['server']; port = cred.get('port', 5000)
    user = cred['user']; pwd = cred['password']
    attempts = []
    if kind == "jtds":
        attempts = [("net.sourceforge.jtds.jdbc.Driver", f"jdbc:jtds:sybase://{host}:{port}/{dbname}")]
    else:
        attempts = [("com.sybase.jdbc4.jdbc.SybDriver", f"jdbc:sybase:Tds:{host}:{port}/{dbname}"),
                    ("com.sybase.jdbc4.jdbc.SybDriver", f"jdbc:sybase:Tds:{host}:{port}"),
                    ("com.sybase.jdbc3.jdbc.SybDriver", f"jdbc:sybase:Tds:{host}:{port}/{dbname}")]
    conn = None; lasterr = "?"
    for drv, url in attempts:
        try:
            conn = jaydebeapi.connect(drv, url, [user, pwd], jar)
            print(f"connected via {drv} {url}")
            break
        except Exception as e:
            lasterr = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"  attempt {drv} failed: {lasterr}")
    if not conn:
        print("Sybase connect FAILED:", lasterr)
        if WEBHOOK.startswith("http"):
            try:
                post_card(f"🔑 prerequisites (Sybase) · {utc}",
                          f"SIMAHDWH connect FAILED: {lasterr}", WEBHOOK)
            except Exception:
                pass
        return 1

    # read catalog once (current db = dbname): user tables + owner
    catalog = set()
    try:
        cur = conn.cursor()
        try:
            cur.execute(f"USE {dbname}")
        except Exception:
            pass
        cur.execute("SELECT user_name(o.uid), o.name FROM sysobjects o WHERE o.type='U'")
        for owner, nm in cur.fetchall():
            catalog.add(((owner or "").strip().lower(), (nm or "").strip().lower()))
        cur.close()
    except Exception as e:
        print("catalog read FAILED:", type(e).__name__, e); conn.close(); return 1
    print(f"catalog: {len(catalog)} user tables in {dbname}")

    results = []; dist = Counter(); gaps = Counter()
    for r in drows:
        sch = (r.get("d") or "").strip().lower(); tbl = (r.get("f") or "").strip().lower()
        t = (r.get("t") or "").strip().lower()
        present = (sch, tbl) in catalog or any(tn == tbl for _, tn in catalog)
        if not present:
            gaps["MISSING_TABLE_SRC"] += 1
            results.append({"r": r["r"], "prop": {}, "gap": "MISSING_TABLE_SRC"})
        else:
            istat = "Done" if t != "cdc" else "Read granted, but no CDC"
            dist[istat] += 1
            results.append({"r": r["r"], "prop": {"I": istat}, "gap": None})
    conn.close()

    IMAP = {"Done": "D", "Read granted, but no CDC": "C", "Not started": "N"}
    GMAP = {"MISSING_TABLE_SRC": "M"}
    lines = []
    for res in results:
        g = res.get("gap") or ""
        gc = GMAP.get(g, "_")
        lines.append(f'{res["r"]}:{IMAP.get(res["prop"].get("I",""), "_")}:{gc}')
    payload = json.dumps({"utc": utc, "fmt": "ci", "kind": "prereq_sybase", "chunk": 1, "chunks": 1,
                          "rows_str": "\n".join(lines)}, ensure_ascii=False, separators=(",", ":"))
    print(f"computed {len(results)} rows  I={dict(dist)}  gaps={dict(gaps)}")
    if WEBHOOK.startswith("http"):
        body = f"Prerequisites (Sybase) I={dict(dist)} gaps={dict(gaps)} tables={len(catalog)}\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                print("[selfpost]", post_card(f"🔑 prerequisites (Sybase) · {utc}", body, WEBHOOK))
            except Exception as ex:
                print("[selfpost] FAIL:", ex)
    print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== prereq sybase v1 done (NO sheet writes) ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
