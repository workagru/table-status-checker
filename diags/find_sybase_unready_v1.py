#!/usr/bin/env python3
"""Sybase SIMAHDWH table hunt for the 22 ddl-generator targets (DW01: 16 +
DW02: 6 — see ddl-generator/uc_not_ready_table_v1.md). Uses the freshly
provided cred (GPUser1 / Si/19-80\\@h) and tries every plausible ODBC
driver/keyword combo. On the first connection that works, scans the
ASE catalog for table+view names and matches them against the targets
(EXACT ci + difflib CLOSE).

The legacy '{SQL Server}' driver on the VDI does NOT speak Sybase TDS.
Hopefully a DataDirect Sybase / Sybase ASE ODBC driver is installed.

Injected: WEBHOOK, SYBASE_CRED_JSON. READ-ONLY.
"""
import difflib
import json
import os
import socket
import subprocess
import sys
import time
import traceback
from collections import defaultdict

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

WEBHOOK = "__WEBHOOK__"
SYBASE_CRED_JSON = "__SYBASE_CRED_JSON__"
CONNECT_TIMEOUT = 10

try:
    SYBASE_CRED = json.loads(SYBASE_CRED_JSON)
except Exception:
    SYBASE_CRED = {}

# 22 SIMAHDWH targets (orig schema, table)
TARGETS = [
    ("PRS", "ACCOUNT_HIS"),
    ("PRS", "PRODUCT"),
    ("simah_dm", "consumer_usage"),
    ("STG", "Consumer_CI_Issuance"),
    ("stg", "Consumer_CI_Issuance_Enquiries"),
    ("STG", "Consumer_ProductGroups"),
    ("STG", "Member_Profile_Consumer"),
    ("STG", "OperationMemberProfile"),
    ("STG", "SAMA_Performing_Non_Performing_Data"),
    ("STG", "SAMA_QuarterlyRequestedInformation"),
    ("stg", "Score_Result_Master"),
    ("stg", "UserList"),
    ("COM", "COMACXA0"),
    ("COM", "Commercial_CI_Issuance"),
    ("COM", "Commercial_ProductGroups"),
    ("COM", "Member_Profile_Commercial"),
    ("PRS", "ACCOUNT_HIS_COM"),
    ("SIMAH_DM", "Commercial_Usage"),
]
TARGET_NAMES = sorted(set(t[1].lower() for t in TARGETS))


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


def build_variants(server, port, user, pwd, db):
    """Yield label + connect-string combos for various Sybase ODBC drivers
    and TDS keyword conventions."""
    drivers = [
        "Sybase ASE ODBC Driver",
        "DataDirect 8.0 SQL Server Wire Protocol",
        "DataDirect 8.0 Sybase Wire Protocol",
        "DataDirect 7.1 Sybase Wire Protocol",
        "DataDirect 6.1 Sybase Wire Protocol",
        "Adaptive Server Enterprise",
        "Sybase OEM ASE ODBC Driver",
        "{SQL Server}",  # almost certainly NO, but report the exact error if it tries
    ]
    base_uid = f"UID={user};PWD={pwd}"
    base_db  = f"DATABASE={db}"
    timeouts = f"Connect Timeout={CONNECT_TIMEOUT};LoginTimeout={CONNECT_TIMEOUT}"
    keyword_sets = [
        f"NetworkAddress={server},{port}",
        f"HostName={server};PortNumber={port}",
        f"Host={server};Port={port}",
        f"SERVER={server},{port}",
        f"ServerName={server}",
        f"NetworkLibrary=DBMSSOCN;SERVER={server},{port}",
    ]
    for drv in drivers:
        for kw in keyword_sets:
            label = f"{drv}  |  {kw.split(';')[0]}"
            cs = f"DRIVER={{{drv}}};{kw};{base_db};{base_uid};{timeouts};"
            yield label, drv, cs


def fetch_catalog(cn):
    """Try a couple of ways to list (schema/owner, table_name) on Sybase ASE."""
    cur = cn.cursor()
    out = []
    queries = [
        "SELECT user_name(o.uid) AS sch, o.name AS tname, o.type "
        "FROM sysobjects o WHERE o.type IN ('U','V') ORDER BY o.name",
        "SELECT u.name, o.name, o.type FROM sysobjects o "
        "JOIN sysusers u ON u.uid = o.uid WHERE o.type IN ('U','V')",
        "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES",
    ]
    last_err = None
    for q in queries:
        try:
            cur.execute(q)
            rows = cur.fetchall()
            for r in rows:
                out.append((str(r[0]), str(r[1]), str(r[2]) if len(r) > 2 else "U"))
            cur.close()
            return out, q, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
    cur.close()
    return out, None, last_err


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"find_sybase_unready v1 @ {utc}")
    if not SYBASE_CRED:
        print("FATAL: SYBASE_CRED_JSON empty"); return 1
    server = SYBASE_CRED.get("server", "SYBDWHUATHQ.ksacb.com.sa")
    port   = int(SYBASE_CRED.get("port", 5000))
    user   = SYBASE_CRED.get("user", "")
    pwd    = SYBASE_CRED.get("password", "")
    db     = SYBASE_CRED.get("database", "SIMAHDWH")
    pwd_tag = f"len={len(pwd)} tail=...{pwd[-4:]}" if pwd else "EMPTY"
    print(f"target: {server}:{port} db={db} user={user} pwd_tag={pwd_tag}")
    print(f"targets: {len(TARGETS)} ({len(TARGET_NAMES)} unique names)")

    # TCP first
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(5)
        rc = s.connect_ex((server, port)); s.close()
        print(f"TCP {server}:{port} -> {'OPEN' if rc == 0 else 'closed/timeout rc=' + str(rc)}")
    except Exception as e:
        print(f"TCP probe err: {e}")

    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1
    installed = list(pyodbc.drivers())
    print(f"installed drivers ({len(installed)}):")
    for d in installed:
        print(f"  {d}")

    # try variants
    cn = None
    success_label = None
    attempt_log = []
    for label, drv, cs in build_variants(server, port, user, pwd, db):
        if drv not in installed:
            attempt_log.append(f"  SKIP  [{label}]  driver not installed")
            continue
        try:
            cn_try = pyodbc.connect(cs, timeout=CONNECT_TIMEOUT, autocommit=True)
            attempt_log.append(f"  OK    [{label}]")
            cn = cn_try; success_label = label; break
        except Exception as e:
            attempt_log.append(f"  FAIL  [{label}]  {type(e).__name__}: {str(e)[:140]}")

    out = [f"find_sybase_unready v1 @ {utc}",
           f"target: {server}:{port} db={db} user={user} pwd_tag={pwd_tag}",
           f"installed ODBC drivers: {installed}",
           "",
           "=== attempts ==="]
    out.extend(attempt_log)

    if not cn:
        out.append("")
        out.append("NO CONNECTION — no working Sybase driver / connect-string on this VDI.")
        body = "\n".join(out)
        print(body[:5000])
        if WEBHOOK.startswith("http"):
            post_card(f"🔍 sybase unready hunt · {utc}", body[:17000], WEBHOOK)
        return 1

    out.append("")
    out.append(f"CONNECTED via: {success_label}")
    catalog, qu, err = fetch_catalog(cn)
    cn.close()
    if not catalog:
        out.append(f"catalog fetch FAIL: {err}")
        body = "\n".join(out)
        print(body[:5000])
        if WEBHOOK.startswith("http"):
            post_card(f"🔍 sybase unready hunt · {utc}", body[:17000], WEBHOOK)
        return 1
    out.append(f"catalog query: {qu}")
    out.append(f"catalog entries: {len(catalog)}  (BASE/VIEW)")

    # Per-schema counts
    by_schema = defaultdict(int)
    by_name = defaultdict(list)
    all_names_l = []
    for sch, nm, typ in catalog:
        by_schema[sch] += 1
        by_name[nm.lower()].append((sch, nm, typ))
        all_names_l.append(nm.lower())
    out.append("")
    out.append(f"per-schema (top 25 of {len(by_schema)}):")
    for sch, n in sorted(by_schema.items(), key=lambda x: -x[1])[:25]:
        out.append(f"  {sch:20} {n}")

    # Match each target
    out.append("")
    out.append("=== target reconciliation ===")
    for orig_schema, tab in TARGETS:
        tl = tab.lower()
        hits = by_name.get(tl, [])
        if hits:
            label = f"  EXACT  {orig_schema}.{tab}"
            for sch, nm, typ in hits[:5]:
                label += f"\n      -> {sch}.{nm} [{typ}]"
            out.append(label)
        else:
            close = difflib.get_close_matches(tl, list(set(all_names_l)), n=3, cutoff=0.78)
            if close:
                label = f"  CLOSE  {orig_schema}.{tab}"
                for nm_l in close:
                    for sch, nm, typ in by_name.get(nm_l, []):
                        label += f"\n      ~  {sch}.{nm}  [{typ}]"
                out.append(label)
            else:
                out.append(f"  MISS   {orig_schema}.{tab}")

    body = "\n".join(out)
    print(body[:6000])
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔍 sybase unready hunt · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
