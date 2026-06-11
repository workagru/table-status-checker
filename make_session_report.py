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

# source DBs unreachable EITHER from the VDI directly OR from the ssh-bridge
# (10.0.135.81). DBUATCJ2:1451/1452/1453 and TRUAT01:1450 are now reachable
# via the ssh-bridge tunnel and resolve cleanly — they're NOT firewalled anymore.
# Verified 2026-06-07: only DBSIMAHUAT1 and DEVDB01 stay truly unreachable.
FIREWALL = {"moarif", "leiportal", "linq2simah", "linq2simah_clone", "ksapoc", "identitylei"}

# verified reachability (raw TCP + ODBC). VDI = direct from agruzdev@VDI11-VENDOR20
# (10.0.220.28); bridge = via ssh-tunnel debapp@10.0.135.81 (GP coordinator).
REACH = [
    ["10.0.135.37:1450 (molim)", "OPEN/VDI", "OK", "MOLIM_*"],
    ["BMSERV01:1450 (benchmark)", "OPEN/VDI", "OK", "Benchmark_*"],
    ["INFUATHQSQL:1450", "OPEN/VDI", "OK", "INFA_MFT_STAGE"],
    ["DATAHUBDEV01:1450", "OPEN/VDI", "OK", "HUB, POOL"],
    ["DBUATCJ2:1450", "OPEN/VDI", "OK", "UAT_B2C* (+ AMSConsumer); sheet SIMAT_B2C* are aliases"],
    ["DQUATIDQ:1450", "OPEN/VDI", "OK", "SIMAHDQ, SIMAHDQ_REP; EDWH visible but USE denied"],
    ["DBMSTRUAT:1450", "OPEN/VDI", "OK", "SIMAH_UNIFIED — login confirmed 2026-06-11 (DBA reset complete)"],
    ["DBUATCJ2:1451 (InstantUpdate)", "TUNNEL/.81", "OK", "via ssh-bridge -> localhost:31451"],
    ["DBUATCJ2:1452 (Identity)", "TUNNEL/.81", "OK", "via ssh-bridge -> localhost:31452"],
    ["DBUATCJ2:1453 (Enquiry)", "TUNNEL/.81", "OK", "via ssh-bridge -> localhost:31453"],
    ["TRUAT01:1450 (KSATR)", "TUNNEL/.81", "OK", "via ssh-bridge -> localhost:31454"],
    ["10.0.135.20:1433 (UAT CRM)", "TUNNEL/.81", "OK", "SIMAH_MSCRM — via ssh-bridge -> localhost:31455"],
    ["DBSIMAHUAT1:1450", "FILTERED", "—", "firewall: Moarif, LEIPortal, LINQ2SIMAH, KSAPOC"],
    ["DEVDB01:1450", "FILTERED", "—", "firewall: IdentityLei"],
    ["SYBDWHUATHQ:5000 (Sybase)", "OPEN/VDI", "pending", "SIMAHDWH — needs valid Sybase creds"],
]
ALIASES = [
    ["SIMAT_B2CEnquiry", "UAT_B2CEnquiry"], ["SIMAT_B2CFinance", "UAT_B2CFinance"],
    ["SIMAT_B2CIdentity", "UAT_B2CIdentity"], ["SIMAT_B2CDispute", "UAT_B2CDispute"],
    ["SIMAT_B2CNarratives", "UAT_B2CNarratives"], ["SIMAT_B2CPackagesAlerts", "UAT_B2CPackagesAlerts"],
    ["SIMAT_B2CCreditScore", "UAT_B2CCreditScore"],
]
ACTIONS = [
    ["network", "Open firewall from UAT GP cluster + VDI subnets to DBSIMAHUAT1:1450 and DEVDB01:1450 (other earlier targets now covered by ssh-bridge tunnel)"],
    ["DBA", "Grant gpuatsrvusr USE permission on EDWH at DQUATIDQ:1450 (visible but denied)"],
    ["sheet owner", "SIMAH_UNIFIED has 6 rows with table names that don't exist in source DB — likely typo or table not yet created (login works, 61 of 67 rows verified Done 2026-06-11)"],
    ["checker", "SIMAHDWH (Sybase): provide working creds — current ones get 28000 Login Failed via DataDirect ODBC"],
    ["checker", "Add MSSQL creds for SIMAHDQ / HUB / POOL / leid / Molim_Enquiry (servers reachable from VDI, just no profile yet)"],
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
        if dl == "simahdwh": reason["Sybase SIMAHDWH (working creds pending)"] += 1
        elif dl == "edwh": reason["PERM — EDWH visible but USE denied (DBA grant)"] += 1
        elif dl == "simah_mscrm": reason["SIMAH_MSCRM — bridge route, awaiting next cycle"] += 1
        elif dl == "simah_unified": reason["SIMAH_UNIFIED — table missing in source (sheet name?) — login confirmed working"] += 1
        elif dl in FIREWALL: reason["firewall — DBSIMAHUAT1 / DEVDB01 unreachable (network tkt open)"] += 1
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
