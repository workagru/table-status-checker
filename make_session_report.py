#!/usr/bin/env python3
"""Write a consolidated 'Auto-check report' tab into the '2 Table Status'
spreadsheet: live stage-completion counts + the col-I remaining-blank
decomposition by reason + source-DB reachability (verified from the VDI) +
the DB-name aliases applied + action items by owner.

Read-only on probe data; writes ONE tab ('Auto-check report') via RAW so the
'==' headers aren't parsed as formulas. Run after each apply to refresh it.
"""
import warnings
warnings.filterwarnings("ignore")
import time
from collections import Counter, defaultdict

import gspread

CRED = "/Users/alexandrgruzdev/Downloads/sheets-tool-498316-6a47b98b256f.json"
SID = "1qoswNdf61-EdNFPF0wgQc2f7cgAeSvkc-CBYQ2rKZis"
SHEET = "2 Table Status"
REPORT = "Auto-check report"

# source DBs unreachable from the VDI by firewall (TCP filtered, verified 2026-06-04)
FIREWALL = {"enquiry", "identity", "instantupdate", "moarif", "leiportal",
            "linq2simah", "linq2simah_clone", "ksapoc", "identitylei", "ksatr"}

# verified VDI reachability (raw TCP + ODBC), 2026-06-04
REACH = [
    ["10.0.135.37:1450 (molim)", "OPEN", "OK", "MOLIM_*"],
    ["BMSERV01:1450 (benchmark)", "OPEN", "OK", "Benchmark_*"],
    ["INFUATHQSQL:1450", "OPEN", "OK", "INFA_MFT_STAGE"],
    ["DATAHUBDEV01:1450", "OPEN", "OK", "HUB, POOL"],
    ["DBUATCJ2:1450", "OPEN", "OK", "UAT_B2C* (+ AMSConsumer); sheet SIMAT_B2C* are aliases"],
    ["DQUATIDQ:1450", "OPEN", "OK", "SIMAHDQ, SIMAHDQ_REP; EDWH visible but USE denied"],
    ["DBMSTRUAT:1450", "OPEN", "FAIL 08001", "SIMAH_UNIFIED — port up, SQL fails (DB down)"],
    ["DBSIMAHUAT1:1450", "FILTERED", "—", "firewall: Moarif, LEIPortal, LINQ2SIMAH, KSAPOC"],
    ["DBUATCJ2:1451/1452/1453", "FILTERED", "—", "firewall: InstantUpdate / Identity / Enquiry"],
    ["DEVDB01:1450", "FILTERED", "—", "firewall: IdentityLei"],
    ["TRUAT01:1450", "FILTERED", "—", "firewall: KSATR"],
    ["SYBDWHUATHQ:5000 (Sybase)", "see probe", "pending", "SIMAHDWH — ODBC DataDirect; JDBC jar absent"],
]
ALIASES = [
    ["SIMAT_B2CEnquiry", "UAT_B2CEnquiry"], ["SIMAT_B2CFinance", "UAT_B2CFinance"],
    ["SIMAT_B2CIdentity", "UAT_B2CIdentity"], ["SIMAT_B2CDispute", "UAT_B2CDispute"],
    ["SIMAT_B2CNarratives", "UAT_B2CNarratives"], ["SIMAT_B2CPackagesAlerts", "UAT_B2CPackagesAlerts"],
    ["SIMAT_B2CCreditScore", "UAT_B2CCreditScore"],
]
ACTIONS = [
    ["network", "Open firewall from VDI subnet to: DBSIMAHUAT1, DBUATCJ2:1451/1452/1453, DEVDB01, TRUAT01 (1450)"],
    ["DBA", "Grant gpuatsrvusr access (USE) to EDWH on DQUATIDQ:1450 (visible but denied)"],
    ["DBA", "Bring SIMAH_UNIFIED up on DBMSTRUAT:1450 (port open, DB down)"],
    ["you (owner)", "Confirm real db name for SIMAH_MSCRM (no such db on DBUATCJ2:1450)"],
    ["checker", "SIMAHDWH (Sybase): finish ODBC DataDirect connect (probe v3)"],
]


def col(letter):
    return {"I": 8, "K": 10, "M": 12, "O": 14, "Q": 16, "S": 18}[letter]


def main():
    gc = gspread.service_account(filename=CRED)
    ss = gc.open_by_key(SID)
    vals = ss.worksheet(SHEET).get_all_values()

    def dist(letter):
        i = col(letter); c = Counter()
        for r in vals[1:]:
            c[(r[i].strip() if len(r) > i else "") or "(blank)"] += 1
        return c

    di, ds, dm = dist("I"), dist("S"), dist("M")

    # col-I remaining-blank decomposition by reason
    reason = Counter(); blank_db = Counter()
    for r in vals[1:]:
        g = lambda x: (r[x].strip() if len(r) > x else "")
        if g(8) or not g(2):
            continue
        db = g(2); dl = db.lower(); sysn = g(0).upper()
        blank_db[db] += 1
        if dl == "simahdwh": reason["Sybase SIMAHDWH (ODBC probe in progress)"] += 1
        elif dl == "edwh": reason["PERM — EDWH visible but USE denied (DBA grant)"] += 1
        elif dl == "simah_mscrm": reason["NOTFOUND — SIMAH_MSCRM (real db name unknown)"] += 1
        elif dl == "simah_unified": reason["SIMAH_UNIFIED on DBMSTRUAT (DB down)"] += 1
        elif dl in FIREWALL: reason["firewall — 6 hosts unreachable from VDI"] += 1
        elif "DB2" in sysn or dl == "b7031210": reason["DB2i (outside the MSSQL probe)"] += 1
        else: reason["table not found in source DB / other"] += 1

    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    out = [[f"{REPORT} — verified from VDI", stamp]]
    out += [[], ["== Stage completion (live counts) =="], ["stage", "Done", "Read granted no CDC", "blank", "Canceled", "other"]]

    def row_for(letter, d):
        done = d.get("Done", 0); rg = d.get("Read granted, but no CDC", 0)
        blank = d.get("(blank)", 0); canc = d.get("Canceled", 0)
        other = sum(v for k, v in d.items() if k not in ("Done", "Read granted, but no CDC", "(blank)", "Canceled"))
        return [letter, str(done), str(rg), str(blank), str(canc), str(other)]
    out += [row_for("I Prerequisites", di), row_for("M Create GP table", dm), row_for("S Data reconciliation", ds)]
    out += [["", "", "", "", "", ""], ["S detail", str(dict(ds))]]

    out += [[], ["== col I — remaining blanks by reason =="], ["rows", "reason"]]
    out += [[str(n), k] for k, n in reason.most_common()]

    out += [[], ["== Source-DB reachability from the VDI (2026-06-04) =="],
            ["endpoint", "TCP", "SQL", "databases / note"]]
    out += REACH

    out += [[], ["== DB-name aliases applied (sheet -> real server db) =="], ["sheet name", "real db"]]
    out += ALIASES

    out += [[], ["== Action items =="], ["owner", "action"]]
    out += ACTIONS

    out += [[], ["Note", "Stage I/S are live; M refreshes after the GP probe applies. "
                 "Auto-applied: only rows with a real probe result; blanks left untouched."]]

    try:
        ws = ss.worksheet(REPORT); ws.clear()
    except Exception:
        ws = ss.add_worksheet(title=REPORT, rows=max(60, len(out) + 5), cols=6)
    ws.update(out, value_input_option="RAW")
    print(f"wrote '{REPORT}' tab: {len(out)} rows")
    print("  I:", dict(di)); print("  M:", dict(dm)); print("  S:", dict(ds))
    print("  blank-I reasons:", dict(reason))


if __name__ == "__main__":
    main()
