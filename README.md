# table-status-checker

Automated verification of the SIMAH **"2 Table Status"** Google Sheet —
the migration-pipeline tracker. For each source table (1 per row) it probes
Greenplum (and, later, the source DBs) to compute the real status of each
pipeline stage, then writes the result back into the sheet on a schedule.

It replaces manual status upkeep with ground-truth checks, and flags every
row where the sheet and reality disagree.

> Separate from the shared `claude-vdi-handoff` repo on purpose — that one
> is edited by another session. This repo keeps its own copies of the
> bridge tools (`gmail_send.py`, `sheet_tool.py`) so they don't collide.

## What it checks (per row, by `Table type`)

| Stage | Source of truth |
|---|---|
| Prerequisites | source DB access (`HAS_PERMS_BY_NAME`) + CDC-enabled (MSSQL `is_tracked_by_cdc`) |
| DBZ→RMQ / RMQ→GPSS | `lz_stream_*` table exists & non-empty |
| Create GP table | exists in GP `information_schema` |
| IPC init load | `lz_psa_*` table non-empty |
| Data reconciliation | latest verdict in `recon_meta.recon_results` |

`cdc` rows get all stages; `lookup` = create + init; `inter` = create
(+ init soft). Inapplicable stages → `N/A`. Manual `Canceled` rows are
never overwritten.

## Architecture

DBs are reachable only from the locked-down VDI; Google Sheets only from
the Mac. So the loop is: **Mac reads the sheet → emails a self-contained
probe (work-list embedded) → VDI runs it against GP → posts a card to Teams
→ Mac reads the card → writes statuses back to the sheet.** See
[CLAUDE.md](CLAUDE.md) for the full picture.

## Layout

```
sheet_tool.py                 # read/write Google Sheets (service account)
gmail_send.py                 # email a probe to the VDI (gitignored — has app pw)
gmail_send.example.py         # template; copy to gmail_send.py + add password
diags/
  gen_dryrun_and_send.py      # reads sheet, embeds work-list, emails the probe
  discover_table_status_meta_v1.py   # GP-side discovery (schemas, recon_meta, …)
  discover_source_prereq_cdc_v1.py   # source-side discovery (profiles, CDC-on)
  check_table_status_dryrun_v1.py    # GP dry-run checker
  check_table_status_dryrun_v2.py    # + partition fix, schema aliases, AGREE/CONFLICT
```

## Setup

```bash
pip3 install gspread psycopg2-binary
cp gmail_send.example.py gmail_send.py          # then paste the Gmail app password
# point sheet_tool.py CREDENTIALS at your service-account JSON (kept outside the repo)
```

## Run a dry-run (read-only, no sheet writes)

```bash
python3 diags/gen_dryrun_and_send.py diags/check_table_status_dryrun_v2.py check_table_status_dryrun_v2
# read the reply in Teams "tech channel" (keep that tab active in the capture browser)
cd ../teams-channel-watcher && python3 get_messages.py --channel "tech channel" --since 15m --format text
```

## Status

Discovery + dry-run v2 done (validated on all 1231 rows). Next: strict
schema resolution, the Mac-side batch writer, and a schedule. Internal use only.
