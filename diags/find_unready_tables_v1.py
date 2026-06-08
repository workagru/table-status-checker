#!/usr/bin/env python3
"""Cross-database table hunt for the ddl-generator's 'not ready' list.

Targets come from ddl-generator/uc_not_ready_table_v1.md (DW01/02/09/10),
hard-coded below as (orig_db, orig_schema, orig_table, dw). For every MSSQL
endpoint we can reach (VDI-direct + ssh-bridge tunnels), walk every
accessible database and pull (TABLE_SCHEMA, TABLE_NAME) from
INFORMATION_SCHEMA.TABLES, then for each target report:
  EXACT     name matches ci on some (server, db, schema)
  CLOSE     no exact name match, but difflib top hits (ratio>=0.78)
  not-found if nothing close

Injected: WEBHOOK, MSSQL_CREDS_JSON, TUNNEL_MAP_JSON, BRIDGE_PWD. READ-ONLY.
"""
import difflib
import json
import os
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
MSSQL_CREDS_JSON = "__MSSQL_CREDS_JSON__"
TUNNEL_MAP_JSON = "__TUNNEL_MAP_JSON__"
BRIDGE_PWD = "__BRIDGE_PWD__"
PLINK_PATH = r"C:\PuTTY\plink.exe"
BRIDGE_HOST = "10.0.135.81"
BRIDGE_USER = "debapp"
CONNECT_TIMEOUT = 6
PER_DB_TIMEOUT = 25

try:
    EXTRA = json.loads(MSSQL_CREDS_JSON)
except Exception:
    EXTRA = []
try:
    _TUNNELS = json.loads(TUNNEL_MAP_JSON) if isinstance(TUNNEL_MAP_JSON, str) else {}
    _TUNNELS = {k.lower(): int(v) for k, v in (_TUNNELS or {}).items()}
except Exception:
    _TUNNELS = {}
_PLINK_PROC = None

# (orig_db, orig_schema, orig_table, dw) — from uc_not_ready_table_v1.md
TARGETS = [
    # DW01 (16)
    ("LINQ2SIMAH", "dbo", "MESSAGE_ARCHIVE", "DW01"),
    ("LINQ2SIMAH", "dbo", "MESSAGE_OUT_BKP", "DW01"),
    ("LINQ2SIMAH_clone", "dbo", "MESSAGE_ARCHIVE", "DW01"),
    ("LINQ2SIMAH_clone", "dbo", "MESSAGE_OUT_BKP", "DW01"),
    ("SIMAHDWH", "PRS", "ACCOUNT_HIS", "DW01"),
    ("SIMAHDWH", "PRS", "PRODUCT", "DW01"),
    ("SIMAHDWH", "simah_dm", "consumer_usage", "DW01"),
    ("SIMAHDWH", "STG", "Consumer_CI_Issuance", "DW01"),
    ("SIMAHDWH", "stg", "Consumer_CI_Issuance_Enquiries", "DW01"),
    ("SIMAHDWH", "STG", "Consumer_ProductGroups", "DW01"),
    ("SIMAHDWH", "STG", "Member_Profile_Consumer", "DW01"),
    ("SIMAHDWH", "STG", "OperationMemberProfile", "DW01"),
    ("SIMAHDWH", "STG", "SAMA_Performing_Non_Performing_Data", "DW01"),
    ("SIMAHDWH", "STG", "SAMA_QuarterlyRequestedInformation", "DW01"),
    ("SIMAHDWH", "stg", "Score_Result_Master", "DW01"),
    ("SIMAHDWH", "stg", "UserList", "DW01"),
    # DW02 (7)
    ("SIMAHDWH", "COM", "COMACXA0", "DW02"),
    ("SIMAHDWH", "COM", "Commercial_CI_Issuance", "DW02"),
    ("SIMAHDWH", "COM", "Commercial_ProductGroups", "DW02"),
    ("SIMAHDWH", "COM", "Member_Profile_Commercial", "DW02"),
    ("SIMAHDWH", "PRS", "ACCOUNT_HIS_COM", "DW02"),
    ("SIMAHDWH", "SIMAH_DM", "Commercial_Usage", "DW02"),
    ("SIMAHDWH", "STG", "OperationMemberProfile", "DW02"),
    # DW09 (19)
    ("IdentityLei", "dbo", "AspNetUsers", "DW09"),
    ("IdentityLei", "dbo", "User", "DW09"),
    ("KSAPOC", "dbo", "NDP_Bulk", "DW09"),
    ("KSAPOC", "dbo", "NDP_Score", "DW09"),
    ("leid", "lei", "address", "DW09"),
    ("leid", "lei", "lei", "DW09"),
    ("leid", "lei", "RequestHistory", "DW09"),
    ("leiportal", "dbo", "LeiRequest", "DW09"),
    ("LEIPortal", "dbo", "PaymentStatusMaster", "DW09"),
    ("leiportal", "dbo", "Request", "DW09"),
    ("LEIPortal", "dbo", "RequestAddress", "DW09"),
    ("leiportal", "dbo", "RequestStatus", "DW09"),
    ("leiportal", "dbo", "RequestStatusMaster", "DW09"),
    ("LEIPortal", "dbo", "RequestTypeMaster", "DW09"),
    ("LGD", "lgd", "LoanSecurity", "DW09"),
    ("Moarif", "lei", "LeiRequest", "DW09"),
    ("Moarif", "lei", "RequestData", "DW09"),
    ("Moarif", "lei", "Status", "DW09"),
    ("SIMAH_UNIFIED", "dbo", "F_Com_Lei_Data_Loading_Stats", "DW09"),
    # DW10 (1)
    ("SIMAH_UNIFIED", "dbo", "F_Con_Salary_Certificate_Data_Loading_Stats", "DW10"),
]
TARGET_NAMES = sorted(set(t[2].lower() for t in TARGETS))   # unique table names (ci)


# ---- ssh tunnel plumbing (identical to prereq probe) -----------------
def effective_endpoint(server, port):
    key = f"{(server or '').lower()}:{int(port or 0)}"
    if key in _TUNNELS:
        return ("127.0.0.1", _TUNNELS[key])
    return (server, port)


def start_tunnels():
    global _PLINK_PROC
    if _PLINK_PROC is not None or not _TUNNELS:
        return
    import socket as _so
    if not os.path.isfile(PLINK_PATH) or not BRIDGE_PWD or BRIDGE_PWD.startswith("__"):
        print(f"[tunnel] skip"); return
    args = [PLINK_PATH, "-batch", "-ssh", "-l", BRIDGE_USER, "-pw", BRIDGE_PWD, "-N"]
    for k, lp in _TUNNELS.items():
        host, port = k.rsplit(":", 1)
        args += ["-L", f"{lp}:{host}:{port}"]
    args.append(BRIDGE_HOST)
    print(f"[tunnel] starting plink ({len(_TUNNELS)} forwards)")
    try:
        _PLINK_PROC = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        print(f"[tunnel] spawn FAIL: {e}"); _PLINK_PROC = None; return
    deadline = time.time() + 10
    for _, lp in _TUNNELS.items():
        while time.time() < deadline:
            s = _so.socket(_so.AF_INET, _so.SOCK_STREAM); s.settimeout(0.5)
            try:
                rc = s.connect_ex(("127.0.0.1", lp))
                s.close()
                if rc == 0: break
            except: pass
            time.sleep(0.3)
    print(f"[tunnel] ready pid={_PLINK_PROC.pid}")
    import atexit; atexit.register(stop_tunnels)


def stop_tunnels():
    global _PLINK_PROC
    if _PLINK_PROC is None: return
    try: _PLINK_PROC.terminate(); _PLINK_PROC.wait(timeout=3)
    except:
        try: _PLINK_PROC.kill()
        except: pass
    _PLINK_PROC = None


# ---- ad-card / endpoint building ------------------------------------
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


def build_endpoints():
    by_host = {}
    try:
        from configs.config_sources import SOURCE_PROFILES
        for nm, p in SOURCE_PROFILES.items():
            if (p.get('dialect') or '').lower() != 'mssql': continue
            if 'CHANGEME' in str(p.get('server', '')): continue
            ep = (p['server'], p.get('port', 1433))
            spec = dict(p)
            spec['driver'] = 'SQL Server'
            by_host.setdefault(ep, spec)
    except Exception:
        pass
    for c in EXTRA:
        if (c.get('dialect') or 'mssql').lower() != 'mssql': continue
        ep = (c['server'], c.get('port', 1433))
        if ep in by_host: continue
        spec = dict(c); spec['driver'] = 'SQL Server'; by_host[ep] = spec
    return by_host


def connect(spec, database):
    import pyodbc
    h, p = effective_endpoint(spec['server'], spec.get('port'))
    server = f"{h},{p}" if p else h
    cs = (f"DRIVER={{SQL Server}};SERVER={server};DATABASE={database};"
          f"UID={spec['user']};PWD={spec['password']};Network=DBMSSOCN;")
    return pyodbc.connect(cs, timeout=CONNECT_TIMEOUT, autocommit=True)


def main():
    utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"find_unready_tables v1 @ {utc}")
    print(f"targets: {len(TARGETS)} ({len(TARGET_NAMES)} unique names)")
    try:
        import pyodbc
    except Exception as e:
        print("pyodbc not available:", e); return 1

    start_tunnels()

    endpoints = build_endpoints()
    # all found entries: name_lower -> set of "server | db | schema.table"
    found_exact = defaultdict(set)
    # also keep entire (db_filter) name list per endpoint for fuzzy close
    endpoint_table_names = defaultdict(set)   # ep -> set of name_lower (across all DBs we scanned)
    endpoint_db_scanned = defaultdict(list)   # ep -> list of (db, n_tables_total)
    endpoint_fail = {}

    for ep, spec in sorted(endpoints.items()):
        ep_label = f"{ep[0]}:{ep[1]}"
        print(f"\n[{ep_label}] connecting...")
        try:
            cn = connect(spec, "master")
            cur = cn.cursor()
            cur.execute("SELECT name FROM sys.databases WHERE state_desc='ONLINE' "
                        "AND name NOT IN ('master','model','msdb','tempdb','SSISDB','distribution','ReportServer','ReportServerTempDB') "
                        "AND HAS_DBACCESS(name)=1 ORDER BY name")
            dbs = [r[0] for r in cur.fetchall()]
            cur.close(); cn.close()
            print(f"  accessible DBs: {len(dbs)}")
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:90]}"
            endpoint_fail[ep_label] = err
            print(f"  master connect FAIL: {err}")
            continue

        for db in dbs:
            try:
                cn = connect(spec, db)
                cur = cn.cursor()
                cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                            "WHERE TABLE_TYPE IN ('BASE TABLE','VIEW')")
                rows = cur.fetchall()
                cur.close(); cn.close()
            except Exception as e:
                print(f"  [{db}] scan FAIL: {type(e).__name__}: {str(e)[:60]}")
                continue
            endpoint_db_scanned[ep_label].append((db, len(rows)))
            for sch, nm in rows:
                nm_l = (nm or "").lower()
                endpoint_table_names[ep_label].add(nm_l)
                if nm_l in TARGET_NAMES:
                    found_exact[nm_l].add(f"{ep_label} | {db}.{sch}.{nm}")

    stop_tunnels()

    # Build per-target reports
    out = [f"find_unready_tables v1 @ {utc}",
           f"targets: {len(TARGETS)} ({len(TARGET_NAMES)} unique names)",
           f"endpoints scanned: {sum(1 for _ in endpoint_db_scanned)}/{len(endpoints)}"]
    for ep, dbs in endpoint_db_scanned.items():
        total = sum(n for _, n in dbs)
        out.append(f"  {ep}  scanned {len(dbs)} DBs, {total} table+view objects")
    if endpoint_fail:
        out.append("")
        out.append("Endpoints that FAILED master-connect:")
        for ep, err in endpoint_fail.items():
            out.append(f"  {ep}  {err}")

    # Pre-compute fuzzy index — union of all table names across endpoints
    all_names = set()
    for s in endpoint_table_names.values():
        all_names.update(s)
    all_names_l = list(all_names)
    print(f"\ntotal distinct table names across all endpoints: {len(all_names_l)}")

    # Per target
    sections = defaultdict(list)
    for dw_group in ("DW01", "DW02", "DW09", "DW10"):
        for orig_db, orig_schema, orig_table, dw in TARGETS:
            if dw != dw_group: continue
            tl = orig_table.lower()
            hits_exact = sorted(found_exact.get(tl, []))
            if hits_exact:
                sections[dw_group].append((orig_db, orig_schema, orig_table, "EXACT", hits_exact))
            else:
                close = difflib.get_close_matches(tl, all_names_l, n=5, cutoff=0.78)
                # for each close name, list all (endpoint|db.schema.name)
                close_hits = []
                for nm_l in close:
                    matches = [v for v in found_exact.get(nm_l, [])]
                    # if not in found_exact, build from endpoint_table_names + need to know location
                    # we only kept actual hits for TARGET_NAMES; for fuzzy we need a wider hit list.
                    close_hits.append((nm_l, matches))
                sections[dw_group].append((orig_db, orig_schema, orig_table,
                                            "CLOSE" if close else "MISS", close_hits if close else []))

    for dw_group in ("DW01", "DW02", "DW09", "DW10"):
        rows = sections.get(dw_group) or []
        if not rows: continue
        out.append("")
        out.append(f"=== {dw_group} ===")
        for orig_db, orig_schema, orig_table, status, hits in rows:
            head = f"  [{status}] {orig_db}.{orig_schema}.{orig_table}"
            out.append(head)
            if status == "EXACT":
                for h in hits[:12]:
                    out.append(f"      -> {h}")
                if len(hits) > 12: out.append(f"      ... +{len(hits)-12} more")
            elif status == "CLOSE":
                for nm_l, matches in hits:
                    if matches:
                        out.append(f"      ~ {nm_l}:")
                        for h in matches[:6]:
                            out.append(f"          {h}")
                    else:
                        out.append(f"      ~ {nm_l}  (no exact-found list; in fuzzy index only)")
            else:
                out.append(f"      (nothing similar across scanned endpoints)")

    body = "\n".join(out)
    print(body[:6000])
    if WEBHOOK.startswith("http"):
        try:
            print("[selfpost]", post_card(f"🔍 find unready tables · {utc}", body[:17000], WEBHOOK))
        except Exception as ex:
            print("[selfpost] FAIL:", ex)
    print("=== done ===")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        print("FATAL:"); traceback.print_exc(); sys.exit(1)
