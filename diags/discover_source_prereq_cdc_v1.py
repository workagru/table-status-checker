#!/usr/bin/env python3
"""Read-only discovery probe #2 — SOURCE side (Prerequisites + CDC-enabled).

Validates that, per the live SOURCE_PROFILES the recon loop uses, we can:
  - enumerate which source DBs we actually have profiles for (coverage),
  - on MSSQL: read CDC-enabled flags (sys.databases.is_cdc_enabled,
    sys.tables.is_tracked_by_cdc) and SELECT-permission
    (HAS_PERMS_BY_NAME) -- the two facts behind "Prerequisites",
  - on DB2 for i: read object/journal metadata via QSYS2.OBJECT_STATISTICS.

Mirrors heartbeat_v4 connection patterns. NO writes. Passwords never
printed. Per-DB connect timeout so a hung server can't starve the watcher.
Run by the VDI mail-watcher; stdout -> tech channel.
"""
import os
import sys
import time
import traceback

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

RUNTIME = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\agruzdev\AppData\Local"),
    "autorecon_runtime",
)
if os.path.isdir(RUNTIME) and RUNTIME not in sys.path:
    sys.path.insert(0, RUNTIME)

CONNECT_TIMEOUT = 8

# Known DB2 i sample (CORE) to probe journaling metadata shape.
DB2I_SAMPLE = ("CRBSAUPD", "ADMCUSM0")


def section(t):
    print("\n" + "=" * 8 + " " + t + " " + "=" * 8)


def trunc(e, n=140):
    s = f"{type(e).__name__}: {e}".replace("\n", " ")
    return s if len(s) <= n else s[:n - 3] + "..."


def load_profiles():
    try:
        from configs.config_sources import SOURCE_PROFILES, GP_PROFILES
        return dict(SOURCE_PROFILES), dict(GP_PROFILES), None
    except Exception as e:
        return {}, {}, trunc(e)


def mssql_conn_str(p):
    port = p.get('port', 1433)
    server = f"{p['server']},{port}" if port else p['server']
    return ";".join([
        f"DRIVER={{{p['driver']}}}",
        f"SERVER={server}",
        f"DATABASE={p['database']}",
        f"UID={p['user']}",
        f"PWD={p['password']}",
        f"Encrypt={p.get('encrypt', 'yes')}",
        f"TrustServerCertificate={p.get('trust_server_certificate', 'no')}",
        f"Connect Timeout={CONNECT_TIMEOUT}",
    ]) + ";"


def probe_mssql(name, p):
    print(f"\n  [MSSQL] {name}  server={p.get('server')}:{p.get('port')}  db={p.get('database')}")
    try:
        import pyodbc
    except Exception as e:
        print("    pyodbc not available:", trunc(e)); return
    conn = None
    try:
        conn = pyodbc.connect(mssql_conn_str(p), timeout=CONNECT_TIMEOUT, autocommit=True)
        cur = conn.cursor()
        cur.execute("SELECT name, is_cdc_enabled FROM sys.databases WHERE database_id = DB_ID()")
        r = cur.fetchone()
        print(f"    DB cdc_enabled: {r[1] if r else '?'}  (db={r[0] if r else '?'})")
        cur.execute("SELECT COUNT(*) FROM sys.tables WHERE is_tracked_by_cdc = 1")
        ntracked = cur.fetchone()[0]
        print(f"    tables tracked_by_cdc: {ntracked}")
        cur.execute("""
            SELECT TOP 12 SCHEMA_NAME(schema_id) AS sch, name
            FROM sys.tables WHERE is_tracked_by_cdc = 1 ORDER BY name
        """)
        sample = cur.fetchall()
        for sch, tbl in sample:
            print(f"      cdc: {sch}.{tbl}")
        # Demonstrate the access check on the first tracked table.
        if sample:
            sch, tbl = sample[0]
            cur.execute("SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'SELECT')", (f"{sch}.{tbl}",))
            print(f"    HAS_PERMS SELECT on {sch}.{tbl}: {cur.fetchone()[0]}")
        cur.close()
    except Exception as e:
        print("    ERR:", trunc(e))
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


def db2i_url(p):
    host = p['host']; port = p.get('port', 8470)
    return f"jdbc:as400://{host}:{port}" if port and port != 8470 else f"jdbc:as400://{host}"


def probe_db2i(name, p):
    print(f"\n  [DB2i] {name}  host={p.get('host')}:{p.get('port')}")
    jar = p.get('jt400_jar_path', '')
    if not jar or not os.path.exists(jar):
        print("    jt400 jar missing:", jar or "(empty)"); return
    try:
        import jaydebeapi, jpype
    except Exception as e:
        print("    jaydebeapi/jpype not available:", trunc(e)); return
    try:
        if not jpype.isJVMStarted():
            jpype.startJVM(
                jpype.getDefaultJVMPath(),
                '--add-opens=java.base/java.nio=ALL-UNNAMED',
                '--add-opens=java.base/java.util=ALL-UNNAMED',
                '--add-opens=java.base/java.lang=ALL-UNNAMED',
                '--add-opens=java.base/java.util.concurrent=ALL-UNNAMED',
                '--add-opens=java.base/sun.nio.ch=ALL-UNNAMED',
                classpath=[jar],
            )
        conn = jaydebeapi.connect(
            "com.ibm.as400.access.AS400JDBCDriver", db2i_url(p),
            [p['user'], p['password']], jar)
        cur = conn.cursor()
        lib, obj = DB2I_SAMPLE
        # OBJECT_STATISTICS exposes JOURNALED / JOURNAL_NAME columns.
        try:
            cur.execute(
                "SELECT * FROM TABLE(QSYS2.OBJECT_STATISTICS(?, '*FILE', ?)) X "
                "FETCH FIRST 1 ROW ONLY", (lib, obj))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            print(f"    OBJECT_STATISTICS({lib},{obj}) cols: {len(cols)}")
            # print only journal-related fields to keep output small
            for c, v in zip(cols, row or []):
                if any(k in c.upper() for k in ("JOURNAL", "OBJNAME", "OBJTYPE", "OBJTEXT")):
                    print(f"      {c} = {v}")
        except Exception as e:
            print("    OBJECT_STATISTICS ERR:", trunc(e))
        cur.close(); conn.close()
    except Exception as e:
        print("    ERR:", trunc(e))


def main():
    print(f"discover_source_prereq_cdc v1  (read-only)  @ {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    src, gp, err = load_profiles()
    if err:
        print("SOURCE_PROFILES load FAILED:", err)
        print("  sys.path[0] =", sys.path[0] if sys.path else "?")
        return
    section("A. profile inventory (no passwords)")
    print(f"  GP profiles: {sorted(gp)}")
    print(f"  SOURCE profiles: {len(src)}")
    by_dialect = {}
    for nm in sorted(src):
        p = src[nm]
        d = (p.get('dialect') or '?').lower()
        by_dialect.setdefault(d, []).append(nm)
        loc = p.get('server') or p.get('host') or '?'
        print(f"    {nm:24s} dialect={d:6s} loc={loc} db={p.get('database','-')}")
    print("  by dialect:", {k: len(v) for k, v in by_dialect.items()})

    section("B. MSSQL — CDC-enabled + SELECT-perm checks")
    for nm in by_dialect.get('mssql', []):
        try:
            probe_mssql(nm, src[nm])
        except Exception as e:
            print("   probe ERR:", trunc(e))

    section("C. DB2 for i — journaling metadata shape")
    for nm in by_dialect.get('db2i', []):
        try:
            probe_db2i(nm, src[nm])
        except Exception as e:
            print("   probe ERR:", trunc(e))

    print("\n=== source discovery done ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FATAL:"); traceback.print_exc()
