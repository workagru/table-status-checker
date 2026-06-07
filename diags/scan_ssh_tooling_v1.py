#!/usr/bin/env python3
"""Scan the VDI for any usable SSH plumbing — before we ask the user to
set up keys, see if Posh-SSH / plink / pre-existing keys are already there.

Checks:
  - `ssh.exe` location + version
  - `plink.exe` anywhere on PATH or common paths (PuTTY, MobaXterm)
  - `Get-Module -ListAvailable` matching '*SSH*' (Posh-SSH and friends)
  - `%USERPROFILE%\\.ssh\\` contents (existing keys / known_hosts / config)
  - `%PROGRAMFILES%`, `%PROGRAMFILES(X86)%`, `%LOCALAPPDATA%` quick scan for
    PuTTY / MobaXterm / WinSCP / Git-bash / Cygwin / WSL
  - Whether OpenSSH client option is enabled (Get-WindowsCapability)

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


def run_ps(cmd, timeout=30):
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return -1, "", f"{type(e).__name__}: {e}"


def safe(fn):
    try: return fn()
    except Exception as e: return f"EXC: {type(e).__name__}: {e}"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    out = [f"scan_ssh_tooling v1 @ {utc}", f"user: {os.environ.get('USERNAME','?')}",
           f"home: {os.environ.get('USERPROFILE','?')}", ""]

    # 1) ssh.exe
    out.append("=== ssh.exe ===")
    sshpath = shutil.which("ssh")
    out.append(f"which ssh -> {sshpath}")
    if sshpath:
        rc, so, se = run_ps(f"& '{sshpath}' -V 2>&1")
        out.append(f"  version: rc={rc} {so} {se}")

    # 2) plink.exe / PuTTY / MobaXterm anywhere
    out.append("")
    out.append("=== alt SSH binaries ===")
    for name in ("plink", "plink.exe", "putty", "kitty", "mobaxterm"):
        out.append(f"which {name} -> {shutil.which(name)}")
    # search common install dirs
    candidates = []
    for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                 os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")):
        if not base or not os.path.isdir(base): continue
        for entry in os.listdir(base)[:300]:
            low = entry.lower()
            if any(k in low for k in ("putty", "moba", "winscp", "git", "cygwin", "openssh", "wsl")):
                candidates.append(os.path.join(base, entry))
    out.append(f"common dirs hit: {candidates[:20]}")

    # 3) PowerShell SSH modules
    out.append("")
    out.append("=== PowerShell modules matching SSH ===")
    rc, so, se = run_ps("Get-Module -ListAvailable | Where-Object { $_.Name -match 'SSH' } | "
                        "Select-Object -Property Name,Version,Path | Format-Table -AutoSize | Out-String")
    out.append(so if so else "(none / empty)")
    if se: out.append(f"err: {se}")

    # 3b) any global SSH-related cmdlets
    rc, so, se = run_ps("Get-Command -Module Posh-SSH 2>$null | Select-Object -First 5 -Property Name "
                        "| Format-Table -AutoSize | Out-String")
    if so: out.append("Posh-SSH cmdlets:\n" + so)

    # 4) %USERPROFILE%\.ssh — existing keys / config
    out.append("")
    out.append("=== %USERPROFILE%\\.ssh\\ ===")
    sshdir = os.path.join(os.environ.get("USERPROFILE", ""), ".ssh")
    out.append(f"dir: {sshdir}")
    if os.path.isdir(sshdir):
        try:
            for fn in sorted(os.listdir(sshdir)):
                fp = os.path.join(sshdir, fn)
                try:
                    sz = os.path.getsize(fp)
                except Exception:
                    sz = -1
                out.append(f"  {fn}  ({sz} bytes)")
            # show config (no secrets there)
            cfgp = os.path.join(sshdir, "config")
            if os.path.isfile(cfgp):
                out.append("  --- config contents ---")
                out.append(open(cfgp, encoding="utf-8", errors="replace").read()[:1500])
        except Exception as e:
            out.append(f"  scan EXC: {e}")
    else:
        out.append("  (no .ssh dir)")

    # 5) Test PASSWORDLESS ssh to debapp@10.0.135.81 — if keys are already
    # there for this Windows user, this works without prompting
    out.append("")
    out.append("=== probe: passwordless ssh debapp@10.0.135.81 'hostname' ===")
    if sshpath:
        try:
            p = subprocess.run([sshpath, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                                "-o", "ConnectTimeout=8", "debapp@10.0.135.81", "hostname"],
                               capture_output=True, text=True, timeout=20)
            out.append(f"rc={p.returncode}")
            if p.stdout.strip(): out.append("stdout: " + p.stdout.strip()[:200])
            if p.stderr.strip(): out.append("stderr: " + p.stderr.strip()[:400])
        except Exception as e:
            out.append(f"EXC: {type(e).__name__}: {e}")
    else:
        out.append("no ssh.exe — skipped")

    body = "\n".join(out)
    print(body)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔎 ssh tooling scan · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    main()
