import sqlite3
import getpass
import psycopg


SQLITE_DB = r"C:\MF\MoldflowSynergyPlugin\mobile_backend\moldflow_jobs.db"


def main():
    print("=" * 70)
    print("SQLITE -> POSTGRESQL INDEPENDENT VERIFICATION")
    print("=" * 70)

    # ------------------------------------------------------------
    # SQLITE READ-ONLY
    # ------------------------------------------------------------

    print("\nConnecting to SQLite READ-ONLY...")

    sqlite_conn = sqlite3.connect(
        f"file:{SQLITE_DB}?mode=ro",
        uri=True
    )

    sqlite_cur = sqlite_conn.cursor()

    sqlite_jobs = sqlite_cur.execute("""
        SELECT
            job_id,
            name,
            job_type,
            status,
            percent,
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
        ORDER BY job_id
    """).fetchall()

    sqlite_event_summary = sqlite_cur.execute("""
        SELECT
            job_id,
            COUNT(*),
            MIN(received_at),
            MAX(received_at)
        FROM job_events
        GROUP BY job_id
        ORDER BY job_id
    """).fetchall()

    sqlite_notification_summary = sqlite_cur.execute("""
        SELECT
            job_id,
            user_id,
            notification_type,
            COUNT(*),
            MIN(sent_at),
            MAX(sent_at)
        FROM job_notifications
        GROUP BY
            job_id,
            user_id,
            notification_type
        ORDER BY job_id
    """).fetchall()

    sqlite_event_count = sqlite_cur.execute(
        "SELECT COUNT(*) FROM job_events"
    ).fetchone()[0]

    sqlite_notification_count = sqlite_cur.execute(
        "SELECT COUNT(*) FROM job_notifications"
    ).fetchone()[0]

    sqlite_conn.close()

    # ------------------------------------------------------------
    # POSTGRESQL
    # ------------------------------------------------------------

    print("Connecting to PostgreSQL...")

    password = getpass.getpass(
        "\nEnter PostgreSQL password: "
    )

    pg_conn = psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname="moldflow_mobile",
        user="moldflow_app",
        password=password,
    )

    pg_cur = pg_conn.cursor()

    # ------------------------------------------------------------
    # JOBS
    # ------------------------------------------------------------

    pg_jobs = pg_cur.execute("""
        SELECT
            job_id,
            name,
            job_type,
            status,
            percent,
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
        ORDER BY job_id
    """).fetchall()

    # ------------------------------------------------------------
    # EVENT SUMMARY
    # ------------------------------------------------------------

    pg_event_summary = pg_cur.execute("""
        SELECT
            job_id,
            COUNT(*),
            MIN(received_at),
            MAX(received_at)
        FROM job_events
        GROUP BY job_id
        ORDER BY job_id
    """).fetchall()

    pg_event_count = pg_cur.execute(
        "SELECT COUNT(*) FROM job_events"
    ).fetchone()[0]

    # ------------------------------------------------------------
    # NOTIFICATION SUMMARY
    # ------------------------------------------------------------

    pg_notification_summary = pg_cur.execute("""
        SELECT
            job_id,
            user_id,
            notification_type,
            COUNT(*),
            MIN(sent_at),
            MAX(sent_at)
        FROM job_notifications
        GROUP BY
            job_id,
            user_id,
            notification_type
        ORDER BY job_id
    """).fetchall()

    pg_notification_count = pg_cur.execute(
        "SELECT COUNT(*) FROM job_notifications"
    ).fetchone()[0]

    # ------------------------------------------------------------
    # CLOSE
    # ------------------------------------------------------------

    pg_cur.close()
    pg_conn.close()

    # ------------------------------------------------------------
    # RESULTS
    # ------------------------------------------------------------

    print("\n" + "=" * 70)
    print("ROW COUNT VERIFICATION")
    print("=" * 70)

    print(
        f"SQLite jobs:              {len(sqlite_jobs)}"
    )
    print(
        f"PostgreSQL jobs:          {len(pg_jobs)}"
    )

    print(
        f"SQLite job events:        {sqlite_event_count}"
    )
    print(
        f"PostgreSQL job events:    {pg_event_count}"
    )

    print(
        f"SQLite notifications:     {sqlite_notification_count}"
    )
    print(
        f"PostgreSQL notifications: {pg_notification_count}"
    )

    all_ok = True

    if len(sqlite_jobs) != len(pg_jobs):
        print("❌ JOB COUNT MISMATCH")
        all_ok = False
    else:
        print("✅ JOB COUNT MATCH")

    if sqlite_event_count != pg_event_count:
        print("❌ EVENT COUNT MISMATCH")
        all_ok = False
    else:
        print("✅ EVENT COUNT MATCH")

    if sqlite_notification_count != pg_notification_count:
        print("❌ NOTIFICATION COUNT MISMATCH")
        all_ok = False
    else:
        print("✅ NOTIFICATION COUNT MATCH")

    # ------------------------------------------------------------
    # JOB DATA COMPARISON
    # ------------------------------------------------------------

    print("\n" + "=" * 70)
    print("JOB DATA VERIFICATION")
    print("=" * 70)

    sqlite_job_map = {
        row[0]: row
        for row in sqlite_jobs
    }

    pg_job_map = {
        row[0]: row
        for row in pg_jobs
    }

    sqlite_ids = set(sqlite_job_map)
    pg_ids = set(pg_job_map)

    missing_in_pg = sqlite_ids - pg_ids
    extra_in_pg = pg_ids - sqlite_ids

    if missing_in_pg:
        print("\n❌ Missing PostgreSQL jobs:")

        for job_id in sorted(missing_in_pg):
            print(f"   {job_id}")

        all_ok = False
    else:
        print("\n✅ No SQLite jobs missing in PostgreSQL")

    if extra_in_pg:
        print("\n❌ Extra PostgreSQL jobs:")

        for job_id in sorted(extra_in_pg):
            print(f"   {job_id}")

        all_ok = False
    else:
        print("✅ No unexpected PostgreSQL jobs")

    # ------------------------------------------------------------
    # COMPARE EACH JOB
    # ------------------------------------------------------------

    job_mismatches = []

    for job_id in sorted(sqlite_ids & pg_ids):

        s = sqlite_job_map[job_id]
        p = pg_job_map[job_id]

        # SQLite and PostgreSQL represent finished differently.
        s_finished = bool(s[5])
        p_finished = bool(p[5])

        fields = [
            ("name", s[1], p[1]),
            ("job_type", s[2], p[2]),
            ("status", s[3], p[3]),
            ("percent", s[4], p[4]),
            ("finished", s_finished, p_finished),
            ("error_message", s[6], p[6]),
            ("user_id", s[7], p[7]),
            ("machine_id", s[8], p[8]),
            ("created_at", s[9], p[9]),
            ("updated_at", s[10], p[10]),
            ("update_count", s[11], p[11]),
            ("scm_job_id", s[12], p[12]),
            ("scm_type", s[13], p[13]),
            ("compute_source", s[14], p[14]),
            ("scm_user", s[15], p[15]),
            ("worker", s[16], p[16]),
            ("parent_job_id", s[17], p[17]),
        ]

        for field, sqlite_value, pg_value in fields:

            if sqlite_value != pg_value:
                job_mismatches.append(
                    (
                        job_id,
                        field,
                        sqlite_value,
                        pg_value
                    )
                )

    if job_mismatches:

        print("\n❌ JOB DATA MISMATCHES:")

        for job_id, field, sqlite_value, pg_value in job_mismatches:

            print(
                f"\n  Job: {job_id}"
            )
            print(
                f"  Field: {field}"
            )
            print(
                f"  SQLite:     {sqlite_value!r}"
            )
            print(
                f"  PostgreSQL: {pg_value!r}"
            )

        all_ok = False

    else:
        print(
            "\n✅ All common job records match"
        )

    # ------------------------------------------------------------
    # EVENT SUMMARY COMPARISON
    # ------------------------------------------------------------

    print("\n" + "=" * 70)
    print("JOB EVENT VERIFICATION")
    print("=" * 70)

    sqlite_events = {
        row[0]: row
        for row in sqlite_event_summary
    }

    pg_events = {
        row[0]: row
        for row in pg_event_summary
    }

    event_job_ids = (
        set(sqlite_events) |
        set(pg_events)
    )

    event_mismatches = []

    for job_id in sorted(event_job_ids):

        s = sqlite_events.get(job_id)
        p = pg_events.get(job_id)

        if s is None:
            event_mismatches.append(
                (job_id, "missing_in_sqlite", None, p)
            )
            continue

        if p is None:
            event_mismatches.append(
                (job_id, "missing_in_postgresql", s, None)
            )
            continue

        # Compare:
        # count
        # minimum timestamp
        # maximum timestamp

        if s[1] != p[1]:
            event_mismatches.append(
                (job_id, "event_count", s[1], p[1])
            )

        if s[2] != p[2]:
            event_mismatches.append(
                (job_id, "first_event", s[2], p[2])
            )

        if s[3] != p[3]:
            event_mismatches.append(
                (job_id, "last_event", s[3], p[3])
            )

    if event_mismatches:

        print("\n❌ EVENT MISMATCHES:")

        for item in event_mismatches:
            print("  ", item)

        all_ok = False

    else:
        print(
            "\n✅ Event counts and timestamps match"
        )

    # ------------------------------------------------------------
    # NOTIFICATION COMPARISON
    # ------------------------------------------------------------

    print("\n" + "=" * 70)
    print("NOTIFICATION VERIFICATION")
    print("=" * 70)

    sqlite_notifications = {
        row[0]: row
        for row in sqlite_notification_summary
    }

    pg_notifications = {
        row[0]: row
        for row in pg_notification_summary
    }

    notification_keys = (
        set(sqlite_notifications) |
        set(pg_notifications)
    )

    notification_mismatches = []

    for key in sorted(notification_keys):

        s = sqlite_notifications.get(key)
        p = pg_notifications.get(key)

        if s is None:
            notification_mismatches.append(
                (key, "missing_in_sqlite", None, p)
            )
            continue

        if p is None:
            notification_mismatches.append(
                (key, "missing_in_postgresql", s, None)
            )
            continue

        if s[1:] != p[1:]:
            notification_mismatches.append(
                (key, "data", s, p)
            )

    if notification_mismatches:

        print("\n❌ NOTIFICATION MISMATCHES:")

        for item in notification_mismatches:
            print("  ", item)

        all_ok = False

    else:
        print(
            "\n✅ Notification data matches"
        )

    # ------------------------------------------------------------
    # FINAL RESULT
    # ------------------------------------------------------------

    print("\n" + "=" * 70)

    if all_ok:

        print("✅ MIGRATION VERIFICATION PASSED")
        print("=" * 70)

        print("""
SQLite and PostgreSQL contain matching application data.

Safe to proceed to backend PostgreSQL configuration.
""")

    else:

        print("❌ MIGRATION VERIFICATION FAILED")
        print("=" * 70)

        print("""
Do NOT modify the backend yet.

There is at least one data difference between
SQLite and PostgreSQL. Review the output above.
""")

        raise SystemExit(1)


if __name__ == "__main__":
    main()