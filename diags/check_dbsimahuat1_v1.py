#!/usr/bin/env python3
"""DBSIMAHUAT1:1450 deep diagnostic.

memory said: "TCP ✅ login ❌ — gpuatsrvusr gets 42000". But every
cycle_prereq run shows `OperationalError ('08001',...)` — and 08001 is the
connect-level SQLSTATE, not 42000. The card truncates the error to ~55
chars so we can't tell whether 08001 = firewall / unreachable / login-fail
disguised by the legacy driver.

This probe gets the *full* error text (no truncation) for that single
endpoint, with several login + driver combos, so we can disambiguate:
  - TCP connect_ex result (independent of ODBC)
  - DNS resolution
  - ODBC: legacy {SQL Server}     gpuatsrvusr / Gp$r3viCc203345 -> Moarif
  - ODBC: legacy {SQL Server}     gpuatsrvusr / Gp$r3viCc203345 -> master
  - ODBC: legacy {SQL Server}     gpuatsrvusr / Gp$r3viCc203345 -> tempdb
  - ODBC: DataDirect 7.1 SQL Server Wire Protocol (if installed) -> master
  - ODBC: DataDirect 8.0 New SQL Server Wire Protocol (if installed) -> master

For each ODBC try, print state + full message (no [:N] truncation).

Injected: WEBHOOK. READ-ONLY.
"""
import json
import os
import socket
import subprocess
import sys
import time
import traceback

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

WEBHOOK = "__WEBHOOK__"
SERVER = "DBSIMAHUAT1.ksacb.com.sa"
PORT = 1450
USER = "gpuatsrvusr"
PWD = "Gp$r3viCc203345"
CONNECT_TIMEOUT = 8

DRIVERS = [
    "SQL Server",
    "DataDirect 7.1 SQL Server Wire Protocol",
    "DataDirect 8.0 New SQL Server Wire Protocol",
]

# (driver, database) pairs to try for the working creds
TRIES = [
    ("SQL Server",                                "master"),
    ("SQL Server",                                "tempdb"),
    ("SQL Server",                                "Moarif"),
    ("DataDirect 8.0 New SQL Server Wire Protocol", "master"),
    ("DataDirect 7.1 SQL Server Wire Protocol",   "master"),
]


def post_card(title, body, url):
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4",
                    "body": [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True},
                             {"type": "TextBlock", "text": body, "wrap": True, "fontType": "Monospace"}]}}]}
    if sys.platform == 'win32':
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


def dns():
    try:
        infos = socket.getaddrinfo(SERVER, PORT, socket.AF_INET, socket.SOCK_STREAM)
        return ", ".join(sorted({i[4][0] for i in infos}))
    except Exception as e:
        return f"err: {e}"


def tcp():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(6)
        t0 = time.time()
        rc = s.connect_ex((SERVER, PORT)); s.close()
        return f"{'OPEN' if rc == 0 else f'closed/timeout rc={rc}'} ({int((time.time()-t0)*1000)}ms)"
    except Exception as e:
        return f"err: {e}"


def try_odbc(driver, database):
    import pyodbc
    cs = (f"DRIVER={{{driver}}};SERVER={SERVER},{PORT};DATABASE={database};"
          f"UID={USER};PWD={PWD};"
          f"Encrypt=no;TrustServerCertificate=yes;Connect Timeout={CONNECT_TIMEOUT};")
    label = f"{driver[:38]:38} -> {database}"
    t0 = time.time()
    try:
        cn = pyodbc.connect(cs, timeout=CONNECT_TIMEOUT, autocommit=True)
        cur = cn.cursor(); cur.execute("SELECT @@version, suser_name(), db_name()")
        row = cur.fetchone()
        cur.close(); cn.close()
        dt = int((time.time()-t0)*1000)
        return f"OK   [{label}] {dt}ms\n   ver={str(row[0])[:120]}\n   login={row[1]} db={row[2]}"
    except Exception as e:
        dt = int((time.time()-t0)*1000)
        args = list(getattr(e, "args", []) or [])
        state = args[0] if (args and isinstance(args[0], str) and len(args[0]) == 5) else "?"
        msg = str(args[1]) if len(args) > 1 else str(e)
        # FULL message — no truncation
        return f"FAIL [{label}] {dt}ms state={state}\n   {msg}"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_dbsimahuat1 v1 @ {utc}")
    out = [f"check_dbsimahuat1 v1 @ {utc}",
           f"target: {SERVER}:{PORT}  user={USER}",
           f"DNS resolves to: {dns()}",
           f"TCP {SERVER}:{PORT} -> {tcp()}",
           ""]
    try:
        import pyodbc
    except Exception as e:
        out.append(f"pyodbc not available: {e}")
        body = "\n".join(out)
        if WEBHOOK.startswith("http"):
            post_card(f"DBSIMAHUAT1 deep · {utc}", body[:17000], WEBHOOK)
        return 1
    installed = set(pyodbc.drivers())
    out.append("relevant drivers installed: " +
               ", ".join(d for d in DRIVERS if d in installed))
    out.append("")

    out.append("=== ODBC attempts ===")
    for drv, db in TRIES:
        if drv not in installed:
            out.append(f"SKIP {drv} (not installed)")
            continue
        line = try_odbc(drv, db)
        print(line); out.append(line)
    out.append("")

    body = "\n".join(out)
    print(body[:4000])
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"DBSIMAHUAT1 deep · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
