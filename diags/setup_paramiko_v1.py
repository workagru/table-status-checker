#!/usr/bin/env python3
"""Setup probe — try to install paramiko on the VDI so the ssh-bridge recon
can run. Tries pip --user with several index URLs. Reports what worked and
the Python prefix. Injected: WEBHOOK. READ-ONLY w.r.t. the sheet.
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


def have(mod):
    try:
        __import__(mod); return True
    except Exception:
        return False


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    lines = [f"setup_paramiko v1 @ {utc}"]
    lines.append(f"python: {sys.executable}  ver={sys.version.split()[0]}")
    lines.append(f"prefix: {sys.prefix}")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "PIP_INDEX_URL", "PIP_TRUSTED_HOST"):
        v = os.environ.get(k, "")
        lines.append(f"env {k}={v}")
    lines.append("")
    lines.append("Pre-check imports:")
    for mod in ("paramiko", "pyodbc", "pymssql", "pytds", "psycopg2"):
        lines.append(f"  {mod:10} -> {'OK' if have(mod) else 'MISSING'}")
    lines.append("")

    if not have("paramiko"):
        # try pip install --user paramiko
        for args in (
            [sys.executable, "-m", "pip", "install", "--user", "paramiko"],
            [sys.executable, "-m", "pip", "install", "--user", "--index-url",
             "https://pypi.org/simple", "paramiko"],
        ):
            lines.append("RUN " + " ".join(args[1:]))
            try:
                p = subprocess.run(args, capture_output=True, text=True, timeout=180)
                out = (p.stdout or "")[-1200:]
                err = (p.stderr or "")[-1200:]
                lines.append(f"rc={p.returncode}")
                if out: lines.append("stdout:\n" + out)
                if err: lines.append("stderr:\n" + err)
                if p.returncode == 0:
                    break
            except Exception as e:
                lines.append(f"EXC: {type(e).__name__}: {e}")
            lines.append("")

        # re-test import
        lines.append("Post-install import check:")
        try:
            # purge cache to pick up newly installed site-packages
            import importlib, importlib.util
            importlib.invalidate_caches()
            spec = importlib.util.find_spec("paramiko")
            lines.append(f"  paramiko spec: {spec}")
            import paramiko
            lines.append(f"  paramiko version: {paramiko.__version__}")
        except Exception as e:
            lines.append(f"  STILL FAIL: {type(e).__name__}: {e}")
    else:
        lines.append("paramiko already importable; nothing to do.")

    body = "\n".join(lines)
    print(body)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"📦 setup paramiko · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    main()
