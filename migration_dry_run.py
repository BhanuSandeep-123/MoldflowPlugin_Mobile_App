import sqlite3
import getpass
import psycopg


SQLITE_DB = r"C:\MF\MoldflowSynergyPlugin\mobile_backend\moldflow_jobs.db"


def get_sqlite_data():
    print("\nConnecting to SQLite in READ-ONLY mode...")

    conn = sqlite3.connect(
        f"file:{SQLITE_DB}?mode=ro",
        uri=True
    )

    cur = conn.cursor()

    data = {}

    tables = [
        "users",
        "machines",
        "api_clients",
        "devices",
        "jobs",
        "job_events",
        "job_notifications",
    ]

    for table in tables:
        rows = cur.execute(
            f'SELECT * FROM "{table}"'
        ).fetchall()

        data[table] = rows

    conn.close()

    return data


def get_postgres_data(password):
    print("Connecting to PostgreSQL...")

    conn = psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname="moldflow_mobile",
        user="moldflow_app",
        password=password,
    )

    cur = conn.cursor()

    data = {}

    tables = [
        "users",
        "machines",
        "api_clients",
        "devices",
        "jobs",
        "job_events",
        "job_notifications",
    ]

    for table in tables:
        rows = cur.execute(
            f'SELECT * FROM "{table}"'
        ).fetchall()

        data[table] = rows

    conn.close()

    return data


def get_column_names(password):
    conn = psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname="moldflow_mobile",
        user="moldflow_app",
        password=password,
    )

    cur = conn.cursor()

    result = {}

    for table in [
        "users",
        "machines",
        "api_clients",
        "devices",
        "jobs",
        "job_events",
        "job_notifications",
    ]:
        columns = cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        ).fetchall()

        result[table] = [row[0] for row in columns]

    conn.close()

    return result


def main():
    print("=" * 70)
    print("MOLDFLOW SQLITE -> POSTGRESQL MIGRATION DRY RUN")
    print("=" * 70)

    print("\nIMPORTANT:")
    print("This script performs NO INSERT, UPDATE, DELETE, or schema changes.")
    print("SQLite is opened READ-ONLY.")
    print("PostgreSQL is opened READ-ONLY for this inspection.")

    password = getpass.getpass(
        "\nEnter PostgreSQL password: "
    )

    sqlite_data = get_sqlite_data()
    postgres_data = get_postgres_data(password)
    postgres_columns = get_column_names(password)

    print("\n" + "=" * 70)
    print("SOURCE DATA")
    print("=" * 70)

    for table, rows in sqlite_data.items():
        print(f"{table:20} {len(rows):>6} rows")

    print("\n" + "=" * 70)
    print("CURRENT POSTGRESQL DATA")
    print("=" * 70)

    for table, rows in postgres_data.items():
        print(f"{table:20} {len(rows):>6} rows")

    print("\n" + "=" * 70)
    print("JOB ID CONFLICT CHECK")
    print("=" * 70)

    sqlite_job_ids = {
        row[0]
        for row in sqlite_data["jobs"]
    }

    postgres_job_ids = {
        row[0]
        for row in postgres_data["jobs"]
    }

    job_conflicts = sqlite_job_ids & postgres_job_ids

    print(f"SQLite jobs:             {len(sqlite_job_ids)}")
    print(f"PostgreSQL jobs:         {len(postgres_job_ids)}")
    print(f"Job ID conflicts:        {len(job_conflicts)}")

    if job_conflicts:
        for job_id in sorted(job_conflicts):
            print(f"  CONFLICT: {job_id}")
    else:
        print("  NONE")

    print("\n" + "=" * 70)
    print("JOB EVENT ID CHECK")
    print("=" * 70)

    sqlite_event_ids = {
        row[0]
        for row in sqlite_data["job_events"]
    }

    postgres_event_ids = {
        row[0]
        for row in postgres_data["job_events"]
    }

    event_conflicts = sqlite_event_ids & postgres_event_ids

    print(f"SQLite event IDs:       {len(sqlite_event_ids)}")
    print(f"PostgreSQL event IDs:   {len(postgres_event_ids)}")
    print(f"Event ID conflicts:     {len(event_conflicts)}")

    if event_conflicts:
        print("  Existing overlapping IDs:")
        for event_id in sorted(event_conflicts):
            print(f"    {event_id}")

    print("\n" + "=" * 70)
    print("NOTIFICATION ID CHECK")
    print("=" * 70)

    sqlite_notification_ids = {
        row[0]
        for row in sqlite_data["job_notifications"]
    }

    postgres_notification_ids = {
        row[0]
        for row in postgres_data["job_notifications"]
    }

    notification_conflicts = (
        sqlite_notification_ids
        & postgres_notification_ids
    )

    print(
        f"SQLite notification IDs:     "
        f"{len(sqlite_notification_ids)}"
    )

    print(
        f"PostgreSQL notification IDs: "
        f"{len(postgres_notification_ids)}"
    )

    print(
        f"Notification ID conflicts:   "
        f"{len(notification_conflicts)}"
    )

    if notification_conflicts:
        print("  Existing overlapping IDs:")
        for notification_id in sorted(notification_conflicts):
            print(f"    {notification_id}")

    print("\n" + "=" * 70)
    print("PARENT RECORD CHECK")
    print("=" * 70)

    # users
    sqlite_users = {
        row[0]: row
        for row in sqlite_data["users"]
    }

    postgres_users = {
        row[0]: row
        for row in postgres_data["users"]
    }

    print(
        f"Users - SQLite: {len(sqlite_users)}, "
        f"PostgreSQL: {len(postgres_users)}"
    )

    for user_id in sqlite_users:
        if user_id in postgres_users:
            print(f"  USER EXISTS: {user_id}")
        else:
            print(f"  USER TO INSERT: {user_id}")

    # machines
    sqlite_machines = {
        row[0]: row
        for row in sqlite_data["machines"]
    }

    postgres_machines = {
        row[0]: row
        for row in postgres_data["machines"]
    }

    print(
        f"\nMachines - SQLite: {len(sqlite_machines)}, "
        f"PostgreSQL: {len(postgres_machines)}"
    )

    for machine_id in sqlite_machines:
        if machine_id in postgres_machines:
            print(f"  MACHINE EXISTS: {machine_id}")
        else:
            print(f"  MACHINE TO INSERT: {machine_id}")

    # devices
    sqlite_devices = {
        row[0]: row
        for row in sqlite_data["devices"]
    }

    postgres_devices = {
        row[0]: row
        for row in postgres_data["devices"]
    }

    print(
        f"\nDevices - SQLite: {len(sqlite_devices)}, "
        f"PostgreSQL: {len(postgres_devices)}"
    )

    for device_id in sqlite_devices:
        if device_id in postgres_devices:
            print(f"  DEVICE EXISTS: {device_id}")
        else:
            print(f"  DEVICE TO INSERT: {device_id}")

    print("\n" + "=" * 70)
    print("POSTGRESQL COLUMN ORDER")
    print("=" * 70)

    for table, columns in postgres_columns.items():
        print(f"\n{table}:")
        for index, column in enumerate(columns, start=1):
            print(f"  {index:2}. {column}")

    print("\n" + "=" * 70)
    print("DRY RUN COMPLETE")
    print("=" * 70)

    print("\nNO DATABASE CHANGES WERE MADE.")


if __name__ == "__main__":
    main()