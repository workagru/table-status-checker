#!/usr/bin/env python3
"""EDWH table discovery: list every (TABLE_SCHEMA, TABLE_NAME) in the
EDWH database on DQUATIDQ:1450, then reconcile against the embedded
sheet work-list (71 EDWH rows). For each sheet row, report:
  MATCH  schema.table   if (d,f) found ci
  ALT    schema.table   if found in a different schema (table name unique)
  CLOSE  schema.table   best fuzzy hit when no exact name match
  MISS                 no hint

Also dumps a per-schema table count so we can see EDWH's layout.

Injected: WORKLIST_B64, WEBHOOK, MSSQL_CREDS_JSON, TUNNEL_MAP_JSON,
BRIDGE_PWD (tunnel inert for direct-from-VDI endpoint).
READ-ONLY.
"""
import base64
import difflib
import json
import os
import sys
import time
import traceback
from collections import Counter, defaultdict

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

WORKLIST_B64 = "__WORKLIST_B64__"
WEBHOOK = "__WEBHOOK__"
MSSQL_CREDS_JSON = "__MSSQL_CREDS_JSON__"
try:
    EXTRA = json.loads(MSSQL_CREDS_JSON)
except Exception:
    EXTRA = []
CONNECT_TIMEOUT = 8

# EDWH lives on DQUATIDQ:1450, directly reachable from the VDI.
SERVER = "DQUATIDQ.ksacb.com.sa"
PORT = 1450
DBNAME = "EDWH"


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


def find_cred():
    """Pick the working DQUATIDQ:1450 cred from the injected list (or fallback
    to one of the profile entries)."""
    for c in EXTRA:
        sv = (c.get('server', '') or '').lower()
        if 'dquatidq' in sv and int(c.get('port', 0)) == PORT:
            return c
    return None


def conn_str(cred, database):
    server = f"{cred['server']},{cred['port']}"
    return ";".join([f"DRIVER={{SQL Server}}", f"SERVER={server}", f"DATABASE={database}",
                     f"UID={cred['user']}", f"PWD={cred['password']}",
                     "Encrypt=no", "TrustServerCertificate=yes",
                     f"Connect Timeout={CONNECT_TIMEOUT}"]) + ";"


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"discover_edwh_tables v1 @ {utc}")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    print(f"work-list: {len(rows)} sheet rows for EDWH")
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    cred = find_cred()
    if not cred:
        # try the profile path too — falls back to whatever the prereq usually does
        try:
            from configs.config_sources import SOURCE_PROFILES
            for nm, p in SOURCE_PROFILES.items():
                if (p.get('dialect') or '').lower() == 'mssql' \
                        and (p.get('server','') or '').lower().startswith('dquatidq'):
                    cred = dict(p); break
        except Exception:
            pass
    if not cred:
        print("FATAL: no DQUATIDQ cred available")
        return 1
    print(f"using cred: {cred['server']}:{cred.get('port')} user={cred['user']}")

    # First: connect to master and confirm EDWH is visible+USE-able
    try:
        cn = pyodbc.connect(conn_str(cred, "master"), timeout=CONNECT_TIMEOUT, autocommit=True)
        cur = cn.cursor()
        cur.execute("SELECT name, HAS_DBACCESS(name) FROM sys.databases WHERE name=?", DBNAME)
        row = cur.fetchone()
        print(f"sys.databases EDWH visible: {row is not None}  HAS_DBACCESS={row[1] if row else None}")
        cur.close(); cn.close()
    except Exception as e:
        print(f"master probe FAIL: {type(e).__name__}: {str(e)[:200]}")
        return 1

    # Connect to EDWH directly and list all tables
    try:
        cn = pyodbc.connect(conn_str(cred, DBNAME), timeout=CONNECT_TIMEOUT, autocommit=True)
    except Exception as e:
        print(f"EDWH connect FAIL: {type(e).__name__}: {str(e)[:300]}")
        return 1
    cur = cn.cursor()
    cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                "FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE IN ('BASE TABLE','VIEW') "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME")
    tables = cur.fetchall()
    cur.close(); cn.close()
    print(f"EDWH has {len(tables)} objects (tables+views)")

    # build lookup structures (ci)
    by_schema_table = {}                # (schema_l, name_l) -> (schema, name, type)
    by_name = defaultdict(list)          # name_l -> [(schema, name, type), ...]
    per_schema_count = Counter()
    for sch, nm, typ in tables:
        per_schema_count[sch] += 1
        by_schema_table[(sch.lower(), nm.lower())] = (sch, nm, typ)
        by_name[nm.lower()].append((sch, nm, typ))
    all_names = [nm for (_, nm, _) in tables]
    all_names_lower = [nm.lower() for nm in all_names]

    # Walk sheet rows: classify
    cat = Counter()
    detail = []   # list of tuples (row, sheet_schema, sheet_table, status, hint)
    for r in rows:
        sheet_schema = (r.get('d') or '').strip()
        sheet_table = (r.get('f') or '').strip()
        if not sheet_table:
            cat['no-name-in-sheet'] += 1
            detail.append((r['r'], sheet_schema, sheet_table, 'NO-NAME', ''))
            continue
        s_l = sheet_schema.lower()
        t_l = sheet_table.lower()
        hit = by_schema_table.get((s_l, t_l))
        if hit:
            cat['MATCH'] += 1
            detail.append((r['r'], sheet_schema, sheet_table, 'MATCH', f"{hit[0]}.{hit[1]} [{hit[2]}]"))
            continue
        same_name = by_name.get(t_l)
        if same_name:
            if len(same_name) == 1:
                sch, nm, typ = same_name[0]
                cat['ALT-SCHEMA'] += 1
                detail.append((r['r'], sheet_schema, sheet_table, 'ALT', f"{sch}.{nm} [{typ}]  (sheet said {sheet_schema})"))
            else:
                cat['ALT-MULTI'] += 1
                hint = ", ".join(f"{s}.{n}" for s, n, _ in same_name[:5])
                detail.append((r['r'], sheet_schema, sheet_table, 'ALT-MULTI', hint))
            continue
        # fuzzy by name only
        close = difflib.get_close_matches(t_l, all_names_lower, n=3, cutoff=0.7)
        if close:
            best = close[0]
            cands = by_name[best]
            sch, nm, typ = cands[0]
            cat['CLOSE'] += 1
            detail.append((r['r'], sheet_schema, sheet_table, 'CLOSE', f"{sch}.{nm} [{typ}]  ratio≈{difflib.SequenceMatcher(None, t_l, best).ratio():.2f}"))
        else:
            cat['MISS'] += 1
            detail.append((r['r'], sheet_schema, sheet_table, 'MISS', ''))

    # render
    out = [f"discover_edwh_tables v1 @ {utc}"]
    out.append(f"target: {SERVER}:{PORT}  DB={DBNAME}  user={cred['user']}")
    out.append(f"sheet rows (EDWH): {len(rows)}   EDWH objects: {len(tables)}")
    out.append("")
    out.append("=== per-schema table count in EDWH ===")
    for sch, n in per_schema_count.most_common():
        out.append(f"  {sch:24} {n}")
    out.append("")
    out.append(f"=== reconciliation: {dict(cat)} ===")
    # limit body — group by status, sorted by row id
    for st in ('MATCH', 'ALT', 'ALT-MULTI', 'CLOSE', 'MISS', 'NO-NAME'):
        items = [d for d in detail if d[3] == st]
        if not items:
            continue
        out.append("")
        out.append(f"--- {st} ({len(items)}) ---")
        for row_id, ss, st_, status, hint in items[:80]:
            line = f"  r{row_id:>4}  {ss}.{st_}"
            if hint: line += f"  -> {hint}"
            out.append(line)
        if len(items) > 80:
            out.append(f"  ... +{len(items)-80} more")

    body = "\n".join(out)
    print(body[:5000])
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔍 EDWH discovery · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
