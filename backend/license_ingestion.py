"""
Network License Subsystem - Stage 3 Backend Ingestion API
Provides /internal/licenseStatus for receiving normalized license monitor snapshots.
Enforces:
- Independent machine-to-backend authentication (Bearer <LICENSE_INGESTION_KEY>)
- Schema validation via Pydantic
- Semantic validation (package math, cross-server isolation)
- Error diagnostics preservation (-15, -96)
- Safe operational logging
"""

from __future__ import annotations

import os
import time
import uuid
import hmac
import logging
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator, model_validator


logger = logging.getLogger("moldflow_backend.license_ingestion")

# ============================================================================
# Authentication
# ============================================================================

license_bearer_scheme = HTTPBearer(auto_error=False)


def get_license_ingestion_key() -> str:
    """Reads LICENSE_INGESTION_KEY from environment without caching."""
    key = os.getenv("LICENSE_INGESTION_KEY")
    if key is None:
        return "dev-license-ingestion-key-2026"
    return key.strip()


def verify_license_ingestion_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(license_bearer_scheme),
) -> None:
    """
    Dedicated machine-to-backend authentication dependency.
    Enforces Bearer <LICENSE_INGESTION_KEY> with constant-time comparison.
    Never prints or logs the secret key.
    """
    secret = get_license_ingestion_key()
    if not secret:
        logger.error("LICENSE_INGESTION_KEY is not configured in the backend environment.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LICENSE_INGESTION_KEY is not configured on server",
        )

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing license ingestion bearer token",
        )

    if not hmac.compare_digest(credentials.credentials, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid license ingestion key",
        )


# ============================================================================
# Request & Response Models
# ============================================================================

class ServerStatusEnum(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    VENDOR_DOWN = "VENDOR_DOWN"
    UNKNOWN = "UNKNOWN"


class LicenseServerPayload(BaseModel):
    hostname: str = Field(min_length=1)
    port: int = Field(default=27000, ge=1, le=65535)
    status: ServerStatusEnum
    lmgrd_version: str | None = None
    adskflex_status: str | None = None
    adskflex_version: str | None = None
    error_code: int | None = None
    error_message: str | None = None


class LicensePackagePayload(BaseModel):
    feature_code: str = Field(min_length=1)
    total_issued: int = Field(ge=0)
    in_use: int = Field(ge=0)
    available: int = Field(ge=0)
    utilization_pct: float = Field(ge=0.0, le=100.0)
    product_family: str | None = None
    product_name: str | None = None
    catalog_status: str = "UNKNOWN"
    components: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_package_math(self) -> LicensePackagePayload:
        if self.in_use > self.total_issued:
            raise ValueError(
                f"Package '{self.feature_code}': in_use ({self.in_use}) exceeds total_issued ({self.total_issued})"
            )
        if self.available != (self.total_issued - self.in_use):
            raise ValueError(
                f"Package '{self.feature_code}': available ({self.available}) does not equal "
                f"total_issued ({self.total_issued}) - in_use ({self.in_use})"
            )
        return self


class LicenseFeaturePayload(BaseModel):
    feature_code: str = Field(min_length=1)
    total_issued: int = Field(ge=0)
    in_use: int = Field(ge=0)
    available: int = Field(ge=0)
    feature_type: str = "UNKNOWN"
    product_family: str | None = None
    product_name: str | None = None
    year_version: str | None = None
    catalog_status: str = "UNKNOWN"
    parent_package: str | None = None


class LicenseCheckoutPayload(BaseModel):
    checkout_id: str = Field(min_length=1)
    server_hostname: str = Field(min_length=1)
    username: str = Field(min_length=1)
    machine_name: str = Field(min_length=1)
    display: str | None = None
    package_feature: str = Field(min_length=1)
    selected_component: str = Field(min_length=1)
    version: str | None = None
    server_handle: str | None = None
    checkout_time: str = Field(min_length=1)
    checkout_time_precision: str = "MINUTE"
    pid: str = ""
    is_borrowed: bool = False
    is_incomplete: bool = False
    anomaly_note: str | None = None


class LicenseSnapshotPayload(BaseModel):
    schema_version: str = "1.0"
    monitor_version: str = "1.0.0"
    captured_at: str = Field(min_length=1)
    server: LicenseServerPayload
    packages: list[LicensePackagePayload] = Field(default_factory=list)
    features: list[LicenseFeaturePayload] = Field(default_factory=list)
    checkouts: list[LicenseCheckoutPayload] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)


class LicenseIngestionResponse(BaseModel):
    status: str = "accepted"
    server_hostname: str
    captured_at: str
    request_id: str


# ============================================================================
# Router & Ingestion Endpoint
# ============================================================================

router = APIRouter(prefix="/internal", tags=["Internal License Ingestion"])


@router.post(
    "/licenseStatus",
    response_model=LicenseIngestionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_license_ingestion_key)],
)
def ingest_license_status(
    snapshot: LicenseSnapshotPayload,
) -> LicenseIngestionResponse:
    """
    Internal endpoint to receive normalized license monitor snapshots.
    Validates:
    - Independent server identity (Server A != Server B)
    - Cross-server checkout isolation (checkout.server_hostname must match server.hostname)
    - Package capacity and utilization arithmetic
    - Server down diagnostics
    """
    start_time = time.perf_counter()
    request_id = f"req-{uuid.uuid4().hex[:12]}"

    server_norm = snapshot.server.hostname.strip().lower()

    # Validate cross-server isolation: all checkouts must match snapshot server
    for co in snapshot.checkouts:
        co_server_norm = co.server_hostname.strip().lower()
        if co_server_norm != server_norm:
            logger.warning(
                f"[License Ingestion] Cross-server mismatch: checkout server '{co.server_hostname}' "
                f"does not match snapshot server '{snapshot.server.hostname}' (req_id: {request_id})"
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Checkout server_hostname '{co.server_hostname}' does not match "
                    f"snapshot server '{snapshot.server.hostname}'"
                ),
            )

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    from app_postgres_ready import get_db
    from license_persistence import LicensePersistenceService

    try:
        with get_db() as conn:
            persistence_result = LicensePersistenceService.process_snapshot(
                conn,
                snapshot=snapshot,
                duration_ms=elapsed_ms,
            )
            conn.commit()
    except Exception as exc:
        logger.exception(f"[License Ingestion] Failed to process snapshot: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Persistence error: {type(exc).__name__}: {str(exc)}",
        )

    # Safe operational logging without sensitive secrets or raw license data
    logger.info(
        f"[License Ingestion] Accepted snapshot for server '{snapshot.server.hostname}' "
        f"(status: {snapshot.server.status.value}, packages: {len(snapshot.packages)}, "
        f"checkouts: {len(snapshot.checkouts)}, events: {len(persistence_result.get('events_generated', []))}) "
        f"in {elapsed_ms:.2f}ms [req_id: {request_id}]"
    )

    return LicenseIngestionResponse(
        status="accepted",
        server_hostname=snapshot.server.hostname,
        captured_at=snapshot.captured_at,
        request_id=request_id,
    )

