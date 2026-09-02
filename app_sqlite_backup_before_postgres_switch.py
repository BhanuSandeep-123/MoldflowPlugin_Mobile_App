"""Moldflow Mobile Job Backend - Phase 4

Architecture:

Moldflow plugin
    -> X-Api-Key
    -> POST /reportJobStatus

Mobile app
    -> email/password
    -> POST /auth/login
    -> JWT Bearer token
    -> /me, /jobs, /devices

Development only.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel, Field
from pwdlib import PasswordHash

from fcm_service import send_fcm_notification


# ============================================================================
# Configuration
# ============================================================================

APP_NAME = "Moldflow Mobile Job Backend"

# Existing Moldflow -> backend authentication.
MOLDFLOW_API_KEY = os.getenv(
    "MOLDFLOW_API_KEY",
    "dev-moldflow-key-change-me",
)

# JWT signing secret.
JWT_SECRET_KEY = os.getenv(
    "JWT_SECRET_KEY",
    "DEV-ONLY-CHANGE-THIS-SECRET",
)

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Development account.
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
    version="0.4.0",
)

_db_lock = threading.Lock()

password_hash = PasswordHash.recommended()

bearer_scheme = HTTPBearer(
    auto_error=False,
)


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
    # The backend does NOT trust these fields.
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
# Database
# ============================================================================

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    """Add a column to an existing development database if necessary."""

    columns = conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    existing_names = {
        str(row["name"])
        for row in columns
    }

    if column not in existing_names:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_database() -> None:
    with _db_lock:
        conn = get_connection()

        try:
            # ----------------------------------------------------------------
            # Users
            # ----------------------------------------------------------------

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    password_hash TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

            # Migration for databases created during Phase 3.
            ensure_column(
                conn,
                "users",
                "password_hash",
                "TEXT",
            )

            # ----------------------------------------------------------------
            # Machines
            # ----------------------------------------------------------------

            conn.execute(
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
                """
            )

            # ----------------------------------------------------------------
            # Devices
            # ----------------------------------------------------------------

            conn.execute(
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
                """
            )
            # ----------------------------------------------------------------
            # Push notification history
            # ----------------------------------------------------------------

            conn.execute(
                """
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
            )
            # ----------------------------------------------------------------
            # API clients
            # ----------------------------------------------------------------

            conn.execute(
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
                """
            )

            # ----------------------------------------------------------------
            # Existing jobs
            # ----------------------------------------------------------------

            conn.execute(
                """
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
            )

            # ----------------------------------------------------------------
            # Simulation Compute Manager metadata migration
            # ----------------------------------------------------------------

            ensure_column(conn, "jobs", "scm_job_id", "TEXT")
            ensure_column(conn, "jobs", "scm_type", "TEXT")
            ensure_column(conn, "jobs", "compute_source", "TEXT")
            ensure_column(conn, "jobs", "scm_user", "TEXT")
            ensure_column(conn, "jobs", "worker", "TEXT")
            ensure_column(conn, "jobs", "parent_job_id", "TEXT")

            # ----------------------------------------------------------------
            # Existing job history
            # ----------------------------------------------------------------

            conn.execute(
                """
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
            )

            # ----------------------------------------------------------------
            # Indexes
            # ----------------------------------------------------------------

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_user_id
                ON jobs(user_id)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_updated_at
                ON jobs(updated_at)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_job_events_job_id
                ON job_events(job_id)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_devices_user_id
                ON devices(user_id)
                """
            )

            # ----------------------------------------------------------------
            # Development user
            # ----------------------------------------------------------------

            timestamp = now_utc()

            hashed_password = password_hash.hash(
                DEV_USER_PASSWORD
            )

            existing_user = conn.execute(
                """
                SELECT user_id, password_hash
                FROM users
                WHERE user_id = ?
                """,
                (DEV_USER_ID,),
            ).fetchone()

            if existing_user is None:

                conn.execute(
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

                # Populate the password hash only if this existing
                # development user does not already have one.
                if not existing_user["password_hash"]:

                    conn.execute(
                        """
                        UPDATE users
                        SET
                            password_hash = ?
                        WHERE user_id = ?
                        """,
                        (
                            hashed_password,
                            DEV_USER_ID,
                        ),
                    )

                conn.execute(
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

            conn.execute(
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

            conn.execute(
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

        finally:
            conn.close()


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

    with _db_lock:

        conn = get_connection()

        try:

            row = conn.execute(
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

            conn.execute(
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

        finally:
            conn.close()


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

    with _db_lock:

        conn = get_connection()

        try:

            row = conn.execute(
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

        finally:
            conn.close()


# ============================================================================
# Startup
# ============================================================================

@app.on_event("startup")
def startup() -> None:

    init_database()

    print(
        f"[DB] SQLite database: {DB_PATH}"
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


# ============================================================================
# Health
# ============================================================================

@app.get("/health")
def health() -> dict[str, str]:

    return {
        "status": "ok",
        "service": APP_NAME,
    }


# ============================================================================
# Mobile login
# ============================================================================

@app.post("/auth/login")
def mobile_login(
    credentials: LoginRequest,
) -> dict[str, Any]:

    with _db_lock:

        conn = get_connection()

        try:

            row = conn.execute(
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

        finally:
            conn.close()


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

    with _db_lock:

        conn = get_connection()

        try:

            rows = conn.execute(
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
                ORDER BY updated_at DESC
                """,
                (user["user_id"],),
            ).fetchall()

            result = []

            for row in rows:

                item = dict(row)
                item["finished"] = bool(
                    item["finished"]
                )

                result.append(item)

            return result

        finally:
            conn.close()


# ============================================================================
# Mobile single job
# ============================================================================

@app.get("/jobs/{job_id}")
def mobile_job(
    job_id: str,
    user: dict[str, Any] = Depends(get_mobile_user),
) -> dict[str, Any]:

    with _db_lock:

        conn = get_connection()

        try:

            row = conn.execute(
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

            result["finished"] = bool(
                result["finished"]
            )

            return result

        finally:
            conn.close()


# ============================================================================
# Mobile job history
# ============================================================================

@app.get("/jobs/{job_id}/events")
def mobile_job_events(
    job_id: str,
    user: dict[str, Any] = Depends(get_mobile_user),
) -> list[dict[str, Any]]:

    with _db_lock:

        conn = get_connection()

        try:

            rows = conn.execute(
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

                item["finished"] = bool(
                    item["finished"]
                )

                result.append(item)

            return result

        finally:
            conn.close()


# ============================================================================
# Mobile devices
# ============================================================================

@app.get("/devices")
def mobile_devices(
    user: dict[str, Any] = Depends(get_mobile_user),
) -> list[dict[str, Any]]:

    with _db_lock:

        conn = get_connection()

        try:

            rows = conn.execute(
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

        finally:
            conn.close()


# ============================================================================
# Mobile device registration
# ============================================================================

@app.post("/devices/register")
def register_device(
    device: DeviceRegistration,
    user: dict[str, Any] = Depends(get_mobile_user),
) -> dict[str, Any]:

    timestamp = now_utc()

    with _db_lock:

        conn = get_connection()

        try:

            conn.execute(
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

        finally:
            conn.close()

    return {
        "registered": True,
        "device_id": device.device_id,
        "user_id": user["user_id"],
        "platform": device.platform,
    }


def send_job_completion_notification(
    user_id: str,
    job_id: str,
    job_name: str,
    status_value: str,
    percent: float | int | None,
    error_message: str | None,
) -> None:
    """
    Send exactly one push notification for a terminal Moldflow job state.

    Notifications:
      COMPLETED -> success notification
      FAILED    -> failure notification
    """

    normalized_status = (
        (status_value or "")
        .strip()
        .upper()
    )

    # Only notify for terminal states.
    if normalized_status not in {
        "COMPLETED",
        "FAILED",
    }:
        return

    notification_type = normalized_status

    # ------------------------------------------------------------
    # Check whether this notification was already sent
    # ------------------------------------------------------------

    with _db_lock:
        conn = get_connection()

        try:
            existing = conn.execute(
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
        finally:
            conn.close()

    if existing:
        print(
            f"[FCM] Notification already sent "
            f"job={job_id} "
            f"type={notification_type}"
        )
        return

    # ------------------------------------------------------------
    # Find registered Android devices for this user
    # ------------------------------------------------------------

    with _db_lock:
        conn = get_connection()

        try:
            devices = conn.execute(
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
        finally:
            conn.close()

    if not devices:
        print(
            f"[FCM] No registered Android devices "
            f"for user={user_id}"
        )
        return

    # ------------------------------------------------------------
    # Notification content
    # ------------------------------------------------------------

    if normalized_status == "COMPLETED":
        title = "Moldflow Analysis Completed"

        body = (
            f"{job_name} completed successfully."
        )

    else:
        title = "Moldflow Analysis Failed"

        if error_message:
            body = (
                f"{job_name} failed: "
                f"{error_message}"
            )
        else:
            body = (
                f"{job_name} failed."
            )

    # ------------------------------------------------------------
    # Send notification to registered devices
    # ------------------------------------------------------------

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
                    "percent": str(
                        percent if percent is not None else 0
                    ),
                },
            )

            print(
                f"[FCM] Sent notification "
                f"device={device_id} "
                f"job={job_id} "
                f"type={notification_type}"
            )

            sent_successfully = True

        except Exception as ex:
            print(
                f"[FCM] Failed notification "
                f"device={device_id}: {ex}"
            )

    # ------------------------------------------------------------
    # Record notification only if at least one device succeeded
    # ------------------------------------------------------------

    if sent_successfully:
        timestamp = now_utc()

        with _db_lock:
            conn = get_connection()

            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO job_notifications (
                        job_id,
                        user_id,
                        notification_type,
                        sent_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        user_id,
                        notification_type,
                        timestamp,
                    ),
                )

                conn.commit()

            finally:
                conn.close()

# ============================================================================
# Moldflow job ingestion
#
# IMPORTANT:
# This remains API-key based because the existing Moldflow plugin uses it.
# ============================================================================


@app.post("/reportJobStatus")
def report_job_status(
    job: JobStatus,
    identity: dict[str, str] = Depends(get_moldflow_identity),
) -> dict[str, Any]:

    authenticated_user_id = identity["user_id"]
    authenticated_machine_id = identity["machine_id"]

    received_at = now_utc()

    with _db_lock:

        conn = get_connection()

        try:

            existing = conn.execute(
                """
                SELECT update_count
                FROM jobs
                WHERE job_id = ?
                """,
                (job.job_id,),
            ).fetchone()

            update_count = (
                int(existing["update_count"]) + 1
                if existing
                else 1
            )

            conn.execute(
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

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
                    scm_job_id = COALESCE(excluded.scm_job_id, jobs.scm_job_id),
                    scm_type = COALESCE(excluded.scm_type, jobs.scm_type),
                    compute_source = COALESCE(excluded.compute_source, jobs.compute_source),
                    scm_user = COALESCE(excluded.scm_user, jobs.scm_user),
                    worker = COALESCE(excluded.worker, jobs.worker),
                    parent_job_id = COALESCE(excluded.parent_job_id, jobs.parent_job_id)
                """,
                (
                    job.job_id,
                    job.name,
                    job.type,
                    job.status,
                    job.percent,
                    job.started,
                    int(job.finished),
                    job.error_message,
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

            conn.execute(
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
                    int(job.finished),
                    job.error_message,
                    received_at,
                ),
            )

            conn.commit()

        finally:
            conn.close()

    # ------------------------------------------------------------
    # Push notification for terminal job states
    # ------------------------------------------------------------

    send_job_completion_notification(
        user_id=authenticated_user_id,
        job_id=job.job_id,
        job_name=job.name,
        status_value=job.status,
        percent=job.percent,
        error_message=job.error_message,
    )

    print(
        f"[JOB] {job.job_id} | "
        f"{job.status:<10} | "
        f"{job.percent}% | "
        f"{job.name} | "
        f"user={authenticated_user_id} "
        f"machine={authenticated_machine_id}"
    )

    return {
        "accepted": True,
        "job_id": job.job_id,
        "status": job.status,
        "received_at": received_at,
        "user_id": authenticated_user_id,
        "machine_id": authenticated_machine_id,
    }
