#!/usr/bin/env python3
"""Update '2 Table Status' col H (Table type) from the authoritative
'1 UC-WF-Table' tab (col D 'CDC/inter/Lookup').

Join: sheet2 (GP schema E, table F) -> sheet1 (GP schema H, GP table name I);
source-side (DB C, schema D, table F) -> sheet1 (Database E, Schema F, Table G)
as a fallback for GP-unmatched rows. Only single-type matches are written;
sheet1-ambiguous keys (same table, >1 type) and unmatched rows are LEFT as-is
and reported. Dry by default; --apply writes col H (USER_ENTERED).
"""
import argparse
import warnings
warnings.filterwarnings("ignore")
from collections import Counter, defaultdict

import gspread

CRED = "/Users/alexandrgruzdev/Downloads/sheets-tool-498316-6a47b98b256f.json"
SID = "1qoswNdf61-EdNFPF0wgQc2f7cgAeSvkc-CBYQ2rKZis"
S1, S2 = "1 UC-WF-Table", "2 Table Status"
H_COL = 7   # 0-based col H (Table type) on sheet 2


def norm(t):
    t = (t or "").strip().lower()
    if t == "cdc":
        return "cdc"
    if t.startswith("inter"):
        return "inter"
    if t.startswith("lookup") or t == "lkp":
        return "lookup"
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    gc = gspread.service_account(filename=CRED)
    ss = gc.open_by_key(SID)
    s1 = ss.worksheet(S1).get_all_values()
    ws2 = ss.worksheet(S2)
    s2 = ws2.get_all_values()

    m_gp = defaultdict(set); m_src = defaultdict(set)
    for r in s1[1:]:
        g = lambda i: (r[i].strip() if len(r) > i else "")
        tt = norm(g(3))
        if not tt:
            continue
        if g(7) and g(8):
            m_gp[(g(7).lower(), g(8).lower())].add(tt)
        if g(4) and g(6):
            m_src[(g(4).lower(), g(5).lower(), g(6).lower())].add(tt)

    updates = []; matrix = Counter(); conf = 0; unm = 0; agree = 0
    canon = {"cdc": "cdc", "inter": "inter", "lookup": "lookup"}
    for i, r in enumerate(s2[1:], start=2):
        g = lambda x: (r[x].strip() if len(r) > x else "")
        if not (g(4) or g(5)):
            continue
        cur_raw = g(H_COL); cur = norm(cur_raw)
        types = m_gp.get((g(4).lower(), g(5).lower()))
        if not types:
            types = m_src.get((g(2).lower(), g(3).lower(), g(5).lower()))
        if not types:
            unm += 1; continue
        if len(types) > 1:
            conf += 1; continue
        new = next(iter(types))
        if new == cur:
            agree += 1; continue
        matrix[(cur or "(blank)", new)] += 1
        updates.append({"range": f"H{i}", "values": [[canon[new]]]})

    print(f"matched & agree={agree}; to change={len(updates)}; conflicts(skipped)={conf}; unmatched(skipped)={unm}")
    for (a, b), n in matrix.most_common():
        print(f"  {n:4d}  {a:8s} -> {b}")
    if args.apply and updates:
        for k in range(0, len(updates), 500):
            ws2.batch_update(updates[k:k + 500], value_input_option="USER_ENTERED")
        print(f"[write] updated {len(updates)} Table type cells in col H")
    elif not args.apply:
        print("(dry — pass --apply to write col H)")


if __name__ == "__main__":
    main()
