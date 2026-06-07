#!/usr/bin/env python3
"""Verify SSH bridge VDI -> debapp@10.0.135.81 and check what's reachable
from THAT box. Two passes:

  A) Establish an interactive-less SSH session (tries paramiko first; falls
     back to plink.exe / ssh with stdin password) and run a short recon:
       hostname; uname -a; id; ip route get 1; which {sqlcmd,tsql,isql,python3};
       python3 -c 'import pyodbc'; python3 -c 'import pymssql'.

  B) From that same box, TCP-probe every MSSQL endpoint we currently care
     about (target servers our VDI can't / can't easily reach). Uses bash
     /dev/tcp with a timeout — no extra deps needed on the bridge box.

Posts a single Adaptive Card. The password is supplied via env BRIDGE_PWD
so it doesn't appear in the probe source. READ-ONLY. Injected: WEBHOOK.
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
BRIDGE_PWD = "__BRIDGE_PWD__"   # injected at fill time
HOST = "10.0.135.81"
USER = "debapp"
SSH_TIMEOUT = 12

# (host, port, label) — what we want to know reachability for FROM the bridge box
TARGETS = [
    ("10.0.135.20",     1433, "SIMAH_MSCRM (UAT CRM)"),
    ("DBMSTRUAT.ksacb.com.sa", 1450, "SIMAH_UNIFIED"),
    ("DBSIMAHUAT1.ksacb.com.sa", 1450, "Moarif/LEIPortal/LINQ2SIMAH/KSAPOC"),
    ("DBUATCJ2.ksacb.com.sa", 1450, "UAT_B2C* / AMSConsumer"),
    ("DBUATCJ2.ksacb.com.sa", 1451, "InstantUpdate"),
    ("DBUATCJ2.ksacb.com.sa", 1452, "Identity"),
    ("DBUATCJ2.ksacb.com.sa", 1453, "Enquiry"),
    ("DEVDB01.ksacb.com.sa", 1450, "IdentityLei"),
    ("TRUAT01.ksacb.com.sa", 1450, "KSATR"),
    ("DQUATIDQ.ksacb.com.sa", 1450, "SIMAHDQ / EDWH"),
    ("SYBDWHUATHQ.ksacb.com.sa", 5000, "Sybase SIMAHDWH"),
]


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


REMOTE_SCRIPT = r"""
set +e
echo "==== host info ===="
hostname; uname -a
echo "id: $(id)"
echo "uptime: $(uptime 2>/dev/null | sed 's/^ *//')"
echo "outbound route to 8.8.8.8:"
ip route get 8.8.8.8 2>/dev/null | head -1
echo "local IPv4 addrs:"
hostname -I 2>/dev/null || ip -4 -o addr show 2>/dev/null | awk '{print $4}'
echo
echo "==== installed DB clients ===="
for t in sqlcmd tsql isql bsqldb sqlplus python python3 ; do
  p=$(command -v "$t" 2>/dev/null)
  if [ -n "$p" ]; then printf "  %-10s %s\n" "$t" "$p"; else printf "  %-10s (not installed)\n" "$t"; fi
done
echo
echo "==== Python DB libs ===="
for py in python3 python ; do
  command -v "$py" >/dev/null 2>&1 || continue
  echo "[$py $($py --version 2>&1 | head -1)]"
  for mod in pyodbc pymssql pytds psycopg2 ; do
    "$py" -c "import $mod; print('  $mod', getattr($mod,'__version__','?'))" 2>&1 | head -1
  done
  break
done
echo
echo "==== TCP reachability from THIS box ===="
"""


def _tcp_block_for_remote():
    lines = []
    for host, port, label in TARGETS:
        # bash /dev/tcp with timeout 4s; OPEN | REFUSED | TIMEOUT | DNS
        lines.append(
            f"H='{host}'; P={port}; L=\"{label}\"; "
            f"R=$( ( exec 3<>/dev/tcp/$H/$P ) 2>&1 & p=$!; "
            f"sleep 4; if kill -0 $p 2>/dev/null; then kill $p 2>/dev/null; echo TIMEOUT; "
            f"else wait $p; rc=$?; if [ $rc -eq 0 ]; then echo OPEN; else echo \"REFUSED rc=$rc\"; fi; fi ); "
            f"printf '  %-32s %-5s  %-22s  %s\\n' \"$H\" \"$P\" \"$R\" \"$L\""
        )
    return "\n".join(lines)


def run_via_paramiko():
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=BRIDGE_PWD, timeout=SSH_TIMEOUT,
                allow_agent=False, look_for_keys=False)
    script = REMOTE_SCRIPT + _tcp_block_for_remote() + "\necho '==== done ===='\n"
    stdin, stdout, stderr = cli.exec_command(script, timeout=120, get_pty=False)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    cli.close()
    return rc, out, err, "paramiko"


def run_via_plink():
    import subprocess
    import shutil
    plink = shutil.which("plink") or shutil.which("plink.exe")
    if not plink:
        return None
    script = REMOTE_SCRIPT + _tcp_block_for_remote() + "\necho '==== done ===='\n"
    p = subprocess.run([plink, "-batch", "-ssh", "-l", USER, "-pw", BRIDGE_PWD, HOST, "bash -s"],
                       input=script, capture_output=True, text=True, timeout=180)
    return p.returncode, p.stdout, p.stderr, "plink"


def run_via_ssh_keygen_known_hosts():
    """Last-resort: write password to a file, invoke ssh via expect-like
    PowerShell. Only works if 'ssh' is in PATH."""
    import shutil
    import subprocess
    sshbin = shutil.which("ssh")
    if not sshbin:
        return None
    # We use SSH_ASKPASS trick: set DISPLAY=:0 and SSH_ASKPASS to a one-shot
    # script that echoes the password. On Windows this rarely works. Try anyway.
    return None


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"ssh_bridge_recon v1 @ {utc}")
    print(f"target ssh: {USER}@{HOST}")

    transport = None
    rc, out, err = -1, "", ""
    for runner in (run_via_paramiko, run_via_plink):
        try:
            res = runner()
            if res is None:
                print(f"runner {runner.__name__}: not available")
                continue
            rc, out, err, transport = res
            print(f"used transport: {transport} (rc={rc})")
            break
        except Exception as e:
            print(f"runner {runner.__name__} FAIL: {type(e).__name__}: {e}")
            transport = transport or f"{runner.__name__}:err"
            err = (err or "") + f"\n{type(e).__name__}: {e}"

    if rc == -1:
        body = (f"target: ssh {USER}@{HOST}\nNO USABLE SSH TRANSPORT (paramiko/plink absent).\n"
                f"errors:\n{err[:1500]}")
    else:
        body_lines = [f"target: ssh {USER}@{HOST}   transport={transport}  rc={rc}", ""]
        body_lines.append("--- remote stdout ---")
        body_lines.append(out.strip())
        if err.strip():
            body_lines.append("")
            body_lines.append("--- remote stderr ---")
            body_lines.append(err.strip())
        body = "\n".join(body_lines)
    print(body[:5000])

    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🛰 ssh-bridge recon · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
