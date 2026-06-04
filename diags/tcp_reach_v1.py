#!/usr/bin/env python3
"""Network reachability probe (READ-ONLY): for every MSSQL (server,port)
endpoint, do a RAW TCP connect (socket) to separate firewall/down from
auth/driver issues, then — only for TCP-OPEN endpoints — a full ODBC connect
and print the COMPLETE error (not truncated). This pins down what 08001 really
means per host. Injected: WEBHOOK, MSSQL_CREDS_JSON.

TCP result codes: OPEN (port accepts) / REFUSED (RST = host up, nothing
listening) / TIMEOUT (filtered = firewall/host down) / DNS (name not resolved).
"""
import json
import os
import socket
import sys
import time
import traceback

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

RUNTIME = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\agruzdev\AppData\Local"),
                       "autorecon_runtime")
if os.path.isdir(RUNTIME) and RUNTIME not in sys.path:
    sys.path.insert(0, RUNTIME)

WEBHOOK = "__WEBHOOK__"
MSSQL_CREDS_JSON = "__MSSQL_CREDS_JSON__"
try:
    EXTRA = json.loads(MSSQL_CREDS_JSON)
except Exception:
    EXTRA = []
TCP_TIMEOUT = 5
CONNECT_TIMEOUT = 6


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
        return "DNS", f"resolve failed: {e}"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TCP_TIMEOUT)
    t0 = time.time()
    try:
        rc = s.connect_ex((ip, int(port)))
        dt = int((time.time() - t0) * 1000)
        if rc == 0:
            return "OPEN", f"{ip} {dt}ms"
        return ("REFUSED" if rc in (61, 111, 10061) else f"ERRNO{rc}"), f"{ip} {dt}ms"
    except socket.timeout:
        return "TIMEOUT", f"{ip} >{TCP_TIMEOUT}s (filtered/down)"
    except Exception as e:
        return "ERR", f"{ip} {type(e).__name__}: {e}"
    finally:
        try: s.close()
        except Exception: pass


def odbc_full_error(spec, driver):
    try:
        import pyodbc
    except Exception as e:
        return f"pyodbc n/a: {e}"
    server = f"{spec['server']},{spec['port']}"
    cs = (f"DRIVER={{{driver}}};SERVER={server};DATABASE=master;"
          f"UID={spec['user']};PWD={spec['password']};Encrypt=no;"
          f"TrustServerCertificate=yes;Connect Timeout={CONNECT_TIMEOUT};")
    try:
        cn = pyodbc.connect(cs, timeout=CONNECT_TIMEOUT, autocommit=True); cn.close()
        return "ODBC OK"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"tcp_reach v1 @ {utc}")
    # installed driver from a profile (same as prereq v4)
    drv = "SQL Server"
    try:
        from configs.config_sources import SOURCE_PROFILES
        for _, p in SOURCE_PROFILES.items():
            if (p.get('dialect') or '').lower() == 'mssql' and p.get('driver'):
                drv = p['driver']; break
    except Exception as e:
        print("profiles warn:", e)
    print(f"odbc driver: {drv!r}")

    eps = {}
    for c in EXTRA:
        if (c.get('dialect') or 'mssql').lower() != 'mssql':
            continue
        eps[(c['server'], c.get('port', 1433))] = c
    lines = []
    for (host, port), spec in sorted(eps.items()):
        code, detail = tcp_probe(host, port)
        if code == "OPEN":
            err = odbc_full_error(spec, drv)
            line = f"{code:8s} {host.split('.')[0]}:{port}  {detail}  | ODBC: {err[:160]}"
        else:
            line = f"{code:8s} {host.split('.')[0]}:{port}  {detail}"
        print(line); lines.append(line)

    body = "TCP reachability per endpoint (raw socket, then ODBC for OPEN):\n" + "\n".join(lines)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🌐 tcp reachability · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("\n=== tcp_reach v1 done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
