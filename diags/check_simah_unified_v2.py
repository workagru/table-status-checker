#!/usr/bin/env python3
"""Sharper SIMAH_UNIFIED probe — pin down 18456 substate.

Attempts, on DBMSTRUAT.ksacb.com.sa:1450 via the legacy '{SQL Server}' driver:
  1) UID=gpuatsrvusr + RIGHT pwd + DATABASE=SIMAH_UNIFIED
  2) UID=gpuatsrvusr + RIGHT pwd + DATABASE=master
  3) UID=gpuatsrvusr + RIGHT pwd + NO DATABASE  -> logs into default db
  4) UID=gpuatsrvusr + WRONG pwd + DATABASE=master  (compare error)
  5) UID=__nope__    + any   pwd + DATABASE=master  (compare error)

For each attempt: full pyodbc error tuple is captured (not truncated). Goal: a
substate (e.g. ' State: 38.', 'State 11', etc.) inside the message body to tell
'creds wrong' from 'db not online for this login' from 'login can't use server'.

Posts a single Adaptive Card to the table_status channel. Injected: WEBHOOK.
READ-ONLY.
"""
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

WEBHOOK = "__WEBHOOK__"
SERVER = "DBMSTRUAT.ksacb.com.sa"
PORT = 1450
RIGHT_USER = "gpuatsrvusr"
RIGHT_PWD = "QwertyEr45"
WRONG_PWD = "DEFINITELY_NOT_THE_REAL_PWD_xxxx"
NOPE_USER = "__nope_user_xyz__"
TIMEOUT = 8


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
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def try_connect(label, uid, pwd, database):
    """Return a verdict line + (sqlstate, native, full message)."""
    import pyodbc
    parts = ["DRIVER={SQL Server}", f"SERVER={SERVER},{PORT}",
             f"UID={uid}", f"PWD={pwd}"]
    if database is not None:
        parts.append(f"DATABASE={database}")
    parts.append("Network=DBMSSOCN")  # force TCP/IP on legacy driver
    cs = ";".join(parts) + ";"

    t0 = time.time()
    try:
        cn = pyodbc.connect(cs, timeout=TIMEOUT, autocommit=True)
        cur = cn.cursor()
        cur.execute("SELECT DB_NAME(), SUSER_SNAME(), @@SERVERNAME")
        row = cur.fetchone(); cur.close(); cn.close()
        dt = int((time.time() - t0) * 1000)
        return (f"OK   [{label:32}] {dt}ms  -> db={row[0]} login={row[1]} server={row[2]}",
                "00000", 0, "ok")
    except Exception as e:
        dt = int((time.time() - t0) * 1000)
        sqlstate, native, msg = "?", "?", ""
        args = list(getattr(e, "args", []) or [])
        if args and isinstance(args[0], str) and len(args[0]) == 5:
            sqlstate = args[0]
        if len(args) > 1:
            msg = str(args[1])
        # try to extract native error like '(18456)'
        import re
        m = re.search(r"\((\d{4,6})\)", msg)
        if m:
            native = m.group(1)
        return (f"FAIL [{label:32}] {dt}ms  SQLSTATE={sqlstate} native={native}\n     {msg}",
                sqlstate, native, msg)


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_simah_unified v2 @ {utc}")
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1
    print("installed drivers:", list(pyodbc.drivers()))

    plan = [
        ("right pwd, DB=SIMAH_UNIFIED", RIGHT_USER, RIGHT_PWD, "SIMAH_UNIFIED"),
        ("right pwd, DB=master       ", RIGHT_USER, RIGHT_PWD, "master"),
        ("right pwd, DB=<default>    ", RIGHT_USER, RIGHT_PWD, None),
        ("WRONG pwd, DB=master       ", RIGHT_USER, WRONG_PWD, "master"),
        ("nonexistent login          ", NOPE_USER, WRONG_PWD, "master"),
    ]
    out_lines = [f"target {SERVER}:{PORT}"]
    triples = []
    for label, uid, pwd, db in plan:
        line, st, nat, msg = try_connect(label, uid, pwd, db)
        print(line); out_lines.append(line)
        triples.append((label, st, nat, msg))

    # quick diff: do right-pwd and wrong-pwd give the same error text?
    same = (triples[1][1] == triples[3][1] and triples[1][2] == triples[3][2]
            and triples[1][3] == triples[3][3])
    out_lines.append("")
    out_lines.append(f"right-pwd vs wrong-pwd error identical?  {same}")
    out_lines.append(f"right-pwd vs no-such-login identical?    "
                     f"{triples[1][1] == triples[4][1] and triples[1][2] == triples[4][2] and triples[1][3] == triples[4][3]}")

    body = "\n".join(out_lines)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔐 SIMAH_UNIFIED probe v2 · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
