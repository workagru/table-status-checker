#!/usr/bin/env python3
"""Quick Sybase login probe — try every plausible password interpretation
of the JDBC hint Aziza shared (`Si/19-80\\@h`):
  - literal with backslash       Si/19-80\@h     (len 11)
  - URL-unescaped (\\@ -> @)      Si/19-80@h      (len 10)
  - just stripping \\ wherever    Si/19-80@h      (same as above)
  - explicit empty database (default db of GPUser1)

Uses DataDirect 8.0 Sybase Wire Protocol (confirmed installed) with
NetworkAddress=server,port (the only key the driver accepted last time).

Injected: WEBHOOK. (Password variants are hard-coded — no secret injection.)
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
SERVER = "SYBDWHUATHQ.ksacb.com.sa"
PORT = 5000
USER = "GPUser1"
DBNAME = "SIMAHDWH"
CONNECT_TIMEOUT = 10

# password variants to try in order
PWD_VARIANTS = [
    ("Si/19-80\\@h", "literal 11ch with backslash"),
    ("Si/19-80@h",   "URL-unescaped 10ch"),
    ("Si19-80@h",    "no slash 9ch"),
    ("si/19-80@h",   "lowercase s 10ch"),
]
DRIVERS = [
    "DataDirect 8.0 Sybase Wire Protocol",
    "DataDirect 7.1 Sybase Wire Protocol",
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


def try_one(drv, pwd, database, label):
    import pyodbc
    cs = (f"DRIVER={{{drv}}};NetworkAddress={SERVER},{PORT};"
          f"DATABASE={database};UID={USER};PWD={pwd};"
          f"Connect Timeout={CONNECT_TIMEOUT};LoginTimeout={CONNECT_TIMEOUT};")
    t0 = time.time()
    try:
        cn = pyodbc.connect(cs, timeout=CONNECT_TIMEOUT, autocommit=True)
        cur = cn.cursor()
        try:
            cur.execute("SELECT @@servername, suser_name(), db_name()")
            row = cur.fetchone()
            srv, login, db = (str(x) for x in row)
        except Exception:
            srv, login, db = "?", "?", database
        cur.close(); cn.close()
        dt = int((time.time()-t0)*1000)
        return f"OK   [{label:38}] {dt}ms  server={srv} login={login} db={db}"
    except Exception as e:
        dt = int((time.time()-t0)*1000)
        args = list(getattr(e, "args", []) or [])
        state = args[0] if (args and isinstance(args[0], str) and len(args[0]) == 5) else "?"
        msg = str(args[1]) if len(args) > 1 else str(e)
        return f"FAIL [{label:38}] {dt}ms  state={state}  {msg[:280]}"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"check_sybase_login v1 @ {utc}")

    # TCP first
    out = [f"check_sybase_login v1 @ {utc}",
           f"target: {SERVER}:{PORT}  db={DBNAME}  user={USER}"]
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
            post_card(f"🔐 sybase login · {utc}", body[:17000], WEBHOOK)
        return 1
    installed = list(pyodbc.drivers())
    drivers_avail = [d for d in DRIVERS if d in installed]
    out.append(f"drivers available: {drivers_avail}")
    out.append("")

    out.append("=== login attempts (target DB) ===")
    success = None
    for drv in drivers_avail:
        for pwd, desc in PWD_VARIANTS:
            label = f"{drv[:15]} | pwd={desc}"
            line = try_one(drv, pwd, DBNAME, label)
            print(line); out.append(line)
            if line.startswith("OK"):
                success = (drv, pwd, desc); break
        if success: break

    # If no DB-target success, try with master / no-db
    if not success:
        out.append("")
        out.append("=== fallback: empty DATABASE (default of GPUser1) ===")
        for drv in drivers_avail[:1]:
            for pwd, desc in PWD_VARIANTS:
                label = f"{drv[:15]} | pwd={desc} | nodb"
                line = try_one(drv, pwd, "", label)
                print(line); out.append(line)
                if line.startswith("OK"):
                    success = (drv, pwd, desc + " (no DB)"); break
            if success: break

    out.append("")
    if success:
        out.append(f"✅ SUCCESS via {success[0]}  pwd-form: {success[2]}")
    else:
        out.append("❌ All variants rejected.")

    body = "\n".join(out)
    print(body[:4000])
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔐 sybase login · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
