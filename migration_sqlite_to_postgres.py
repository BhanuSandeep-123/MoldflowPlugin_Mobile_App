import sqlite3
import getpass
import psycopg


SQLITE_DB = r"C:\MF\MoldflowSynergyPlugin\mobile_backend\moldflow_jobs.db"


def main():
    print("=" * 70)
    print("MOLDFLOW SQLITE -> POSTGRESQL MIGRATION")
    print("=" * 70)

    print("\nThis migration will:")
    print("  - Preserve existing user")
    print("  - Preserve existing machine")
    print("  - Preserve existing API client")
    print("  - Preserve existing device / FCM token")
    print("  - Remove PostgreSQL test jobs")
    print("  - Import 24 SQLite jobs")
    print("  - Import 548 SQLite job events")
    print("  - Import 14 SQLite notifications")
    print("  - Reset PostgreSQL sequences")
    print("\nSQLite database will NOT be modified.")

    confirm = input(
        "\nType MIGRATE to continue: "
    ).strip()

    if confirm != "MIGRATE":
        print("\nMigration cancelled.")
        return

    password = getpass.getpass(
        "\nEnter PostgreSQL password: "
    )

    # ------------------------------------------------------------
    # READ SQLITE
    # ------------------------------------------------------------

    print("\nConnecting to SQLite READ-ONLY...")

    sqlite_conn = sqlite3.connect(
        f"file:{SQLITE_DB}?mode=ro",
        uri=True
    )

    sqlite_cur = sqlite_conn.cursor()

    # Parent records
    users = sqlite_cur.execute("""
        SELECT
            user_id,
            display_name,
            email,
            password_hash,
            created_at
        FROM users
    """).fetchall()

    machines = sqlite_cur.execute("""
        SELECT
            machine_id,
            user_id,
            machine_name,
            created_at,
            last_seen_at
        FROM machines
    """).fetchall()

    api_clients = sqlite_cur.execute("""
        SELECT
            api_key,
            user_id,
            machine_id,
            enabled,
            created_at
        FROM api_clients
    """).fetchall()

    devices = sqlite_cur.execute("""
        SELECT
            device_id,
            user_id,
            platform,
            push_token,
            created_at,
            updated_at
        FROM devices
    """).fetchall()

    jobs = sqlite_cur.execute("""
        SELECT
            job_id,
            name,
            job_type,
            status,
            percent,
            started,
            finished,
            error_message,
            user_id,
            machine_id,
            created_at,
            updated_at,
            update_count,
            scm_job_id,
            scm_type,
            compute_source,
            scm_user,
            worker,
            parent_job_id
        FROM jobs
        ORDER BY created_at, job_id
    """).fetchall()

    job_events = sqlite_cur.execute("""
        SELECT
            id,
            job_id,
            status,
            percent,
            finished,
            error_message,
            received_at
        FROM job_events
        ORDER BY id
    """).fetchall()

    job_notifications = sqlite_cur.execute("""
        SELECT
            id,
            job_id,
            user_id,
            notification_type,
            sent_at
        FROM job_notifications
        ORDER BY id
    """).fetchall()

    sqlite_conn.close()

    print("\nSQLite records loaded:")
    print(f"  Users:          {len(users)}")
    print(f"  Machines:       {len(machines)}")
    print(f"  API clients:    {len(api_clients)}")
    print(f"  Devices:        {len(devices)}")
    print(f"  Jobs:           {len(jobs)}")
    print(f"  Job events:     {len(job_events)}")
    print(f"  Notifications:  {len(job_notifications)}")

    # ------------------------------------------------------------
    # CONNECT POSTGRES
    # ------------------------------------------------------------

    print("\nConnecting to PostgreSQL...")

    pg_conn = psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname="moldflow_mobile",
        user="moldflow_app",
        password=password,
    )

    pg_cur = pg_conn.cursor()

    try:
        # --------------------------------------------------------
        # START TRANSACTION
        # --------------------------------------------------------

        print("\nStarting PostgreSQL transaction...")

        # psycopg starts a transaction automatically when SQL is
        # executed. Nothing is committed until pg_conn.commit().

        # --------------------------------------------------------
        # VERIFY EXISTING PARENT RECORDS
        # --------------------------------------------------------

        print("\nVerifying parent records...")

        for row in users:
            user_id = row[0]

            existing = pg_cur.execute(
                """
                SELECT user_id
                FROM users
                WHERE user_id = %s
                """,
                (user_id,)
            ).fetchone()

            if existing:
                print(f"  User exists: {user_id}")
            else:
                print(f"  Inserting user: {user_id}")

                pg_cur.execute(
                    """
                    INSERT INTO users
                    (
                        user_id,
                        display_name,
                        email,
                        password_hash,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    row
                )

        for row in machines:
            machine_id = row[0]

            existing = pg_cur.execute(
                """
                SELECT machine_id
                FROM machines
                WHERE machine_id = %s
                """,
                (machine_id,)
            ).fetchone()

            if existing:
                print(f"  Machine exists: {machine_id}")
            else:
                print(f"  Inserting machine: {machine_id}")

                pg_cur.execute(
                    """
                    INSERT INTO machines
                    (
                        machine_id,
                        user_id,
                        machine_name,
                        created_at,
                        last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    row
                )

        for row in api_clients:
            api_key = row[0]

            existing = pg_cur.execute(
                """
                SELECT api_key
                FROM api_clients
                WHERE api_key = %s
                """,
                (api_key,)
            ).fetchone()

            if existing:
                print("  API client exists")
            else:
                print("  Inserting API client")

                pg_cur.execute(
                    """
                    INSERT INTO api_clients
                    (
                        api_key,
                        user_id,
                        machine_id,
                        enabled,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    row
                )

        # --------------------------------------------------------
        # DEVICES
        # --------------------------------------------------------
        #
        # IMPORTANT:
        # Existing PostgreSQL push_token is preserved.
        # We do NOT overwrite it with SQLite's value.
        # --------------------------------------------------------

        for row in devices:
            device_id = row[0]

            existing = pg_cur.execute(
                """
                SELECT device_id
                FROM devices
                WHERE device_id = %s
                """,
                (device_id,)
            ).fetchone()

            if existing:
                print(
                    f"  Device exists and FCM token preserved: "
                    f"{device_id}"
                )
            else:
                print(f"  Inserting device: {device_id}")

                pg_cur.execute(
                    """
                    INSERT INTO devices
                    (
                        device_id,
                        user_id,
                        platform,
                        push_token,
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    row
                )

        # --------------------------------------------------------
        # REMOVE ONLY POSTGRESQL TEST JOBS
        # --------------------------------------------------------

        print("\nRemoving PostgreSQL test jobs...")

        test_job_ids = [
            "string",
            "postgres-test-001",
        ]

        for job_id in test_job_ids:

            exists = pg_cur.execute(
                """
                SELECT job_id
                FROM jobs
                WHERE job_id = %s
                """,
                (job_id,)
            ).fetchone()

            if exists:
                print(f"  Removing test job: {job_id}")

                # Delete notification first because it references jobs.
                pg_cur.execute(
                    """
                    DELETE FROM job_notifications
                    WHERE job_id = %s
                    """,
                    (job_id,)
                )

                # job_events has ON DELETE NO ACTION, so delete events
                # explicitly before deleting the job.
                pg_cur.execute(
                    """
                    DELETE FROM job_events
                    WHERE job_id = %s
                    """,
                    (job_id,)
                )

                pg_cur.execute(
                    """
                    DELETE FROM jobs
                    WHERE job_id = %s
                    """,
                    (job_id,)
                )

            else:
                print(f"  Test job not found: {job_id}")

        # --------------------------------------------------------
        # INSERT JOBS
        # --------------------------------------------------------

        print("\nImporting jobs...")

        inserted_jobs = 0

        for row in jobs:

            row = list(row)

            # SQLite stores finished as 0/1.
            # PostgreSQL expects BOOLEAN.
            row[6] = bool(row[6])

            pg_cur.execute(
                """
                INSERT INTO jobs
                (
                    job_id,
                    name,
                    job_type,
                    status,
                    percent,
                    started,
                    finished,
                    error_message,
                    user_id,
                    machine_id,
                    created_at,
                    updated_at,
                    update_count,
                    scm_job_id,
                    scm_type,
                    compute_source,
                    scm_user,
                    worker,
                    parent_job_id
                )
                VALUES
                (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                """,
                tuple(row)
            )

            inserted_jobs += 1

        print(f"  Jobs inserted: {inserted_jobs}")

        # --------------------------------------------------------
        # INSERT JOB EVENTS
        # --------------------------------------------------------

        print("\nImporting job events...")

        inserted_events = 0

        for row in job_events:

            row = list(row)

            # SQLite stores finished as 0/1.
            # PostgreSQL expects BOOLEAN.
            row[4] = bool(row[4])

            pg_cur.execute(
                """
                INSERT INTO job_events
                (
                    id,
                    job_id,
                    status,
                    percent,
                    finished,
                    error_message,
                    received_at
                )
                VALUES
                (
                    %s, %s, %s, %s,
                    %s, %s, %s
                )
                """,
                tuple(row)
            )

            inserted_events += 1

        print(f"  Job events inserted: {inserted_events}")

        # --------------------------------------------------------
        # INSERT NOTIFICATIONS
        # --------------------------------------------------------

        print("\nImporting job notifications...")

        inserted_notifications = 0

        for row in job_notifications:

            pg_cur.execute(
                """
                INSERT INTO job_notifications
                (
                    id,
                    job_id,
                    user_id,
                    notification_type,
                    sent_at
                )
                VALUES
                (
                    %s, %s, %s, %s, %s
                )
                """,
                row
            )

            inserted_notifications += 1

        print(
            f"  Notifications inserted: "
            f"{inserted_notifications}"
        )

        # --------------------------------------------------------
        # RESET SEQUENCES
        # --------------------------------------------------------

        print("\nResetting PostgreSQL sequences...")

        pg_cur.execute("""
            SELECT setval(
                'job_events_id_seq',
                COALESCE(
                    (SELECT MAX(id) FROM job_events),
                    1
                ),
                true
            )
        """)

        pg_cur.execute("""
            SELECT setval(
                'job_notifications_id_seq',
                COALESCE(
                    (SELECT MAX(id) FROM job_notifications),
                    1
                ),
                true
            )
        """)

        # --------------------------------------------------------
        # VALIDATION BEFORE COMMIT
        # --------------------------------------------------------

        print("\n" + "=" * 70)
        print("VALIDATING MIGRATION")
        print("=" * 70)

        expected_counts = {
            "users": 1,
            "machines": 1,
            "api_clients": 1,
            "devices": 1,
            "jobs": 24,
            "job_events": 548,
            "job_notifications": 14,
        }

        all_ok = True

        for table, expected in expected_counts.items():

            actual = pg_cur.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]

            if actual == expected:
                print(
                    f"  OK   {table:20} "
                    f"{actual:>6} rows"
                )
            else:
                print(
                    f"  FAIL {table:20} "
                    f"expected={expected}, actual={actual}"
                )

                all_ok = False

        # --------------------------------------------------------
        # VALIDATE JOB EVENT RELATIONSHIPS
        # --------------------------------------------------------

        orphan_events = pg_cur.execute("""
            SELECT COUNT(*)
            FROM job_events e
            LEFT JOIN jobs j
                ON e.job_id = j.job_id
            WHERE j.job_id IS NULL
        """).fetchone()[0]

        print(
            f"\n  Orphan job events: "
            f"{orphan_events}"
        )

        if orphan_events != 0:
            all_ok = False

        # --------------------------------------------------------
        # VALIDATE NOTIFICATION RELATIONSHIPS
        # --------------------------------------------------------

        orphan_notifications = pg_cur.execute("""
            SELECT COUNT(*)
            FROM job_notifications n
            LEFT JOIN jobs j
                ON n.job_id = j.job_id
            WHERE j.job_id IS NULL
        """).fetchone()[0]

        print(
            f"  Orphan notifications: "
            f"{orphan_notifications}"
        )

        if orphan_notifications != 0:
            all_ok = False

        # --------------------------------------------------------
        # VALIDATE USER/MACHINE RELATIONSHIPS
        # --------------------------------------------------------

        invalid_jobs = pg_cur.execute("""
            SELECT COUNT(*)
            FROM jobs j
            LEFT JOIN users u
                ON j.user_id = u.user_id
            LEFT JOIN machines m
                ON j.machine_id = m.machine_id
            WHERE u.user_id IS NULL
               OR m.machine_id IS NULL
        """).fetchone()[0]

        print(
            f"  Jobs with invalid parent records: "
            f"{invalid_jobs}"
        )

        if invalid_jobs != 0:
            all_ok = False

        # --------------------------------------------------------
        # COMMIT / ROLLBACK
        # --------------------------------------------------------

        print("\n" + "=" * 70)

        if all_ok:
            print("VALIDATION PASSED")
            print("Committing migration...")
            pg_conn.commit()

            print("\n" + "=" * 70)
            print("MIGRATION SUCCESSFUL")
            print("=" * 70)

        else:
            print("VALIDATION FAILED")
            print("Rolling back migration...")
            pg_conn.rollback()

            print("\n" + "=" * 70)
            print("MIGRATION ROLLED BACK")
            print("=" * 70)

            return

    except Exception as exc:

        print("\n" + "=" * 70)
        print("MIGRATION ERROR")
        print("=" * 70)

        print(type(exc).__name__)
        print(str(exc))

        print("\nRolling back PostgreSQL transaction...")

        pg_conn.rollback()

        print("Rollback complete.")
        raise

    finally:
        pg_cur.close()
        pg_conn.close()


if __name__ == "__main__":
    main()