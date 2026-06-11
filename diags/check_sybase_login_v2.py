#!/usr/bin/env python3
r"""Sybase login probe v2 — confirmed working password Si/19-80\@h.

v1 finding (2026-06-11 09:05): with literal `Si/19-80\@h` (11 chars incl
backslash) and DataDirect 8.0/7.1 Sybase Wire Protocol, login itself
SUCCEEDS but SQLDriverConnect fails with `42S22 ... SQL Anywhere Error
-143: Column '@@maxpagesize' not found` + `-265: Procedure 'sp_server_info'
not found`. The remote engine is SAP SQL Anywhere (negative SQLCODE,
"SQL Anywhere" error tag), not Sybase ASE — DataDirect's post-connect
ASE-version probe doesn't find ASE system objects.

This probe tries a matrix of DataDirect connection-string knobs that
might suppress the post-connect ASE probe or switch TDS dialect:
  - WireProtocolMode=1 (TDS 4.2 — pre-ASE-version-probe era)
  - WireProtocolMode=2 (TDS 5.0)
  - WireProtocolMode=4 (newer)
  - DATABASE=master  (default ASE db; maybe @@maxpagesize lives there)
  - OptimizedPrepare=0; ProcedureRetResults=0
  - EnableDescribeParam=0
  - Connection Reset=0
  - ApplicationName=Informatica (some servers gate features by app)

For each successful connect we try a minimal query `SELECT 1` so we can
distinguish "connect OK, query OK" vs "connect raised but session was
half-up".

Also tries the Informatica Data Services ODBC Driver 10.5.6 — it's a
wrapper that may route through SQL Anywhere differently.

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
SERVER = "SYBDWHUATHQ.ksacb.com.sa"
PORT = 5000
USER = "GPUser1"
PWD = "Si/19-80\\@h"  # 11 chars, literal incl backslash — confirmed working in v1
CONNECT_TIMEOUT = 10

# (label, driver, extra-kvs, database)
COMBOS = [
    # baseline (= v1) — should reproduce the 42S22 finding
    ("baseline-DD8.0",                "DataDirect 8.0 Sybase Wire Protocol", {}, "SIMAHDWH"),
    # WireProtocolMode variations
    ("DD8.0-WPM1-tds42",              "DataDirect 8.0 Sybase Wire Protocol", {"WireProtocolMode": "1"}, "SIMAHDWH"),
    ("DD8.0-WPM2-tds50",              "DataDirect 8.0 Sybase Wire Protocol", {"WireProtocolMode": "2"}, "SIMAHDWH"),
    ("DD8.0-WPM3",                    "DataDirect 8.0 Sybase Wire Protocol", {"WireProtocolMode": "3"}, "SIMAHDWH"),
    ("DD8.0-WPM4",                    "DataDirect 8.0 Sybase Wire Protocol", {"WireProtocolMode": "4"}, "SIMAHDWH"),
    # try master DB (ASE system DB)
    ("DD8.0-master",                  "DataDirect 8.0 Sybase Wire Protocol", {}, "master"),
    ("DD8.0-WPM1-master",             "DataDirect 8.0 Sybase Wire Protocol", {"WireProtocolMode": "1"}, "master"),
    # DataDirect knobs to suppress prepare/describe
    ("DD8.0-OptPrepare-0",            "DataDirect 8.0 Sybase Wire Protocol", {"OptimizedPrepare": "0"}, "SIMAHDWH"),
    ("DD8.0-EnableDescParam-0",       "DataDirect 8.0 Sybase Wire Protocol", {"EnableDescribeParam": "0"}, "SIMAHDWH"),
    ("DD8.0-ProcRetResults-0",        "DataDirect 8.0 Sybase Wire Protocol", {"ProcedureRetResults": "0"}, "SIMAHDWH"),
    ("DD8.0-ConnReset-0",             "DataDirect 8.0 Sybase Wire Protocol", {"Connection Reset": "0"}, "SIMAHDWH"),
    # app/charset/lang
    ("DD8.0-App-Informatica",         "DataDirect 8.0 Sybase Wire Protocol", {"ApplicationName": "Informatica"}, "SIMAHDWH"),
    ("DD8.0-Charset-iso_1",           "DataDirect 8.0 Sybase Wire Protocol", {"Charset": "iso_1"}, "SIMAHDWH"),
    # 7.1 variants (older driver — different post-connect probe?)
    ("DD7.1-baseline",                "DataDirect 7.1 Sybase Wire Protocol", {}, "SIMAHDWH"),
    ("DD7.1-WPM1-tds42",              "DataDirect 7.1 Sybase Wire Protocol", {"WireProtocolMode": "1"}, "SIMAHDWH"),
    ("DD7.1-WPM2-tds50",              "DataDirect 7.1 Sybase Wire Protocol", {"WireProtocolMode": "2"}, "SIMAHDWH"),
    ("DD7.1-master",                  "DataDirect 7.1 Sybase Wire Protocol", {}, "master"),
    # Informatica wrapper drivers
    ("Inf-DS-10.5.6",                 "Informatica Data Services ODBC Driver 10.5.6", {}, "SIMAHDWH"),
    ("Inf-DS-10.5.1",                 "Informatica Data Services ODBC Driver 10.5.1", {}, "SIMAHDWH"),
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


def build_cs(driver, database, extras):
    parts = [f"DRIVER={{{driver}}}",
             f"NetworkAddress={SERVER},{PORT}",
             f"DATABASE={database}",
             f"UID={USER}",
             f"PWD={PWD}",
             f"LoginTimeout={CONNECT_TIMEOUT}"]
    for k, v in extras.items():
        parts.append(f"{k}={v}")
    return ";".join(parts) + ";"


def try_one(label, driver, extras, database):
    import pyodbc
    cs = build_cs(driver, database, extras)
    t0 = time.time()
    try:
        cn = pyodbc.connect(cs, timeout=CONNECT_TIMEOUT, autocommit=True)
        dt_connect = int((time.time()-t0)*1000)
        # if we got here, connect succeeded — try a trivial query
        cur = cn.cursor()
        q_results = []
        for q in ("SELECT 1", "SELECT @@version", "SELECT db_name()"):
            tq = time.time()
            try:
                cur.execute(q)
                row = cur.fetchone()
                q_results.append(f"OK  {q} -> {str(row)[:90]} ({int((time.time()-tq)*1000)}ms)")
            except Exception as eq:
                q_results.append(f"err {q} -> {str(eq)[:120]}")
        cur.close(); cn.close()
        return f"OK   [{label:24}] connect={dt_connect}ms\n      " + "\n      ".join(q_results)
    except Exception as e:
        dt = int((time.time()-t0)*1000)
        args = list(getattr(e, "args", []) or [])
        state = args[0] if (args and isinstance(args[0], str) and len(args[0]) == 5) else "?"
        msg = str(args[1]) if len(args) > 1 else str(e)
        return f"FAIL [{label:24}] {dt}ms state={state} {msg[:240]}"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_sybase_login v2 @ {utc}")

    out = [f"check_sybase_login v2 @ {utc}",
           f"target: {SERVER}:{PORT}  db=SIMAHDWH  user={USER}",
           f"pwd: literal Si/19-80\\@h (len 11, confirmed in v1)",
           ""]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(5)
        rc = s.connect_ex((SERVER, PORT)); s.close()
        out.append(f"TCP -> {'OPEN' if rc == 0 else f'closed/timeout rc={rc}'}")
    except Exception as e:
        out.append(f"TCP probe err: {e}")
    out.append("")

    try:
        import pyodbc
    except Exception as e:
        out.append(f"pyodbc not available: {e}")
        body = "\n".join(out)
        if WEBHOOK.startswith("http"):
            post_card(f"sybase login v2 · {utc}", body[:17000], WEBHOOK)
        return 1
    installed = set(pyodbc.drivers())
    out.append(f"installed drivers: {len(installed)} (relevant skipped if absent)")
    out.append("")

    out.append("=== attempts ===")
    winners = []
    for label, driver, extras, database in COMBOS:
        if driver not in installed:
            out.append(f"SKIP [{label:24}] driver not installed: {driver}")
            continue
        line = try_one(label, driver, extras, database)
        print(line); out.append(line)
        if line.startswith("OK"):
            winners.append(label)
    out.append("")
    if winners:
        out.append(f"WINNERS ({len(winners)}): " + ", ".join(winners))
    else:
        out.append("No winner. All combos failed.")

    body = "\n".join(out)
    print(body[:4000])
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"sybase login v2 · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
