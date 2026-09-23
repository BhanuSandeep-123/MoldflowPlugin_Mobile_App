"""backend/register_machine_client.py
----------------------------------
Zero-external-dependency script to register a workstation machine and API key.
Works directly with SQLite (backend/moldflow_jobs.db) or PostgreSQL (when DATABASE_URL is set).
Enforces:
- Verifies user exists in users table
- Inserts or updates machine in machines table
- Inserts or updates API key in api_clients table
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "moldflow_jobs.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def register_machine(
    machine_id: str,
    user_id: str = "DEV-USER-001",
    machine_name: str | None = None,
    api_key: str | None = None,
) -> dict[str, str]:
    machine_id = machine_id.strip()
    user_id = user_id.strip()
    if not machine_id:
        raise ValueError("machine_id cannot be empty")
    if not user_id:
        raise ValueError("user_id cannot be empty")

    if not machine_name:
        machine_name = f"Workstation {machine_id}"

    if not api_key:
        api_key = f"mf-client-{machine_id.lower()}-{uuid.uuid4().hex[:12]}"

    ts = now_utc()

    conn = get_connection()
    try:
        cur = conn.cursor()
        placeholder = "%s" if DATABASE_URL else "?"

        # 1. Verify user exists
        cur.execute(
            f"SELECT user_id, display_name FROM users WHERE user_id = {placeholder}",
            (user_id,),
        )
        user_row = cur.fetchone()
        if not user_row:
            # If development user doesn't exist yet, insert default
            cur.execute(
                f"""
                INSERT INTO users (user_id, display_name, email, password_hash, created_at)
                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user_id, "Development User", "dev@example.local", "hash", ts),
            )

        # 2. Upsert machine
        cur.execute(
            f"""
            INSERT INTO machines (
                machine_id,
                user_id,
                machine_name,
                created_at,
                last_seen_at
            )
            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
            ON CONFLICT (machine_id)
            DO UPDATE SET
                user_id = excluded.user_id,
                machine_name = excluded.machine_name,
                last_seen_at = excluded.last_seen_at
            """,
            (machine_id, user_id, machine_name, ts, ts),
        )

        # 3. Upsert API client
        cur.execute(
            f"""
            INSERT INTO api_clients (
                api_key,
                user_id,
                machine_id,
                enabled,
                created_at
            )
            VALUES ({placeholder}, {placeholder}, {placeholder}, 1, {placeholder})
            ON CONFLICT (api_key)
            DO UPDATE SET
                user_id = excluded.user_id,
                machine_id = excluded.machine_id,
                enabled = 1
            """,
            (api_key, user_id, machine_id, ts),
        )

        conn.commit()
    finally:
        conn.close()

    return {
        "machine_id": machine_id,
        "machine_name": machine_name,
        "user_id": user_id,
        "api_key": api_key,
        "registered_at": ts,
        "database": "PostgreSQL" if DATABASE_URL else f"SQLite ({DB_PATH})",
    }


def register_machine_remote(
    backend_url: str,
    machine_id: str,
    user_id: str = "DEV-USER-001",
    machine_name: str | None = None,
    api_key: str | None = None,
    ingestion_key: str = "dev-license-ingestion-key-2026",
) -> dict[str, str]:
    import json
    import urllib.request
    import urllib.error

    machine_id = machine_id.strip()
    user_id = user_id.strip()
    if not machine_name:
        machine_name = f"Workstation {machine_id}"
    if not api_key:
        api_key = f"mf-client-{machine_id.lower()}-{uuid.uuid4().hex[:12]}"

    endpoint = backend_url.rstrip("/") + "/internal/register-client"
    payload = json.dumps({
        "api_key": api_key,
        "user_id": user_id,
        "machine_id": machine_id,
        "machine_name": machine_name,
    }).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Bearer {ingestion_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "machine_id": machine_id,
                "machine_name": machine_name,
                "user_id": user_id,
                "api_key": api_key,
                "registered_at": now_utc(),
                "database": f"Remote Backend ({backend_url})",
                "remote_response": str(data),
            }
    except urllib.error.HTTPError as ex:
        err_body = ex.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {ex.code} from {endpoint}: {err_body}") from ex


def main():
    parser = argparse.ArgumentParser(description="Register a workstation machine and API key.")
    parser.add_argument("--machine-id", required=True, help="Machine hostname or ID (e.g. DESKTOP-23TMNR6)")
    parser.add_argument("--user-id", default="DEV-USER-001", help="Owner user ID (default: DEV-USER-001)")
    parser.add_argument("--machine-name", default=None, help="Display name for the machine")
    parser.add_argument("--api-key", default=None, help="Custom API key (auto-generated if omitted)")
    parser.add_argument("--backend-url", default=None, help="Register via remote backend HTTP URL (e.g. https://moldflowplugin-mobile-app.onrender.com)")
    parser.add_argument("--ingestion-key", default=os.getenv("LICENSE_INGESTION_KEY", "dev-license-ingestion-key-2026"), help="Secret ingestion key for remote registration")

    args = parser.parse_args()

    try:
        if args.backend_url:
            res = register_machine_remote(
                backend_url=args.backend_url,
                machine_id=args.machine_id,
                user_id=args.user_id,
                machine_name=args.machine_name,
                api_key=args.api_key,
                ingestion_key=args.ingestion_key,
            )
        else:
            res = register_machine(
                machine_id=args.machine_id,
                user_id=args.user_id,
                machine_name=args.machine_name,
                api_key=args.api_key,
            )
        print("============================================================")
        print(" Machine Registration Successful")
        print("============================================================")
        print(f" Database/Target: {res['database']}")
        print(f" Machine ID     : {res['machine_id']}")
        print(f" Machine Name   : {res['machine_name']}")
        print(f" User ID        : {res['user_id']}")
        print(f" API Key        : {res['api_key']}")
        print(f" Registered At  : {res['registered_at']}")
        print("============================================================")
    except Exception as e:
        print(f"ERROR registering machine: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
