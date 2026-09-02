"""
migration_local_to_supabase_dry_run.py
---------------------------------------
READ-ONLY dry run for the local-PostgreSQL -> Supabase data migration.

Connects to the LOCAL database ONLY, in an explicit read-only transaction
(the backend rejects any write outright, even by accident). Never connects
to Supabase and never writes anything anywhere. Its only output is a report
of exactly what a real migration would insert, exclude, and set -- nothing
here performs that migration.

Filtering rules (per user decision, 2026-09-02):
  - jobs: EXCLUDE any job whose job_id is not a real SCM-assigned GUID --
    these are 22 hand-inserted test-harness rows from earlier validation
    sessions (TEST-*, VERIFY-*, E2E-*, LIVE-PHONE-TEST-*, etc.), not real
    Moldflow analyses. Recomputed live via regex, not a hardcoded list, so
    this stays correct if the source data changes.
  - job_events / job_notifications: EXCLUDE any row whose job_id belongs to
    an excluded job (cascades from the jobs filter).
  - devices: EXCLUDE the 'sdk_gphone64_x86_64' emulator row specifically
    (explicit one-off decision, not a general rule). Keep everything else,
    including the real phone.
  - users / machines / api_clients: migrate ALL rows unchanged (including
    the DEV-USER-002 / DEV-PC-002 subtree, per explicit decision to keep it
    despite having no genuine job history).
  - The stale-but-genuine job 9f61ab41-... (real GUID, real study name) is
    KEPT -- it is not synthetic, just orphaned by the now-fixed monitor gap.

Never prints password_hash / api_key / push_token raw values -- only
lengths / presence, exactly as done during the earlier read-only inspection.
"""

import os
import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

BACKEND_DIR = Path(__file__).resolve().parent

# GUID job_id shape used by every real SCM-assigned job (verified against
# the local data: exactly the 49 genuine jobs match this, the 22 synthetic
# ones do not).
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

EXCLUDED_DEVICE_IDS = {"sdk_gphone64_x86_64"}


def _load_env():
    env_file = BACKEND_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _connect_read_only():
    """Local DB connection, hard-locked to read-only at the Postgres level --
    any accidental write statement is rejected by the server itself, not
    just by this script's own discipline."""
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    conn.read_only = True
    conn.autocommit = False
    return conn


def main():
    _load_env()
    conn = _connect_read_only()

    print("=" * 78)
    print("FILTERED MIGRATION DRY RUN -- read-only, writes NOTHING, anywhere")
    print("=" * 78)

    # ------------------------------------------------------------------
    # 1. Classify jobs: genuine (GUID job_id) vs synthetic (everything else)
    # ------------------------------------------------------------------
    all_jobs = conn.execute(
        "SELECT job_id, name, status, machine_id, user_id, scm_job_id, "
        "created_at, updated_at FROM jobs ORDER BY created_at"
    ).fetchall()

    migrate_jobs = [j for j in all_jobs if GUID_RE.match(j["job_id"])]
    exclude_jobs = [j for j in all_jobs if not GUID_RE.match(j["job_id"])]
    migrate_job_ids = {j["job_id"] for j in migrate_jobs}
    exclude_job_ids = {j["job_id"] for j in exclude_jobs}

    # ------------------------------------------------------------------
    # 2. Cascade the jobs filter down to job_events / job_notifications
    # ------------------------------------------------------------------
    all_events = conn.execute(
        "SELECT id, job_id FROM job_events ORDER BY id"
    ).fetchall()
    migrate_events = [e for e in all_events if e["job_id"] in migrate_job_ids]
    exclude_events = [e for e in all_events if e["job_id"] not in migrate_job_ids]

    all_notifs = conn.execute(
        "SELECT id, job_id, user_id, notification_type FROM job_notifications ORDER BY id"
    ).fetchall()
    migrate_notifs = [n for n in all_notifs if n["job_id"] in migrate_job_ids]
    exclude_notifs = [n for n in all_notifs if n["job_id"] not in migrate_job_ids]

    # ------------------------------------------------------------------
    # 3. Devices: exclude the named emulator row only
    # ------------------------------------------------------------------
    all_devices = conn.execute(
        "SELECT device_id, user_id, platform, "
        "(push_token IS NOT NULL) AS has_push_token "
        "FROM devices ORDER BY created_at"
    ).fetchall()
    migrate_devices = [d for d in all_devices if d["device_id"] not in EXCLUDED_DEVICE_IDS]
    exclude_devices = [d for d in all_devices if d["device_id"] in EXCLUDED_DEVICE_IDS]

    # ------------------------------------------------------------------
    # 4. users / machines / api_clients: migrate everything, unchanged
    # ------------------------------------------------------------------
    all_users = conn.execute(
        "SELECT user_id, email, display_name, (password_hash IS NOT NULL) AS has_password_hash "
        "FROM users ORDER BY created_at"
    ).fetchall()
    all_machines = conn.execute(
        "SELECT machine_id, user_id, machine_name FROM machines ORDER BY created_at"
    ).fetchall()
    all_api_clients = conn.execute(
        "SELECT user_id, machine_id, enabled, length(api_key) AS key_len "
        "FROM api_clients ORDER BY created_at"
    ).fetchall()

    # ------------------------------------------------------------------
    # 5. Integrity checks -- fail loudly rather than silently migrating
    #    something inconsistent.
    # ------------------------------------------------------------------
    problems = []

    # (a) Pre-existing orphans in the SOURCE data itself (unrelated to our
    #     filter -- a job_events/job_notifications row whose job_id does not
    #     exist in jobs at all would be a real local data-integrity bug).
    all_job_ids = {j["job_id"] for j in all_jobs}
    src_orphan_events = [e for e in all_events if e["job_id"] not in all_job_ids]
    src_orphan_notifs = [n for n in all_notifs if n["job_id"] not in all_job_ids]
    if src_orphan_events:
        problems.append(
            f"{len(src_orphan_events)} job_events row(s) reference a job_id "
            f"that does not exist in `jobs` at all (pre-existing source data issue)."
        )
    if src_orphan_notifs:
        problems.append(
            f"{len(src_orphan_notifs)} job_notifications row(s) reference a job_id "
            f"that does not exist in `jobs` at all (pre-existing source data issue)."
        )

    # (b) Every row we plan to MIGRATE must have its FK parent also in the
    #     migrate set (never partially migrate a child without its parent).
    all_user_ids = {u["user_id"] for u in all_users}
    all_machine_ids = {m["machine_id"] for m in all_machines}

    bad_device_fk = [d for d in migrate_devices if d["user_id"] not in all_user_ids]
    bad_machine_fk = [m for m in all_machines if m["user_id"] not in all_user_ids]
    bad_apiclient_fk = [
        a for a in all_api_clients
        if a["user_id"] not in all_user_ids or a["machine_id"] not in all_machine_ids
    ]
    bad_event_fk = [e for e in migrate_events if e["job_id"] not in migrate_job_ids]
    bad_notif_fk = [
        n for n in migrate_notifs
        if n["job_id"] not in migrate_job_ids or n["user_id"] not in all_user_ids
    ]

    for label, bad in (
        ("devices -> users", bad_device_fk),
        ("machines -> users", bad_machine_fk),
        ("api_clients -> users/machines", bad_apiclient_fk),
        ("job_events -> jobs (post-filter)", bad_event_fk),
        ("job_notifications -> jobs/users (post-filter)", bad_notif_fk),
    ):
        if bad:
            problems.append(f"{len(bad)} FK inconsistency in {label}.")

    # ------------------------------------------------------------------
    # 6. Sequence setval targets
    # ------------------------------------------------------------------
    seq_events_local = conn.execute("SELECT last_value FROM job_events_id_seq").fetchone()["last_value"]
    seq_notifs_local = conn.execute("SELECT last_value FROM job_notifications_id_seq").fetchone()["last_value"]
    max_migrated_event_id = max((e["id"] for e in migrate_events), default=0)
    max_migrated_notif_id = max((n["id"] for n in migrate_notifs), default=0)

    conn.rollback()  # read-only transaction; nothing to commit, closing cleanly
    conn.close()

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    print()
    print("--- Rows to migrate / exclude, per table ---")
    print(f"users              : migrate {len(all_users):3d}  exclude   0")
    print(f"machines           : migrate {len(all_machines):3d}  exclude   0")
    print(f"api_clients        : migrate {len(all_api_clients):3d}  exclude   0")
    print(f"devices            : migrate {len(migrate_devices):3d}  exclude {len(exclude_devices):3d}")
    print(f"jobs               : migrate {len(migrate_jobs):3d}  exclude {len(exclude_jobs):3d}")
    print(f"job_events         : migrate {len(migrate_events):3d}  exclude {len(exclude_events):3d}")
    print(f"job_notifications  : migrate {len(migrate_notifs):3d}  exclude {len(exclude_notifs):3d}")

    print()
    print(f"--- Excluded devices ({len(exclude_devices)}) ---")
    for d in exclude_devices:
        print(f"  {d['device_id']}  (user_id={d['user_id']}, platform={d['platform']})")

    print()
    print(f"--- Excluded jobs ({len(exclude_jobs)}) -- non-GUID job_id (synthetic test rows) ---")
    for j in exclude_jobs:
        print(f"  {j['job_id']:<28} name={j['name']!r:<32} status={j['status']:<10} machine={j['machine_id']}")

    print()
    print("--- Genuine job kept despite being stale (per explicit instruction) ---")
    stale = next((j for j in migrate_jobs if j["job_id"] == "9f61ab41-831e-46a8-ae06-059f39b7df07"), None)
    if stale:
        print(f"  {stale['job_id']}  name={stale['name']!r} status={stale['status']} "
              f"updated_at={stale['updated_at']}  -- KEPT, no technical reason found to exclude it")
    else:
        print("  NOT FOUND -- unexpected, please re-check (job_id may have changed)")

    print()
    print("--- Integrity check ---")
    if problems:
        print("  FAILED -- issues found:")
        for p in problems:
            print("   -", p)
    else:
        print("  PASSED -- no orphans, no FK inconsistencies in either the source data")
        print("  or the filtered migrate-set.")

    print()
    print("--- Sequence setval targets ---")
    print(f"  job_events_id_seq        local current last_value = {seq_events_local}")
    print(f"                           MAX(id) among migrated rows = {max_migrated_event_id}")
    print(f"                           -> recommended setval target = {max_migrated_event_id}")
    print(f"  job_notifications_id_seq local current last_value = {seq_notifs_local}")
    print(f"                           MAX(id) among migrated rows = {max_migrated_notif_id}")
    print(f"                           -> recommended setval target = {max_migrated_notif_id}")
    print()
    print("  (Recommended target = highest id actually being inserted, so Supabase's")
    print("   next auto-generated id continues cleanly above what will really exist")
    print("   there. The excluded synthetic rows' ids are simply never reused --")
    print("   harmless, since nothing else references job_events/job_notifications ids.)")

    print()
    print("--- Sensitive columns (values never printed) ---")
    n_hash = sum(1 for u in all_users if u["has_password_hash"])
    print(f"  users.password_hash   : {n_hash}/{len(all_users)} rows have a hash -- would migrate as-is (opaque hash, not plaintext)")
    print(f"  api_clients.api_key   : {len(all_api_clients)} live key(s) -- would migrate with EXACT existing value (required: plugin configs depend on it)")
    n_tok = sum(1 for d in migrate_devices if d["has_push_token"])
    print(f"  devices.push_token    : {n_tok}/{len(migrate_devices)} migrated devices have a token -- would migrate as-is")

    print()
    print("=" * 78)
    print("DRY RUN COMPLETE. Nothing was written anywhere (local read-only,")
    print("no Supabase connection was ever opened).")
    print("=" * 78)


if __name__ == "__main__":
    main()
