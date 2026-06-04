#!/usr/bin/env python3
"""Sybase ASE (SIMAHDWH) prerequisites via ODBC — v3 (READ-ONLY, diagnostic).

v2 failed with 'HY000 Insufficient information to connect' but only printed the
LAST variant's error, so we couldn't tell a wrong-keyword failure from a real
network/auth one. v3:
  1) RAW TCP test to SYBDWHUATHQ:5000 first (was never tested — tcp_reach
     skipped the sybase entry). OPEN/FILTERED/REFUSED.
  2) only if OPEN, try DataDirect connection-string shapes, printing the FULL
     error of EACH variant (canonical NetworkAddress=host,port first; also a
     resolved-IP form in case DNS is the issue).
  3) on success, read the ASE catalog and decide col I per SIMAHDWH row.
Compact 'ci' output + stdout. Injected: WORKLIST_B64, WEBHOOK, MSSQL_CREDS_JSON.
"""
import base64
import json
import os
import socket
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


def tcp_probe(host, port):
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:
        return "DNS", None, f"resolve failed: {e}"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(6)
    t0 = time.time()
    try:
        rc = s.connect_ex((ip, int(port)))
        dt = int((time.time() - t0) * 1000)
        if rc == 0:
            return "OPEN", ip, f"{ip} {dt}ms"
        return ("REFUSED" if rc in (61, 111, 10061) else f"FILTERED/ERR{rc}"), ip, f"{ip} {dt}ms rc={rc}"
    except Exception as e:
        return "ERR", ip, f"{ip} {type(e).__name__}: {e}"
    finally:
        try: s.close()
        except Exception: pass


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_prereq_sybase v3 @ {utc}")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    cred = next((c for c in EXTRA if (c.get('dialect') or '').lower() == 'sybase'), None)
    if not cred:
        print("no sybase cred"); return 1
    dbname = cred.get('database', 'SIMAHDWH')
    host = cred['server']; port = cred.get('port', 5000)
    user = cred['user']; pwd = cred['password']
    drows = [r for r in rows if (r.get("db") or "").strip().lower() == dbname.lower()]
    print(f"target {host}:{port}/{dbname}  worklist rows={len(drows)}")

    code, ip, detail = tcp_probe(host, port)
    print(f"TCP {host}:{port} -> {code}  ({detail})")
    notes = [f"TCP {code} {detail}"]

    import_ok = True
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); import_ok = False
    cn = None; used = None
    if code == "OPEN" and import_ok:
        drivers = list(pyodbc.drivers())
        syb = [d for d in drivers if 'sybase' in d.lower()]
        print("sybase ODBC drivers:", syb or "(none)")
        def variants(drv):
            return [
                ("NetworkAddress+UID", f"DRIVER={{{drv}}};NetworkAddress={host},{port};Database={dbname};UID={user};PWD={pwd};"),
                ("NetworkAddress+LogonID", f"DRIVER={{{drv}}};NetworkAddress={host},{port};Database={dbname};LogonID={user};Password={pwd};"),
                ("NetworkAddress+IP", f"DRIVER={{{drv}}};NetworkAddress={ip},{port};Database={dbname};UID={user};PWD={pwd};"),
                ("Host/Port", f"DRIVER={{{drv}}};Host={host};Port={port};Database={dbname};UID={user};PWD={pwd};"),
                ("NetworkAddress noDB", f"DRIVER={{{drv}}};NetworkAddress={host},{port};UID={user};PWD={pwd};"),
            ]
        for drv in syb:
            for label, cs in variants(drv):
                try:
                    cn = pyodbc.connect(cs, timeout=CONNECT_TIMEOUT, autocommit=True)
                    used = f"{drv} | {label}"; print(f"  OK   [{label}] via {drv}"); break
                except Exception as e:
                    print(f"  FAIL [{label}] {type(e).__name__}: {str(e)[:150]}")
            if cn:
                break

    results = []; dist = Counter(); gaps = Counter()
    catalog = set()
    if cn:
        try:
            cur = cn.cursor()
            cur.execute("SELECT user_name(uid), name FROM sysobjects WHERE type='U'")
            for owner, nm in cur.fetchall():
                catalog.add(((owner or "").strip().lower(), (nm or "").strip().lower()))
            cur.close()
        except Exception as e:
            print("catalog read FAILED:", type(e).__name__, e)
        cn.close()
        print(f"catalog: {len(catalog)} user tables")
        tblnames = {t for _, t in catalog}
        for r in drows:
            sch = (r.get("d") or "").strip().lower(); tbl = (r.get("f") or "").strip().lower()
            t = (r.get("t") or "").strip().lower()
            if (sch, tbl) in catalog or tbl in tblnames:
                istat = "Done" if t != "cdc" else "Read granted, but no CDC"
                dist[istat] += 1
                results.append({"r": r["r"], "prop": {"I": istat}, "gap": None})
            else:
                gaps["MISSING_TABLE_SRC"] += 1
                results.append({"r": r["r"], "prop": {}, "gap": "MISSING_TABLE_SRC"})

    IMAP = {"Done": "D", "Read granted, but no CDC": "C"}
    lines = [f'{res["r"]}:{IMAP.get(res["prop"].get("I",""), "_")}:{"M" if res.get("gap") else "_"}'
             for res in results]
    payload = json.dumps({"utc": utc, "fmt": "ci", "kind": "prereq_sybase", "chunk": 1, "chunks": 1,
                          "rows_str": "\n".join(lines)}, ensure_ascii=False, separators=(",", ":"))
    print(f"computed {len(results)} rows  I={dict(dist)}  gaps={dict(gaps)}")
    if WEBHOOK.startswith("http"):
        status = f"connected via {used}" if cn or used else f"NOT connected ({code})"
        body = (f"Prerequisites (Sybase v3) {status}\n" + "; ".join(notes)
                + f"\nI={dict(dist)} gaps={dict(gaps)} tables={len(catalog)}")
        if results:
            body += f"\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                print("[selfpost]", post_card(f"🔑 prerequisites (Sybase v3) · {utc}", body, WEBHOOK))
            except Exception as ex:
                print("[selfpost] FAIL:", ex)
    if results:
        print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== prereq sybase v3 done (NO sheet writes) ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
