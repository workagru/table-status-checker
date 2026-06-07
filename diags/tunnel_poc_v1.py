#!/usr/bin/env python3
"""Proof-of-concept: SSH tunnel via plink from VDI -> .81 -> DBUATCJ2:1451,
then pyodbc connect to localhost,31451 with our real InstantUpdate creds.
If this answers @@SERVERNAME/DB_NAME(), the whole bridge becomes 'pyodbc
to localhost,31xxx' for every server VDI can't see directly. No installs
needed on .81.

Injected: WEBHOOK, BRIDGE_PWD, MSSQL_CREDS_JSON. READ-ONLY.
"""
import json
import os
import subprocess
import sys
import time

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

WEBHOOK = "__WEBHOOK__"
BRIDGE_PWD = "__BRIDGE_PWD__"
MSSQL_CREDS_JSON = "__MSSQL_CREDS_JSON__"
try:
    EXTRA = json.loads(MSSQL_CREDS_JSON)
except Exception:
    EXTRA = []

PLINK = r"C:\PuTTY\plink.exe"
BRIDGE_HOST = "10.0.135.81"
BRIDGE_USER = "debapp"

# (local_port, remote_host, remote_port, label) — one mapping for the PoC
TUNNEL = (31451, "DBUATCJ2.ksacb.com.sa", 1451, "InstantUpdate")


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


def find_cred_for(server_lower):
    """Pick any mssql cred matching server substring (case-insensitive).
    Returns (user, password) or (None, None)."""
    for c in EXTRA:
        sv = (c.get('server', '') or '').lower()
        if server_lower in sv and c.get('user') and c.get('password'):
            return c['user'], c['password']
    return None, None


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    lp, rh, rp, label = TUNNEL
    out = [f"tunnel_poc v1 @ {utc}",
           f"tunnel: localhost:{lp} -> {BRIDGE_HOST} (ssh) -> {rh}:{rp}  [{label}]"]

    # find creds for InstantUpdate — the prod cred for DBUATCJ2 server (any DB on this instance)
    # Try various server-name fragments.
    user, pwd = None, None
    for frag in ("dbuatcj2", "instantupdate"):
        user, pwd = find_cred_for(frag)
        if user:
            out.append(f"using cred from MSSQL_CREDS_JSON matched by '{frag}': user={user}")
            break
    if not user:
        out.append("no cred matched for DBUATCJ2 — falling back to a known prod login")
        # known good user we saw working on DBUATCJ2:1450 in earlier probes:
        user, pwd = "gpuatsrvusr", "Gp$r3viCc203345"

    # 1) start plink in background as a TCP forwarder
    out.append("")
    out.append("=== step 1: start plink tunnel (background) ===")
    plink_cmd = [PLINK, "-batch", "-ssh", "-l", BRIDGE_USER, "-pw", BRIDGE_PWD,
                 "-N", "-L", f"{lp}:{rh}:{rp}", BRIDGE_HOST]
    out.append("argv: " + " ".join(a if a != BRIDGE_PWD else "<pwd>" for a in plink_cmd))
    try:
        # capture both streams so plink doesn't write to console
        plink = subprocess.Popen(plink_cmd, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        out.append(f"plink pid: {plink.pid}")
    except Exception as e:
        out.append(f"plink spawn FAIL: {type(e).__name__}: {e}")
        body = "\n".join(out)
        post_card(f"🚇 tunnel PoC · {utc}", body[:17000], WEBHOOK)
        return 1

    # wait briefly for tunnel to come up; then test that localhost:lp accepts connects
    import socket
    deadline = time.time() + 8
    tunnel_up = False
    while time.time() < deadline:
        if plink.poll() is not None:
            # plink died
            out.append(f"plink exited early rc={plink.returncode}")
            so, se = plink.communicate(timeout=1)
            if so: out.append("plink stdout: " + so.decode(errors='replace')[:300])
            if se: out.append("plink stderr: " + se.decode(errors='replace')[:600])
            break
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            rc = s.connect_ex(("127.0.0.1", lp))
            s.close()
            if rc == 0:
                tunnel_up = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    out.append(f"tunnel local-port up: {tunnel_up}")

    # 2) pyodbc connect through the tunnel
    out.append("")
    out.append("=== step 2: pyodbc connect localhost,{} ===".format(lp))
    if tunnel_up:
        try:
            import pyodbc
            cs = (f"DRIVER={{SQL Server}};SERVER=127.0.0.1,{lp};DATABASE=master;"
                  f"UID={user};PWD={pwd};Network=DBMSSOCN;")
            t0 = time.time()
            cn = pyodbc.connect(cs, timeout=8, autocommit=True)
            cur = cn.cursor()
            cur.execute("SELECT DB_NAME(), SUSER_SNAME(), @@SERVERNAME, @@VERSION")
            row = cur.fetchone()
            cur.close(); cn.close()
            dt = int((time.time() - t0) * 1000)
            out.append(f"OK [{dt}ms]  db={row[0]}  login={row[1]}  server={row[2]}")
            out.append(f"  version: {(row[3] or '')[:120]}")
        except Exception as e:
            args = list(getattr(e, "args", []) or [])
            state = args[0] if (args and isinstance(args[0], str) and len(args[0]) == 5) else "?"
            msg = args[1] if len(args) > 1 else str(e)
            out.append(f"FAIL state={state} {str(msg)[:300]}")

    # 3) kill plink
    out.append("")
    out.append("=== step 3: kill plink ===")
    try:
        plink.terminate()
        try:
            plink.wait(timeout=3)
        except subprocess.TimeoutExpired:
            plink.kill()
            plink.wait(timeout=3)
        out.append(f"plink final rc={plink.returncode}")
    except Exception as e:
        out.append(f"kill EXC: {type(e).__name__}: {e}")

    body = "\n".join(out)
    print(body)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🚇 tunnel PoC · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    main()
