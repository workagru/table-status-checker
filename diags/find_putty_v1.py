#!/usr/bin/env python3
"""Hunt for PuTTY / plink on the VDI — not just PATH but everywhere.
PUTTY.RND artifact suggests PuTTY was installed at some point. If plink.exe
is here, we can do programmatic SSH with -pw password and skip ssh-keygen.

Checks:
  - PATH search
  - Common install locations (Program Files / Program Files (x86) / LocalAppData /
    Roaming / Public / Chocolatey / Scoop / Tools / Apps)
  - Registry (App Paths, Uninstall)
  - Recursive scan up to depth 3 in those roots for plink.exe / putty.exe

Posts an Adaptive Card. READ-ONLY. Injected: WEBHOOK.
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


TARGETS = {"plink.exe", "putty.exe", "pscp.exe", "psftp.exe", "puttygen.exe", "kitty.exe", "kitty_portable.exe"}


def scan_dir(root, max_depth=4, found=None):
    """Walk root up to max_depth, collecting paths whose basename is in TARGETS."""
    if found is None:
        found = []
    if not os.path.isdir(root):
        return found
    root = os.path.abspath(root)
    base_depth = root.count(os.sep)
    try:
        for dirpath, dirs, files in os.walk(root):
            depth = dirpath.count(os.sep) - base_depth
            if depth > max_depth:
                dirs[:] = []
                continue
            # skip noise dirs that explode the walk
            dirs[:] = [d for d in dirs if d.lower() not in (
                "windowsapps", "winsxs", "assembly", "node_modules", "$recycle.bin",
                "ai overviews", "system volume information", "installer", "drivers",
                "amd64_microsoft", "amd64_netfx", "msoffice", "office", "officeshared")]
            for f in files:
                if f.lower() in TARGETS:
                    found.append(os.path.join(dirpath, f))
    except Exception:
        pass
    return found


def reg_query():
    """Look in registry App Paths + Uninstall for putty traces."""
    queries = [
        r'Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\*" '
        r'-ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match "putty|plink|kitty" } '
        r'| Select-Object PSChildName, "(default)" | Format-Table -AutoSize | Out-String',
        r'Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" '
        r'-ErrorAction SilentlyContinue | ForEach-Object { Get-ItemProperty $_.PSPath } '
        r'| Where-Object { $_.DisplayName -match "PuTTY|KiTTY|plink" } '
        r'| Select-Object DisplayName, InstallLocation, Publisher | Format-Table -AutoSize | Out-String',
        r'Get-ChildItem "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall" '
        r'-ErrorAction SilentlyContinue | ForEach-Object { Get-ItemProperty $_.PSPath } '
        r'| Where-Object { $_.DisplayName -match "PuTTY|KiTTY|plink" } '
        r'| Select-Object DisplayName, InstallLocation, Publisher | Format-Table -AutoSize | Out-String',
    ]
    out = []
    for q in queries:
        try:
            p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", q],
                               capture_output=True, text=True, timeout=30)
            if p.stdout.strip():
                out.append(p.stdout.strip())
        except Exception as e:
            out.append(f"reg query EXC: {type(e).__name__}: {e}")
    return out


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    lines = [f"find_putty v1 @ {utc}"]

    # PATH search
    lines.append("")
    lines.append("=== PATH search ===")
    for nm in ("plink", "plink.exe", "putty", "putty.exe", "pscp", "psftp", "puttygen"):
        lines.append(f"  shutil.which({nm!r}) = {shutil.which(nm)}")

    # PUTTY.RND location confirms LocalAppData hit; also worth scanning more
    lines.append("")
    lines.append("=== PUTTY.RND markers ===")
    for d in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", ""),
              os.environ.get("USERPROFILE", "")):
        rnd = os.path.join(d, "PUTTY.RND") if d else ""
        if rnd and os.path.isfile(rnd):
            lines.append(f"  FOUND: {rnd}  (sz={os.path.getsize(rnd)})")
        elif rnd:
            lines.append(f"  not in: {d}")

    # broad fs walk
    roots = list(filter(None, [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
        r"C:\Users\Public",
        r"C:\Tools",
        r"C:\Apps",
        r"C:\PuTTY",
        r"C:\Program Files\PuTTY",
        r"C:\Program Files (x86)\PuTTY",
        r"C:\ProgramData\chocolatey\bin",
        r"C:\ProgramData\scoop",
        os.path.join(os.environ.get("USERPROFILE", ""), "scoop"),
        os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin"),
    ]))
    found = []
    for r in roots:
        scan_dir(r, max_depth=4, found=found)
    lines.append("")
    lines.append(f"=== filesystem scan ({len(roots)} roots, depth=4) ===")
    if not found:
        lines.append("  no plink/putty exe found in scanned roots")
    else:
        for p in found:
            lines.append(f"  {p}")

    # registry
    lines.append("")
    lines.append("=== registry (Uninstall + AppPaths) ===")
    for blk in reg_query():
        lines.append(blk)

    # if we found plink anywhere, attempt a NON-DESTRUCTIVE version probe
    plink_paths = [p for p in found if os.path.basename(p).lower() == "plink.exe"]
    if plink_paths:
        lines.append("")
        lines.append("=== plink.exe -V ===")
        try:
            p = subprocess.run([plink_paths[0], "-V"], capture_output=True, text=True, timeout=10)
            lines.append(f"rc={p.returncode}  stdout={p.stdout.strip()[:200]}  stderr={p.stderr.strip()[:200]}")
        except Exception as e:
            lines.append(f"EXC: {type(e).__name__}: {e}")

    body = "\n".join(lines)
    print(body)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔎 find putty/plink · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    main()
