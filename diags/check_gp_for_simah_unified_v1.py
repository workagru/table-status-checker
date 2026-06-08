#!/usr/bin/env python3
"""Check whether GP's landing layer has anything for SIMAH_UNIFIED, so the
ddl-generator can synthesize DDL from the GP-side column metadata even
though the source DBMSTRUAT login is still rejected.

For every schema on GP that contains 'unified' (anywhere), list:
  - tables (top 40)
  - row count (reltuples — fast estimate)
For the two ddl-generator targets we specifically look at:
  F_Com_Lei_Data_Loading_Stats
  F_Con_Salary_Certificate_Data_Loading_Stats
dump column list (name + type + nullable) if found anywhere.

Injected: WEBHOOK. Reads GP UAT (grnplumvipuat:5442 simah_test gpadmin/gpadmin).
READ-ONLY.
"""
import json
import os
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
GP_DSN = dict(host="grnplumvipuat.ksacb.com.sa", port=5442,
              dbname="simah_test", user="gpadmin", password="gpadmin")
TARGETS = ["F_Com_Lei_Data_Loading_Stats", "F_Con_Salary_Certificate_Data_Loading_Stats"]


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
    print(f"check_gp_for_simah_unified v1 @ {utc}")
    try:
        import psycopg2
    except Exception as e:
        print("psycopg2 not available:", e); return 1
    cn = psycopg2.connect(**GP_DSN, connect_timeout=10)
    cur = cn.cursor()

    out = [f"check_gp_for_simah_unified v1 @ {utc}",
           f"GP: {GP_DSN['host']}:{GP_DSN['port']} db={GP_DSN['dbname']}"]

    # 1) any schema containing 'unified' (case-insensitive)
    cur.execute("""
        SELECT n.nspname, COUNT(c.oid) AS n_objects
        FROM pg_namespace n
        LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r','v','f','p')
        WHERE LOWER(n.nspname) LIKE '%unified%'
        GROUP BY n.nspname
        ORDER BY n.nspname
    """)
    schemas = cur.fetchall()
    out.append("")
    out.append(f"=== GP schemas matching '%unified%': {len(schemas)} ===")
    for sch, n in schemas:
        out.append(f"  {sch:40} {n} objects")

    # 2) for each such schema, list table names (first 40)
    out.append("")
    out.append("=== tables per matching schema ===")
    for sch, n in schemas:
        cur.execute("""
            SELECT relname FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relkind IN ('r','v','f','p')
            ORDER BY relname
        """, (sch,))
        tabs = [r[0] for r in cur.fetchall()]
        out.append(f"  [{sch}] {len(tabs)} tables")
        for t in tabs[:40]:
            out.append(f"    {t}")
        if len(tabs) > 40:
            out.append(f"    ... +{len(tabs)-40} more")

    # 3) for the two ddl-generator targets, find ANY matching table name (ci) anywhere in GP
    out.append("")
    out.append("=== specific ddl-generator targets ===")
    for target in TARGETS:
        out.append(f"\n--- '{target}' ---")
        cur.execute("""
            SELECT n.nspname, c.relname, c.relkind
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind IN ('r','v','f','p')
              AND LOWER(c.relname) = LOWER(%s)
            ORDER BY n.nspname, c.relname
        """, (target,))
        exact = cur.fetchall()
        if exact:
            out.append(f"  EXACT name matches: {len(exact)}")
            for sch, nm, kind in exact:
                out.append(f"    {sch}.{nm}  [{kind}]")
            # for the first hit, dump columns
            sch, nm, _ = exact[0]
            cur.execute("""
                SELECT column_name, data_type, is_nullable, character_maximum_length, numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE LOWER(table_schema)=LOWER(%s) AND LOWER(table_name)=LOWER(%s)
                ORDER BY ordinal_position
            """, (sch, nm))
            cols = cur.fetchall()
            out.append(f"  columns of {sch}.{nm} ({len(cols)}):")
            for c in cols:
                cn_, dt, nu, mlen, prec, scale = c
                tspec = dt
                if mlen: tspec = f"{dt}({mlen})"
                elif prec is not None: tspec = f"{dt}({prec},{scale or 0})"
                out.append(f"    {cn_:36} {tspec:24} {'NULL' if nu=='YES' else 'NOT NULL'}")
            continue
        # fuzzy: any name with the same prefix
        prefix = target.split('_')[0]   # F_Com or F_Con
        cur.execute("""
            SELECT n.nspname, c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind IN ('r','v','f','p')
              AND LOWER(c.relname) LIKE LOWER(%s)
            ORDER BY n.nspname, c.relname
            LIMIT 25
        """, (prefix + '%',))
        fuzzy = cur.fetchall()
        if fuzzy:
            out.append(f"  no exact, but {len(fuzzy)} names starting with '{prefix}_*':")
            for sch, nm in fuzzy:
                out.append(f"    {sch}.{nm}")
        else:
            out.append(f"  not found anywhere in GP (no '{prefix}_*' either)")

    cur.close(); cn.close()
    body = "\n".join(out)
    print(body[:6000])
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🧬 GP unified lookup · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
