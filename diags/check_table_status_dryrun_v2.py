#!/usr/bin/env python3
"""DRY-RUN status checker v2 — GP side only, READ-ONLY.

v2 over v1:
  1. Partition-aware row counts: a partitioned parent reports reltuples=0,
     so we add the summed reltuples of its inheritance children (handles
     lz_stream_* tables split into _p2026MM partitions).
  2. Auto schema-alias resolution: the sheet's GP-schema (col E) is stale
     for some rows (lz_mlmdsp_lkp vs real lz_psa_mlm_dspt_lkp). For any
     col-E schema not present in the catalog, pick the GP schema whose
     table-name set best overlaps the sheet schema's tables. Derived map
     is printed for review.
  3. AGREE/CONFLICT buckets: compare proposed vs the sheet's current value
     per stage, so real discrepancies (sheet=Done but GP missing) separate
     from resolution gaps.

Still READ-ONLY: no sheet writes, no source DBs.
"""
import base64
import json
import sys
import traceback
from collections import Counter, defaultdict

GP = dict(host="grnplumvipuat.ksacb.com.sa", port=5442, dbname="simah_test",
          user="gpadmin", password="gpadmin", connect_timeout=15)

WORKLIST_B64 = "__WORKLIST_B64__"

STAGES = ["Create GP table", "IPC init load", "DBZ->RMQ", "RMQ->GPSS", "Data recon"]
# proposed-stage -> index into cur [I,K,M,O,Q,S] = [Prereq,DBZ,Create,init,GPSS,recon]
CUR_IDX = {"Create GP table": 2, "IPC init load": 3, "DBZ->RMQ": 1,
           "RMQ->GPSS": 4, "Data recon": 5}


def stream_schema(psa):
    return "lz_stream_" + psa[len("lz_psa_"):] if psa.startswith("lz_psa_") else None


def doneness(v):
    v = (v or "").strip().lower()
    if v == "done":
        return "done"
    if v in ("", "not started"):
        return "no"
    return "other"


def main():
    print("check_table_status DRY-RUN v2  (READ-ONLY, GP side)")
    try:
        rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    except Exception as e:
        print("worklist decode FAILED:", e); return
    print(f"worklist rows: {len(rows)}")

    try:
        import psycopg2
        conn = psycopg2.connect(**GP)
    except Exception as e:
        print("GP connect FAILED:", e); return
    conn.autocommit = True
    cur = conn.cursor()

    # catalog: existing tables (lowercased) + schema->tables
    exists = set()
    schema_tables = defaultdict(set)
    cur.execute("""SELECT table_schema, table_name FROM information_schema.tables
                   WHERE table_type='BASE TABLE'""")
    for s, t in cur.fetchall():
        s, t = s.lower(), t.lower()
        exists.add((s, t)); schema_tables[s].add(t)

    # base reltuples
    reltuples = {}
    cur.execute("""SELECT n.nspname, c.relname, c.reltuples::bigint
                   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE c.relkind='r'""")
    for s, t, n in cur.fetchall():
        reltuples[(s.lower(), t.lower())] = n or 0

    # partition children sum (pg_inherits)
    partsum = defaultdict(int)
    try:
        cur.execute("""
            SELECT pn.nspname, pc.relname, SUM(cc.reltuples)::bigint
            FROM pg_inherits i
            JOIN pg_class pc ON pc.oid=i.inhparent
            JOIN pg_namespace pn ON pn.oid=pc.relnamespace
            JOIN pg_class cc ON cc.oid=i.inhrelid
            GROUP BY 1,2""")
        for s, t, n in cur.fetchall():
            partsum[(s.lower(), t.lower())] = n or 0
    except Exception as e:
        print("partition sum WARN:", e)

    def eff_rows(key):
        return max(reltuples.get(key, 0), partsum.get(key, 0))

    # recon latest verdict per gp_table
    recon = {}
    try:
        cur.execute("""SELECT s.gp_table, r.verdict, r.started_at
                       FROM recon_meta.recon_results r
                       JOIN recon_meta.recon_schedule s ON s.id=r.schedule_id""")
        latest = {}
        for gp_table, verdict, started in cur.fetchall():
            k = (gp_table or "").lower()
            at, vs = latest.get(k, (None, []))
            if at is None or (started and started > at):
                at, vs = started, [verdict]
            elif started == at:
                vs.append(verdict)
            latest[k] = (at, vs)
        for k, (_, vs) in latest.items():
            recon[k] = ("Error" if "ERROR" in vs else "Discrepancies" if "DIFF" in vs
                        else "Done" if "PASS" in vs else "Ready")
    except Exception as e:
        print("recon read WARN:", e)

    # ---- derive schema aliases by table-name overlap ----
    sheet_schema_tables = defaultdict(set)
    for r in rows:
        e = (r["e"] or "").strip().lower(); f = (r["f"] or "").strip().lower()
        if e and f:
            sheet_schema_tables[e].add(f)
    alias = {}
    for e, tabs in sheet_schema_tables.items():
        if e in schema_tables:
            continue  # col E already a real GP schema
        best, bestn = None, 0
        for gs, gt in schema_tables.items():
            n = len(tabs & gt)
            if n > bestn:
                best, bestn = gs, n
        if best and bestn >= max(1, (len(tabs) + 1) // 2):
            alias[e] = (best, bestn, len(tabs))

    print(f"GP catalog: {len(exists)} tables, {len(schema_tables)} schemas; "
          f"recon covers {len(recon)} gp_tables; derived {len(alias)} schema aliases\n")

    print("=" * 8, "derived schema aliases (col E -> real GP schema, overlap/total)", "=" * 8)
    for e in sorted(alias, key=lambda k: -alias[k][1])[:30]:
        gs, n, tot = alias[e]
        print(f"  {e:28s} -> {gs:28s} {n}/{tot}")

    # ---- per-row compute ----
    dist = {st: Counter() for st in STAGES}
    buckets = {st: Counter() for st in STAGES}
    conflict_samples = defaultdict(list)
    gaps = Counter(); gap_samples = []
    skipped = 0; NA = "N/A"

    for r in rows:
        e = (r["e"] or "").strip().lower(); f = (r["f"] or "").strip().lower()
        t = (r["t"] or "").strip().lower()
        curv = r.get("cur", [])
        if any((c or "").strip().lower() == "canceled" for c in curv):
            skipped += 1; continue

        psa = alias[e][0] if e in alias else e
        pkey = (psa, f); pexists = pkey in exists; prows = eff_rows(pkey)
        ss = stream_schema(psa); skey = (ss, f) if ss else None
        sexists = bool(skey and skey in exists); srows = eff_rows(skey) if skey else 0
        gp_table = f"{psa}.{f}"

        prop = {}
        prop["Create GP table"] = "Done" if pexists else "Not started"
        if t == "inter":
            prop["IPC init load"] = ("Done" if (pexists and prows > 0)
                                     else "Not started" if pexists else NA)
        else:
            prop["IPC init load"] = "Done" if (pexists and prows > 0) else "Not started"
        if t == "cdc":
            sv = "Done" if (sexists and srows > 0) else "Not started"
            prop["DBZ->RMQ"] = sv; prop["RMQ->GPSS"] = sv
            prop["Data recon"] = recon.get(gp_table, "(not scheduled)")
        else:
            prop["DBZ->RMQ"] = prop["RMQ->GPSS"] = prop["Data recon"] = NA

        for st in STAGES:
            p = prop[st]; dist[st][p] += 1
            if p == NA:
                continue
            cv = doneness(curv[CUR_IDX[st]] if CUR_IDX[st] < len(curv) else "")
            pv = doneness(p)
            if cv == pv:
                buckets[st]["AGREE"] += 1
            elif cv == "done" and pv == "no":
                buckets[st]["CONFLICT sheet=Done, GP=no"] += 1
                if len(conflict_samples[st]) < 12:
                    conflict_samples[st].append(f"{gp_table} [{t}]")
            elif cv == "no" and pv == "done":
                buckets[st]["CONFLICT GP=Done, sheet=no"] += 1
            else:
                buckets[st]["other"] += 1

        if not pexists:
            gaps["NO_TABLE_GP"] += 1
            if len(gap_samples) < 20:
                gap_samples.append(f"{e}->{psa}.{f} [{t}]")

    cur.close(); conn.close()

    print("\n" + "=" * 8, "proposed-status distribution", "=" * 8)
    for st in STAGES:
        print(f"  {st:16s}: " + "  ".join(f"{v}={n}" for v, n in dist[st].most_common()))

    print("\n" + "=" * 8, f"AGREE/CONFLICT vs sheet (skipped Canceled: {skipped})", "=" * 8)
    for st in STAGES:
        print(f"  {st:16s}: " + "  ".join(f"{v}={n}" for v, n in buckets[st].most_common()))

    print("\n" + "=" * 8, "CONFLICT samples (sheet=Done but GP=no)", "=" * 8)
    for st in ("Create GP table", "DBZ->RMQ", "IPC init load"):
        if conflict_samples[st]:
            print(f"  -- {st}:")
            for s in conflict_samples[st]:
                print("       ", s)

    print("\n" + "=" * 8, f"NO_TABLE_GP gap ({gaps['NO_TABLE_GP']})", "=" * 8)
    for s in gap_samples:
        print("   ", s)

    print("\n=== dry-run v2 done (NO writes) ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FATAL:"); traceback.print_exc()
