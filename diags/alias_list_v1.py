#!/usr/bin/env python3
"""Build the schema-alias list — 'what we have -> what we're looking for'.

For each sheet GP-schema (col E) that is NOT a real GP schema, list its
tables and find the GP schema(s) that actually contain those tables (by
table-name overlap). Emits a readable list + a ready-to-paste alias map for
the confident cases. READ-ONLY. Injected: WORKLIST_B64, WEBHOOK, ALIAS_MAP_JSON.
"""
import base64
import json
import os
import sys
import time
import traceback
from collections import defaultdict

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

GP = dict(host="grnplumvipuat.ksacb.com.sa", port=5442, dbname="simah_test",
          user="gpadmin", password="gpadmin", connect_timeout=15)
WORKLIST_B64 = "__WORKLIST_B64__"
WEBHOOK = "__WEBHOOK__"
ALIAS_MAP_JSON = "__ALIAS_MAP_JSON__"
try:
    ALIAS_MAP = json.loads(ALIAS_MAP_JSON)
except Exception:
    ALIAS_MAP = {}
MARK_BEGIN, MARK_END = "===RESULTS_JSON_BEGIN===", "===RESULTS_JSON_END==="


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


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"alias_list v1 @ {utc}")
    rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    try:
        import psycopg2
        conn = psycopg2.connect(**GP)
    except Exception as e:
        print("GP connect FAILED:", e); return 1
    conn.autocommit = True
    cur = conn.cursor()
    schema_tables = defaultdict(set)
    cur.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_type='BASE TABLE'")
    for s, t in cur.fetchall():
        schema_tables[s.lower()].add(t.lower())
    cur.close(); conn.close()

    sheet_st = defaultdict(set)
    for r in rows:
        e = (r["e"] or "").strip().lower(); f = (r["f"] or "").strip().lower()
        if e and f:
            sheet_st[e].add(f)

    lines = []; suggestions = {}
    unresolved = [e for e in sheet_st if e not in schema_tables and e not in ALIAS_MAP]
    lines.append(f"unresolved sheet-schemas (col E not a real GP schema): {len(unresolved)}")
    for e in sorted(unresolved, key=lambda k: -len(sheet_st[k])):
        tabs = sheet_st[e]
        cands = sorted(((len(tabs & gt), gs) for gs, gt in schema_tables.items() if tabs & gt), reverse=True)
        top = cands[:3]
        desc = "  ".join(f"{gs}({k}/{len(tabs)})" for k, gs in top) or "(no GP schema has these tables)"
        mark = ""
        if top:
            k, gs = top[0]
            # confident: dominant unique majority overlap
            if k >= max(2, (len(tabs) + 1) // 2) and (len(top) == 1 or top[1][0] < k):
                suggestions[e] = gs; mark = "  <= CONFIDENT"
        lines.append(f"  {e:30s} [{len(tabs):3d}] -> {desc}{mark}")

    lines.append("")
    lines.append(f"=== confident alias suggestions ({len(suggestions)}) — paste into schema_aliases.json ===")
    for e, gs in sorted(suggestions.items()):
        lines.append(f'  "{e}": "{gs}",')

    body_txt = "\n".join(lines)
    print(body_txt)
    payload = json.dumps({"utc": utc, "kind": "alias_list", "suggestions": suggestions,
                          "unresolved": len(unresolved)}, ensure_ascii=False, separators=(",", ":"))
    posted = False
    if WEBHOOK.startswith("http"):
        body = body_txt + f"\n{MARK_BEGIN}\n{payload}\n{MARK_END}"
        if len(body) <= 17000:
            try:
                posted = post_card(f"🔗 alias list · {utc}", body, WEBHOOK) == 200
            except Exception as ex:
                print("[selfpost] FAIL:", ex)
    if not posted:
        print(MARK_BEGIN); print(payload); print(MARK_END)
    print("\n=== alias list done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
