#!/usr/bin/env python3
"""Read-only discovery probe for the "2 Table Status" auto-checker.

Goal: pin down WHERE on Greenplum we can read, per source table:
  - GP table existence            (Create GP table)
  - init-load / streaming metadata (IPC init load, DBZ->RMQ, RMQ->GPSS)
  - reconciliation verdicts        (Data reconciliation, from recon_meta)

Self-contained: only psycopg2 + GP UAT. NO writes. Output capped for the
20 KB Teams card. Run by the VDI mail-watcher; stdout lands in tech channel.
"""

import sys
import traceback

GP = dict(host="grnplumvipuat.ksacb.com.sa", port=5442, dbname="simah_test",
          user="gpadmin", password="gpadmin", connect_timeout=15)

# A few known targets from the sheet, one per Table type, to inspect columns.
SAMPLES = [
    ("cdc",    "lz_psa_core",      "ADMCUSM0"),
    ("cdc",    "lz_psa_mlm_dspt",  "CONSUMERDISPUTE"),
    ("lookup", "lz_mlmdsp_lkp",    "DISPUTESTATUS"),
    ("inter",  "dw_edwh",          "PRODUCT"),
]

# Name fragments that often mark stream / CDC / control / metadata objects.
PATTERNS = ["stream", "gpss", "cdc", "offset", "kafka", "rmq", "debezium",
            "meta", "ctl", "control", "audit", "load", "_log", "progress"]


def section(t):
    print("\n" + "=" * 8 + " " + t + " " + "=" * 8)


def q(cur, sql, args=None, cap=80):
    cur.execute(sql, args or ())
    rows = cur.fetchall()
    return rows[:cap], len(rows)


def main():
    print("discover_table_status_meta v1  (read-only)")
    try:
        import psycopg2
    except Exception as e:
        print("psycopg2 import FAILED:", e)
        return
    try:
        conn = psycopg2.connect(**GP)
    except Exception as e:
        print("GP connect FAILED:", e)
        return
    conn.autocommit = True
    cur = conn.cursor()

    # ---- A. schemas + table counts ----
    section("A. schemas + table counts")
    try:
        rows, n = q(cur, """
            SELECT table_schema, COUNT(*)
            FROM information_schema.tables
            WHERE table_type='BASE TABLE'
            GROUP BY table_schema ORDER BY 2 DESC
        """, cap=120)
        for sch, c in rows:
            print(f"  {c:6d}  {sch}")
    except Exception as e:
        print("  ERR:", e)

    # ---- B. objects whose schema/name look like stream/meta/control ----
    section("B. stream/meta/control candidates (name or schema match)")
    try:
        like = " OR ".join(
            ["lower(table_schema) LIKE %s OR lower(table_name) LIKE %s"] * len(PATTERNS))
        params = []
        for p in PATTERNS:
            params += [f"%{p}%", f"%{p}%"]
        rows, n = q(cur, f"""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_type='BASE TABLE' AND ({like})
            ORDER BY table_schema, table_name
        """, params, cap=80)
        print(f"  matched {n} (showing {len(rows)})")
        for sch, tbl in rows:
            print(f"    {sch}.{tbl}")
    except Exception as e:
        print("  ERR:", e)

    # ---- C. recon_meta inventory ----
    section("C. recon_meta.* inventory")
    try:
        rows, _ = q(cur, """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='recon_meta' ORDER BY table_name
        """)
        print("  tables:", ", ".join(r[0] for r in rows) or "(none)")
    except Exception as e:
        print("  ERR list:", e)
    # recon_results columns + verdict distribution + sample
    for tbl in ("recon_results", "recon_schedule"):
        try:
            cols, _ = q(cur, """
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema='recon_meta' AND table_name=%s
                ORDER BY ordinal_position
            """, (tbl,))
            print(f"  -- recon_meta.{tbl} columns:")
            for cn, dt in cols:
                print(f"       {cn} : {dt}")
        except Exception as e:
            print(f"  ERR cols {tbl}:", e)
    try:
        rows, _ = q(cur, """
            SELECT verdict, COUNT(*) FROM recon_meta.recon_results
            GROUP BY verdict ORDER BY 2 DESC
        """)
        print("  recon_results verdict distribution:")
        for v, c in rows:
            print(f"       {c:6d}  {v}")
    except Exception as e:
        print("  ERR verdicts:", e)
    try:
        rows, _ = q(cur, """
            SELECT gp_table, src_table, src_profile, tests, uc_groups
            FROM recon_meta.recon_schedule ORDER BY id
        """, cap=40)
        print(f"  recon_schedule rows (showing {len(rows)}):")
        for r in rows:
            print("      ", " | ".join(str(x) for x in r))
    except Exception as e:
        print("  ERR schedule:", e)

    # ---- D. inspect sample target tables (existence, cols, est rows) ----
    section("D. sample targets: existence + columns + est rowcount")
    for ttype, sch, tbl in SAMPLES:
        print(f"  [{ttype}] {sch}.{tbl}")
        try:
            ex, _ = q(cur, """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema=%s AND table_name=%s
            """, (sch, tbl))
            if not ex:
                # retry case-insensitive to learn the real casing
                ci, _ = q(cur, """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema=%s AND lower(table_name)=lower(%s)
                """, (sch, tbl))
                print("      exists: NO (ci match:", [r[0] for r in ci], ")")
                continue
            print("      exists: YES")
            cols, ncol = q(cur, """
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s
                ORDER BY ordinal_position
            """, (sch, tbl), cap=60)
            print(f"      {ncol} columns:", ", ".join(c for c, _ in cols))
            # fast estimate from catalog (no full scan)
            est, _ = q(cur, """
                SELECT c.reltuples::bigint
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relname=%s
            """, (sch, tbl))
            print("      est rows (reltuples):", est[0][0] if est else "?")
        except Exception as e:
            print("      ERR:", e)

    # ---- E. look for a GPSS / streaming job catalog ----
    section("E. GPSS / streaming job catalog hunt")
    try:
        rows, _ = q(cur, """
            SELECT nspname FROM pg_namespace
            WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
            ORDER BY nspname
        """, cap=200)
        print("  all schemas:", ", ".join(r[0] for r in rows))
    except Exception as e:
        print("  ERR:", e)

    cur.close()
    conn.close()
    print("\n=== discovery done ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FATAL:")
        traceback.print_exc()
