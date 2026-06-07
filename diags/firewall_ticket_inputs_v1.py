#!/usr/bin/env python3
"""Collect inputs for a firewall-open request from the VDI:
- this VDI's hostname + outbound IPv4 (the source for the ticket)
- each target host -> resolved IP, TCP-OPEN/REFUSED/TIMEOUT to the listed port

Posts a single Adaptive Card with one block per host so the user can paste
straight into the firewall ticket. READ-ONLY. Injected: WEBHOOK.
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

WEBHOOK = "__WEBHOOK__"
TCP_TIMEOUT = 4

# (host, port, comment) — what we want opened from VDI
TARGETS = [
    ("DBSIMAHUAT1.ksacb.com.sa", 1450, "Moarif / LEIPortal / LINQ2SIMAH / KSAPOC"),
    ("DBUATCJ2.ksacb.com.sa",    1451, "InstantUpdate"),
    ("DBUATCJ2.ksacb.com.sa",    1452, "Identity"),
    ("DBUATCJ2.ksacb.com.sa",    1453, "Enquiry"),
    ("DEVDB01.ksacb.com.sa",     1450, "IdentityLei"),
    ("TRUAT01.ksacb.com.sa",     1450, "KSATR"),
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


def outbound_ip():
    """Outbound IPv4 the OS would use to reach an external address (no packet sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "?"
    finally:
        s.close()


def all_local_ipv4():
    out = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.add(info[4][0])
    except Exception:
        pass
    return sorted(out)


def tcp_state(host, port):
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:
        return None, "DNS", f"resolve failed: {e}"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TCP_TIMEOUT)
    t0 = time.time()
    try:
        rc = s.connect_ex((ip, port))
        dt = int((time.time() - t0) * 1000)
        if rc == 0:
            s.close()
            return ip, "OPEN", f"{dt}ms"
        # rc != 0: refused vs filtered
        if rc in (61, 111, 10061):  # ECONNREFUSED variants
            return ip, "REFUSED", f"{dt}ms (host up, port closed)"
        return ip, f"err{rc}", f"{dt}ms"
    except socket.timeout:
        return ip, "TIMEOUT", f"{int((time.time() - t0) * 1000)}ms (firewall-blocked)"
    except Exception as e:
        return ip, "EXC", str(e)[:80]
    finally:
        try: s.close()
        except Exception: pass


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    hostname = socket.gethostname()
    out_ip = outbound_ip()
    all_ips = all_local_ipv4()
    print(f"firewall-ticket inputs @ {utc}")
    print(f"hostname: {hostname}")
    print(f"outbound IPv4 (route-to-internet): {out_ip}")
    print(f"all local IPv4: {', '.join(all_ips) or '?'}")

    lines = [f"VDI hostname: {hostname}",
             f"VDI outbound IP (source for ticket): {out_ip}",
             f"all local IPv4: {', '.join(all_ips) or '?'}",
             "",
             "Target host                              IP             Port   TCP        Comment"]

    for host, port, comment in TARGETS:
        ip, state, detail = tcp_state(host, port)
        line = f"{host:<40} {ip or '-':<14} {port:<6} {state:<10} {comment}  [{detail}]"
        print(line); lines.append(line)

    body = "\n".join(lines)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🧱 firewall ticket inputs · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
