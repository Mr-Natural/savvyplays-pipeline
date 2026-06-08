# World Cup 2026 data pipeline

Scripts that research, populate, verify, and quality-check the SavvyPlays
`/world-cup` hub data in Supabase. Frontend lives in the `savvyplays` repo.

## Setup

```powershell
cd E:\OneDrive\World_Cup
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then fill in the values
```

`.env` needs `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ANTHROPIC_API_KEY`.
For table creation only, also add one of `SUPABASE_ACCESS_TOKEN`,
`SUPABASE_DB_PASSWORD`, or `DATABASE_URL`.

## Run order

```powershell
# 1. Create tables (one-off)
python wc_create_tables.py

# 2. Populate one group first and eyeball the output
python wc_populate.py --group D

# 3. Fact-check that group
python wc_verify.py --group D

# 4. Populate everything (batches by group, with delays)
python wc_populate.py

# 5. Full verification pass; --fix auto-corrects low-risk fields
python wc_verify.py
python wc_verify.py --fix

# 6. Plagiarism scan
python wc_plagiarism_check.py

# 7. After June 7-10 friendlies, backfill warm-up results only
python wc_populate.py --warmups-only
```

Reports are written to `wc_verification_report.md` and
`wc_plagiarism_report.md`.

## Notes

- Generation/fact-check model: `claude-sonnet-4-6` with server-side web search.
- All content rules (anti-AI-detection, voice, FIFA naming) live in `wc_lib.py`
  (`CONTENT_RULES`) and are linted by `lint_content` / `lint_record`.
- `wc_populate.py` and `wc_verify.py` call the paid Anthropic API. A full run
  covers 48 teams plus ~150-240 players, so expect real token cost.
