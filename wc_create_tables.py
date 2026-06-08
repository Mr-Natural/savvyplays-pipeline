"""
wc_create_tables.py
Create the World Cup 2026 tables (wc_teams, wc_players, wc_predictions, wc_groups)
in Supabase by applying 001_wc_create_tables.sql.

The Supabase service-role key alone CANNOT run DDL via PostgREST, so this needs
one of the following (checked in order). Set whichever you have:

  Option A - Supabase personal access token (easiest, no extra packages):
    Get it: https://app.supabase.com/account/tokens
    $env:SUPABASE_ACCESS_TOKEN = "sbp_..."

  Option B - Direct PostgreSQL connection (needs psycopg2):
    pip install psycopg2-binary
    $env:DATABASE_URL = "postgresql://postgres.<ref>:<pw>@aws-0-...pooler.supabase.com:6543/postgres"

  Option C - Database password only (project ref hardcoded below):
    $env:SUPABASE_DB_PASSWORD = "your-db-password"

Fallback: with no credential, the SQL is printed for you to paste into the
Supabase SQL editor: https://app.supabase.com/project/crdpsfpqhbduwcfgrckj/sql/new

Usage:
    python wc_create_tables.py
"""

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

PROJECT_REF = "crdpsfpqhbduwcfgrckj"
SQL_FILE = Path(__file__).with_name("001_wc_create_tables.sql")


def load_sql() -> str:
    if not SQL_FILE.exists():
        sys.exit(f"Missing SQL file: {SQL_FILE}")
    return SQL_FILE.read_text(encoding="utf-8")


def run_with_management_api(access_token: str, ddl: str) -> bool:
    import requests
    url = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json={"query": ddl}, timeout=60)
    if resp.status_code in (200, 201):
        return True
    print(f"Management API error {resp.status_code}: {resp.text[:500]}")
    return False


def run_with_psycopg2(db_url: str, ddl: str) -> bool:
    try:
        import psycopg2  # type: ignore
    except ImportError:
        print("psycopg2 not installed - run: pip install psycopg2-binary")
        return False
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(ddl)
        cur.close()
        conn.close()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"psycopg2 error: {e}")
        return False


def get_database_url() -> str | None:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    password = os.environ.get("SUPABASE_DB_PASSWORD")
    if password:
        return (
            f"postgresql://postgres.{PROJECT_REF}:{password}"
            f"@aws-0-ap-southeast-2.pooler.supabase.com:6543/postgres"
        )
    return None


def main() -> None:
    ddl = load_sql()

    access_token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    if access_token:
        print(f"Using Supabase Management API for project {PROJECT_REF} ...")
        if run_with_management_api(access_token, ddl):
            print("World Cup tables created successfully.")
            return
        print("Management API attempt failed - trying next option.")

    db_url = get_database_url()
    if db_url:
        print(f"Connecting directly to Supabase project {PROJECT_REF} ...")
        if run_with_psycopg2(db_url, ddl):
            print("World Cup tables created successfully.")
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
