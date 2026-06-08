"""
wc_create_match_tables.py
Apply 002_wc_create_match_tables.sql (wc_matches, wc_match_previews) to Supabase.

Reuses the credential logic from wc_create_tables.py: a Supabase personal access
token (SUPABASE_ACCESS_TOKEN) or a Postgres connection (DATABASE_URL /
SUPABASE_DB_PASSWORD). With no credential it prints the SQL for the SQL editor.

Usage:
    python wc_create_match_tables.py
"""

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

from wc_create_tables import (
    PROJECT_REF,
    get_database_url,
    run_with_management_api,
    run_with_psycopg2,
)

SQL_FILE = Path(__file__).with_name("002_wc_create_match_tables.sql")


def main() -> None:
    if not SQL_FILE.exists():
        sys.exit(f"Missing SQL file: {SQL_FILE}")
    ddl = SQL_FILE.read_text(encoding="utf-8")

    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    if token:
        print(f"Using Supabase Management API for project {PROJECT_REF} ...")
        if run_with_management_api(token, ddl):
            print("Match tables created successfully.")
            return
        print("Management API attempt failed - trying next option.")

    db_url = get_database_url()
    if db_url:
        print(f"Connecting directly to Supabase project {PROJECT_REF} ...")
        if run_with_psycopg2(db_url, ddl):
            print("Match tables created successfully.")
            return
        print("psycopg2 attempt failed.")

    print()
    print("-" * 70)
    print("No working credential. Paste the SQL below into the Supabase SQL editor:")
    print(f"  https://app.supabase.com/project/{PROJECT_REF}/sql/new")
    print("-" * 70)
    print(ddl)
    sys.exit(1)


if __name__ == "__main__":
    main()
