#!/usr/bin/env python3
"""Read / write cells in a Google Sheet via service account."""

import argparse
import re
import sys

import gspread

CREDENTIALS = "/Users/alexandrgruzdev/Downloads/sheets-tool-498316-6a47b98b256f.json"
SPREADSHEET_ID = "1qoswNdf61-EdNFPF0wgQc2f7cgAeSvkc-CBYQ2rKZis"


def get_sheet(sheet_name=None):
    gc = gspread.service_account(filename=CREDENTIALS)
    ss = gc.open_by_key(SPREADSHEET_ID)
    if sheet_name:
        return ss.worksheet(sheet_name)
    return ss.sheet1


def cmd_read(args):
    ws = get_sheet(args.sheet)
    val = ws.acell(args.cell).value
    print(val if val is not None else "")


def cmd_write(args):
    ws = get_sheet(args.sheet)
    ws.update_acell(args.cell, args.value)
    print(f"OK  {args.cell} = {args.value}")


def cmd_range(args):
    ws = get_sheet(args.sheet)
    rows = ws.get(args.range)
    for row in rows:
        print("\t".join(str(c) for c in row))


def resolve_range(ws, rng):
    """Expand column-only ranges like G:G → G1:G{row_count}."""
    m = re.fullmatch(r"([A-Z]+):([A-Z]+)", rng, re.IGNORECASE)
    if m:
        return f"{m[1]}1:{m[2]}{ws.row_count}"
    return rng


def cmd_fill(args):
    ws = get_sheet(args.sheet)
    resolved = resolve_range(ws, args.range)
    cells = ws.range(resolved)
    for cell in cells:
        cell.value = args.value
    ws.update_cells(cells)
    print(f"OK  {resolved} ← {args.value!r}  ({len(cells)} cells)")


def cmd_sheets(args):
    gc = gspread.service_account(filename=CREDENTIALS)
    ss = gc.open_by_key(SPREADSHEET_ID)
    for ws in ss.worksheets():
        print(f"{ws.title}  (gid={ws.id})")


def main():
    p = argparse.ArgumentParser(description="Google Sheets cell tool")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("read", help="read one cell")
    r.add_argument("--cell", required=True, help="e.g. A1, B5, C12")
    r.add_argument("--sheet", help="worksheet name (default: first sheet)")

    w = sub.add_parser("write", help="write one cell")
    w.add_argument("--cell", required=True, help="e.g. A1, B5, C12")
    w.add_argument("--value", required=True, help="value to set")
    w.add_argument("--sheet", help="worksheet name (default: first sheet)")

    rng = sub.add_parser("range", help="read a range")
    rng.add_argument("--range", required=True, help="e.g. A1:D10")
    rng.add_argument("--sheet", help="worksheet name (default: first sheet)")

    f = sub.add_parser("fill", help="fill a range with one value")
    f.add_argument("--range", required=True, help="e.g. F2:F100, A1:C5")
    f.add_argument("--value", required=True, help="value to set in every cell")
    f.add_argument("--sheet", help="worksheet name (default: first sheet)")

    sub.add_parser("sheets", help="list all worksheets")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        sys.exit(1)

    {"read": cmd_read, "write": cmd_write, "range": cmd_range, "fill": cmd_fill, "sheets": cmd_sheets}[args.cmd](args)


if __name__ == "__main__":
    main()
