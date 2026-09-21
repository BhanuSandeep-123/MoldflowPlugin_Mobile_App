"""
Network License Subsystem - Stage 4 PostgreSQL Persistence & Snapshot Diff Engine
Provides:
- Schema initialization for PostgreSQL and SQLite
- Server identification and catalog management
- Atomic snapshot persistence
- Current feature state and active checkout tracking
- Snapshot diff engine for event generation (CHECKOUT, RETURN, EXHAUSTED, AVAILABLE, SERVER_UP, SERVER_DOWN, VENDOR_DOWN)
- Server DOWN protection (prevents spurious RETURN events during outages)
- Idempotency protection against duplicate delivery
- Configurable snapshot retention pruning
"""

from __future__ import annotations

import os
import json
import uuid
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from license_ingestion import LicenseSnapshotPayload, ServerStatusEnum


logger = logging.getLogger("moldflow_backend.license_persistence")

# Snapshot retention setting (default: 7 days)
SNAPSHOT_RETENTION_DAYS = int(os.getenv("LICENSE_SNAPSHOT_RETENTION_DAYS", "7"))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# Schema Initialization
# ============================================================================

def init_license_tables(conn) -> None:
    """Creates all Network License tables and indexes if they do not exist."""
    from app_postgres_ready import db_execute, USE_POSTGRES

    # 1. License Servers
    db_execute(
        conn,
        f"""
        CREATE TABLE IF NOT EXISTS license_servers (
            server_id TEXT PRIMARY KEY,
            hostname TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            lmgrd_port INTEGER NOT NULL DEFAULT 27000,
            vendor_daemon TEXT NOT NULL DEFAULT 'adskflex',
            vendor_daemon_port INTEGER,
            status TEXT NOT NULL DEFAULT 'UNKNOWN',
            last_successful_poll TEXT,
            last_poll_attempt TEXT,
            last_error_code INTEGER,
            last_error_message TEXT,
            is_active {'BOOLEAN NOT NULL DEFAULT TRUE' if USE_POSTGRES else 'INTEGER NOT NULL DEFAULT 1'},
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # 2. Feature Catalog (Global Metadata)
    db_execute(
        conn,
        """
        CREATE TABLE IF NOT EXISTS license_feature_catalog (
            feature_code TEXT PRIMARY KEY,
            feature_type TEXT NOT NULL,
            product_family TEXT,
            product_name TEXT,
            year_version TEXT,
            parent_package_code TEXT,
            catalog_status TEXT NOT NULL DEFAULT 'VERIFIED',
            description TEXT
        )
        """
    )

    # 3. Snapshots Header
    db_execute(
        conn,
        """
        CREATE TABLE IF NOT EXISTS license_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            server_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            server_status TEXT NOT NULL,
            lmgrd_version TEXT,
            adskflex_status TEXT,
            adskflex_version TEXT,
            error_code INTEGER,
            error_message TEXT,
            monitor_version TEXT NOT NULL DEFAULT '1.0.0',
            schema_version TEXT NOT NULL DEFAULT '1.0',
            raw_payload_hash TEXT NOT NULL,
            query_duration_ms REAL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (server_id) REFERENCES license_servers(server_id) ON DELETE CASCADE
        )
        """
    )

    # 4. Snapshot Features (Per Snapshot History)
    id_col = "id BIGSERIAL PRIMARY KEY" if USE_POSTGRES else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    db_execute(
        conn,
        f"""
        CREATE TABLE IF NOT EXISTS license_snapshot_features (
            {id_col},
            snapshot_id TEXT NOT NULL,
            server_id TEXT NOT NULL,
            feature_code TEXT NOT NULL,
            feature_type TEXT NOT NULL,
            total_issued INTEGER NOT NULL DEFAULT 0,
            in_use INTEGER NOT NULL DEFAULT 0,
            available INTEGER NOT NULL DEFAULT 0,
            utilization_pct REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (snapshot_id) REFERENCES license_snapshots(snapshot_id) ON DELETE CASCADE,
            FOREIGN KEY (server_id) REFERENCES license_servers(server_id) ON DELETE CASCADE
        )
        """
    )

    # 5. Current Feature State (Per Server + Feature)
    db_execute(
        conn,
        """
        CREATE TABLE IF NOT EXISTS license_server_features (
            server_id TEXT NOT NULL,
            feature_code TEXT NOT NULL,
            feature_type TEXT NOT NULL,
            total_issued INTEGER NOT NULL DEFAULT 0,
            in_use INTEGER NOT NULL DEFAULT 0,
            available INTEGER NOT NULL DEFAULT 0,
            utilization_pct REAL,
            last_snapshot_id TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (server_id, feature_code),
            FOREIGN KEY (server_id) REFERENCES license_servers(server_id) ON DELETE CASCADE
        )
        """
    )

    # 6. Current Active Physical Checkouts
    db_execute(
        conn,
        f"""
        CREATE TABLE IF NOT EXISTS license_active_checkouts (
            checkout_id TEXT NOT NULL,
            server_id TEXT NOT NULL,
            username TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            display TEXT,
            package_feature_code TEXT NOT NULL,
            selected_component_code TEXT NOT NULL,
            version TEXT,
            server_handle TEXT,
            checkout_time TEXT NOT NULL,
            checkout_time_precision TEXT NOT NULL DEFAULT 'MINUTE',
            pid TEXT,
            is_borrowed {'BOOLEAN NOT NULL DEFAULT FALSE' if USE_POSTGRES else 'INTEGER NOT NULL DEFAULT 0'},
            is_incomplete {'BOOLEAN NOT NULL DEFAULT FALSE' if USE_POSTGRES else 'INTEGER NOT NULL DEFAULT 0'},
            anomaly_note TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_snapshot_id TEXT NOT NULL,
            PRIMARY KEY (server_id, checkout_id),
            FOREIGN KEY (server_id) REFERENCES license_servers(server_id) ON DELETE CASCADE
        )
        """
    )

    # 7. License Events Audit History
    db_execute(
        conn,
        """
        CREATE TABLE IF NOT EXISTS license_events (
            event_id TEXT PRIMARY KEY,
            server_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            feature_code TEXT,
            checkout_id TEXT,
            username TEXT,
            machine_name TEXT,
            selected_component_code TEXT,
            previous_in_use INTEGER,
            new_in_use INTEGER,
            total_issued INTEGER,
            detected_at TEXT NOT NULL,
            snapshot_id TEXT,
            previous_snapshot_id TEXT,
            details TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (server_id) REFERENCES license_servers(server_id) ON DELETE CASCADE
        )
        """
    )

    # Indexes
    db_execute(conn, "CREATE INDEX IF NOT EXISTS idx_lic_snapshots_server_time ON license_snapshots(server_id, captured_at)")
    db_execute(conn, "CREATE INDEX IF NOT EXISTS idx_lic_snap_features_snap ON license_snapshot_features(snapshot_id)")
    db_execute(conn, "CREATE INDEX IF NOT EXISTS idx_lic_checkouts_server ON license_active_checkouts(server_id)")
    db_execute(conn, "CREATE INDEX IF NOT EXISTS idx_lic_events_server_time ON license_events(server_id, detected_at)")
    db_execute(conn, "CREATE INDEX IF NOT EXISTS idx_lic_events_type_time ON license_events(event_type, detected_at)")


# ============================================================================
# Persistence & Diff Engine Service
# ============================================================================

class LicensePersistenceService:

    @staticmethod
    def get_or_create_server(
        conn,
        hostname: str,
        port: int = 27000,
        display_name: Optional[str] = None
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Resolves server_id for a given hostname.
        Normalizes hostname (lowercase, trim).
        Creates record if not exists.
        Returns (server_id, previous_server_record).
        """
        from app_postgres_ready import db_execute

        norm_hostname = hostname.strip().lower()
        row = db_execute(
            conn,
            "SELECT * FROM license_servers WHERE LOWER(hostname) = LOWER(?)",
            (norm_hostname,)
        ).fetchone()

        if row is not None:
            prev_record = dict(row)
            return prev_record["server_id"], prev_record

        # Create new server record
        new_server_id = str(uuid.uuid4())
        ts = now_utc()
        d_name = display_name or hostname.strip()
        db_execute(
            conn,
            """
            INSERT INTO license_servers (
                server_id, hostname, display_name, lmgrd_port,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'UNKNOWN', ?, ?)
            """,
            (new_server_id, norm_hostname, d_name, port, ts, ts)
        )
        return new_server_id, None

    @classmethod
    def process_snapshot(
        cls,
        conn,
        snapshot: LicenseSnapshotPayload,
        duration_ms: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Atomically persists a snapshot, updates current state, and executes diff engine.
        Returns dict with snapshot_id, server_id, and list of generated events.
        """
        from app_postgres_ready import db_execute

        # 0. Ensure tables exist
        init_license_tables(conn)

        # 1. Compute raw payload hash for idempotency
        payload_bytes = snapshot.model_dump_json(exclude_none=False).encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()

        # 2. Resolve server
        server_id, prev_server = cls.get_or_create_server(
            conn,
            hostname=snapshot.server.hostname,
            port=snapshot.server.port,
        )

        # 3. Idempotency check: same server + captured_at + hash
        existing_snap = db_execute(
            conn,
            """
            SELECT snapshot_id FROM license_snapshots
            WHERE server_id = ? AND captured_at = ? AND raw_payload_hash = ?
            """,
            (server_id, snapshot.captured_at, payload_hash)
        ).fetchone()

        if existing_snap is not None:
            logger.info(
                f"[License Ingestion] Duplicate snapshot absorbed for server '{snapshot.server.hostname}' "
                f"at '{snapshot.captured_at}'. Idempotent response returned."
            )
            return {
                "snapshot_id": existing_snap["snapshot_id"],
                "server_id": server_id,
                "idempotent_duplicate": True,
                "events_generated": [],
            }

        # 4. Generate snapshot_id and insert snapshot header
        snapshot_id = str(uuid.uuid4())
        ts = now_utc()

        db_execute(
            conn,
            """
            INSERT INTO license_snapshots (
                snapshot_id, server_id, captured_at, server_status,
                lmgrd_version, adskflex_status, adskflex_version,
                error_code, error_message, monitor_version,
                schema_version, raw_payload_hash, query_duration_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                server_id,
                snapshot.captured_at,
                snapshot.server.status.value,
                snapshot.server.lmgrd_version,
                snapshot.server.adskflex_status,
                snapshot.server.adskflex_version,
                snapshot.server.error_code,
                snapshot.server.error_message,
                snapshot.monitor_version,
                snapshot.schema_version,
                payload_hash,
                duration_ms,
                ts,
            )
        )

        events_generated: List[str] = []

        # 5. Server Health Transitions
        prev_status = (prev_server["status"] if prev_server else "UNKNOWN").upper()
        curr_status = snapshot.server.status.value.upper()

        if prev_status != curr_status:
            if curr_status == ServerStatusEnum.UP.value and prev_status in ("DOWN", "VENDOR_DOWN", "UNKNOWN"):
                event_type = "SERVER_UP"
            elif curr_status == ServerStatusEnum.DOWN.value and prev_status == ServerStatusEnum.UP.value:
                event_type = "SERVER_DOWN"
            elif curr_status == ServerStatusEnum.VENDOR_DOWN.value and prev_status != ServerStatusEnum.VENDOR_DOWN.value:
                event_type = "VENDOR_DOWN"
            else:
                event_type = None

            if event_type:
                cls._record_event(
                    conn,
                    server_id=server_id,
                    event_type=event_type,
                    detected_at=snapshot.captured_at,
                    snapshot_id=snapshot_id,
                    details={"previous_status": prev_status, "current_status": curr_status},
                )
                events_generated.append(event_type)

        # 6. Global Catalog Upsert
        cls._upsert_catalog(conn, snapshot)

        # 7. Snapshot Features History & Current State Diff
        for pkg in snapshot.packages:
            # Insert historical feature row
            db_execute(
                conn,
                """
                INSERT INTO license_snapshot_features (
                    snapshot_id, server_id, feature_code, feature_type,
                    total_issued, in_use, available, utilization_pct, created_at
                ) VALUES (?, ?, ?, 'PACKAGE', ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id, server_id, pkg.feature_code,
                    pkg.total_issued, pkg.in_use, pkg.available,
                    pkg.utilization_pct, ts
                )
            )

            # Capacity transitions: EXHAUSTED / AVAILABLE
            prev_feat = db_execute(
                conn,
                """
                SELECT total_issued, in_use, available FROM license_server_features
                WHERE server_id = ? AND feature_code = ?
                """,
                (server_id, pkg.feature_code)
            ).fetchone()

            if prev_feat is not None and curr_status == ServerStatusEnum.UP.value:
                prev_avail = int(prev_feat["available"])
                if prev_avail > 0 and pkg.available == 0 and pkg.total_issued > 0:
                    cls._record_event(
                        conn,
                        server_id=server_id,
                        event_type="EXHAUSTED",
                        feature_code=pkg.feature_code,
                        previous_in_use=int(prev_feat["in_use"]),
                        new_in_use=pkg.in_use,
                        total_issued=pkg.total_issued,
                        detected_at=snapshot.captured_at,
                        snapshot_id=snapshot_id,
                        details={"product_family": pkg.product_family},
                    )
                    events_generated.append(f"EXHAUSTED:{pkg.feature_code}")
                elif prev_avail == 0 and pkg.available > 0:
                    cls._record_event(
                        conn,
                        server_id=server_id,
                        event_type="AVAILABLE",
                        feature_code=pkg.feature_code,
                        previous_in_use=int(prev_feat["in_use"]),
                        new_in_use=pkg.in_use,
                        total_issued=pkg.total_issued,
                        detected_at=snapshot.captured_at,
                        snapshot_id=snapshot_id,
                        details={"product_family": pkg.product_family},
                    )
                    events_generated.append(f"AVAILABLE:{pkg.feature_code}")

            # Upsert operational state in license_server_features
            cls._upsert_server_feature(
                conn,
                server_id=server_id,
                feature_code=pkg.feature_code,
                feature_type="PACKAGE",
                total_issued=pkg.total_issued,
                in_use=pkg.in_use,
                available=pkg.available,
                utilization_pct=pkg.utilization_pct,
                snapshot_id=snapshot_id,
                updated_at=ts,
            )

        # Snapshot Features for Components (Informational only, utilization_pct = None)
        for feat in snapshot.features:
            if feat.feature_type == "COMPONENT":
                db_execute(
                    conn,
                    """
                    INSERT INTO license_snapshot_features (
                        snapshot_id, server_id, feature_code, feature_type,
                        total_issued, in_use, available, utilization_pct, created_at
                    ) VALUES (?, ?, ?, 'COMPONENT', ?, ?, ?, NULL, ?)
                    """,
                    (
                        snapshot_id, server_id, feat.feature_code,
                        feat.total_issued, feat.in_use, feat.available, ts
                    )
                )
                cls._upsert_server_feature(
                    conn,
                    server_id=server_id,
                    feature_code=feat.feature_code,
                    feature_type="COMPONENT",
                    total_issued=feat.total_issued,
                    in_use=feat.in_use,
                    available=feat.available,
                    utilization_pct=None,
                    snapshot_id=snapshot_id,
                    updated_at=ts,
                )

        # 8. Checkouts Diff Engine
        # CRITICAL FAILURE CASE RULE:
        # If server is DOWN or VENDOR_DOWN, do NOT generate RETURN events.
        # Checkouts remain unobserved, NOT returned.
        if curr_status == ServerStatusEnum.UP.value:
            existing_checkouts = db_execute(
                conn,
                """
                SELECT checkout_id, username, machine_name, package_feature_code,
                       selected_component_code
                FROM license_active_checkouts
                WHERE server_id = ?
                """,
                (server_id,)
            ).fetchall()

            existing_map = {row["checkout_id"]: dict(row) for row in existing_checkouts}
            incoming_map = {co.checkout_id: co for co in snapshot.checkouts}

            # New checkouts: present in incoming, absent in DB
            for cid, co in incoming_map.items():
                if cid not in existing_map:
                    db_execute(
                        conn,
                        """
                        INSERT INTO license_active_checkouts (
                            checkout_id, server_id, username, machine_name, display,
                            package_feature_code, selected_component_code, version,
                            server_handle, checkout_time, checkout_time_precision,
                            pid, is_borrowed, is_incomplete, anomaly_note,
                            first_seen_at, last_seen_at, last_snapshot_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cid, server_id, co.username, co.machine_name, co.display,
                            co.package_feature, co.selected_component, co.version,
                            co.server_handle, co.checkout_time, co.checkout_time_precision,
                            co.pid, 1 if co.is_borrowed else 0, 1 if co.is_incomplete else 0,
                            co.anomaly_note, ts, ts, snapshot_id
                        )
                    )
                    cls._record_event(
                        conn,
                        server_id=server_id,
                        event_type="CHECKOUT",
                        feature_code=co.package_feature,
                        checkout_id=cid,
                        username=co.username,
                        machine_name=co.machine_name,
                        selected_component_code=co.selected_component,
                        detected_at=snapshot.captured_at,
                        snapshot_id=snapshot_id,
                        details={"pid": co.pid, "version": co.version},
                    )
                    events_generated.append(f"CHECKOUT:{co.username}:{cid[:8]}")
                else:
                    # Existing checkout: update last_seen_at and last_snapshot_id
                    db_execute(
                        conn,
                        """
                        UPDATE license_active_checkouts
                        SET last_seen_at = ?, last_snapshot_id = ?
                        WHERE server_id = ? AND checkout_id = ?
                        """,
                        (ts, snapshot_id, server_id, cid)
                    )

            # Returned checkouts: present in DB, absent in incoming
            for cid, old_co in existing_map.items():
                if cid not in incoming_map:
                    db_execute(
                        conn,
                        "DELETE FROM license_active_checkouts WHERE server_id = ? AND checkout_id = ?",
                        (server_id, cid)
                    )
                    cls._record_event(
                        conn,
                        server_id=server_id,
                        event_type="RETURN",
                        feature_code=old_co["package_feature_code"],
                        checkout_id=cid,
                        username=old_co["username"],
                        machine_name=old_co["machine_name"],
                        selected_component_code=old_co["selected_component_code"],
                        detected_at=snapshot.captured_at,
                        snapshot_id=snapshot_id,
                    )
                    events_generated.append(f"RETURN:{old_co['username']}:{cid[:8]}")

        # 9. Update Server Record
        last_success = ts if curr_status == ServerStatusEnum.UP.value else (prev_server.get("last_successful_poll") if prev_server else None)
        db_execute(
            conn,
            """
            UPDATE license_servers
            SET status = ?, last_poll_attempt = ?, last_successful_poll = ?,
                last_error_code = ?, last_error_message = ?, updated_at = ?
            WHERE server_id = ?
            """,
            (
                curr_status, ts, last_success,
                snapshot.server.error_code, snapshot.server.error_message,
                ts, server_id
            )
        )

        # 10. Snapshot Retention Pruning
        if SNAPSHOT_RETENTION_DAYS > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_RETENTION_DAYS)).isoformat()
            db_execute(
                conn,
                "DELETE FROM license_snapshots WHERE server_id = ? AND captured_at < ?",
                (server_id, cutoff)
            )

        return {
            "snapshot_id": snapshot_id,
            "server_id": server_id,
            "idempotent_duplicate": False,
            "events_generated": events_generated,
        }

    @staticmethod
    def _upsert_catalog(conn, snapshot: LicenseSnapshotPayload) -> None:
        """Upserts global catalog metadata for observed features."""
        from app_postgres_ready import db_execute

        # Catalog packages
        for pkg in snapshot.packages:
            db_execute(
                conn,
                """
                INSERT INTO license_feature_catalog (
                    feature_code, feature_type, product_family, product_name,
                    catalog_status
                ) VALUES (?, 'PACKAGE', ?, ?, ?)
                ON CONFLICT(feature_code) DO UPDATE SET
                    feature_type = excluded.feature_type,
                    product_family = COALESCE(excluded.product_family, license_feature_catalog.product_family),
                    product_name = COALESCE(excluded.product_name, license_feature_catalog.product_name),
                    catalog_status = excluded.catalog_status
                """,
                (pkg.feature_code, pkg.product_family, pkg.product_name, pkg.catalog_status)
            )

        # Catalog components
        for feat in snapshot.features:
            if feat.feature_type == "COMPONENT":
                db_execute(
                    conn,
                    """
                    INSERT INTO license_feature_catalog (
                        feature_code, feature_type, product_family, product_name,
                        year_version, parent_package_code, catalog_status
                    ) VALUES (?, 'COMPONENT', ?, ?, ?, ?, ?)
                    ON CONFLICT(feature_code) DO UPDATE SET
                        feature_type = excluded.feature_type,
                        product_family = COALESCE(excluded.product_family, license_feature_catalog.product_family),
                        product_name = COALESCE(excluded.product_name, license_feature_catalog.product_name),
                        year_version = COALESCE(excluded.year_version, license_feature_catalog.year_version),
                        parent_package_code = COALESCE(excluded.parent_package_code, license_feature_catalog.parent_package_code),
                        catalog_status = excluded.catalog_status
                    """,
                    (
                        feat.feature_code, feat.product_family, feat.product_name,
                        feat.year_version, feat.parent_package, feat.catalog_status
                    )
                )

    @staticmethod
    def _upsert_server_feature(
        conn,
        server_id: str,
        feature_code: str,
        feature_type: str,
        total_issued: int,
        in_use: int,
        available: int,
        utilization_pct: Optional[float],
        snapshot_id: str,
        updated_at: str,
    ) -> None:
        """Upserts operational feature state for a specific server."""
        from app_postgres_ready import db_execute

        db_execute(
            conn,
            """
            INSERT INTO license_server_features (
                server_id, feature_code, feature_type, total_issued,
                in_use, available, utilization_pct, last_snapshot_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, feature_code) DO UPDATE SET
                feature_type = excluded.feature_type,
                total_issued = excluded.total_issued,
                in_use = excluded.in_use,
                available = excluded.available,
                utilization_pct = excluded.utilization_pct,
                last_snapshot_id = excluded.last_snapshot_id,
                updated_at = excluded.updated_at
            """,
            (
                server_id, feature_code, feature_type, total_issued,
                in_use, available, utilization_pct, snapshot_id, updated_at
            )
        )

    @staticmethod
    def _record_event(
        conn,
        server_id: str,
        event_type: str,
        feature_code: Optional[str] = None,
        checkout_id: Optional[str] = None,
        username: Optional[str] = None,
        machine_name: Optional[str] = None,
        selected_component_code: Optional[str] = None,
        previous_in_use: Optional[int] = None,
        new_in_use: Optional[int] = None,
        total_issued: Optional[int] = None,
        detected_at: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Inserts an immutable event record into license_events."""
        from app_postgres_ready import db_execute

        event_id = str(uuid.uuid4())
        ts = now_utc()
        det_at = detected_at or ts
        details_str = json.dumps(details) if details else None

        db_execute(
            conn,
            """
            INSERT INTO license_events (
                event_id, server_id, event_type, feature_code, checkout_id,
                username, machine_name, selected_component_code,
                previous_in_use, new_in_use, total_issued,
                detected_at, snapshot_id, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, server_id, event_type, feature_code, checkout_id,
                username, machine_name, selected_component_code,
                previous_in_use, new_in_use, total_issued,
                det_at, snapshot_id, details_str, ts
            )
        )
        return event_id
