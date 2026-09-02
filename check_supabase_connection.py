"""
check_supabase_connection.py
------------------------------
Safe, read-only verification of the SUPABASE_DATABASE_URL credential and
connection. Designed so the secret CANNOT be printed by this script under
any code path -- the value is read into a local variable and only ever
used as a connection argument, never passed to print()/str()/repr() output,
never interpolated into an f-string that gets printed, never logged.

Checks, in order:
  1. .env contains exactly one SUPABASE_DATABASE_URL= line.
  2. Its value starts with 'postgresql://'.
  3. A live connection succeeds.
  4. Row counts for all 7 tables match the expected post-migration counts.

Prints ONLY booleans, counts, and structural facts. Never the value itself,
never a substring of it, never a partially-redacted version of it.
"""

import sys
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")

EXPECTED_COUNTS = {
    "users": 2,
    "machines": 2,
    "api_clients": 2,
    "devices": 1,
    "jobs": 49,
    "job_events": 1004,
    "job_notifications": 43,
}


def find_supabase_url_lines(env_text: str):
    """Returns the list of raw lines (not printed, ever) that define
    SUPABASE_DATABASE_URL, for a structural count/shape check only."""
    return [
        line for line in env_text.splitlines()
        if line.strip().startswith("SUPABASE_DATABASE_URL=")
    ]


def main() -> int:
    ok = True

    env_text = ENV_PATH.read_text(encoding="utf-8-sig")
    matches = find_supabase_url_lines(env_text)

    print(f"[check] exactly one SUPABASE_DATABASE_URL= line in .env: "
          f"{'PASS' if len(matches) == 1 else 'FAIL'} (found {len(matches)})")
    if len(matches) != 1:
        ok = False
        print("Aborting further checks -- fix the .env line shape first.")
        return 1

    value = matches[0].split("=", 1)[1].strip()
    starts_ok = value.startswith("postgresql://")
    print(f"[check] value starts with 'postgresql://': {'PASS' if starts_ok else 'FAIL'}")
    if not starts_ok:
        ok = False
        # Deliberately do NOT print what it actually starts with.
        return 1

    # No trailing/duplicate stray connection-string-shaped lines elsewhere
    # in the file (the exact mistake made twice before: a bare line with
    # no variable name).
    stray = [
        line for line in env_text.splitlines()
        if line.strip().startswith("postgresql://") and line not in matches
    ]
    print(f"[check] no stray un-prefixed connection-string line elsewhere in .env: "
          f"{'PASS' if not stray else 'FAIL'} (found {len(stray)})")
    if stray:
        ok = False

    import psycopg
    from psycopg.rows import dict_row

    try:
        conn = psycopg.connect(value, row_factory=dict_row, connect_timeout=10)
    except Exception as exc:
        # Print only the exception type, never str(exc) -- some drivers
        # echo the failing conninfo (including the password) into their
        # error messages.
        print(f"[check] connection succeeds: FAIL ({type(exc).__name__})")
        return 1
    else:
        print("[check] connection succeeds: PASS")
    finally:
        value = None  # drop the only reference in this frame ASAP

    print()
    print("[check] row counts vs. expected post-migration values:")
    all_match = True
    for table, expected in EXPECTED_COUNTS.items():
        n = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        match = n == expected
        all_match = all_match and match
        print(f"  {table:<18} expected={expected:<5} actual={n:<5} {'PASS' if match else 'FAIL'}")

    conn.close()

    ok = ok and all_match
    print()
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
