#!/usr/bin/env python3
"""DRY-RUN status checker v3 — GP side only, READ-ONLY.

v3 over v2: drop the unsafe auto-alias-by-overlap (it false-matched generic
lookup names). Resolve schema STRICTLY: psa = ALIAS_MAP.get(colE, colE);
a row resolves only if (psa, table) is in the GP catalog (ci). Everything
unresolved goes to the gap report. To help BUILD the alias map (without
bugging the user), v3 dumps, for each unresolved sheet-schema, the GP
schemas that share its table names, ranked by overlap — that's the evidence
to fill schema_aliases.json. Keeps v2's partition-aware rowcount,
distribution and AGREE/CONFLICT buckets.

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
# Optional injected {sheet_schema_lower: real_gp_schema_lower}. Empty for v3.
ALIAS_MAP = {}

STAGES = ["Create GP table", "IPC init load", "DBZ->RMQ", "RMQ->GPSS", "Data recon"]
CUR_IDX = {"Create GP table": 2, "IPC init load": 3, "DBZ->RMQ": 1,
           "RMQ->GPSS": 4, "Data recon": 5}


def stream_schema(psa):
    return "lz_stream_" + psa[len("lz_psa_"):] if psa.startswith("lz_psa_") else None


def doneness(v):
    v = (v or "").strip().lower()
    return "done" if v == "done" else "no" if v in ("", "not started") else "other"


def main():
    print("check_table_status DRY-RUN v3  (READ-ONLY, GP side, strict resolve)")
    try:
        rows = json.loads(base64.b64decode(WORKLIST_B64.encode()).decode("utf-8"))
    except Exception as e:
        print("worklist decode FAILED:", e); return
    print(f"worklist rows: {len(rows)}  aliases injected: {len(ALIAS_MAP)}")

    try:
        import psycopg2
        conn = psycopg2.connect(**GP)
    except Exception as e:
        print("GP connect FAILED:", e); return
    conn.autocommit = True
    cur = conn.cursor()

    exists = set(); schema_tables = defaultdict(set)
    cur.execute("""SELECT table_schema, table_name FROM information_schema.tables
                   WHERE table_type='BASE TABLE'""")
    for s, t in cur.fetchall():
        s, t = s.lower(), t.lower(); exists.add((s, t)); schema_tables[s].add(t)

    reltuples = {}
    cur.execute("""SELECT n.nspname, c.relname, c.reltuples::bigint
                   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE c.relkind='r'""")
    for s, t, n in cur.fetchall():
        reltuples[(s.lower(), t.lower())] = n or 0

    partsum = defaultdict(int)
    try:
        cur.execute("""SELECT pn.nspname, pc.relname, SUM(cc.reltuples)::bigint
                       FROM pg_inherits i
                       JOIN pg_class pc ON pc.oid=i.inhparent
                       JOIN pg_namespace pn ON pn.oid=pc.relnamespace
                       JOIN pg_class cc ON cc.oid=i.inhrelid
                       GROUP BY 1,2""")
        for s, t, n in cur.fetchall():
            partsum[(s.lower(), t.lower())] = n or 0
    except Exception as e:
        print("partition sum WARN:", e)

    def eff_rows(k):
        return max(reltuples.get(k, 0), partsum.get(k, 0))

    recon = {}
    try:
        cur.execute("""SELECT s.gp_table, r.verdict, r.started_at
                       FROM recon_meta.recon_results r
                       JOIN recon_meta.recon_schedule s ON s.id=r.schedule_id""")
        latest = {}
        for gp_table, verdict, started in cur.fetchall():
            k = (gp_table or "").lower(); at, vs = latest.get(k, (None, []))
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

    # sheet schema -> its table set (for alias evidence)
    sheet_schema_tables = defaultdict(set)
    for r in rows:
        e = (r["e"] or "").strip().lower(); f = (r["f"] or "").strip().lower()
        if e and f:
            sheet_schema_tables[e].add(f)

    print(f"GP catalog: {len(exists)} tables, {len(schema_tables)} schemas; "
          f"recon covers {len(recon)} gp_tables\n")

    dist = {st: Counter() for st in STAGES}
    buckets = {st: Counter() for st in STAGES}
    gaps = Counter(); unresolved_schemas = Counter()
    skipped = 0; NA = "N/A"

    for r in rows:
        e = (r["e"] or "").strip().lower(); f = (r["f"] or "").strip().lower()
        t = (r["t"] or "").strip().lower(); curv = r.get("cur", [])
        if any((c or "").strip().lower() == "canceled" for c in curv):
            skipped += 1; continue

        psa = ALIAS_MAP.get(e, e)
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
            elif cv == "no" and pv == "done":
                buckets[st]["CONFLICT GP=Done, sheet=no"] += 1
            else:
                buckets[st]["other"] += 1

        if not pexists:
            gaps["NO_TABLE_GP"] += 1
            if psa not in schema_tables:
                unresolved_schemas[e] += 1

    cur.close(); conn.close()

    print("=" * 8, "proposed-status distribution", "=" * 8)
    for st in STAGES:
        print(f"  {st:16s}: " + "  ".join(f"{v}={n}" for v, n in dist[st].most_common()))

    print("\n" + "=" * 8, f"AGREE/CONFLICT vs sheet (skipped Canceled: {skipped})", "=" * 8)
    for st in STAGES:
        print(f"  {st:16s}: " + "  ".join(f"{v}={n}" for v, n in buckets[st].most_common()))

    # alias evidence: for each unresolved sheet-schema, GP schemas sharing tables
    print("\n" + "=" * 8, "ALIAS EVIDENCE (unresolved sheet-schema -> candidate GP schemas by table overlap)", "=" * 8)
    print("  (rows = sheet col E not found in catalog; pick the dominant unique candidate)")
    for e in sorted(unresolved_schemas, key=lambda k: -len(sheet_schema_tables[k]))[:35]:
        tabs = sheet_schema_tables[e]
        cands = []
        for gs, gt in schema_tables.items():
            k = len(tabs & gt)
            if k:
                cands.append((k, gs))
        cands.sort(reverse=True)
        top = "  ".join(f"{gs}({k}/{len(tabs)})" for k, gs in cands[:3]) or "(no overlap anywhere)"
        print(f"  {e:30s} [{len(tabs):3d} tbl] -> {top}")

    print(f"\n  NO_TABLE_GP total: {gaps['NO_TABLE_GP']}  (unresolved schemas: {len(unresolved_schemas)})")
    print("\n=== dry-run v3 done (NO writes) ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FATAL:"); traceback.print_exc()
