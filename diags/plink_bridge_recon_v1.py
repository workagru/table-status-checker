#!/usr/bin/env python3
"""SSH bridge VDI -> debapp@10.0.135.81 via plink.exe (portable PuTTY at
C:\\PuTTY\\plink.exe). Password is passed via plink's -pw flag; the first
call accepts and caches the host key (no -batch + y on stdin), the second
call runs the actual recon under -batch.

Recon (on the .81 side):
  - host info: hostname, id, ip, uname
  - available DB clients (sqlcmd / tsql / isql / python3 / pyodbc / pymssql)
  - TCP reachability to 11 target endpoints we care about

Injected: WEBHOOK, BRIDGE_PWD. READ-ONLY. No keys written, no remote files.
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
PLINK = r"C:\PuTTY\plink.exe"
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


def build_recon_script_lines():
    """A bash script (newline-separated) to feed to `bash -s` via stdin
    on .81. No quoting through argv -> no escaping headaches."""
    parts = [
        "set +e",
        "echo '==== host info ===='",
        "hostname",
        "id",
        "echo \"local IPs: $(hostname -I 2>/dev/null)\"",
        "uname -a",
        "echo",
        "echo '==== DB clients on PATH ===='",
        "for t in sqlcmd tsql isql bsqldb sqlplus python3 python ; do",
        "  p=$(command -v \"$t\" 2>/dev/null)",
        "  if [ -n \"$p\" ]; then printf '  %-10s %s\\n' \"$t\" \"$p\"",
        "  else printf '  %-10s (missing)\\n' \"$t\" ; fi",
        "done",
        "echo",
        "echo '==== Python DB libs ===='",
        "PY=$(command -v python3 || command -v python)",
        "if [ -n \"$PY\" ]; then",
        "  for mod in pyodbc pymssql pytds psycopg2 ; do",
        "    \"$PY\" -c \"import $mod; print('  $mod', getattr($mod,'__version__','?'))\" 2>&1 | head -1",
        "  done",
        "else echo '  no python on .81' ; fi",
        "echo",
        "echo '==== TCP reachability from .81 ===='",
    ]
    for host, port, label in TARGETS:
        parts.append(
            f"if timeout 4 bash -c '</dev/tcp/{host}/{port}' 2>/dev/null; then "
            f"printf '  %-8s %-32s %-5s  %s\\n' OPEN '{host}' '{port}' '{label}'; else "
            f"printf '  %-8s %-32s %-5s  %s\\n' BLOCKED '{host}' '{port}' '{label}'; fi"
        )
    parts.append("echo")
    parts.append("echo '==== done ===='")
    return "\n".join(parts) + "\n"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    out = [f"plink_bridge_recon v1 @ {utc}",
           f"plink: {PLINK}  (exists={os.path.isfile(PLINK)})",
           f"target: ssh {USER}@{HOST}"]

    if not os.path.isfile(PLINK):
        body = "\n".join(out) + "\nFATAL: plink.exe not found at expected path"
        print(body)
        if WEBHOOK.startswith("http"):
            post_card(f"🛰 plink-bridge recon · {utc}", body[:17000], WEBHOOK)
        return 1

    # STEP 1: cache host key (no -batch, accept y) — runs trivial 'exit'
    out.append("")
    out.append("=== step 1: cache host key ===")
    try:
        p1 = subprocess.run(
            [PLINK, "-ssh", "-l", USER, "-pw", BRIDGE_PWD, HOST, "exit"],
            input="y\n", capture_output=True, text=True, timeout=30)
        out.append(f"rc={p1.returncode}")
        if p1.stdout.strip(): out.append("stdout: " + p1.stdout.strip()[:600])
        if p1.stderr.strip(): out.append("stderr: " + p1.stderr.strip()[:600])
    except Exception as e:
        out.append(f"EXC: {type(e).__name__}: {e}")

    # STEP 2: actual recon — pipe the script into remote `bash` via stdin
    # (avoids all argv quoting). Plink in non-interactive command mode
    # forwards local stdin -> remote stdin.
    out.append("")
    out.append("=== step 2: recon (script via stdin) ===")
    script = build_recon_script_lines()
    out.append(f"recon script: {len(script)} chars, {script.count(chr(10))+1} lines")
    try:
        # text=False + bytes -> no CRLF mangling on Windows
        p2 = subprocess.run(
            [PLINK, "-batch", "-ssh", "-l", USER, "-pw", BRIDGE_PWD, HOST, "bash -s"],
            input=script.encode("utf-8"), capture_output=True, timeout=240)
        so = p2.stdout.decode("utf-8", errors="replace")
        se = p2.stderr.decode("utf-8", errors="replace")
        out.append(f"rc={p2.returncode}")
        if so.strip():
            out.append("--- remote stdout ---")
            out.append(so.strip())
        if se.strip():
            out.append("--- remote stderr ---")
            out.append(se.strip()[:1500])
    except Exception as e:
        out.append(f"EXC: {type(e).__name__}: {e}")

    body = "\n".join(out)
    print(body[:5000])

    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🛰 plink-bridge recon · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    main()
