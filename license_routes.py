"""
backend/license_routes.py
-------------------------
Mobile-facing Network License Read APIs (Stage 5).
Provides authenticated, read-only endpoints for mobile clients:
- GET /licenses/overview: Dashboard summary with per-server status and aggregate environment inventory.
- GET /licenses/servers: Concise list of configured license servers.
- GET /licenses/servers/{server_id}: Full server detail with nested packages and components.
- GET /licenses/servers/{server_id}/consumers: Active physical checkouts (1 row per physical seat).
- GET /licenses/history: Paginated license events with database-level filtering.

Strict Security & Architectural Rules:
- Uses existing JWT authentication (get_mobile_user dependency).
- Strictly read-only; zero mutation of licensing or server state.
- Component features are nested under packages and never treated as independent pools.
- Aggregate inventory is explicitly marked as non-pooled with clear notice.
- Non-UP servers do NOT report fake zero licenses; in_use/available are null with data_state=UNAVAILABLE.
- Consumer response omits PID, server handle, and internal diagnostic info.
- History reads directly from license_events using indexed queries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

logger = logging.getLogger("moldflow_backend.license_routes")

router = APIRouter(prefix="/licenses", tags=["Network Licenses"])

from app_postgres_ready import db_execute, get_db, get_mobile_user

# Stale timeout: If an UP server has not reported in 5 minutes (300s), flag as STALE
STALE_THRESHOLD_SECONDS = 300

VALID_EVENT_TYPES = {
    "CHECKOUT",
    "RETURN",
    "EXHAUSTED",
    "AVAILABLE",
    "SERVER_UP",
    "SERVER_DOWN",
    "VENDOR_DOWN",
}


# ============================================================================
# Pydantic Response Models
# ============================================================================

class LicenseServerSummary(BaseModel):
    server_id: str
    hostname: str
    display_name: str
    status: str
    last_successful_poll: Optional[str] = None
    last_poll_attempt: Optional[str] = None
    last_error_code: Optional[int] = None
    last_error_message: Optional[str] = None
    data_state: str = "AVAILABLE"


class LicenseComponentDetail(BaseModel):
    feature_code: str
    product_name: Optional[str] = None
    product_family: Optional[str] = None
    year_version: Optional[str] = None
    catalog_status: str
    in_use: Optional[int] = None


class LicensePackageDetail(BaseModel):
    feature_code: str
    product_name: Optional[str] = None
    product_family: Optional[str] = None
    display_name: str
    total_issued: Optional[int] = None
    in_use: Optional[int] = None
    available: Optional[int] = None
    utilization_pct: Optional[float] = None
    status: str
    catalog_status: str
    description: Optional[str] = None
    components: List[LicenseComponentDetail] = Field(default_factory=list)


class LicenseServerDetail(BaseModel):
    server_id: str
    hostname: str
    display_name: str
    lmgrd_port: int
    vendor_daemon: str
    vendor_daemon_port: Optional[int] = None
    status: str
    last_successful_poll: Optional[str] = None
    last_poll_attempt: Optional[str] = None
    last_error_code: Optional[int] = None
    last_error_message: Optional[str] = None
    is_active: bool
    data_state: str
    packages: List[LicensePackageDetail] = Field(default_factory=list)


class ServerOverviewItem(BaseModel):
    server_id: str
    hostname: str
    display_name: str
    status: str
    last_successful_poll: Optional[str] = None
    last_poll_attempt: Optional[str] = None
    last_error_code: Optional[int] = None
    last_error_message: Optional[str] = None
    data_state: str
    products: List[LicensePackageDetail] = Field(default_factory=list)


class AggregateProductInventory(BaseModel):
    feature_code: str
    product_name: Optional[str] = None
    product_family: Optional[str] = None
    display_name: str
    total_inventory: int
    current_inventory_used: int
    inventory_available: int
    server_count: int


class EnvironmentInventory(BaseModel):
    notice: str = "Aggregate inventory across independent license servers. Licenses are not pooled."
    is_pooled: bool = False
    total_servers: int
    servers_up: int
    servers_down: int
    active_checkouts_total: int
    products: List[AggregateProductInventory] = Field(default_factory=list)


class LicenseOverviewResponse(BaseModel):
    servers: List[ServerOverviewItem] = Field(default_factory=list)
    environment_inventory: EnvironmentInventory


class ActiveConsumerItem(BaseModel):
    checkout_id: str
    server_id: str
    server_hostname: Optional[str] = None
    username: str
    machine_name: str
    product_name: Optional[str] = None
    package_feature: str
    component_feature: Optional[str] = None
    version: Optional[str] = None
    checkout_time: Optional[str] = None
    checkout_time_precision: Optional[str] = None
    is_borrowed: bool = False
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None


class LicenseEventItem(BaseModel):
    event_id: str
    server_id: str
    server_hostname: Optional[str] = None
    event_type: str
    feature_code: Optional[str] = None
    checkout_id: Optional[str] = None
    username: Optional[str] = None
    machine_name: Optional[str] = None
    selected_component_code: Optional[str] = None
    previous_in_use: Optional[int] = None
    new_in_use: Optional[int] = None
    total_issued: Optional[int] = None
    detected_at: str
    details: Optional[Dict[str, Any]] = None


class LicenseHistoryResponse(BaseModel):
    items: List[LicenseEventItem] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


# ============================================================================
# Helpers
# ============================================================================

def _compute_server_health(server_row: Dict[str, Any]) -> tuple[str, str]:
    """
    Computes effective server status and data state.
    Distinguishes UP, DOWN, VENDOR_DOWN, UNKNOWN, STALE.
    Returns (effective_status, data_state).
    """
    raw_status = (server_row.get("status") or "UNKNOWN").upper()
    last_poll = server_row.get("last_successful_poll")

    effective_status = raw_status

    # Check stale if status is UP
    if raw_status == "UP" and last_poll:
        try:
            poll_dt = datetime.fromisoformat(last_poll)
            age = (datetime.now(timezone.utc) - poll_dt).total_seconds()
            if age > STALE_THRESHOLD_SECONDS:
                effective_status = "STALE"
        except Exception:
            pass

    if effective_status == "UP":
        data_state = "AVAILABLE"
    else:
        data_state = "UNAVAILABLE"

    return effective_status, data_state


def _format_display_name(feature_code: str, product_name: Optional[str], product_family: Optional[str]) -> str:
    """Provides user-friendly display name without inventing commercial names."""
    if product_name:
        return product_name
    if product_family:
        return f"Unknown Moldflow Product ({product_family})"
    return f"Unknown Feature ({feature_code})"


# ============================================================================
# API Endpoints
# ============================================================================

@router.get(
    "/overview",
    response_model=LicenseOverviewResponse,
    status_code=status.HTTP_200_OK,
)
def get_license_overview(
    user: dict[str, Any] = Depends(get_mobile_user),
) -> LicenseOverviewResponse:
    """
    Dashboard overview returning:
    1. Per-server operational state with package status.
    2. Aggregate environment inventory across independent servers (explicitly non-pooled).
    """
    with get_db() as conn:
        # 1. Fetch all active servers
        server_rows = db_execute(
            conn,
            """
            SELECT * FROM license_servers
            ORDER BY display_name ASC, hostname ASC
            """
        ).fetchall()

        # 2. Fetch all feature catalog entries
        cat_rows = db_execute(
            conn,
            """
            SELECT * FROM license_feature_catalog
            """
        ).fetchall()
        catalog_map = {r["feature_code"]: dict(r) for r in cat_rows}

        # 3. Fetch all current server features
        feat_rows = db_execute(
            conn,
            """
            SELECT * FROM license_server_features
            """
        ).fetchall()
        # Key: (server_id, feature_code)
        server_feat_map: Dict[tuple[str, str], Dict[str, Any]] = {
            (r["server_id"], r["feature_code"]): dict(r) for r in feat_rows
        }

        # 4. Fetch total active checkouts count across all servers
        active_co_count_row = db_execute(
            conn,
            "SELECT COUNT(*) as cnt FROM license_active_checkouts"
        ).fetchone()
        active_checkouts_total = active_co_count_row["cnt"] if active_co_count_row else 0

        servers_overview: List[ServerOverviewItem] = []
        servers_up = 0
        servers_down = 0

        # Aggregate collectors across healthy servers
        # Key: feature_code -> {total, used, avail, server_count}
        agg_products: Dict[str, Dict[str, Any]] = {}

        for s_row in server_rows:
            s_dict = dict(s_row)
            server_id = s_dict["server_id"]
            eff_status, data_state = _compute_server_health(s_dict)

            if eff_status == "UP":
                servers_up += 1
            else:
                servers_down += 1

            # Build package products for this server
            server_packages: List[LicensePackageDetail] = []

            # Find all PACKAGE features present on this server
            for (f_srv_id, f_code), sf in server_feat_map.items():
                if f_srv_id != server_id:
                    continue

                cat_info = catalog_map.get(f_code, {})
                f_type = cat_info.get("feature_type", "UNKNOWN")
                if f_type != "PACKAGE":
                    continue

                prod_name = cat_info.get("product_name")
                prod_fam = cat_info.get("product_family")
                disp_name = _format_display_name(f_code, prod_name, prod_fam)
                cat_status = cat_info.get("catalog_status", "UNKNOWN")
                desc = cat_info.get("description")

                if data_state == "AVAILABLE":
                    t_issued = sf.get("total_issued")
                    in_use = sf.get("in_use")
                    avail = sf.get("available")
                    util_pct = sf.get("utilization_pct")
                    pkg_status = "EXHAUSTED" if (avail is not None and avail == 0) else "AVAILABLE"

                    # Add to environment aggregate
                    if f_code not in agg_products:
                        agg_products[f_code] = {
                            "feature_code": f_code,
                            "product_name": prod_name,
                            "product_family": prod_fam,
                            "display_name": disp_name,
                            "total_inventory": 0,
                            "current_inventory_used": 0,
                            "inventory_available": 0,
                            "server_count": 0,
                        }
                    agg_products[f_code]["total_inventory"] += (t_issued or 0)
                    agg_products[f_code]["current_inventory_used"] += (in_use or 0)
                    agg_products[f_code]["inventory_available"] += (avail or 0)
                    agg_products[f_code]["server_count"] += 1
                else:
                    # For non-UP server: in_use, available, util_pct are null; status=UNAVAILABLE
                    t_issued = None
                    in_use = None
                    avail = None
                    util_pct = None
                    pkg_status = "UNAVAILABLE"

                server_packages.append(
                    LicensePackageDetail(
                        feature_code=f_code,
                        product_name=prod_name,
                        product_family=prod_fam,
                        display_name=disp_name,
                        total_issued=t_issued,
                        in_use=in_use,
                        available=avail,
                        utilization_pct=util_pct,
                        status=pkg_status,
                        catalog_status=cat_status,
                        description=desc,
                        components=[],
                    )
                )

            # Sort packages deterministically
            server_packages.sort(key=lambda p: p.feature_code)

            servers_overview.append(
                ServerOverviewItem(
                    server_id=server_id,
                    hostname=s_dict["hostname"],
                    display_name=s_dict["display_name"],
                    status=eff_status,
                    last_successful_poll=s_dict.get("last_successful_poll"),
                    last_poll_attempt=s_dict.get("last_poll_attempt"),
                    last_error_code=s_dict.get("last_error_code"),
                    last_error_message=s_dict.get("last_error_message"),
                    data_state=data_state,
                    products=server_packages,
                )
            )

        env_prod_list = [
            AggregateProductInventory(**item)
            for item in sorted(agg_products.values(), key=lambda x: x["feature_code"])
        ]

        env_inventory = EnvironmentInventory(
            notice="Aggregate inventory across independent license servers. Licenses are not pooled.",
            is_pooled=False,
            total_servers=len(server_rows),
            servers_up=servers_up,
            servers_down=servers_down,
            active_checkouts_total=active_checkouts_total,
            products=env_prod_list,
        )

        return LicenseOverviewResponse(
            servers=servers_overview,
            environment_inventory=env_inventory,
        )


@router.get(
    "/servers",
    response_model=List[LicenseServerSummary],
    status_code=status.HTTP_200_OK,
)
def get_license_servers(
    user: dict[str, Any] = Depends(get_mobile_user),
) -> List[LicenseServerSummary]:
    """
    Concise authenticated list of configured license servers.
    Omit internal implementation mechanics (e.g. paths, encryption, SIGN).
    """
    with get_db() as conn:
        rows = db_execute(
            conn,
            """
            SELECT
                server_id,
                hostname,
                display_name,
                status,
                last_successful_poll,
                last_poll_attempt,
                last_error_code,
                last_error_message
            FROM license_servers
            ORDER BY display_name ASC, hostname ASC
            """
        ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            eff_status, data_state = _compute_server_health(d)
            result.append(
                LicenseServerSummary(
                    server_id=d["server_id"],
                    hostname=d["hostname"],
                    display_name=d["display_name"],
                    status=eff_status,
                    last_successful_poll=d.get("last_successful_poll"),
                    last_poll_attempt=d.get("last_poll_attempt"),
                    last_error_code=d.get("last_error_code"),
                    last_error_message=d.get("last_error_message"),
                    data_state=data_state,
                )
            )
        return result


@router.get(
    "/servers/{server_id}",
    response_model=LicenseServerDetail,
    status_code=status.HTTP_200_OK,
)
def get_license_server_detail(
    server_id: str,
    user: dict[str, Any] = Depends(get_mobile_user),
) -> LicenseServerDetail:
    """
    Detailed current operational state for one license server.
    PACKAGE features are returned with nested COMPONENT features.
    Components are never treated as an independent pool.
    """
    with get_db() as conn:
        s_row = db_execute(
            conn,
            "SELECT * FROM license_servers WHERE server_id = ?",
            (server_id,)
        ).fetchone()

        if s_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"License server '{server_id}' not found",
            )

        s_dict = dict(s_row)
        eff_status, data_state = _compute_server_health(s_dict)

        # Fetch all features for this server
        sf_rows = db_execute(
            conn,
            "SELECT * FROM license_server_features WHERE server_id = ?",
            (server_id,)
        ).fetchall()
        server_feats = {r["feature_code"]: dict(r) for r in sf_rows}

        # Fetch feature catalog
        cat_rows = db_execute(conn, "SELECT * FROM license_feature_catalog").fetchall()
        cat_map = {r["feature_code"]: dict(r) for r in cat_rows}

        # Group components by parent_package_code
        components_by_parent: Dict[str, List[LicenseComponentDetail]] = {}
        packages_list: List[LicensePackageDetail] = []

        for f_code, sf in server_feats.items():
            c_info = cat_map.get(f_code, {})
            f_type = c_info.get("feature_type", "UNKNOWN")
            parent_pkg = c_info.get("parent_package_code")

            if f_type == "COMPONENT" and parent_pkg:
                comp = LicenseComponentDetail(
                    feature_code=f_code,
                    product_name=c_info.get("product_name"),
                    product_family=c_info.get("product_family"),
                    year_version=c_info.get("year_version"),
                    catalog_status=c_info.get("catalog_status", "UNKNOWN"),
                    in_use=sf.get("in_use") if data_state == "AVAILABLE" else None,
                )
                components_by_parent.setdefault(parent_pkg, []).append(comp)

        for f_code, sf in server_feats.items():
            c_info = cat_map.get(f_code, {})
            f_type = c_info.get("feature_type", "UNKNOWN")

            if f_type == "PACKAGE":
                prod_name = c_info.get("product_name")
                prod_fam = c_info.get("product_family")
                disp_name = _format_display_name(f_code, prod_name, prod_fam)

                if data_state == "AVAILABLE":
                    t_issued = sf.get("total_issued")
                    in_use = sf.get("in_use")
                    avail = sf.get("available")
                    util_pct = sf.get("utilization_pct")
                    pkg_status = "EXHAUSTED" if (avail is not None and avail == 0) else "AVAILABLE"
                else:
                    t_issued = None
                    in_use = None
                    avail = None
                    util_pct = None
                    pkg_status = "UNAVAILABLE"

                # Sort components by year_version descending
                comps = components_by_parent.get(f_code, [])
                comps.sort(key=lambda x: str(x.year_version or ""), reverse=True)

                packages_list.append(
                    LicensePackageDetail(
                        feature_code=f_code,
                        product_name=prod_name,
                        product_family=prod_fam,
                        display_name=disp_name,
                        total_issued=t_issued,
                        in_use=in_use,
                        available=avail,
                        utilization_pct=util_pct,
                        status=pkg_status,
                        catalog_status=c_info.get("catalog_status", "UNKNOWN"),
                        description=c_info.get("description"),
                        components=comps,
                    )
                )

        packages_list.sort(key=lambda p: p.feature_code)

        return LicenseServerDetail(
            server_id=server_id,
            hostname=s_dict["hostname"],
            display_name=s_dict["display_name"],
            lmgrd_port=s_dict.get("lmgrd_port", 27000),
            vendor_daemon=s_dict.get("vendor_daemon", "adskflex"),
            vendor_daemon_port=s_dict.get("vendor_daemon_port"),
            status=eff_status,
            last_successful_poll=s_dict.get("last_successful_poll"),
            last_poll_attempt=s_dict.get("last_poll_attempt"),
            last_error_code=s_dict.get("last_error_code"),
            last_error_message=s_dict.get("last_error_message"),
            is_active=bool(s_dict.get("is_active", 1)),
            data_state=data_state,
            packages=packages_list,
        )


@router.get(
    "/servers/{server_id}/consumers",
    response_model=List[ActiveConsumerItem],
    status_code=status.HTTP_200_OK,
)
def get_license_server_consumers(
    server_id: str,
    user: dict[str, Any] = Depends(get_mobile_user),
) -> List[ActiveConsumerItem]:
    """
    Returns active physical checkouts for a server.
    Unified physical checkout representation (PACKAGE + COMPONENT consolidated).
    Omit PID, server handle, and internal diagnostic info.
    Ordered deterministically by checkout_time DESC, username ASC, machine_name ASC.
    """
    with get_db() as conn:
        # Verify server exists
        s_row = db_execute(
            conn,
            "SELECT server_id, hostname FROM license_servers WHERE server_id = ?",
            (server_id,)
        ).fetchone()

        if s_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"License server '{server_id}' not found",
            )

        server_hostname = s_row["hostname"]

        # Fetch checkouts with product name joined from catalog
        rows = db_execute(
            conn,
            """
            SELECT
                c.checkout_id,
                c.server_id,
                c.username,
                c.machine_name,
                c.package_feature_code,
                c.selected_component_code,
                c.version,
                c.checkout_time,
                c.checkout_time_precision,
                c.is_borrowed,
                c.first_seen_at,
                c.last_seen_at,
                cat.product_name
            FROM license_active_checkouts c
            LEFT JOIN license_feature_catalog cat ON c.package_feature_code = cat.feature_code
            WHERE c.server_id = ?
            ORDER BY c.checkout_time DESC, c.username ASC, c.machine_name ASC
            """,
            (server_id,)
        ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            result.append(
                ActiveConsumerItem(
                    checkout_id=d["checkout_id"],
                    server_id=d["server_id"],
                    server_hostname=server_hostname,
                    username=d["username"],
                    machine_name=d["machine_name"],
                    product_name=d.get("product_name"),
                    package_feature=d["package_feature_code"],
                    component_feature=d.get("selected_component_code"),
                    version=d.get("version"),
                    checkout_time=d.get("checkout_time"),
                    checkout_time_precision=d.get("checkout_time_precision"),
                    is_borrowed=bool(d.get("is_borrowed", False)),
                    first_seen_at=d.get("first_seen_at"),
                    last_seen_at=d.get("last_seen_at"),
                )
            )

        return result


@router.get(
    "/history",
    response_model=LicenseHistoryResponse,
    status_code=status.HTTP_200_OK,
)
def get_license_history(
    server_id: Optional[str] = Query(None, description="Filter by license server_id"),
    feature_code: Optional[str] = Query(None, description="Filter by package or component feature code"),
    event_type: Optional[str] = Query(None, description="Filter by event type (CHECKOUT, RETURN, etc.)"),
    username: Optional[str] = Query(None, description="Filter by consumer username"),
    limit: int = Query(50, ge=1, le=200, description="Page limit (1-200)"),
    offset: int = Query(0, ge=0, description="Page offset"),
    user: dict[str, Any] = Depends(get_mobile_user),
) -> LicenseHistoryResponse:
    """
    Paginated license history reading directly from license_events.
    Uses indexed database queries and does NOT reconstruct from snapshots.
    """
    if event_type:
        event_type_clean = event_type.strip().upper()
        if event_type_clean not in VALID_EVENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid event_type '{event_type}'. Must be one of: {', '.join(sorted(VALID_EVENT_TYPES))}",
            )
        event_type = event_type_clean

    with get_db() as conn:
        conditions = []
        params = []

        if server_id:
            conditions.append("e.server_id = ?")
            params.append(server_id)

        if feature_code:
            conditions.append("e.feature_code = ?")
            params.append(feature_code.strip())

        if event_type:
            conditions.append("e.event_type = ?")
            params.append(event_type)

        if username:
            conditions.append("LOWER(e.username) = LOWER(?)")
            params.append(username.strip())

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # 1. Total count
        count_sql = f"SELECT COUNT(*) as total FROM license_events e {where_clause}"
        total_row = db_execute(conn, count_sql, tuple(params)).fetchone()
        total = total_row["total"] if total_row else 0

        # 2. Paginated rows with server hostname joined
        items_sql = f"""
            SELECT
                e.event_id,
                e.server_id,
                s.hostname as server_hostname,
                e.event_type,
                e.feature_code,
                e.checkout_id,
                e.username,
                e.machine_name,
                e.selected_component_code,
                e.previous_in_use,
                e.new_in_use,
                e.total_issued,
                e.detected_at,
                e.details
            FROM license_events e
            LEFT JOIN license_servers s ON e.server_id = s.server_id
            {where_clause}
            ORDER BY e.detected_at DESC, e.created_at DESC
            LIMIT ? OFFSET ?
        """
        query_params = list(params) + [limit, offset]
        rows = db_execute(conn, items_sql, tuple(query_params)).fetchall()

        items: List[LicenseEventItem] = []
        for r in rows:
            d = dict(r)
            details_val = d.get("details")
            if isinstance(details_val, str):
                try:
                    details_val = json.loads(details_val)
                except Exception:
                    details_val = {"raw": details_val}

            items.append(
                LicenseEventItem(
                    event_id=d["event_id"],
                    server_id=d["server_id"],
                    server_hostname=d.get("server_hostname"),
                    event_type=d["event_type"],
                    feature_code=d.get("feature_code"),
                    checkout_id=d.get("checkout_id"),
                    username=d.get("username"),
                    machine_name=d.get("machine_name"),
                    selected_component_code=d.get("selected_component_code"),
                    previous_in_use=d.get("previous_in_use"),
                    new_in_use=d.get("new_in_use"),
                    total_issued=d.get("total_issued"),
                    detected_at=d["detected_at"],
                    details=details_val,
                )
            )

        return LicenseHistoryResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )
