"""Moldflow Mobile Job Backend - PostgreSQL Ready

Keeps the existing API contract and supports:

Development:
    SQLite when DATABASE_URL is not set.

PostgreSQL:
    PostgreSQL when DATABASE_URL is set.

Existing architecture:
    Moldflow plugin -> X-Api-Key -> POST /reportJobStatus
    Mobile app -> email/password -> JWT Bearer -> /me, /jobs, /devices

The existing local SQLite database remains untouched unless DATABASE_URL
is explicitly configured.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel, Field
from pwdlib import PasswordHash

from fcm_service import send_fcm_notification, UnregisteredDeviceError


# ============================================================================
# Configuration & Environment
# ============================================================================

# Auto-load .env from backend directory if not already set in environment
_env_file = Path(__file__).resolve().parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

APP_NAME = "Moldflow Mobile Job Backend"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

MOLDFLOW_API_KEY = os.getenv(
    "MOLDFLOW_API_KEY",
    "dev-moldflow-key-change-me",
)

# Minimum 32-byte secure key for HS256 algorithm
JWT_SECRET_KEY = os.getenv(
    "JWT_SECRET_KEY",
    "moldflow-production-secret-key-32bytes-min-2026-secure!",
)

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

DEV_USER_ID = os.getenv(
    "DEV_USER_ID",
    "DEV-USER-001",
)

DEV_USER_NAME = os.getenv(
    "DEV_USER_NAME",
    "Development User",
)

DEV_USER_EMAIL = os.getenv(
    "DEV_USER_EMAIL",
    "dev@example.local",
)

DEV_USER_PASSWORD = os.getenv(
    "DEV_USER_PASSWORD",
    "MoldflowDev@2026!",
)

DEV_MACHINE_ID = os.getenv(
    "DEV_MACHINE_ID",
    "DEV-PC-001",
)

DEV_MACHINE_NAME = os.getenv(
    "DEV_MACHINE_NAME",
    "Development PC",
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "moldflow_jobs.db"

app = FastAPI(
    title=APP_NAME,
    version="0.5.0",
)

_db_lock = threading.Lock()

# PostgreSQL Connection Pool
pg_pool = None
if USE_POSTGRES:
    try:
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row

        pg_pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=2,
            max_size=10,
            open=False,
            kwargs={"row_factory": dict_row},
        )
    except ImportError as ex:
        raise RuntimeError(
            "DATABASE_URL is set, but psycopg_pool is not installed. "
            "Install with: python -m pip install \"psycopg[pool]\""
        ) from ex

password_hash = PasswordHash.recommended()

bearer_scheme = HTTPBearer(
    auto_error=False,
)


# ============================================================================
# Database abstraction
# ============================================================================

def _convert_params(params: tuple[Any, ...] | list[Any] | None) -> tuple[Any, ...]:
    if params is None:
        return ()
    return tuple(params)


def _pg_sql(sql: str) -> str:
    """Convert the existing SQLite-style '?' placeholders to PostgreSQL."""
    return sql.replace("?", "%s")


@contextmanager
def get_db():
    """Yields a database connection with transaction and pool management.
    
    - PostgreSQL: Borrows an active connection from ConnectionPool concurrently.
    - SQLite: Manages a connection with _db_lock for thread safety.
    """
    if USE_POSTGRES:
        if pg_pool is None:
            raise RuntimeError("PostgreSQL connection pool is not initialized")
        with pg_pool.connection() as conn:
            yield conn
    else:
        with _db_lock:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
            finally:
                conn.close()


def get_connection():
    """Direct connection helper maintained for backward compatibility."""
    if USE_POSTGRES:
        if pg_pool is not None:
            return pg_pool.getconn()
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db_execute(
    conn,
    sql: str,
    params: tuple[Any, ...] | list[Any] | None = None,
):
    params = _convert_params(params)

    if USE_POSTGRES:
        return conn.execute(_pg_sql(sql), params)

    return conn.execute(sql, params)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_column(
    conn,
    table: str,
    column: str,
    definition: str,
) -> None:
    if USE_POSTGRES:
        row = db_execute(
            conn,
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ?
              AND column_name = ?
            LIMIT 1
            """,
            (table, column),
        ).fetchone()

        if row is None:
            db_execute(
                conn,
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}",
            )

        return

    columns = db_execute(
        conn,
        f"PRAGMA table_info({table})",
    ).fetchall()

    existing_names = {
        str(row["name"])
        for row in columns
    }

    if column not in existing_names:
        db_execute(
            conn,
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}",
        )


def _jobs_table_sql() -> str:
    if USE_POSTGRES:
        return """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                job_type TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'UNKNOWN',
                percent DOUBLE PRECISION,
                started DOUBLE PRECISION,
                finished BOOLEAN NOT NULL DEFAULT FALSE,
                error_message TEXT,
                user_id TEXT NOT NULL DEFAULT '',
                machine_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                update_count INTEGER NOT NULL DEFAULT 0
            )
        """

    return """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            job_type TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'UNKNOWN',
            percent REAL,
            started REAL,
            finished INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            user_id TEXT NOT NULL DEFAULT '',
            machine_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            update_count INTEGER NOT NULL DEFAULT 0
        )
    """


def _job_events_table_sql() -> str:
    if USE_POSTGRES:
        return """
            CREATE TABLE IF NOT EXISTS job_events (
                id BIGSERIAL PRIMARY KEY,
                job_id TEXT NOT NULL,
                status TEXT NOT NULL,
                percent DOUBLE PRECISION,
                finished BOOLEAN NOT NULL DEFAULT FALSE,
                error_message TEXT,
                received_at TEXT NOT NULL,
                FOREIGN KEY(job_id)
                    REFERENCES jobs(job_id)
                    ON DELETE CASCADE
            )
        """

    return """
        CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            status TEXT NOT NULL,
            percent REAL,
            finished INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            received_at TEXT NOT NULL,
            FOREIGN KEY(job_id)
                REFERENCES jobs(job_id)
                ON DELETE CASCADE
        )
    """


def _job_notifications_table_sql() -> str:
    if USE_POSTGRES:
        return """
            CREATE TABLE IF NOT EXISTS job_notifications (
                id BIGSERIAL PRIMARY KEY,
                job_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                notification_type TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(job_id, notification_type),
                FOREIGN KEY(job_id)
                    REFERENCES jobs(job_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            )
        """

    return """
        CREATE TABLE IF NOT EXISTS job_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            notification_type TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE(job_id, notification_type),
            FOREIGN KEY(job_id)
                REFERENCES jobs(job_id)
                ON DELETE CASCADE,
            FOREIGN KEY(user_id)
                REFERENCES users(user_id)
                ON DELETE CASCADE
        )
    """


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if value is None:
        return False

    return bool(value)


def init_database() -> None:
    with get_db() as conn:
        # ----------------------------------------------------------------
        # Users
        # ----------------------------------------------------------------
        db_execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                password_hash TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )

        ensure_column(
            conn,
            "users",
            "password_hash",
            "TEXT",
        )

        # ----------------------------------------------------------------
        # Machines
        # ----------------------------------------------------------------
        db_execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS machines (
                machine_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                machine_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                FOREIGN KEY(user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            )
            """,
        )

        # ----------------------------------------------------------------
        # Devices
        # ----------------------------------------------------------------
        db_execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                push_token TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            )
            """,
        )

        # ----------------------------------------------------------------
        # Jobs
        # ----------------------------------------------------------------
        db_execute(
            conn,
            _jobs_table_sql(),
        )

        # ----------------------------------------------------------------
        # SCM metadata
        # ----------------------------------------------------------------
        ensure_column(conn, "jobs", "scm_job_id", "TEXT")
        ensure_column(conn, "jobs", "scm_type", "TEXT")
        ensure_column(conn, "jobs", "compute_source", "TEXT")
        ensure_column(conn, "jobs", "scm_user", "TEXT")
        ensure_column(conn, "jobs", "worker", "TEXT")
        ensure_column(conn, "jobs", "parent_job_id", "TEXT")
        ensure_column(
            conn,
            "jobs",
            "cancel_requested",
            "BOOLEAN NOT NULL DEFAULT FALSE"
            if USE_POSTGRES
            else "INTEGER NOT NULL DEFAULT 0",
        )
        # Soft-delete: lets the mobile user remove a finished job from their
        # list without touching job_events/audit history or the row itself.
        ensure_column(
            conn,
            "jobs",
            "archived",
            "BOOLEAN NOT NULL DEFAULT FALSE"
            if USE_POSTGRES
            else "INTEGER NOT NULL DEFAULT 0",
        )

        # ----------------------------------------------------------------
        # Job history
        # ----------------------------------------------------------------
        db_execute(
            conn,
            _job_events_table_sql(),
        )

        # ----------------------------------------------------------------
        # Push notification history
        # ----------------------------------------------------------------
        db_execute(
            conn,
            _job_notifications_table_sql(),
        )

        # ----------------------------------------------------------------
        # API clients
        # ----------------------------------------------------------------
        db_execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS api_clients (
                api_key TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(machine_id)
                    REFERENCES machines(machine_id)
                    ON DELETE CASCADE
            )
            """,
        )

        # ----------------------------------------------------------------
        # Indexes
        # ----------------------------------------------------------------
        db_execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_user_id
            ON jobs(user_id)
            """,
        )

        db_execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_updated_at
            ON jobs(updated_at)
            """,
        )

        db_execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_job_events_job_id
            ON job_events(job_id)
            """,
        )

        db_execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_devices_user_id
            ON devices(user_id)
            """,
        )

        # ----------------------------------------------------------------
        # Development user
        # ----------------------------------------------------------------
        timestamp = now_utc()
        hashed_password = password_hash.hash(
            DEV_USER_PASSWORD
        )

        existing_user = db_execute(
            conn,
            """
            SELECT user_id, password_hash
            FROM users
            WHERE user_id = ?
            """,
            (DEV_USER_ID,),
        ).fetchone()

        if existing_user is None:
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
                (
                    DEV_USER_ID,
                    DEV_USER_NAME,
                    DEV_USER_EMAIL,
                    hashed_password,
                    timestamp,
                ),
            )
        else:
            if not existing_user["password_hash"]:
                db_execute(
                    conn,
                    """
                    UPDATE users
                    SET password_hash = ?
                    WHERE user_id = ?
                    """,
                    (
                        hashed_password,
                        DEV_USER_ID,
                    ),
                )

            db_execute(
                conn,
                """
                UPDATE users
                SET
                    display_name = ?,
                    email = ?
                WHERE user_id = ?
                """,
                (
                    DEV_USER_NAME,
                    DEV_USER_EMAIL,
                    DEV_USER_ID,
                ),
            )

        # ----------------------------------------------------------------
        # Development machine
        # ----------------------------------------------------------------
        db_execute(
            conn,
            """
            INSERT INTO machines (
                machine_id,
                user_id,
                machine_name,
                created_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(machine_id)
            DO UPDATE SET
                user_id = excluded.user_id,
                machine_name = excluded.machine_name,
                last_seen_at = excluded.last_seen_at
            """,
            (
                DEV_MACHINE_ID,
                DEV_USER_ID,
                DEV_MACHINE_NAME,
                timestamp,
                timestamp,
            ),
        )

        # ----------------------------------------------------------------
        # Development API client
        # ----------------------------------------------------------------
        db_execute(
            conn,
            """
            INSERT INTO api_clients (
                api_key,
                user_id,
                machine_id,
                enabled,
                created_at
            )
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(api_key)
            DO UPDATE SET
                user_id = excluded.user_id,
                machine_id = excluded.machine_id,
                enabled = 1
            """,
            (
                MOLDFLOW_API_KEY,
                DEV_USER_ID,
                DEV_MACHINE_ID,
                timestamp,
            ),
        )

        conn.commit()


# ============================================================================
# Models
# ============================================================================

class JobStatus(BaseModel):
    job_id: str = Field(min_length=1)
    name: str = ""
    type: str = ""
    status: str = "UNKNOWN"
    percent: float | int | None = 0
    started: float | int | None = None
    finished: bool = False
    error_message: str | None = None

    # Accepted for compatibility with the current Moldflow reporter.
    # Identity is determined from the X-Api-Key.
    user_id: str = ""
    machine_id: str = ""

    # Simulation Compute Manager metadata.
    scm_job_id: str | None = None
    scm_type: str | None = None
    compute_source: str | None = None
    scm_user: str | None = None
    worker: str | None = None
    parent_job_id: str | None = None


class LoginRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=1)


class DeviceRegistration(BaseModel):
    device_id: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    push_token: str | None = None


# ============================================================================
# Moldflow authentication
# ============================================================================

def get_moldflow_identity(
    x_api_key: str | None = Header(default=None),
) -> dict[str, str]:
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Moldflow API key",
        )

    with get_db() as conn:
        row = db_execute(
            conn,
            """
            SELECT
                api_clients.user_id,
                api_clients.machine_id,
                users.display_name,
                users.email,
                machines.machine_name
            FROM api_clients
            JOIN users
                ON users.user_id = api_clients.user_id
            JOIN machines
                ON machines.machine_id = api_clients.machine_id
            WHERE api_clients.api_key = ?
              AND api_clients.enabled = 1
            """,
            (x_api_key,),
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or disabled Moldflow API key",
            )

        db_execute(
            conn,
            """
            UPDATE machines
            SET last_seen_at = ?
            WHERE machine_id = ?
            """,
            (
                now_utc(),
                row["machine_id"],
            ),
        )

        conn.commit()

        return {
            "user_id": row["user_id"],
            "display_name": row["display_name"],
            "email": row["email"],
            "machine_id": row["machine_id"],
            "machine_name": row["machine_name"],
        }


# ============================================================================
# JWT helpers
# ============================================================================

def create_access_token(user_id: str) -> tuple[str, int]:
    expires_delta = timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )

    expires_at = (
        datetime.now(timezone.utc)
        + expires_delta
    )

    payload = {
        "sub": user_id,
        "type": "access",
        "exp": expires_at,
    }

    token = jwt.encode(
        payload,
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )

    return token, int(expires_delta.total_seconds())


def get_mobile_user(
    credentials: HTTPAuthorizationCredentials | None =
    Depends(bearer_scheme),
) -> dict[str, Any]:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )

    token = credentials.credentials

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
        )

        user_id = payload.get("sub")

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid access token",
            )

    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
        )

    with get_db() as conn:
        row = db_execute(
            conn,
            """
            SELECT
                user_id,
                display_name,
                email,
                created_at
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User no longer exists",
            )

        return dict(row)


# ============================================================================
# Startup
# ============================================================================

@app.on_event("startup")
def startup() -> None:
    if USE_POSTGRES and pg_pool is not None:
        pg_pool.open()

    init_database()

    database_label = (
        f"PostgreSQL (ConnectionPool: min=2, max=10)"
        if USE_POSTGRES
        else f"SQLite database: {DB_PATH}"
    )

    print(
        f"[DB] Database: {database_label}"
    )

    print(
        "[DB] Backend mode: "
        f"{'POSTGRESQL' if USE_POSTGRES else 'SQLITE'}"
    )

    print(
        "[AUTH] Moldflow identity: "
        f"user={DEV_USER_ID}, "
        f"machine={DEV_MACHINE_ID}"
    )

    print(
        "[AUTH] Mobile login: "
        f"{DEV_USER_EMAIL}"
    )


@app.on_event("shutdown")
def shutdown() -> None:
    if USE_POSTGRES and pg_pool is not None:
        pg_pool.close()
        print("[DB] PostgreSQL connection pool closed.")


# ============================================================================
# Health
# ============================================================================

@app.get("/health")
def health() -> dict[str, Any]:
    db_ok = False
    try:
        with get_db() as conn:
            db_execute(conn, "SELECT 1").fetchone()
            db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "healthy" if db_ok else "degraded",
        "service": APP_NAME,
        "database": "postgresql" if USE_POSTGRES else "sqlite",
        "pool": (
            {
                "min_size": pg_pool.min_size,
                "max_size": pg_pool.max_size,
            }
            if USE_POSTGRES and pg_pool is not None
            else None
        ),
        "timestamp": now_utc(),
    }


# ============================================================================
# Mobile login
# ============================================================================

@app.post("/auth/login")
def mobile_login(
    credentials: LoginRequest,
) -> dict[str, Any]:
    with get_db() as conn:
        row = db_execute(
            conn,
            """
            SELECT
                user_id,
                display_name,
                email,
                password_hash
            FROM users
            WHERE lower(email) = lower(?)
            """,
            (credentials.email,),
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

        stored_hash = row["password_hash"]

        if not stored_hash:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account is not configured for password login",
            )

        if not password_hash.verify(
            credentials.password,
            stored_hash,
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

        token, expires_in = create_access_token(
            row["user_id"]
        )

        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "user": {
                "user_id": row["user_id"],
                "display_name": row["display_name"],
                "email": row["email"],
            },
        }


# ============================================================================
# Mobile current user
# ============================================================================

@app.get("/me")
def mobile_me(
    user: dict[str, Any] = Depends(get_mobile_user),
) -> dict[str, Any]:
    return user


# ============================================================================
# Mobile jobs
# ============================================================================

@app.get("/jobs")
def mobile_jobs(
    user: dict[str, Any] = Depends(get_mobile_user),
) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = db_execute(
            conn,
            """
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
            WHERE user_id = ?
              AND archived = FALSE
            ORDER BY updated_at DESC
            """,
            (user["user_id"],),
        ).fetchall()

        result = []

        for row in rows:
            item = dict(row)
            item["finished"] = _bool_value(
                item["finished"]
            )
            result.append(item)

        return result


# ============================================================================
# Mobile single job
# ============================================================================

@app.get("/jobs/{job_id}")
def mobile_job(
    job_id: str,
    user: dict[str, Any] = Depends(get_mobile_user),
) -> dict[str, Any]:
    with get_db() as conn:
        row = db_execute(
            conn,
            """
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
            WHERE job_id = ?
              AND user_id = ?
            """,
            (
                job_id,
                user["user_id"],
            ),
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found",
            )

        result = dict(row)

        result["finished"] = _bool_value(
            result["finished"]
        )

        return result


# ============================================================================
# Mobile job history
# ============================================================================

@app.get("/jobs/{job_id}/events")
def mobile_job_events(
    job_id: str,
    user: dict[str, Any] = Depends(get_mobile_user),
) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = db_execute(
            conn,
            """
            SELECT
                job_events.id,
                job_events.job_id,
                job_events.status,
                job_events.percent,
                job_events.finished,
                job_events.error_message,
                job_events.received_at
            FROM job_events
            JOIN jobs
                ON jobs.job_id = job_events.job_id
            WHERE job_events.job_id = ?
              AND jobs.user_id = ?
            ORDER BY job_events.id ASC
            """,
            (
                job_id,
                user["user_id"],
            ),
        ).fetchall()

        result = []

        for row in rows:
            item = dict(row)
            item["finished"] = _bool_value(
                item["finished"]
            )
            result.append(item)

        return result


# ============================================================================
# Mobile devices
# ============================================================================

@app.get("/devices")
def mobile_devices(
    user: dict[str, Any] = Depends(get_mobile_user),
) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = db_execute(
            conn,
            """
            SELECT
                device_id,
                platform,
                push_token,
                created_at,
                updated_at
            FROM devices
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user["user_id"],),
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]


# ============================================================================
# Mobile device registration
# ============================================================================

@app.post("/devices/register")
def register_device(
    device: DeviceRegistration,
    user: dict[str, Any] = Depends(get_mobile_user),
) -> dict[str, Any]:
    timestamp = now_utc()

    with get_db() as conn:
        db_execute(
            conn,
            """
            INSERT INTO devices (
                device_id,
                user_id,
                platform,
                push_token,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id)
            DO UPDATE SET
                user_id = excluded.user_id,
                platform = excluded.platform,
                push_token = excluded.push_token,
                updated_at = excluded.updated_at
            """,
            (
                device.device_id,
                user["user_id"],
                device.platform,
                device.push_token,
                timestamp,
                timestamp,
            ),
        )

        conn.commit()

    # ------------------------------------------------------------------------
    # Catch-up: Check for active INPROGRESS jobs where STARTED notification has
    # not yet been delivered (e.g. job started before this device registered).
    # ------------------------------------------------------------------------
    authenticated_user_id = user["user_id"]
    try:
        with get_db() as conn:
            active_candidates = db_execute(
                conn,
                """
                SELECT job_id, name, status, percent, finished, error_message
                FROM jobs
                WHERE user_id = ?
                ORDER BY created_at ASC
                """,
                (authenticated_user_id,),
            ).fetchall()

        for candidate in active_candidates:
            if _bool_value(candidate["finished"]):
                continue
            curr_status = (candidate["status"] or "").strip().upper()
            if curr_status not in ("INPROGRESS", "STARTED"):
                continue
            send_job_completion_notification(
                user_id=authenticated_user_id,
                job_id=candidate["job_id"],
                job_name=candidate["name"] or candidate["job_id"],
                status_value=candidate["status"],
                percent=candidate["percent"],
                error_message=candidate["error_message"],
            )
    except Exception as ex:
        print(f"[DEVICE] Catch-up notification check failed: {ex}")

    return {
        "registered": True,
        "device_id": device.device_id,
        "user_id": user["user_id"],
        "platform": device.platform,
    }


# ============================================================================
# Push notifications
# ============================================================================

def send_job_completion_notification(
    user_id: str,
    job_id: str,
    job_name: str,
    status_value: str,
    percent: float | int | None,
    error_message: str | None,
) -> None:
    """
    Send exactly one push notification per (job, lifecycle stage).

    INPROGRESS -> started notification (first solve tick only)
    COMPLETED  -> success notification
    FAILED     -> failure notification
    CANCELED   -> cancellation notification
    """

    normalized_status = (
        (status_value or "")
        .strip()
        .upper()
    )

    if normalized_status not in {
        "INPROGRESS",
        "STARTED",
        "COMPLETED",
        "FAILED",
        "CANCELED",
    }:
        return

    # INPROGRESS is the SCM's real "the solver is actually running" status
    # (there is no "RUNNING" in that vocabulary) -- surfaced to the user as
    # a "Started" notification, distinct from the terminal ones below.
    # Explicit "STARTED" from post-Analyze agent or client maps to the same notification.
    notification_type = "STARTED" if normalized_status in ("INPROGRESS", "STARTED") else normalized_status

    with get_db() as conn:
        existing = db_execute(
            conn,
            """
            SELECT id
            FROM job_notifications
            WHERE job_id = ?
              AND notification_type = ?
            LIMIT 1
            """,
            (
                job_id,
                notification_type,
            ),
        ).fetchone()

    if existing:
        print(
            f"[FCM] Notification already sent "
            f"job={job_id} "
            f"type={notification_type}"
        )
        return

    with get_db() as conn:
        devices = db_execute(
            conn,
            """
            SELECT device_id, push_token
            FROM devices
            WHERE user_id = ?
              AND platform = 'android'
              AND push_token IS NOT NULL
              AND TRIM(push_token) <> ''
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()

    if not devices:
        print(
            f"[FCM] No registered Android devices "
            f"for user={user_id}"
        )
        return

    if normalized_status in ("INPROGRESS", "STARTED"):
        title = "Moldflow Analysis Started"
        body = f"{job_name} has started running."
    elif normalized_status == "COMPLETED":
        title = "Moldflow Analysis Completed"
        body = f"{job_name} completed successfully."
    elif normalized_status == "CANCELED":
        title = "Moldflow Analysis Cancelled"
        body = f"{job_name} was cancelled."
    else:
        title = "Moldflow Analysis Failed"

        if error_message:
            body = (
                f"{job_name} failed: "
                f"{error_message}"
            )
        else:
            body = f"{job_name} failed."

    sent_successfully = False

    for device in devices:
        device_id = device["device_id"]
        push_token = device["push_token"]

        try:
            send_fcm_notification(
                device_token=push_token,
                title=title,
                body=body,
                data={
                    "job_id": job_id,
                    "job_name": job_name,
                    "status": normalized_status,
                    "notification_type": f"JOB_{notification_type}",
                    "percent": str(
                        percent if percent is not None else 0
                    ),
                    "title": title,
                    "body": body,
                },
            )

            print(
                f"[FCM] Sent notification "
                f"device={device_id} "
                f"job={job_id} "
                f"type={notification_type}"
            )

            sent_successfully = True

        except UnregisteredDeviceError as ex:
            print(
                f"[FCM] Dead/unregistered device token detected for device={device_id}: {ex}"
            )
            try:
                with get_db() as conn:
                    db_execute(
                        conn,
                        """
                        DELETE FROM devices
                        WHERE device_id = ?
                          AND user_id = ?
                        """,
                        (device_id, user_id),
                    )
                    conn.commit()
                print(
                    f"[FCM] Automatically purged dead device={device_id} from database."
                )
            except Exception as db_ex:
                print(
                    f"[FCM] Failed to purge dead device {device_id}: {db_ex}"
                )

        except Exception as ex:
            print(
                f"[FCM] Failed notification "
                f"device={device_id}: {ex}"
            )

    if sent_successfully:
        timestamp = now_utc()

        with get_db() as conn:
            db_execute(
                conn,
                """
                INSERT INTO job_notifications (
                    job_id,
                    user_id,
                    notification_type,
                    sent_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, notification_type)
                DO NOTHING
                """,
                (
                    job_id,
                    user_id,
                    notification_type,
                    timestamp,
                ),
            )

            conn.commit()


# ============================================================================
# Moldflow job ingestion
# ============================================================================

@app.post("/reportJobStatus")
def report_job_status(
    job: JobStatus,
    identity: dict[str, str] = Depends(get_moldflow_identity),
) -> dict[str, Any]:
    authenticated_user_id = identity["user_id"]
    authenticated_machine_id = identity["machine_id"]

    received_at = now_utc()

    with get_db() as conn:
        existing = db_execute(
            conn,
            """
            SELECT user_id, update_count, status, percent, finished, error_message
            FROM jobs
            WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()

        if existing is not None:
            existing_user = existing["user_id"]
            if existing_user and existing_user != authenticated_user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Job is owned by another user and cannot be modified",
                )
            update_count = int(existing["update_count"]) + 1
            existing_status = (existing["status"] or "").strip().upper()
        else:
            update_count = 1
            existing_status = None

        # First terminal state wins: once a job has reached a terminal
        # status, a later report carrying a DIFFERENT status (a delayed or
        # retried request, or a cancel-check racing a just-finished solve)
        # must not flip it. job_events below still records exactly what was
        # reported for the audit trail; only the jobs row's result fields
        # are frozen once terminal.
        incoming_status = (job.status or "").strip().upper()
        status_locked = (
            existing_status in _CANCEL_TERMINAL_STATUSES
            and existing_status != incoming_status
        )
        effective_status_upper = existing_status if status_locked else incoming_status

        if status_locked:
            insert_status = existing["status"]
            insert_percent = existing["percent"]
            insert_finished = existing["finished"]
            insert_error_message = existing["error_message"]
        else:
            insert_status = job.status
            insert_percent = job.percent
            insert_finished = job.finished
            insert_error_message = job.error_message

        # The same SQL works for both SQLite and PostgreSQL.
        db_execute(
            conn,
            """
            INSERT INTO jobs (
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
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(job_id)
            DO UPDATE SET
                name = excluded.name,
                job_type = excluded.job_type,
                status = excluded.status,
                percent = excluded.percent,
                started = excluded.started,
                finished = excluded.finished,
                error_message = excluded.error_message,
                user_id = excluded.user_id,
                machine_id = excluded.machine_id,
                updated_at = excluded.updated_at,
                update_count = excluded.update_count,
                scm_job_id =
                    COALESCE(excluded.scm_job_id, jobs.scm_job_id),
                scm_type =
                    COALESCE(excluded.scm_type, jobs.scm_type),
                compute_source =
                    COALESCE(
                        excluded.compute_source,
                        jobs.compute_source
                    ),
                scm_user =
                    COALESCE(excluded.scm_user, jobs.scm_user),
                worker =
                    COALESCE(excluded.worker, jobs.worker),
                parent_job_id =
                    COALESCE(
                        excluded.parent_job_id,
                        jobs.parent_job_id
                    )
            """,
            (
                job.job_id,
                job.name,
                job.type,
                insert_status,
                insert_percent,
                job.started,
                insert_finished,
                insert_error_message,
                authenticated_user_id,
                authenticated_machine_id,
                received_at,
                received_at,
                update_count,
                job.scm_job_id,
                job.scm_type,
                job.compute_source,
                job.scm_user,
                job.worker,
                job.parent_job_id,
            ),
        )

        db_execute(
            conn,
            """
            INSERT INTO job_events (
                job_id,
                status,
                percent,
                finished,
                error_message,
                received_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.status,
                job.percent,
                job.finished,
                job.error_message,
                received_at,
            ),
        )

        # When a terminal status arrives, clear any pending cancel request.
        if effective_status_upper in _CANCEL_TERMINAL_STATUSES:
            db_execute(
                conn,
                """
                UPDATE jobs
                SET cancel_requested = FALSE
                WHERE job_id = ?
                """,
                (job.job_id,),
            )

        conn.commit()

    # Read back the cancel_requested flag to echo it in the response.
    # The plugin reads this field on every reportJobStatus tick and acts
    # on it -- this is the primary signal path for cancellation.
    cancel_requested_flag = False
    try:
        with get_db() as conn:
            flag_row = db_execute(
                conn,
                "SELECT cancel_requested FROM jobs WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()
            if flag_row is not None:
                cancel_requested_flag = _bool_value(
                    flag_row["cancel_requested"]
                )
    except Exception:
        cancel_requested_flag = False

    # Keep notification behavior identical to the working local version.
    send_job_completion_notification(
        user_id=authenticated_user_id,
        job_id=job.job_id,
        job_name=job.name,
        status_value=insert_status,
        percent=job.percent,
        error_message=job.error_message,
    )

    print(
        f"[JOB] {job.job_id} | "
        f"{insert_status:<10} | "
        f"{job.percent}% | "
        f"{job.name} | "
        f"user={authenticated_user_id} "
        f"machine={authenticated_machine_id}"
        + (" | CANCEL_REQUESTED" if cancel_requested_flag else "")
        + (
            f" | STATUS LOCKED (already {existing_status}, ignored {incoming_status})"
            if status_locked
            else ""
        )
    )

    return {
        "accepted": True,
        "job_id": job.job_id,
        "status": insert_status,
        "received_at": received_at,
        "user_id": authenticated_user_id,
        "machine_id": authenticated_machine_id,
        "cancel_requested": cancel_requested_flag,
    }


# ============================================================================
# Cancel job (mobile → backend)
# ============================================================================

# Status values that mean the job is already done — cannot cancel.
_CANCEL_TERMINAL_STATUSES = frozenset(
    {"COMPLETED", "FAILED", "CANCELED", "CANCELLED", "TIMEDOUT"}
)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    user: dict[str, Any] = Depends(get_mobile_user),
) -> dict[str, Any]:
    """Request cancellation of a running job.

    - 202 Accepted  — cancel flag set (or already set, idempotent).
    - 403 Forbidden — job belongs to a different user.
    - 404 Not Found — no such job for this user.
    - 409 Conflict  — job is already in a terminal state.
    """
    authenticated_user_id = user["user_id"]

    with get_db() as conn:
        row = db_execute(
            conn,
            """
            SELECT
                job_id,
                user_id,
                status,
                name,
                cancel_requested
            FROM jobs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found",
            )

        if row["user_id"] != authenticated_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to cancel this job",
            )

        current_status = (row["status"] or "").strip().upper()

        if current_status in _CANCEL_TERMINAL_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Job is already in a terminal state ({current_status}) "
                    "and cannot be cancelled"
                ),
            )

        # Idempotent: if already flagged, just acknowledge.
        if _bool_value(row["cancel_requested"]):
            return {
                "cancel_accepted": True,
                "job_id": job_id,
                "job_name": row["name"],
                "message": "Cancellation already pending — the solver will stop shortly.",
            }

        db_execute(
            conn,
            """
            UPDATE jobs
            SET cancel_requested = TRUE
            WHERE job_id = ?
            """,
            (job_id,),
        )

        conn.commit()

    print(
        f"[CANCEL] Cancel requested for job={job_id} "
        f"by user={authenticated_user_id}"
    )

    return {
        "cancel_accepted": True,
        "job_id": job_id,
        "job_name": row["name"],
        "message": "Cancellation requested. The solver will stop within seconds.",
    }


# ============================================================================
# Cancel-status fast-path (plugin → backend, X-Api-Key authenticated)
# ============================================================================


@app.get("/jobs/{job_id}/cancel-status")
def job_cancel_status(
    job_id: str,
    identity: dict[str, str] = Depends(get_moldflow_identity),
) -> dict[str, Any]:
    """Lightweight endpoint for the workstation plugin to poll whether a
    cancellation has been requested for the given job.

    Called every ~5 s from mobile_reporter.check_cancel() during an active
    solve to guarantee ≤5 s cancel-propagation latency, independent of how
    often report_status() would normally send an update.

    Returns {job_id, cancel_requested: bool, status: str}.
    Returns 404 if the job does not exist in the database yet.
    """
    authenticated_user_id = identity["user_id"]

    with get_db() as conn:
        row = db_execute(
            conn,
            """
            SELECT
                job_id,
                user_id,
                status,
                cancel_requested
            FROM jobs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found",
            )

        if row["user_id"] != authenticated_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to query this job",
            )

        return {
            "job_id": job_id,
            "cancel_requested": _bool_value(row["cancel_requested"]),
            "status": (row["status"] or "").strip().upper(),
        }


# ============================================================================
# Remove job from mobile list (mobile → backend)
# ============================================================================


@app.delete("/jobs/{job_id}")
def archive_job(
    job_id: str,
    user: dict[str, Any] = Depends(get_mobile_user),
) -> dict[str, Any]:
    """Remove a finished job from the mobile job list.

    This is a SOFT delete: it sets jobs.archived = TRUE so the row (and its
    job_events audit trail) is preserved, and GET /jobs simply stops
    returning it. Only jobs already in a terminal state can be removed —
    an active job must be cancelled first, never deleted out from under a
    running solve.

    - 200 OK        — job removed from the list (or already archived).
    - 403 Forbidden — job belongs to a different user.
    - 404 Not Found — no such job for this user.
    - 409 Conflict  — job is still active and must be cancelled first.
    """
    authenticated_user_id = user["user_id"]

    with get_db() as conn:
        row = db_execute(
            conn,
            """
            SELECT job_id, user_id, status, name, archived
            FROM jobs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found",
            )

        if row["user_id"] != authenticated_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to remove this job",
            )

        current_status = (row["status"] or "").strip().upper()

        if current_status not in _CANCEL_TERMINAL_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Job is still active ({current_status}) — cancel it "
                    "before removing it from the list"
                ),
            )

        if not _bool_value(row["archived"]):
            db_execute(
                conn,
                """
                UPDATE jobs
                SET archived = TRUE
                WHERE job_id = ?
                """,
                (job_id,),
            )
            conn.commit()

    print(
        f"[ARCHIVE] Job removed from list: job={job_id} "
        f"by user={authenticated_user_id}"
    )

    return {
        "archived": True,
        "job_id": job_id,
        "job_name": row["name"],
        "message": "Job removed from your list.",
    }


# ============================================================================
# Active jobs for this machine (workstation monitor → backend)
# ============================================================================


@app.get("/internal/active-jobs")
def active_jobs_for_machine(
    identity: dict[str, str] = Depends(get_moldflow_identity),
) -> list[dict[str, Any]]:
    """Jobs on THIS machine that a standalone monitor should keep polling.

    Used by the independent monitoring agent, which keeps watching SCM
    (Simulation Compute Manager) even after Synergy and its plugin have
    closed. The agent never touches PostgreSQL directly -- it only ever
    talks to FastAPI, exactly like the plugin does -- and it never needs to
    resolve which SCM job belongs to which study itself: that identity
    (scm_job_id) is already resolved once by the plugin's existing
    find_job()/exclude_ids logic while Synergy is open, and persisted here
    on the job's first report. This endpoint just hands back the jobs
    already carrying that resolved id.

    Scoped to the authenticated machine via the same X-Api-Key identity
    used by report_job_status/cancel-status, so one machine's monitor can
    never see another machine's jobs -- multi-machine isolation falls out
    of the existing auth join, nothing new to enforce here.

    Only returns jobs that:
    - belong to this machine
    - are not archived
    - are not already in a terminal state (nothing left to poll)
    - have a known scm_job_id (nothing to poll without one)
    """
    authenticated_machine_id = identity["machine_id"]

    with get_db() as conn:
        rows = db_execute(
            conn,
            """
            SELECT
                job_id,
                name,
                job_type,
                status,
                percent,
                started,
                finished,
                scm_job_id,
                scm_type,
                compute_source,
                scm_user,
                worker,
                parent_job_id,
                cancel_requested
            FROM jobs
            WHERE machine_id = ?
              AND archived = FALSE
              AND scm_job_id IS NOT NULL
            ORDER BY updated_at DESC
            """,
            (authenticated_machine_id,),
        ).fetchall()

    result = []
    for row in rows:
        current_status = (row["status"] or "").strip().upper()
        if current_status in _CANCEL_TERMINAL_STATUSES:
            continue

        item = dict(row)
        item["status"] = current_status
        item["finished"] = _bool_value(item["finished"])
        item["cancel_requested"] = _bool_value(item["cancel_requested"])
        result.append(item)

    return result
