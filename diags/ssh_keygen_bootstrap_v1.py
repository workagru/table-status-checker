#!/usr/bin/env python3
"""Generate an ed25519 SSH keypair on the VDI for user agruzdev, if missing,
and post the PUBLIC half to Teams so the user can append it to
debapp@10.0.135.81:~/.ssh/authorized_keys once. The private key stays on VDI.

If the key already exists, just print it (idempotent). READ-ONLY w.r.t. the
sheet. Injected: WEBHOOK.
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
KEY_NAME = "id_ed25519"


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


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    sshdir = os.path.join(os.environ.get("USERPROFILE", ""), ".ssh")
    priv = os.path.join(sshdir, KEY_NAME)
    pub = priv + ".pub"
    lines = [f"ssh_keygen_bootstrap v1 @ {utc}",
             f"USER: {os.environ.get('USERNAME','?')}   .ssh dir: {sshdir}"]

    # ensure dir
    os.makedirs(sshdir, exist_ok=True)

    if os.path.isfile(priv) and os.path.isfile(pub):
        lines.append(f"keypair already exists: {priv}")
    else:
        lines.append(f"generating ed25519 keypair...")
        # -N "" empty passphrase ; -q quiet ; -t ed25519 ; -f path
        try:
            p = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                                "-C", f"agruzdev@VDI -> debapp@10.0.135.81 ({utc})",
                                "-f", priv], capture_output=True, text=True, timeout=60)
            lines.append(f"ssh-keygen rc={p.returncode}")
            if p.stdout: lines.append("stdout:\n" + p.stdout[:500])
            if p.stderr: lines.append("stderr:\n" + p.stderr[:500])
        except Exception as e:
            lines.append(f"EXC ssh-keygen: {type(e).__name__}: {e}")

    if not os.path.isfile(pub):
        lines.append("\nFAILED to produce .pub")
        body = "\n".join(lines)
    else:
        pubtext = open(pub, encoding="utf-8").read().strip()
        lines.append("")
        lines.append("=" * 60)
        lines.append("PUBLIC KEY (paste into debapp@10.0.135.81:~/.ssh/authorized_keys):")
        lines.append("=" * 60)
        lines.append(pubtext)
        lines.append("=" * 60)
        lines.append("")
        lines.append("One-liner to append on the remote box AFTER you ssh in once:")
        lines.append(f"  mkdir -p ~/.ssh && chmod 700 ~/.ssh && \\")
        lines.append(f"  echo '{pubtext}' >> ~/.ssh/authorized_keys && \\")
        lines.append(f"  chmod 600 ~/.ssh/authorized_keys && \\")
        lines.append(f"  echo OK")
        body = "\n".join(lines)

    print(body)
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔑 ssh-keygen bootstrap · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    main()
