#!/usr/bin/env python3
"""DRY-RUN status checker for "2 Table Status" — GP side only, READ-ONLY.

Computes the proposed pipeline status per sheet row from Greenplum, by the
Table-type matrix, and prints aggregates + a sample + a gap mini-report.
It does NOT write the sheet and does NOT touch source DBs (Prerequisites
comes in a later increment). The row work-list is embedded (base64) by the
Mac-side generator, because the VDI can't reach Google Sheets.

Status mapping (this run):
  Create GP table : lz_psa_* table exists            -> Done / Not started
  IPC init load   : lz_psa_* table non-empty (approx)-> Done / Not started
  DBZ->RMQ        : lz_stream_* exists & non-empty   -> Done / Not started (cdc only)
  RMQ->GPSS       : lz_stream_* exists & non-empty   -> Done / Not started (cdc only)
  Data recon      : latest recon_meta verdict        -> Done/Discrepancies/Error
  (Prerequisites  : not computed here)
Matrix: cdc=all stages; lookup=create+init; inter=create(+init soft).
Inapplicable -> 'N/A'. Manual 'Canceled' rows are skipped (never touched).
"""
import base64
import json
import sys
import traceback

GP = dict(host="grnplumvipuat.ksacb.com.sa", port=5442, dbname="simah_test",
          user="gpadmin", password="gpadmin", connect_timeout=15)

# Injected by the Mac generator: base64 of JSON list of
# {"r":rownum,"e":schemaE,"f":tableF,"t":type,"cur":[I,K,M,O,Q,S]}
WORKLIST_B64 = "__WORKLIST_B64__"

STAGES = ["Create GP table", "IPC init load", "DBZ->RMQ", "RMQ->GPSS", "Data recon"]


def load_worklist():
    raw = base64.b64decode(WORKLIST_B64.encode())
    return json.loads(raw.decode("utf-8"))


def stream_schema(psa):
    """lz_psa_X -> lz_stream_X (only for lz_psa_ schemas)."""
    if psa.startswith("lz_psa_"):
        return "lz_stream_" + psa[len("lz_psa_"):]
    return None


def main():
    print("check_table_status DRY-RUN v1  (READ-ONLY, GP side)")
    try:
        rows = load_worklist()
    except Exception as e:
        print("worklist decode FAILED:", e); return
    print(f"worklist rows: {len(rows)}")

    try:
        import psycopg2
    except Exception as e:
        print("psycopg2 import FAILED:", e); return
    try:
        conn = psycopg2.connect(**GP)
    except Exception as e:
        print("GP connect FAILED:", e); return
    conn.autocommit = True
    cur = conn.cursor()

    # ---- bulk GP catalog reads (lowercased) ----
    exists = set()           # (schema, table)
    cur.execute("""SELECT table_schema, table_name FROM information_schema.tables
                   WHERE table_type='BASE TABLE'""")
    for s, t in cur.fetchall():
        exists.add((s.lower(), t.lower()))

    reltuples = {}           # (schema, table) -> est rows
    cur.execute("""SELECT n.nspname, c.relname, c.reltuples::bigint
                   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE c.relkind='r'""")
    for s, t, n in cur.fetchall():
        reltuples[(s.lower(), t.lower())] = n or 0

    # ---- recon latest verdict per gp_table ----
    recon = {}               # gp_table(lower) -> verdict summary
    try:
        cur.execute("""
            SELECT s.gp_table, r.verdict, r.started_at
            FROM recon_meta.recon_results r
            JOIN recon_meta.recon_schedule s ON s.id = r.schedule_id
        """)
        latest = {}          # gp_table -> (started_at, {verdicts})
        for gp_table, verdict, started in cur.fetchall():
            k = (gp_table or "").lower()
            cur_at, vs = latest.get(k, (None, []))
            if cur_at is None or (started and started >= cur_at):
                if started and (cur_at is None or started > cur_at):
                    cur_at, vs = started, []
                vs.append(verdict)
                latest[k] = (cur_at, vs)
        for k, (_, vs) in latest.items():
            if "ERROR" in vs:
                recon[k] = "Error"
            elif "DIFF" in vs:
                recon[k] = "Discrepancies"
            elif "PASS" in vs:
                recon[k] = "Done"
            else:
                recon[k] = "Ready"
    except Exception as e:
        print("recon read WARN:", e)

    print(f"GP catalog: {len(exists)} tables, recon covers {len(recon)} gp_tables\n")

    # ---- per-row compute ----
    from collections import Counter
    dist = {st: Counter() for st in STAGES}
    gaps = Counter()
    gap_samples = []
    samples = []
    skipped_cancel = 0
    NA = "N/A"

    for row in rows:
        e = (row["e"] or "").strip().lower()
        f = (row["f"] or "").strip().lower()
        t = (row["t"] or "").strip().lower()
        cur_vals = row.get("cur", [])
        # skip manually Canceled rows entirely
        if any((c or "").strip().lower() == "canceled" for c in cur_vals):
            skipped_cancel += 1
            continue

        psa = (e, f)
        psa_exists = psa in exists
        psa_rows = reltuples.get(psa, 0)
        ss = stream_schema(e)
        st_key = (ss, f) if ss else None
        st_exists = bool(st_key and st_key in exists)
        st_rows = reltuples.get(st_key, 0) if st_key else 0
        gp_table = f"{e}.{f}"

        prop = {}
        # Create GP table
        prop["Create GP table"] = "Done" if psa_exists else "Not started"
        # IPC init load
        if t == "inter":
            prop["IPC init load"] = "Done" if (psa_exists and psa_rows > 0) else ("Not started" if psa_exists else NA)
        else:
            prop["IPC init load"] = "Done" if (psa_exists and psa_rows > 0) else "Not started"
        # streaming stages (cdc only)
        if t == "cdc":
            sv = "Done" if (st_exists and st_rows > 0) else "Not started"
            prop["DBZ->RMQ"] = sv
            prop["RMQ->GPSS"] = sv
        else:
            prop["DBZ->RMQ"] = NA
            prop["RMQ->GPSS"] = NA
        # reconciliation (cdc only; only where scheduled)
        if t == "cdc":
            prop["Data recon"] = recon.get(gp_table, "(not scheduled)")
        else:
            prop["Data recon"] = NA

        for st in STAGES:
            dist[st][prop[st]] += 1

        if not psa_exists:
            gaps["NO_TABLE_GP (psa schema/table not in catalog)"] += 1
            if len(gap_samples) < 25:
                gap_samples.append(f"{e}.{f} [{t}]")

        if len(samples) < 25:
            samples.append((gp_table, t,
                            [(_short(c)) for c in (cur_vals + [""]*6)[:6]],
                            [prop[st] for st in STAGES]))

    cur.close(); conn.close()

    # ---- report ----
    print("=" * 8, "proposed-status distribution", "=" * 8)
    for st in STAGES:
        line = "  ".join(f"{v}={n}" for v, n in dist[st].most_common())
        print(f"  {st:16s}: {line}")

    print("\n" + "=" * 8, f"gap mini-report (skipped Canceled: {skipped_cancel})", "=" * 8)
    if not gaps:
        print("  (no gaps)")
    for g, n in gaps.most_common():
        print(f"  {n:5d}  {g}")
    if gap_samples:
        print("  sample NO_TABLE_GP rows:")
        for s in gap_samples:
            print("     ", s)

    print("\n" + "=" * 8, "sample rows (current -> proposed)", "=" * 8)
    print("  legend cur/prop order: Create | init | DBZ | GPSS | recon")
    # cur order is sheet stage order [I,K,M,O,Q,S] = [Prereq,DBZ,Create,init,GPSS,recon]
    for gp_table, t, cur6, prop5 in samples:
        cur_disp = "/".join([cur6[2], cur6[3], cur6[1], cur6[4], cur6[5]])  # Create,init,DBZ,GPSS,recon
        prop_disp = "/".join(prop5)
        print(f"  [{t:6s}] {gp_table:42s} cur:{cur_disp:22s} -> {prop_disp}")

    print("\n=== dry-run done (NO writes) ===")


def _short(v):
    v = (v or "").strip()
    return v[:10]


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FATAL:"); traceback.print_exc()
