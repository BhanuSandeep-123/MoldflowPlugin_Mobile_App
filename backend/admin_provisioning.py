"""backend/admin_provisioning.py
------------------------------
Secure Administrative Provisioning CLI for IT / Administrators.

Supports:
1. Creating a real user account with:
   - Email (unique, case-insensitive)
   - Display name
   - Password (hashed with Argon2id; never stored/logged in plaintext)
   - Unique user ID
2. Generating one-time workstation enrollment tokens bound to a user:
   - Uses Phase 2A token format (mf-enroll-<48 hex chars>)
   - Enforces expiration (default 3600 seconds / 1 hour, configurable)
   - Compatible with Install-MoldflowWorkstation.ps1 (-EnrollmentTokenFile)
   - Seamlessly consumed by POST /api/workstation/enroll
3. Combined onboarding workflow (create user + generate token)
4. Listing users and enrolled workstations

Security:
- Never prints or stores plain passwords.
- Only stores secure Argon2id hashes in database.
- One-time enrollment tokens expire and are marked consumed on use.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

try:
    from pwdlib import PasswordHash
    _HAS_PWDLIB = True
except ImportError:
    _HAS_PWDLIB = False

BASE_DIR = Path(__file__).resolve().parent
SQLITE_DB_PATH = BASE_DIR / "moldflow_jobs.db"


def now_utc() -> str:
    """Return current UTC ISO 8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def load_env_file(env_path: Path) -> None:
    """Simple parser to load .env variables if not already present in os.environ."""
    if not env_path.is_file():
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


def resolve_db_target(explicit_target: str | None = None) -> tuple[str, bool]:
    """
    Resolves the database target URL or SQLite file path.
    Returns: (target_string, is_postgres_bool)
    """
    if explicit_target and explicit_target.strip():
        target = explicit_target.strip()
        is_pg = target.startswith("postgresql://") or target.startswith("postgres://")
        return target, is_pg

    # Check common .env locations if not already set in environment
    if not os.getenv("DATABASE_URL") and not os.getenv("SUPABASE_DATABASE_URL"):
        candidates = [
            BASE_DIR / ".env",
            Path("C:/MF/MoldflowSynergyPlugin/mobile_backend/.env"),
            BASE_DIR.parent / ".env",
        ]
        for c in candidates:
            load_env_file(c)

    url = os.getenv("SUPABASE_DATABASE_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()
    if url and (url.startswith("postgresql://") or url.startswith("postgres://")):
        return url, True

    return str(SQLITE_DB_PATH), False


@contextmanager
def get_db_connection(db_target: str | None = None) -> Generator[Any, None, None]:
    """Yields a database connection with dict-like row access."""
    target, is_postgres = resolve_db_target(db_target)

    if is_postgres:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(target, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def db_execute(conn: Any, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> Any:
    """Executes SQL with placeholder translation (? -> %s for PostgreSQL)."""
    p = tuple(params) if params is not None else ()
    is_postgres = hasattr(conn, "info") or "psycopg" in str(type(conn)).lower()
    if is_postgres:
        pg_sql = sql.replace("?", "%s")
        return conn.execute(pg_sql, p)
    return conn.execute(sql, p)


def hash_password(plain_password: str) -> str:
    """Securely hash a password using Argon2id via pwdlib."""
    if not _HAS_PWDLIB:
        raise RuntimeError("pwdlib is required for secure Argon2id password hashing")
    hasher = PasswordHash.recommended()
    return hasher.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against an Argon2id hash."""
    if not _HAS_PWDLIB:
        raise RuntimeError("pwdlib is required for password verification")
    hasher = PasswordHash.recommended()
    return hasher.verify(plain_password, hashed_password)


# ============================================================================
# Core Provisioning Operations
# ============================================================================

def create_user(
    email: str,
    display_name: str,
    password: str,
    user_id: str | None = None,
    db_target: str | None = None,
) -> dict[str, Any]:
    """
    Creates a new user record in the users table.
    Enforces:
    - Email uniqueness (case-insensitive)
    - Argon2id password hashing (plain password is never stored or returned)
    - Non-empty display name
    - Unique user_id generation (USR-<hex>) if not supplied
    """
    clean_email = email.strip()
    clean_name = display_name.strip()

    if not clean_email or "@" not in clean_email or "." not in clean_email:
        raise ValueError(f"Invalid email address: '{email}'")
    if not clean_name:
        raise ValueError("Display name cannot be empty")
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters long")

    if not user_id or not user_id.strip():
        clean_prefix = re.sub(r"[^a-zA-Z0-9]", "", clean_email.split("@")[0])[:10].upper() or "USER"
        user_id = f"USR-{clean_prefix}-{secrets.token_hex(4).upper()}"
    else:
        user_id = user_id.strip()

    pw_hash = hash_password(password)
    created_at = now_utc()

    with get_db_connection(db_target) as conn:
        # Check duplicate email (case-insensitive)
        existing_email = db_execute(
            conn,
            "SELECT user_id, email FROM users WHERE lower(email) = lower(?)",
            (clean_email,),
        ).fetchone()

        if existing_email is not None:
            raise ValueError(
                f"User with email '{clean_email}' already exists (user_id: {existing_email['user_id']})"
            )

        # Check duplicate user_id
        existing_id = db_execute(
            conn,
            "SELECT user_id FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if existing_id is not None:
            raise ValueError(f"User with user_id '{user_id}' already exists")

        db_execute(
            conn,
            """
            INSERT INTO users (
                user_id,
                display_name,
                email,
                password_hash,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, clean_name, clean_email, pw_hash, created_at),
        )

    return {
        "status": "success",
        "user_id": user_id,
        "display_name": clean_name,
        "email": clean_email,
        "created_at": created_at,
    }


def create_workstation_token(
    user_id: str,
    expires_in_seconds: int = 3600,
    db_target: str | None = None,
) -> dict[str, Any]:
    """
    Creates a one-time workstation enrollment token bound to an existing user.
    Uses the Phase 2A token format: mf-enroll-<48 hex chars>.
    """
    clean_user = user_id.strip()
    if not clean_user:
        raise ValueError("user_id cannot be empty")

    token = f"mf-enroll-{secrets.token_hex(24)}"
    created_at = now_utc()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)).isoformat()

    with get_db_connection(db_target) as conn:
        # Verify user exists
        user_row = db_execute(
            conn,
            "SELECT user_id, display_name, email FROM users WHERE user_id = ?",
            (clean_user,),
        ).fetchone()

        if user_row is None:
            raise ValueError(f"User '{clean_user}' does not exist in users table")

        db_execute(
            conn,
            """
            INSERT INTO enrollment_tokens (
                token,
                user_id,
                created_at,
                expires_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (token, clean_user, created_at, expires_at),
        )

    return {
        "status": "success",
        "token": token,
        "user_id": clean_user,
        "user_name": user_row["display_name"],
        "user_email": user_row["email"],
        "created_at": created_at,
        "expires_at": expires_at,
        "expires_in_seconds": expires_in_seconds,
    }


def list_users(db_target: str | None = None) -> list[dict[str, Any]]:
    """List all registered users along with active device count and workstation count."""
    with get_db_connection(db_target) as conn:
        rows = db_execute(
            conn,
            """
            SELECT
                u.user_id,
                u.display_name,
                u.email,
                u.created_at,
                (SELECT count(*) FROM machines m WHERE m.user_id = u.user_id) AS machine_count,
                (SELECT count(*) FROM devices d WHERE d.user_id = u.user_id) AS device_count
            FROM users u
            ORDER BY u.created_at DESC
            """,
        ).fetchall()

        return [dict(r) for r in rows]


def list_tokens(user_id: str | None = None, db_target: str | None = None) -> list[dict[str, Any]]:
    """List enrollment tokens with status (active, consumed, expired)."""
    now = now_utc()
    sql = """
        SELECT
            token,
            user_id,
            created_at,
            expires_at,
            consumed_at,
            consumed_by_machine_id
        FROM enrollment_tokens
    """
    params: list[Any] = []
    if user_id and user_id.strip():
        sql += " WHERE user_id = ?"
        params.append(user_id.strip())
    sql += " ORDER BY created_at DESC LIMIT 50"

    with get_db_connection(db_target) as conn:
        rows = db_execute(conn, sql, params).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            if item.get("consumed_at"):
                item["state"] = "CONSUMED"
            elif item["expires_at"] < now:
                item["state"] = "EXPIRED"
            else:
                item["state"] = "ACTIVE"
            result.append(item)
        return result


# ============================================================================
# Mobile user -> workstation access (view jobs reported by an assigned machine)
# ============================================================================

def _ensure_access_table(conn: Any) -> None:
    """Idempotent; the backend creates the same table at startup (init_database)."""
    db_execute(
        conn,
        """
        CREATE TABLE IF NOT EXISTS mobile_user_machine_access (
            user_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, machine_id),
            FOREIGN KEY(user_id)
                REFERENCES users(user_id)
                ON DELETE CASCADE
        )
        """,
    )


def grant_machine_access(
    user_id: str,
    machine_id: str,
    db_target: str | None = None,
) -> dict[str, Any]:
    """
    Allows an existing mobile user to view (and cancel/remove, and receive push notifications for)
    jobs reported by an existing, enrolled workstation. Job ownership is NOT changed.
    Idempotent: re-granting re-enables a previously revoked assignment.
    """
    clean_user = (user_id or "").strip()
    clean_machine = (machine_id or "").strip()
    if not clean_user:
        raise ValueError("user_id cannot be empty")
    if not clean_machine:
        raise ValueError("machine_id cannot be empty")

    with get_db_connection(db_target) as conn:
        _ensure_access_table(conn)

        user_row = db_execute(
            conn,
            "SELECT user_id, display_name, email FROM users WHERE user_id = ?",
            (clean_user,),
        ).fetchone()
        if user_row is None:
            raise ValueError(f"User '{clean_user}' does not exist in users table")

        machine_row = db_execute(
            conn,
            "SELECT machine_id FROM machines WHERE machine_id = ?",
            (clean_machine,),
        ).fetchone()
        if machine_row is None:
            raise ValueError(
                f"Machine '{clean_machine}' is not enrolled (not found in machines table); "
                "check the machine ID spelling"
            )

        created_at = now_utc()
        db_execute(
            conn,
            """
            INSERT INTO mobile_user_machine_access (user_id, machine_id, created_at, enabled)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id, machine_id)
            DO UPDATE SET enabled = 1
            """,
            (clean_user, clean_machine, created_at),
        )

    return {
        "status": "success",
        "user_id": clean_user,
        "user_email": user_row["email"],
        "machine_id": clean_machine,
        "enabled": True,
    }


def revoke_machine_access(
    user_id: str,
    machine_id: str,
    db_target: str | None = None,
) -> dict[str, Any]:
    """Disables the assignment (the row is kept for audit). Raises if none exists."""
    clean_user = (user_id or "").strip()
    clean_machine = (machine_id or "").strip()
    if not clean_user or not clean_machine:
        raise ValueError("user_id and machine_id are required")

    with get_db_connection(db_target) as conn:
        _ensure_access_table(conn)
        existing = db_execute(
            conn,
            """
            SELECT enabled FROM mobile_user_machine_access
            WHERE user_id = ? AND machine_id = ?
            """,
            (clean_user, clean_machine),
        ).fetchone()
        if existing is None:
            raise ValueError(f"No access assignment exists for user '{clean_user}' on machine '{clean_machine}'")
        db_execute(
            conn,
            """
            UPDATE mobile_user_machine_access
            SET enabled = 0
            WHERE user_id = ? AND machine_id = ?
            """,
            (clean_user, clean_machine),
        )

    return {"status": "success", "user_id": clean_user, "machine_id": clean_machine, "enabled": False}


def list_machine_access(
    user_id: str | None = None,
    db_target: str | None = None,
) -> list[dict[str, Any]]:
    with get_db_connection(db_target) as conn:
        _ensure_access_table(conn)
        sql = """
            SELECT a.user_id, u.email, a.machine_id, a.enabled, a.created_at
            FROM mobile_user_machine_access a
            LEFT JOIN users u ON u.user_id = a.user_id
        """
        params: list[Any] = []
        if user_id and user_id.strip():
            sql += " WHERE a.user_id = ?"
            params.append(user_id.strip())
        sql += " ORDER BY a.user_id, a.machine_id"
        rows = db_execute(conn, sql, params).fetchall()
        return [dict(r) for r in rows]


# ============================================================================
# CLI Entrypoint
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="admin_provisioning",
        description="Moldflow Mobile — Admin Provisioning CLI for IT / Admins",
    )
    parser.add_argument(
        "--db",
        dest="db_target",
        default=None,
        help="Database URL or SQLite path (defaults to SUPABASE_DATABASE_URL/DATABASE_URL or moldflow_jobs.db)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. create-user
    p_user = subparsers.add_parser("create-user", help="Create a new user account")
    p_user.add_argument("--email", required=True, help="User email address")
    p_user.add_argument("--name", required=True, help="User display name")
    p_user.add_argument("--password", default=None, help="Password (prompted securely if omitted)")
    p_user.add_argument("--user-id", default=None, help="Optional custom user ID (generated if omitted)")

    # 2. create-token
    p_token = subparsers.add_parser("create-token", help="Generate a workstation enrollment token for an existing user")
    p_token.add_argument("--user-id", required=True, help="User ID to bind this workstation to")
    p_token.add_argument("--expires-hours", type=float, default=1.0, help="Token validity in hours (default: 1.0)")
    p_token.add_argument("--out-token-file", default=None, help="Optional file path to save the token string")

    # 3. onboard-user (combined)
    p_onboard = subparsers.add_parser("onboard-user", help="Create a user and generate a workstation token in one step")
    p_onboard.add_argument("--email", required=True, help="User email address")
    p_onboard.add_argument("--name", required=True, help="User display name")
    p_onboard.add_argument("--password", default=None, help="Password (prompted securely if omitted)")
    p_onboard.add_argument("--user-id", default=None, help="Optional custom user ID")
    p_onboard.add_argument("--expires-hours", type=float, default=1.0, help="Token validity in hours (default: 1.0)")
    p_onboard.add_argument("--out-token-file", default=None, help="Optional file path to save the token string")

    # 4. list-users
    subparsers.add_parser("list-users", help="List registered users and workstation/device counts")

    # 5. list-tokens
    p_ltok = subparsers.add_parser("list-tokens", help="List enrollment tokens")
    p_ltok.add_argument("--user-id", default=None, help="Filter tokens by user ID")

    # 6-8. mobile user <-> workstation access
    p_grant = subparsers.add_parser(
        "grant-machine-access",
        help="Let a mobile user view jobs reported by an enrolled workstation (ownership unchanged)",
    )
    p_grant.add_argument("--user-id", required=True, help="Mobile user who should gain access")
    p_grant.add_argument("--machine-id", required=True, help="Enrolled workstation machine ID")

    p_revoke = subparsers.add_parser("revoke-machine-access", help="Remove a mobile user's access to a workstation")
    p_revoke.add_argument("--user-id", required=True)
    p_revoke.add_argument("--machine-id", required=True)

    p_lacc = subparsers.add_parser("list-machine-access", help="List mobile user -> workstation access assignments")
    p_lacc.add_argument("--user-id", default=None, help="Filter by mobile user ID")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    db_target = args.db_target

    try:
        if args.command == "create-user":
            password = args.password
            if not password:
                password = getpass.getpass("Enter user password (min 8 characters): ")
                confirm = getpass.getpass("Confirm user password: ")
                if password != confirm:
                    print("ERROR: Passwords do not match.", file=sys.stderr)
                    sys.exit(1)

            res = create_user(
                email=args.email,
                display_name=args.name,
                password=password,
                user_id=args.user_id,
                db_target=db_target,
            )
            print("\n[SUCCESS] User created successfully:")
            print(f"  User ID:      {res['user_id']}")
            print(f"  Display Name: {res['display_name']}")
            print(f"  Email:        {res['email']}")
            print(f"  Created At:   {res['created_at']}")
            print("  (Password securely hashed with Argon2id; plain password was not stored)\n")

        elif args.command == "create-token":
            seconds = int(args.expires_hours * 3600)
            res = create_workstation_token(
                user_id=args.user_id,
                expires_in_seconds=seconds,
                db_target=db_target,
            )
            token = res["token"]
            print("\n[SUCCESS] Workstation enrollment token issued:")
            print(f"  Bound User:   {res['user_name']} ({res['user_id']})")
            print(f"  User Email:   {res['user_email']}")
            print(f"  Token:        {token}")
            print(f"  Expires At:   {res['expires_at']} (valid for {args.expires_hours}h)")

            if args.out_token_file:
                out_path = Path(args.out_token_file).resolve()
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(token.strip())
                print(f"  Token saved:  {out_path}")
            print()

        elif args.command == "onboard-user":
            password = args.password
            if not password:
                password = getpass.getpass("Enter user password (min 8 characters): ")
                confirm = getpass.getpass("Confirm user password: ")
                if password != confirm:
                    print("ERROR: Passwords do not match.", file=sys.stderr)
                    sys.exit(1)

            u_res = create_user(
                email=args.email,
                display_name=args.name,
                password=password,
                user_id=args.user_id,
                db_target=db_target,
            )
            seconds = int(args.expires_hours * 3600)
            t_res = create_workstation_token(
                user_id=u_res["user_id"],
                expires_in_seconds=seconds,
                db_target=db_target,
            )
            token = t_res["token"]

            print("\n[SUCCESS] Onboarding Complete:")
            print("  --- User Account ---")
            print(f"  User ID:      {u_res['user_id']}")
            print(f"  Display Name: {u_res['display_name']}")
            print(f"  Email:        {u_res['email']}")
            print("  --- Workstation Enrollment Token ---")
            print(f"  Token:        {token}")
            print(f"  Expires At:   {t_res['expires_at']} (valid for {args.expires_hours}h)")

            if args.out_token_file:
                out_path = Path(args.out_token_file).resolve()
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(token.strip())
                print(f"  Token saved:  {out_path}")

            print("\nNext Steps:")
            print("  1. User logs in on mobile app with email and password.")
            print("  2. Run Install-MoldflowWorkstation.ps1 with the enrollment token on the workstation.")
            print()

        elif args.command == "grant-machine-access":
            res = grant_machine_access(args.user_id, args.machine_id, db_target=db_target)
            print("\n[SUCCESS] Machine access granted:")
            print(f"  Mobile user: {res['user_id']} ({res['user_email']})")
            print(f"  Machine:     {res['machine_id']}")
            print("  (Job ownership is unchanged; the user can now view jobs from this machine)\n")

        elif args.command == "revoke-machine-access":
            res = revoke_machine_access(args.user_id, args.machine_id, db_target=db_target)
            print(f"\n[SUCCESS] Access revoked: user={res['user_id']} machine={res['machine_id']}\n")

        elif args.command == "list-machine-access":
            rows = list_machine_access(user_id=args.user_id, db_target=db_target)
            if not rows:
                print("\nNo machine access assignments found.\n")
            else:
                print(f"\n{'USER ID':<26} {'EMAIL':<30} {'MACHINE':<24} {'ENABLED':<8} CREATED")
                print("-" * 110)
                for r in rows:
                    print(f"{r['user_id']:<26} {(r.get('email') or '-'):<30} {r['machine_id']:<24} {'yes' if r['enabled'] else 'no':<8} {r['created_at']}")
                print()

        elif args.command == "list-users":
            users = list_users(db_target=db_target)
            print(f"\nRegistered Users ({len(users)}):")
            print(f"{'User ID':<24} {'Email':<30} {'Display Name':<22} {'Machines':<10} {'Devices':<10}")
            print("-" * 100)
            for u in users:
                print(f"{u['user_id']:<24} {u['email']:<30} {u['display_name']:<22} {u['machine_count']:<10} {u['device_count']:<10}")
            print()

        elif args.command == "list-tokens":
            tokens = list_tokens(user_id=args.user_id, db_target=db_target)
            print(f"\nEnrollment Tokens ({len(tokens)}):")
            print(f"{'Token':<38} {'User ID':<22} {'State':<10} {'Expires At':<26} {'Machine'}")
            print("-" * 115)
            for t in tokens:
                mach = t.get("consumed_by_machine_id") or "-"
                print(f"{t['token']:<38} {t.get('user_id') or '-':<22} {t['state']:<10} {t['expires_at']:<26} {mach}")
            print()

    except Exception as ex:
        print(f"\n[ERROR] {ex}\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
