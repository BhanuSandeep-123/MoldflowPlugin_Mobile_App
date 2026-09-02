"""
migration_local_to_supabase.py
--------------------------------
ACTUAL migration: local PostgreSQL (read-only) -> Supabase PostgreSQL (write).

Uses the exact same filter logic already verified by
migration_local_to_supabase_dry_run.py (approved 2026-09-02):
  - jobs: exclude the 22 non-GUID (synthetic test-harness) job_ids.
  - job_events / job_notifications: cascade from the jobs filter.
  - devices: exclude 'sdk_gphone64_x86_64' (emulator) specifically.
  - users / machines / api_clients: migrate all rows, unchanged.

Guarantees:
  - Local connection is hard-locked read-only (conn.read_only = True) --
    this script cannot modify the local database even by accident.
  - All Supabase writes happen inside ONE transaction. Any failure at any
    point rolls back everything already written this run -- no partial
    migration is ever left behind.
  - Refuses to run if any target table on Supabase is already non-empty
    (safety guard against double-migration / duplicate rows).
  - IDs, timestamps, password_hash, api_key, and push_token are inserted
    verbatim -- never regenerated, never reformatted.
  - Sequences are set via setval() to the max id actually inserted, so the
    next Supabase-generated id continues cleanly above real data.
  - Never prints password_hash / api_key / push_token values.
"""

import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

BACKEND_DIR = Path(__file__).resolve().parent

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
EXCLUDED_DEVICE_IDS = {"sdk_gphone64_x86_64"}

TABLES_IN_ORDER = [
    "users", "machines", "api_clients", "devices",
    "jobs", "job_events", "job_notifications",
]


def _load_env():
    env_file = BACKEND_DIR / ".env"
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def fetch_filtered_data(local_conn):
    """Read everything needed from the local DB, applying the approved filter.
    Returns plain dict/list structures -- the local connection is closed
    before any Supabase write happens, so the two are never open at once
    with intermixed operations."""
    all_jobs = local_conn.execute(
        "SELECT job_id, name, job_type, status, percent, started, finished, "
        "error_message, user_id, machine_id, created_at, updated_at, "
        "update_count, scm_job_id, scm_type, compute_source, scm_user, "
        "worker, parent_job_id, cancel_requested, archived "
        "FROM jobs ORDER BY created_at"
    ).fetchall()
    migrate_job_ids = {j["job_id"] for j in all_jobs if GUID_RE.match(j["job_id"])}
    jobs = [j for j in all_jobs if j["job_id"] in migrate_job_ids]

    all_events = local_conn.execute(
        "SELECT id, job_id, status, percent, finished, error_message, received_at "
        "FROM job_events ORDER BY id"
    ).fetchall()
    events = [e for e in all_events if e["job_id"] in migrate_job_ids]

    all_notifs = local_conn.execute(
        "SELECT id, job_id, user_id, notification_type, sent_at "
        "FROM job_notifications ORDER BY id"
    ).fetchall()
    notifs = [n for n in all_notifs if n["job_id"] in migrate_job_ids]

    users = local_conn.execute(
        "SELECT user_id, display_name, email, password_hash, created_at "
        "FROM users ORDER BY created_at"
    ).fetchall()
    machines = local_conn.execute(
        "SELECT machine_id, user_id, machine_name, created_at, last_seen_at "
        "FROM machines ORDER BY created_at"
    ).fetchall()
    api_clients = local_conn.execute(
        "SELECT api_key, user_id, machine_id, enabled, created_at "
        "FROM api_clients ORDER BY created_at"
    ).fetchall()
    all_devices = local_conn.execute(
        "SELECT device_id, user_id, platform, push_token, created_at, updated_at "
        "FROM devices ORDER BY created_at"
    ).fetchall()
    devices = [d for d in all_devices if d["device_id"] not in EXCLUDED_DEVICE_IDS]

    return {
        "users": users,
        "machines": machines,
        "api_clients": api_clients,
        "devices": devices,
        "jobs": jobs,
        "job_events": events,
        "job_notifications": notifs,
    }


def assert_supabase_empty(target_conn):
    for t in TABLES_IN_ORDER:
        n = target_conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
        if n:
            raise RuntimeError(
                f"Refusing to migrate: Supabase table '{t}' already has {n} "
                f"row(s). Aborting before writing anything."
            )


def insert_all(target_conn, data):
    for u in data["users"]:
        target_conn.execute(
            "INSERT INTO users (user_id, display_name, email, password_hash, created_at) "
            "VALUES (%(user_id)s, %(display_name)s, %(email)s, %(password_hash)s, %(created_at)s)",
            u,
        )
    for m in data["machines"]:
        target_conn.execute(
            "INSERT INTO machines (machine_id, user_id, machine_name, created_at, last_seen_at) "
            "VALUES (%(machine_id)s, %(user_id)s, %(machine_name)s, %(created_at)s, %(last_seen_at)s)",
            m,
        )
    for a in data["api_clients"]:
        target_conn.execute(
            "INSERT INTO api_clients (api_key, user_id, machine_id, enabled, created_at) "
            "VALUES (%(api_key)s, %(user_id)s, %(machine_id)s, %(enabled)s, %(created_at)s)",
            a,
        )
    for d in data["devices"]:
        target_conn.execute(
            "INSERT INTO devices (device_id, user_id, platform, push_token, created_at, updated_at) "
            "VALUES (%(device_id)s, %(user_id)s, %(platform)s, %(push_token)s, %(created_at)s, %(updated_at)s)",
            d,
        )
    for j in data["jobs"]:
        target_conn.execute(
            "INSERT INTO jobs (job_id, name, job_type, status, percent, started, finished, "
            "error_message, user_id, machine_id, created_at, updated_at, update_count, "
            "scm_job_id, scm_type, compute_source, scm_user, worker, parent_job_id, "
            "cancel_requested, archived) VALUES (%(job_id)s, %(name)s, %(job_type)s, "
            "%(status)s, %(percent)s, %(started)s, %(finished)s, %(error_message)s, "
            "%(user_id)s, %(machine_id)s, %(created_at)s, %(updated_at)s, %(update_count)s, "
            "%(scm_job_id)s, %(scm_type)s, %(compute_source)s, %(scm_user)s, %(worker)s, "
            "%(parent_job_id)s, %(cancel_requested)s, %(archived)s)",
            j,
        )
    for e in data["job_events"]:
        target_conn.execute(
            "INSERT INTO job_events (id, job_id, status, percent, finished, error_message, received_at) "
            "VALUES (%(id)s, %(job_id)s, %(status)s, %(percent)s, %(finished)s, %(error_message)s, %(received_at)s)",
            e,
        )
    for n in data["job_notifications"]:
        target_conn.execute(
            "INSERT INTO job_notifications (id, job_id, user_id, notification_type, sent_at) "
            "VALUES (%(id)s, %(job_id)s, %(user_id)s, %(notification_type)s, %(sent_at)s)",
            n,
        )


def main():
    _load_env()

    local_conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    local_conn.read_only = True
    data = fetch_filtered_data(local_conn)
    local_conn.rollback()
    local_conn.close()

    counts = {k: len(v) for k, v in data.items()}
    print("Fetched from local (filtered, matches approved dry run):")
    for t in TABLES_IN_ORDER:
        print(f"  {t}: {counts[t]}")

    target_conn = psycopg.connect(os.environ["SUPABASE_DATABASE_URL"], row_factory=dict_row)
    target_conn.autocommit = False
    try:
        assert_supabase_empty(target_conn)
        insert_all(target_conn, data)

        max_event_id = max((e["id"] for e in data["job_events"]), default=0)
        max_notif_id = max((n["id"] for n in data["job_notifications"]), default=0)
        target_conn.execute(
            "SELECT setval('job_events_id_seq', %s, true)", (max_event_id,)
        )
        target_conn.execute(
            "SELECT setval('job_notifications_id_seq', %s, true)", (max_notif_id,)
        )

        target_conn.commit()
        print()
        print(f"COMMITTED. Sequences set: job_events_id_seq={max_event_id}, "
              f"job_notifications_id_seq={max_notif_id}")
    except Exception:
        target_conn.rollback()
        print()
        print("MIGRATION FAILED -- transaction rolled back, Supabase unchanged.")
        raise
    finally:
        target_conn.close()


if __name__ == "__main__":
    sys.exit(main())
