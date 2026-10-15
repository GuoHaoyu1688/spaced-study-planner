# Agent operating guide

This project targets Python 3.11+ on a local machine, including headless Linux. Keep documentation, interfaces, and examples in English. Preserve support for user-authored multilingual study material. No paid APIs or cloud services are needed.

## Install and verify

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest --cov=spaced_study_planner
SSP_DATA_DIR=data/demo .venv/bin/python examples/seed_demo.py
```

Use `.venv/bin/ssp` below. Installation needs package downloads; normal runtime is offline. Data directory precedence: `--data-dir` > `SSP_DATA_DIR` > config `data_dir` > `./data`. Configuration is selected by `--config` or `SSP_CONFIG`; relative config data paths resolve beside that file. Options: `timezone` (default Asia/Shanghai), `algorithm` (fsrs or sm2), `retention` (default 0.9). Current card states retain their original algorithm. SQLite data, normalized images, and backups belong under the chosen data directory; never commit them.

## Complete command reference

Global `--data-dir`, `--config`, `--json`, `--quiet`, and `--version` work before or after subcommands. Query/mutation results are JSON even without `--json`; `--quiet` suppresses successful results. Help/version and server protocols are exceptions. Errors remain visible in quiet mode.

| Command | Purpose and options |
| --- | --- |
| `exam add NAME --date YYYY-MM-DD [--id ID]` | Create and select an exam; a repeated identical ID reuses it |
| `exam list` / `exam use ID` | List or select the active exam |
| `subject add NAME [--weight 1] [--exam ID]` / `subject list [--exam ID]` | Upsert weighted subjects or list them |
| `availability [--key weekday:0\|YYYY-MM-DD --minutes 30]` | Read or set capacity; Monday=0; date overrides weekday |
| `import FILE [--subject NAME] [--exam ID] [--at OFFSET_TIMESTAMP]` | Import JSON/CSV/Markdown/legacy APKG items |
| `import FILE --library` | Restore a trusted native JSON/CSV archive; conflicts roll back |
| `plan [--rebuild] [--exam ID] [--at TIMESTAMP]` | Recompute and persist future plans; rebuild is explicit documentation of the default behavior |
| `today [--exam ID] [--at TIMESTAMP]` | One-day task list and capacity warnings |
| `forecast [--days 30] [--exam ID] [--at TIMESTAMP]` | Conditional future workload; 1–365 days |
| `stats [--days 30] [--exam ID] [--at TIMESTAMP]` | Daily time, observed accuracy, tag evidence, mock scores |
| `review [--limit 20] [--mode due\|new\|mistakes\|topic\|all] [--subject NAME] [--tag TAG] [--exam ID] [--at TIMESTAMP]` | Read candidate material and revision numbers |
| `answer ITEM_ID [--answer A\|--rating good] [--elapsed-s 30] [--request-id ID] [--expected-revision N] [--at TIMESTAMP] [--session ID]` | Record a graded MCQ or a self-rated card review |
| `mock [--n 15] [--minutes 30] [--subject NAME] [--exam ID] [--at TIMESTAMP]` | Start a timed MCQ session; returns IDs and items |
| `micro [--n 15] [--minutes 5\|10\|15] [--subject NAME] [--exam ID] [--at TIMESTAMP]` | Start a capacity-bounded practice session |
| `finish SESSION_ID [--at TIMESTAMP]` | Score all selected items; unanswered count as incorrect |
| `export --out FILE [--format json\|csv] [--yes]` | Native full-library export; explicit overwrite |
| `backup [--out FILE]` | Timestamped ZIP with SQLite, images, and effective settings |
| `serve [--host 127.0.0.1] [--port 8776]` | Start local UI; loopback hosts only |
| `mcp` | Start official SDK stdio service |

Inputs use explicit offset timestamps such as `2026-10-15T09:00:00+08:00`. `--at` is a replay tool, not a way to enforce secure exams. Review candidates include the correct answer and explanations because this is personal practice.

## Output and errors

Actual output from a fresh empty library:

```sh
ssp --data-dir data/empty exam list --json
```

```json
{"ok":true,"data":[],"warnings":[]}
```

Attempting a current plan before creating an exam returns:

```json
{"ok":false,"error":{"code":"NOT_FOUND","message":"Exam not found; add an exam first"}}
```

Exit codes: 0 success, 1 storage/I/O failure, 2 invalid input or conflict, 3 missing record. Parser errors use the same JSON envelope. Uvicorn/MCP logs use stderr. Each MCP tool returns its structured domain result; SDK tool errors are separate from CLI envelopes.

## Idempotency and integrity

- Add items with explicit stable IDs, or let the content hash supply one. Identical records skip; changed payloads under an existing ID abort the batch.
- Every answer should have a stable `request_id` and the revision returned by `review`. Retry an identical request using the same ID. A changed payload under that ID or a stale revision fails. Without a request ID, a new review is recorded each time.
- Review times must not precede the last accepted review. An item can be answered once per session. Finishing the same completed session returns its saved score.
- Offline browser answers keep original timestamps and revision numbers. Sync stops at a conflict and retains pending records. Never discard unsynced data automatically.
- Call `backup` before imports into a populated library, configuration changes, manual edits, or restores. To restore a backup, stop writers and extract to a fresh directory; use its `settings.json` as configuration after setting `data_dir` appropriately. Ordinary library export does not include configuration.
- There are no deletion or schedule-reset commands in this release. Preserve history; do not edit SQLite to hide mistakes.

## MCP

Start with `ssp --data-dir data/private mcp`. Client configuration (replace the example paths):

```json
{"mcpServers":{"study":{"command":"/absolute/project/.venv/bin/ssp","args":["--data-dir","/absolute/private-study","mcp"]}}}
```

Tools: `get_today_plan(exam?)`, `next_items(n=20, subject?, mode="due", tag?)`, `submit_answer(item_id, answer?, rating?, elapsed_s=0, request_id?, expected_revision?)`, `get_stats(days=30)`, `add_items(items, subject?)`. CLI and MCP share the same core. There is no automatic morning notification; a caller can request the data and present it to the user.

## Recipes

1. “Tell me what to study today” → `ssp today --json`; include capacity warnings, and distinguish estimated work from completed study.
2. “I only have five minutes while traveling” → `ssp micro --minutes 5 --json`; submit each answer with the returned session ID, then `finish`.
3. “Focus on my mistakes” → `ssp review --mode mistakes --limit 20 --json`; present items without exposing their supplied answers before the user responds.
4. “I have less time next Tuesday” → `ssp availability --key YYYY-MM-DD --minutes 10`, then `ssp plan --rebuild --json`; report any unfitted work.
5. “Summarize this month's progress” → `ssp stats --days 30 --json` and `ssp forecast --days 30 --json`; label observed accuracy separately from assumed future recall.

## Maintenance rules

Use original fictional fixtures only. Keep scheduler arithmetic independently checked, test accepted/rejected answer sequences and portable restore, and exercise every MCP tool through the official SDK. Retain no-intraday-steps and disabled-fuzzing configuration unless documentation and reference tests change together. Do not load external browser assets or download question banks. HTML rendering must stay safe for untrusted question text. Build with `python -m pip wheel --no-deps . -w dist` and inspect packaged web assets before a release.
