# CLAUDE.md — table-status-checker

Automated, scheduled **verification of the "2 Table Status" Google Sheet**
(SIMAH migration-pipeline tracker): probe Greenplum + source DBs, compute
the real per-table pipeline status, and write it back into the sheet.

This is its **own** project/repo, deliberately separate from the shared
`claude-vdi-handoff` tooling repo (another session edits that one). We keep
our own copies of the bridge tools here so the two don't collide.

## Communication
- **Russian** to the user (Alexander Gruzdev). **English** for
  code/comments/commits/CLI.

## The two-machine constraint (same bridge as Auto-Recon)
DBs (GP / MSSQL / DB2i) are reachable **only from the locked-down Windows
VDI**; Google Sheets is reachable **only from the Mac**. The only bridge is
email → VDI → Teams:

```
 Mac (Claude Code)                         VDI (Windows)
  sheet_tool.py  ── reads/writes Sheet      vdi-mail-watcher.ps1 (runs .py,
  gmail_send.py  ── email (zip) ─────────▶  posts stdout to Teams "tech channel")
  teams-watcher  ◀── Teams card (POST) ───  probe queries GP/source, prints JSON
```

So every check runs in a self-contained `.py` we email to the VDI; the
work-list (which rows/keys to check) is **embedded** because the VDI can't
reach the Sheet. Results come back as a Teams card we read on the Mac.

## The sheet
- Spreadsheet `1qoswNdf61-EdNFPF0wgQc2f7cgAeSvkc-CBYQ2rKZis`, tab
  **`2 Table Status`** (gid 463833003). 1231 data rows, 1 row = 1 source
  table migrated into GP.
- Key = col **G** `gp tech` (= GP schema E + table F). Col **H**
  `Table type` ∈ {cdc, lookup, inter} selects which checks apply.
- Stage columns (each + a Responsible col): **I** Prerequisites, **K**
  DBZ→RMQ, **M** Create GP table, **O** IPC init load, **Q** RMQ→GPSS,
  **S** Data reconciliation; U Discrepancies, V Comment.
- Sibling tabs: `2_1 Table Pivot` (%), `3 UC Status` (per Use Case),
  `1 UC-WF-Table` (table→UC), `TECH 1 GP schema names` (a partly-stale
  source→GP schema map — do NOT trust it as the key, see below).

## Check matrix (by Table type) + status mapping
| Stage | cdc | lookup | inter | How (GP / source) |
|---|---|---|---|---|
| Prerequisites (I) | access + CDC-on | access | access | source HAS_PERMS_BY_NAME; cdc: is_tracked_by_cdc |
| DBZ→RMQ (K) | ✅ | N/A | N/A | `lz_stream_*` table exists & non-empty |
| Create GP table (M) | ✅ | ✅ | ✅ | exists in GP `information_schema` |
| IPC init load (O) | ✅ | ✅ | ✅(soft) | `lz_psa_*` table non-empty |
| RMQ→GPSS (Q) | ✅ | N/A | N/A | `lz_stream_*` non-empty (= same stream signal) |
| Data recon (S) | ✅ | N/A | N/A | latest verdict in `recon_meta.recon_results` |

Mapping: exists/loaded/streaming → `Done`; missing → `Not started`; recon
PASS→`Done`, DIFF→`Discrepancies`, ERROR→`Blocked`, not in schedule →
`(not scheduled)`; inapplicable by type → `N/A`. **Preserve manual
`Canceled` rows — never overwrite them.** Write mode = overwrite cols I…S.

## Hard-won GP facts (from discovery probes, 2026-06-03)
- **Paired schemas:** `lz_psa_X` = init/landing; `lz_stream_X` = streaming
  (CDC-live). Transform is literal `lz_psa_` ↔ `lz_stream_`. Stream tables
  are partitioned (`_p2026MM`, `_arch`, `_old`) → the parent's
  `reltuples=0`; sum inheritance children (`pg_inherits`) for row counts.
- **Case-insensitive:** GP tables are lowercase (`admcusm0`), sheet is
  UPPER. Always ci-match.
- **Sheet col E (GP Schema Name) is unreliable** for some rows (e.g.
  `lz_mlmdsp_lkp`, `lz_smhmscrm_dbo`). Auto-guessing the real schema by
  table-name overlap is UNSAFE (false positives on generic lookup names).
  → resolve strictly by col E (ci); send everything unresolved to the
  **gap mini-report** for the user to map. Do not auto-apply guessed aliases.
- **recon_meta** (`recon_results` keyed by schedule_id → `recon_schedule.id`;
  `gp_table` = lowercase `schema.table`; per-test_type verdicts
  PASS/DIFF/ERROR/SKIPPED, plus `details` JSON + human `comment`). The
  autotester (`run_auto_recon.py`) INSERTs every verdict here; the Teams
  recon card is just a downstream render — so **read recon status from GP,
  never scrape messages.** recon_results is also RICHER than the card
  (details/comment → can feed the sheet's Discrepancies/Comment).
  **Policy (user 2026-06-03):** Data reconciliation is ALWAYS GP-sourced and
  overwritten; manual recon (Haneen's) is legacy and not preserved — the
  goal is full auto-recon, and any future manual recon writes to the same
  store. recon_schedule currently holds only ~55 tables; CDC tables not yet
  scheduled → `(not scheduled)` (a temporary to-do surfaced in the gap
  report, not a gap to engineer around).
  CARE: the sheet's Comment (V) carries notes for ALL stages, so don't
  blanket-overwrite it with recon-only `comment` — enrich carefully.
- **SOURCE_PROFILES = 9** (8 mssql + 1 db2i; `mssql_default` is CHANGEME).
  MSSQL CDC-on via `sys.databases.is_cdc_enabled` + `sys.tables.is_tracked_by_cdc`;
  access via `HAS_PERMS_BY_NAME`. DB2i journaling via `QSYS2.OBJECT_STATISTICS`.
  Most sheet source DBs have **no** profile → gap report (`NO_PROFILE`).

## Gap mini-report (non-blocking)
Don't block on missing coverage. Each run emits buckets: `NO_TABLE_GP`,
`NO_PROFILE`, `NO_CREDS`, `NO_ACCESS`, `MISSING_TABLE_SRC`, … (reuse the
Auto-Recon `error_classifier` taxonomy). For un-checkable rows we leave the
manual status untouched; the user feeds creds/access/schema-mappings
incrementally.

## Stateful re-check engine (planned — user spec 2026-06-03)
**First run trusts NOTHING** in the sheet: every stage is computed fresh
from GP/source. Subsequent runs keep a persistent per-(row, stage) state
(last verified result + timestamp + the value we last wrote to the sheet)
in a **Mac-side store** `state/table_status_state.json`, and re-check
selectively:
- **Data reconciliation: ALWAYS re-checked** (data drifts) — re-read fresh
  from `recon_meta.recon_results` each run.
- A stage is **SKIPPED** (reuse stored result) **only when** stored result
  == Done/OK **and** the current sheet value == Done/OK (stable, confirmed).
  This is the *only* skip case.
- Otherwise re-check — in particular:
  - stored=not-OK but sheet now manually says OK (human claims progress) → check;
  - stored=OK but sheet now says not-OK → check (symmetric);
  - stored=not-OK and sheet=not-OK (table still missing) → check (poll until created).

Effect: steady-state runs only re-probe in-progress rows, freshly
human-edited rows, and recon → a tiny work-list and a tiny result (also
helps stay under the Teams 20 KB cap). Human edits are told apart from our
own writes via the "value we last wrote" field in the store.

## Writer rules (user 2026-06-03)
- The writer overwrites only the **status** columns (I/K/M/O/Q/S). It NEVER
  touches the Responsible columns (J/L/N/P/R/T) — the `auto` label idea was
  dropped per the user. Manual `Canceled` rows stay untouched.
- **Recon 'Ready' rule:** for a `cdc` row whose upstream I/K/M/O/Q are all
  `Done` but there is no real recon verdict (PASS/DIFF/ERROR in recon_meta),
  set Data reconciliation (S) = **`Ready`** (pipeline complete, awaiting
  reconciliation). This honestly replaces unverified manual `Done`.
  Implemented as `apply_results.py --finalize-recon` (uses the GP probe
  result for "has a verdict?" + the sheet for the upstream values).
- Recon write: only a real verdict (PASS→Done, DIFF→Discrepancies,
  ERROR→Error) is written directly; `(not scheduled)` / SKIPPED-only are
  never written (the Ready rule covers the all-upstream-Done case).
- **Non-cdc rule:** for a `lookup`/`inter` row, an old `Done` the autotester
  does NOT confirm (proposed != Done) is written as **`Not started`** (not
  N/A). Confirmed `Done` stays. (cdc's analog is the `Ready` rule above.)
  Applies on the full run; the all-cdc B7031210 pilot is unaffected.
- **Mislabeled-CDC note:** a `lookup`/`inter` row with >3 of its 6 statuses
  filled AND a populated `lz_stream_*` table in GP is really CDC → the writer
  appends to the Comment (col V): **`ключ lookup а реализовано CDC`** (or
  `inter`). The probe sets `cdc_detected` from the stream table; the writer
  applies the >3-filled + note-merge (won't duplicate an existing note).
- **Pilot first:** before the full 1231-row run, do a scoped run on ONE
  source DB — **B7031210** (the DB2i CORE source → lz_psa_core/lz_stream_core)
  — verify, then go wide.
- **Backup before any overwrite:** duplicate the `2 Table Status` tab to a
  dated snapshot tab once, before the first write.
- **Never write fabricated/stale statuses** — only values from a real,
  freshly-captured probe result. If capture fails, hold the write.
- Schema aliases: auto-apply only high-confidence (dominant, unique
  table-name overlap); leave ambiguous ones in the gap report (do NOT block
  on the user to confirm them).

## How to run
```bash
# build a probe with the sheet work-list embedded + email it to the VDI
python3 diags/gen_dryrun_and_send.py diags/check_table_status_dryrun_v2.py check_table_status_dryrun_v2
# then read the reply card in Teams "tech channel" via the teams-channel-watcher
#   (keep the tech-channel tab active in the capture browser)
cd ../teams-channel-watcher && python3 get_messages.py --channel "tech channel" --since 15m --format text
```

## Habits (hard rules)
- **Versioned files**: never overwrite a probe — write `*_vN`.
- **Self-contained probes**: each `.py` emailed to the VDI runs standalone
  (creds inline; UAT GP creds gpadmin/gpadmin). The work-list is embedded.
- **No-confirm sends**: don't ask before emailing a probe to the VDI.
- **3-minute timeout**: if a tool hangs >3 min, abort and switch approach.
- **Read-only until approved**: dry-run probes never write the sheet; the
  Mac-side writer is a separate, explicit step.
- **Teams output is size-limited** (~20 KB — the mail-watcher truncates
  stdout, and Adaptive Cards cap by size/line count). Never dump all 1231
  rows: emit **delta-only** (changed rows) and **chunk** large payloads into
  `[k/N]` cards ≤18 KB that the Mac reassembles from the channel JSONL.

## Secrets
- `gmail_send.py` holds the Gmail app password → **gitignored**. Copy from
  `gmail_send.example.py` and paste the password locally.
- The Google service-account JSON lives **outside** the repo; `sheet_tool.py`
  only stores its path. `*.json` is gitignored.

## Key paths
| What | Where |
|---|---|
| Sheets R/W tool | `sheet_tool.py` |
| Email→VDI sender | `gmail_send.py` (gitignored) / `gmail_send.example.py` |
| Probe generator (reads sheet, embeds work-list, sends) | `diags/gen_dryrun_and_send.py` |
| GP-side discovery | `diags/discover_table_status_meta_v1.py` |
| Source-side discovery | `diags/discover_source_prereq_cdc_v1.py` |
| GP dry-run checker | `diags/check_table_status_dryrun_v2.py` |
| Teams reader | `../teams-channel-watcher/` |
| GP UAT | `grnplumvipuat.ksacb.com.sa:5442` `simah_test` `gpadmin/gpadmin` |

## Status (2026-06-03)
Discovery done; dry-run **v2** validated the matrix on all 1231 rows.
AGREE/CONFLICT works; partition fix works. Next: drop auto-alias guessing
(v3 = strict col-E + richer gap report), confirm a few user-supplied schema
aliases, then build the **Mac-side batch writer** and put it on a schedule.
Open: explicit init-load meta-attribute? meaning of `WFP`? dedicated Teams
channel for this checker's output.
