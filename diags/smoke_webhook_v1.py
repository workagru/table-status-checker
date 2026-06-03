#!/usr/bin/env python3
"""Smoke test: self-POST one Adaptive Card to the 'table status' webhook.

Validates the whole new-channel path before we build the production
self-poster: webhook reachable from the VDI through the corp proxy, card
lands in 'table status', the extension captures it to data/table_status.jsonl.

Reuses heartbeat_v4's PowerShell-proxy POST (urllib gets 407 from the corp
proxy; PowerShell uses the current user's NTLM creds). WEBHOOK is injected
by the Mac generator from secrets.local.json (never committed).
"""
import json
import os
import sys
import time

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

WEBHOOK = "__WEBHOOK__"


def build_card(title, body):
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.4",
            "body": [
                {"type": "TextBlock", "text": title, "weight": "Bolder",
                 "size": "Medium", "wrap": True},
                {"type": "TextBlock", "text": body, "wrap": True,
                 "fontType": "Monospace"},
            ],
        }}]}


def post_via_powershell(card, url):
    import subprocess
    import tempfile
    payload = json.dumps(card, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(suffix='.json')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload.encode('utf-8'))
        ps = (
            "$proxy = [System.Net.WebRequest]::GetSystemWebProxy(); "
            "$proxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials; "
            "[System.Net.WebRequest]::DefaultWebProxy = $proxy; "
            f"$body = [System.IO.File]::ReadAllBytes('{tmp}'); "
            "try { "
            f"  Invoke-RestMethod -Uri '{url}' -Method Post "
            "    -ContentType 'application/json; charset=utf-8' -Body $body | Out-Null; "
            "  Write-Host '[teams-ps] OK' "
            "} catch { Write-Host ('[teams-ps] FAIL: ' + $_.Exception.Message); exit 1 }"
        )
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                           capture_output=True, text=True, timeout=60)
        return (200 if r.returncode == 0 else 599), (r.stdout or '').strip() + (r.stderr or '').strip()[:300]
    finally:
        try: os.remove(tmp)
        except Exception: pass


def post(card, url):
    if sys.platform == 'win32':
        return post_via_powershell(card, url)
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(card).encode('utf-8'),
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read(500).decode('utf-8', 'replace')


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"smoke_webhook v1 @ {utc}")
    if not WEBHOOK or WEBHOOK == "__WEBHOOK__":
        print("WEBHOOK not injected — abort"); return 1
    card = build_card(f"✅ table-status smoke test · {utc}",
                      "If you see this card in 'table status', the self-post path works.")
    try:
        status, resp = post(card, WEBHOOK)
        print(f"[post] status={status} resp={resp[:200]!r}")
        return 0 if status == 200 else 1
    except Exception as e:
        print(f"[post] FAILED: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
