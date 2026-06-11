#!/usr/bin/env python3
"""SIMAH_UNIFIED dual-path connect test.

Aziza reportedly logs in successfully with the same gpuatsrvusr/Gp$r3viCc203345.
For us, the cycle reports 28000/18456. This probe tries BOTH paths to localize:

  (A) Direct from VDI -> DBMSTRUAT:1450    (TCP path: VDI 10.0.220.28 -> dest)
  (B) Through ssh-bridge .81 -> DBMSTRUAT:1450 (TCP path: .81 10.0.135.81 -> dest)

If path B succeeds and A fails, the issue is network/route from the VDI subnet
(or per-source-IP login policy). If both fail with 28000, the login itself is
the problem (DBA reset needed). Full error text + ms timing on each attempt.

Injected: WEBHOOK, MSSQL_CREDS_JSON (to pick the real password), BRIDGE_PWD.
READ-ONLY.
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
MSSQL_CREDS_JSON = "__MSSQL_CREDS_JSON__"
BRIDGE_PWD = "__BRIDGE_PWD__"
PLINK_PATH = r"C:\PuTTY\plink.exe"
BRIDGE_HOST = "10.0.135.81"
BRIDGE_USER = "debapp"
LOCAL_TUNNEL_PORT = 31456
TARGET_SERVER = "DBMSTRUAT.ksacb.com.sa"
TARGET_PORT = 1450
TARGET_DB = "SIMAH_UNIFIED"
CONNECT_TIMEOUT = 8

try:
    EXTRA = json.loads(MSSQL_CREDS_JSON)
except Exception:
    EXTRA = []


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


def find_cred():
    for c in EXTRA:
        sv = (c.get('server','') or '').lower()
        db = (c.get('database','') or '').lower()
        if 'dbmstruat' in sv and db == 'simah_unified':
            return c
    return None


def try_connect(label, server_host, server_port, user, pwd, database):
    """Returns (line, sqlstate, native, msg). Always single line + details."""
    import pyodbc
    cs = (f"DRIVER={{SQL Server}};SERVER={server_host},{server_port};DATABASE={database};"
          f"UID={user};PWD={pwd};Network=DBMSSOCN;")
    t0 = time.time()
    try:
        cn = pyodbc.connect(cs, timeout=CONNECT_TIMEOUT, autocommit=True)
        cur = cn.cursor()
        cur.execute("SELECT DB_NAME(), SUSER_SNAME(), @@SERVERNAME, @@VERSION")
        row = cur.fetchone()
        cur.close(); cn.close()
        dt = int((time.time()-t0)*1000)
        line = (f"OK   [{label:20}] {dt}ms  -> db={row[0]} login={row[1]} "
                f"server={row[2]}\n      version: {(row[3] or '')[:140]}")
        return line, "OK", 0, ""
    except Exception as e:
        dt = int((time.time()-t0)*1000)
        args = list(getattr(e, "args", []) or [])
        state = args[0] if (args and isinstance(args[0], str) and len(args[0]) == 5) else "?"
        msg = str(args[1]) if len(args) > 1 else str(e)
        import re
        m = re.search(r"\((\d{4,6})\)", msg)
        native = int(m.group(1)) if m else 0
        line = (f"FAIL [{label:20}] {dt}ms  SQLSTATE={state} native={native}\n"
                f"      {msg[:600]}")
        return line, state, native, msg


def start_tunnel():
    args = [PLINK_PATH, "-batch", "-ssh", "-l", BRIDGE_USER, "-pw", BRIDGE_PWD, "-N",
            "-L", f"{LOCAL_TUNNEL_PORT}:{TARGET_SERVER}:{TARGET_PORT}", BRIDGE_HOST]
    print(f"[tunnel] starting plink: localhost:{LOCAL_TUNNEL_PORT} -> {BRIDGE_HOST} -> {TARGET_SERVER}:{TARGET_PORT}")
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.time() + 10
    while time.time() < deadline:
        if p.poll() is not None:
            so, se = p.communicate(timeout=1)
            return None, f"plink died rc={p.returncode}: {(se or b'').decode(errors='replace')[:300]}"
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(0.5)
        try:
            rc = s.connect_ex(("127.0.0.1", LOCAL_TUNNEL_PORT))
            s.close()
            if rc == 0:
                return p, f"up pid={p.pid}"
        except Exception:
            pass
        time.sleep(0.3)
    try: p.terminate()
    except: pass
    return None, "tunnel did not come up within 10s"


def stop_tunnel(p):
    if p is None: return
    try: p.terminate(); p.wait(timeout=3)
    except:
        try: p.kill()
        except: pass


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_simah_unified v4 (dual-path) @ {utc}")
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    cred = find_cred()
    if not cred:
        print("FATAL: no DBMSTRUAT/SIMAH_UNIFIED cred"); return 1
    user = cred['user']; pwd = cred['password']
    pwd_tag = f"len={len(pwd)} tail=...{pwd[-6:]}"
    print(f"target: {TARGET_SERVER}:{TARGET_PORT} db={TARGET_DB} user={user} pwd_tag={pwd_tag}")

    out = [f"check_simah_unified v4 (dual-path) @ {utc}",
           f"target: {TARGET_SERVER}:{TARGET_PORT}  db={TARGET_DB}  user={user}  pwd_tag={pwd_tag}",
           ""]

    # (A) direct from VDI
    out.append("=== (A) direct VDI -> DBMSTRUAT ===")
    a_line, a_state, a_native, a_msg = try_connect("VDI-direct", TARGET_SERVER, TARGET_PORT,
                                                    user, pwd, TARGET_DB)
    out.append(a_line)
    out.append("")

    # also try master on VDI direct
    a2_line, _, _, _ = try_connect("VDI-direct/master", TARGET_SERVER, TARGET_PORT,
                                    user, pwd, "master")
    out.append(a2_line)
    out.append("")

    # (B) via .81 tunnel
    out.append("=== (B) ssh-bridge .81 -> DBMSTRUAT ===")
    if not os.path.isfile(PLINK_PATH) or not BRIDGE_PWD or BRIDGE_PWD.startswith("__"):
        out.append("plink missing or BRIDGE_PWD not injected — skipping path B")
    else:
        p, t_info = start_tunnel()
        out.append(f"plink: {t_info}")
        if p is None:
            out.append("tunnel did not establish — skipping path B")
        else:
            try:
                b_line, b_state, b_native, b_msg = try_connect("via-.81", "127.0.0.1", LOCAL_TUNNEL_PORT,
                                                                user, pwd, TARGET_DB)
                out.append(b_line)
                out.append("")
                b2_line, _, _, _ = try_connect("via-.81/master", "127.0.0.1", LOCAL_TUNNEL_PORT,
                                                user, pwd, "master")
                out.append(b2_line)
            finally:
                stop_tunnel(p)

    body = "\n".join(out)
    print(body[:4000])
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔐 SIMAH_UNIFIED dual-path · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
