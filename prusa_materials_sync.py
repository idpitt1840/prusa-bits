#!/usr/bin/env python3
"""
Prusa Connect Materials & Spool Usage Export / Import Tool.

Zero third-party dependencies — uses ONLY the Python 3 standard library
(urllib, sqlite3, json, ssl, http.cookiejar, argparse, pathlib, uuid, math).

Supports:
  1. Direct SQLite export/import (`--sqlite-db /path/to/database.sqlite`):
     - Reads/writes the exact `spools.consumed_weight_g` column (fed by printer
       tool odometers and OpenPrintTag NFC tags), NFC spool UUIDs, `source`,
       every user's `custom_materials`, `locations`, `assignments`, and
       `favourites`.
     - Export opens SQLite read-only (`mode=ro`), safe while Prusa Connect runs.
  2. Pure HTTP REST API export/import (`--url http://host:8080`):
     - Authenticates via `--token` (Bearer `pcl_...`), `--username`/`--password`
       (`POST /api/v1/auth/login`), or open-network mode.
     - Reconstructs `consumed_weight_g` over HTTP from:
         a) `(full_weight_g - empty_weight_g) - remaining_weight_g`,
         b) OpenPrintTag catalogue `package.net_weight_g - remaining_weight_g`,
         c) Core History `com.github.aux.prusa3d.materials.spool` events.
     - Restores locations, custom materials (with UUID remapping and orphan
       material recovery), active + archived spools (2-step POST + PATCH so
       archived spools do not count against `max_spools`), and favourites.
  3. Hybrid mode (`--url ... --sqlite-db ...`):
     - Combines live HTTP API state with exact SQLite `consumed_weight_g` and
       multi-user custom materials.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import getpass
import http.cookiejar
import json
import math
import os
import pathlib
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

SCHEMA_VERSION = "prusa-connect-materials-export/v1"
MODULE_NAMESPACE = "com.github.aux.prusa3d.materials"
MODULE_PREFIX = f"/api/v1/modules/{MODULE_NAMESPACE}"
SPOOL_EVENT_TYPE = f"{MODULE_NAMESPACE}.spool"
MAX_WEIGHT_G = 100_000.0


# ---------------------------------------------------------------------------
# Helpers: timestamps, colour family (1:1 port of internal/colour/family.go)
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def round_g(val: float | None) -> float | None:
    if val is None:
        return None
    return round(float(val), 3)


def split_csv(val: str | None) -> list[str]:
    if not val:
        return []
    return [part for part in val.split(",") if part]


def join_csv(items: list[str] | None) -> str:
    if not items:
        return ""
    return ",".join(items)


def color_family(rgba: str | None) -> str:
    """Exact port of github.com/prusa3d/materials/internal/colour.Family."""
    if not rgba or len(rgba) not in (7, 9) or rgba[0] != "#":
        return ""
    try:
        v = int(rgba[1:], 16)
    except ValueError:
        return ""

    alpha = 0xFF
    if len(rgba) == 9:
        alpha, v = v & 0xFF, v >> 8
    if alpha < 0x80:
        return "natural"

    r = ((v >> 16) & 0xFF) / 255.0
    g = ((v >> 8) & 0xFF) / 255.0
    b = (v & 0xFF) / 255.0
    hi = max(r, g, b)
    lo = min(r, g, b)
    lightness = (hi + lo) / 2.0
    delta = hi - lo

    if lightness < 0.15:
        return "black"
    if lightness > 0.90:
        return "white"
    denom = 1.0 - abs(2.0 * lightness - 1.0)
    if delta == 0.0 or (denom > 0 and (delta / denom) < 0.15):
        return "grey"

    if hi == r:
        hue = math.fmod((g - b) / delta, 6.0)
    elif hi == g:
        hue = ((b - r) / delta) + 2.0
    else:
        hue = ((r - g) / delta) + 4.0

    hue *= 60.0
    if hue < 0.0:
        hue += 360.0

    if hue < 10.0 or hue >= 345.0:
        return "red"
    if hue < 40.0 and lightness < 0.45:
        return "brown"
    if hue < 40.0:
        return "orange"
    if hue < 70.0:
        return "yellow"
    if hue < 170.0:
        return "green"
    if hue < 255.0:
        return "blue"
    if hue < 315.0:
        return "purple"
    return "pink"


# ---------------------------------------------------------------------------
# HTTP Client (Python stdlib urllib + CookieJar)
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        method: str,
        path: str,
        code: str = "",
        detail: str = "",
        body: Any = None,
    ) -> None:
        self.status = status
        self.method = method
        self.path = path
        self.code = code
        self.detail = detail
        self.body = body
        msg = f"HTTP {status} on {method} {path}"
        if code:
            msg += f" [{code}]"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class ConnectHttpClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        insecure: bool = False,
        ca_file: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.cookie_jar = http.cookiejar.CookieJar()

        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif ca_file:
            ctx.load_verify_locations(cafile=ca_file)

        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
        )

    def login_password(self, username: str, password: str) -> None:
        self.request(
            "POST",
            "/api/v1/auth/login",
            body={"username": username, "password": password},
        )

    def request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            filtered = {
                k: ("true" if v is True else "false" if v is False else v)
                for k, v in query.items()
                if v is not None
            }
            if filtered:
                url = f"{url}?{urllib.parse.urlencode(filtered, doseq=True)}"

        headers: dict[str, str] = {
            "Accept": "application/json, application/problem+json",
            "Origin": self.base_url,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        raw_body: bytes | None = None
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
                if not payload or resp.status == 204:
                    return None
                return json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_bytes = exc.read()
            parsed: Any = None
            code = ""
            detail = exc.reason or ""
            if err_bytes:
                try:
                    parsed = json.loads(err_bytes.decode("utf-8", errors="replace"))
                    if isinstance(parsed, dict):
                        code = str(parsed.get("code") or "")
                        detail = str(
                            parsed.get("detail")
                            or parsed.get("title")
                            or detail
                        )
                except ValueError:
                    detail = err_bytes.decode("utf-8", errors="replace")[:300]
            raise ApiError(exc.code, method, path, code=code, detail=detail, body=parsed) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Connection failed for {method} {url}: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Consumed Weight Calculation & Rollups
# ---------------------------------------------------------------------------


def compute_spool_consumption(
    spool: dict[str, Any],
    package_net_weights: dict[str, float] | None = None,
    history_drops_by_id: dict[int, float] | None = None,
    sqlite_consumed_by_uuid: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Populate `initial_net_weight_g`, `odometer_consumed_weight_g`,
    `consumed_weight_g`, and `consumed_weight_source` on a spool dict.
    """
    package_net_weights = package_net_weights or {}
    history_drops_by_id = history_drops_by_id or {}
    sqlite_consumed_by_uuid = sqlite_consumed_by_uuid or {}

    remaining = float(spool.get("remaining_weight_g") or 0.0)
    full_w = spool.get("full_weight_g")
    empty_w = spool.get("empty_weight_g")
    pkg_uuid = spool.get("package_uuid")
    spool_uuid = str(spool.get("uuid") or "")
    spool_id = spool.get("id")

    # 1. Exact odometer / NFC tag consumed weight from SQLite if present
    odom_g: float | None = None
    if "odometer_consumed_weight_g" in spool and spool["odometer_consumed_weight_g"] is not None:
        odom_g = float(spool["odometer_consumed_weight_g"])
    elif spool_uuid and spool_uuid in sqlite_consumed_by_uuid:
        odom_g = float(sqlite_consumed_by_uuid[spool_uuid])

    # 2. Initial net filament weight (full - empty, or catalogue package net_weight_g)
    initial_net_g: float | None = None
    weight_delta_source = ""
    if full_w is not None:
        empty_val = float(empty_w) if empty_w is not None else 0.0
        net_candidate = float(full_w) - empty_val
        if net_candidate >= 0.0:
            initial_net_g = net_candidate
            weight_delta_source = "spool_weights"
    elif pkg_uuid and pkg_uuid in package_net_weights:
        initial_net_g = float(package_net_weights[pkg_uuid])
        weight_delta_source = "catalogue_package"

    delta_consumed_g: float | None = None
    if initial_net_g is not None:
        delta_consumed_g = max(0.0, initial_net_g - remaining)

    # 3. Core History `com.github.aux.prusa3d.materials.spool` event drops
    hist_g: float | None = None
    if isinstance(spool_id, int) and spool_id in history_drops_by_id:
        hist_g = float(history_drops_by_id[spool_id])

    # Choose best consumed_weight_g and record provenance
    consumed_g: float = 0.0
    source: str = "unknown_no_initial_weight"

    if odom_g is not None and delta_consumed_g is not None:
        if delta_consumed_g > odom_g + 0.001:
            consumed_g = delta_consumed_g
            source = f"sqlite_consumed_and_{weight_delta_source}"
        else:
            consumed_g = odom_g
            source = "sqlite_consumed_weight_g"
    elif odom_g is not None:
        consumed_g = odom_g
        source = "sqlite_consumed_weight_g"
        if initial_net_g is None and consumed_g > 0:
            initial_net_g = remaining + consumed_g
    elif delta_consumed_g is not None:
        if hist_g is not None and hist_g > delta_consumed_g + 0.001:
            consumed_g = hist_g
            source = "core_history_events"
        else:
            consumed_g = delta_consumed_g
            source = weight_delta_source
    elif hist_g is not None:
        consumed_g = hist_g
        source = "core_history_events"
        initial_net_g = remaining + consumed_g

    spool["initial_net_weight_g"] = round_g(initial_net_g)
    spool["odometer_consumed_weight_g"] = round_g(odom_g)
    spool["consumed_weight_g"] = round_g(consumed_g) or 0.0
    spool["consumed_weight_source"] = source
    return spool


def build_usage_rollup(spools: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Aggregate usage per material and fleet-wide across active + archived spools."""
    by_mat: dict[str, dict[str, Any]] = {}

    total_active = 0
    total_archived = 0
    total_remaining = 0.0
    total_consumed = 0.0
    total_odom_consumed = 0.0

    for sp in spools:
        mat_uuid = str(sp.get("material_uuid") or "unknown")
        is_archived = sp.get("archived_at") is not None
        rem = float(sp.get("remaining_weight_g") or 0.0)
        cons = float(sp.get("consumed_weight_g") or 0.0)
        odom = float(sp.get("odometer_consumed_weight_g") or 0.0)
        init_net = sp.get("initial_net_weight_g")

        entry = by_mat.get(mat_uuid)
        if entry is None:
            entry = {
                "material_uuid": mat_uuid,
                "material_source": sp.get("material_source") or "",
                "material_name": sp.get("material_name") or "",
                "brand_name": sp.get("brand_name") or "",
                "class": sp.get("class") or "",
                "type": sp.get("type") or "",
                "color_rgba": sp.get("color_rgba") or "",
                "active_spools": 0,
                "archived_spools": 0,
                "total_spools": 0,
                "active_remaining_weight_g": 0.0,
                "archived_remaining_weight_g": 0.0,
                "known_initial_net_weight_g": 0.0,
                "consumed_weight_g": 0.0,
                "odometer_consumed_weight_g": 0.0,
            }
            by_mat[mat_uuid] = entry

        entry["total_spools"] += 1
        if is_archived:
            entry["archived_spools"] += 1
            entry["archived_remaining_weight_g"] += rem
            total_archived += 1
        else:
            entry["active_spools"] += 1
            entry["active_remaining_weight_g"] += rem
            total_active += 1
            total_remaining += rem

        if init_net is not None:
            entry["known_initial_net_weight_g"] += float(init_net)
        entry["consumed_weight_g"] += cons
        entry["odometer_consumed_weight_g"] += odom

        total_consumed += cons
        total_odom_consumed += odom

    rollup_rows = sorted(
        by_mat.values(),
        key=lambda r: (-r["consumed_weight_g"], r["brand_name"].lower(), r["material_name"].lower()),
    )
    for row in rollup_rows:
        for k in (
            "active_remaining_weight_g",
            "archived_remaining_weight_g",
            "known_initial_net_weight_g",
            "consumed_weight_g",
            "odometer_consumed_weight_g",
        ):
            row[k] = round_g(row[k]) or 0.0

    summary = {
        "distinct_materials_with_spools": len(rollup_rows),
        "active_spools": total_active,
        "archived_spools": total_archived,
        "total_spools": total_active + total_archived,
        "active_remaining_weight_g": round_g(total_remaining) or 0.0,
        "total_consumed_weight_g": round_g(total_consumed) or 0.0,
        "total_odometer_consumed_weight_g": round_g(total_odom_consumed) or 0.0,
    }
    return summary, rollup_rows


# ---------------------------------------------------------------------------
# Direct SQLite Export & Import
# ---------------------------------------------------------------------------


def open_sqlite_ro(db_path: pathlib.Path) -> sqlite3.Connection:
    resolved = db_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite database file not found: {resolved}")
    uri = f"{resolved.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def open_sqlite_rw(db_path: pathlib.Path) -> sqlite3.Connection:
    resolved = db_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"Target SQLite database file not found: {resolved} "
            "(install/start the Materials extension once first so it creates its schema)"
        )
    conn = sqlite3.connect(str(resolved), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def export_from_sqlite(db_path: pathlib.Path) -> dict[str, Any]:
    with open_sqlite_ro(db_path) as conn:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"spools", "custom_materials", "locations"}
        missing = required - tables
        if missing:
            raise RuntimeError(
                f"{db_path} does not look like a Prusa Connect Materials database "
                f"(missing tables: {', '.join(sorted(missing))})"
            )

        # Catalogue metadata
        cat_row = conn.execute("SELECT * FROM catalogue_meta WHERE id = 1").fetchone()
        catalogue: dict[str, Any] = {"loaded": cat_row is not None}
        if cat_row is not None:
            catalogue["meta"] = {
                "data_hash": cat_row["data_hash"],
                "last_modified": cat_row["last_modified"],
                "brands": cat_row["brands"],
                "materials": cat_row["materials"],
                "packages": cat_row["packages"],
                "containers": cat_row["containers"],
                "skipped": cat_row["skipped"],
                "imported_at": cat_row["imported_at"],
            }

        # Package net weights from catalogue table if loaded
        pkg_weights: dict[str, float] = {}
        if "packages" in tables:
            for r in conn.execute(
                "SELECT uuid, net_weight_g FROM packages WHERE net_weight_g IS NOT NULL"
            ):
                pkg_weights[r["uuid"]] = float(r["net_weight_g"])

        # Locations
        locations = []
        for r in conn.execute(
            """
            SELECT l.id, l.name, l.created_at, l.updated_at,
                   COUNT(s.id) AS spools
            FROM locations l
            LEFT JOIN spools s
              ON s.location_id = l.id AND s.deleted_at IS NULL AND s.archived_at IS NULL
            GROUP BY l.id
            ORDER BY lower(l.name), l.id
            """
        ):
            locations.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "spools": r["spools"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )

        # Custom materials (ALL owners!)
        custom_materials = []
        for r in conn.execute("SELECT * FROM custom_materials ORDER BY created_at, uuid"):
            custom_materials.append(
                {
                    "uuid": r["uuid"],
                    "owner": r["owner"],
                    "name": r["name"],
                    "brand_name": r["brand_name"],
                    "class": r["class"],
                    "type": r["type"],
                    "abbreviation": r["abbreviation"],
                    "color_rgba": r["color_rgba"],
                    "diameter_um": r["diameter_um"],
                    "density": r["density"],
                    "min_print_temp": r["min_print_temp"],
                    "max_print_temp": r["max_print_temp"],
                    "preheat_temp": r["preheat_temp"],
                    "min_bed_temp": r["min_bed_temp"],
                    "max_bed_temp": r["max_bed_temp"],
                    "chamber_temp": r["chamber_temp"],
                    "drying_temp": r["drying_temp"],
                    "drying_time_min": r["drying_time_min"],
                    "min_chamber_temp": r["min_chamber_temp"],
                    "max_chamber_temp": r["max_chamber_temp"],
                    "min_nozzle_diameter": r["min_nozzle_diameter"],
                    "transmission_distance": r["transmission_distance"],
                    "hardness_shore_a": r["hardness_shore_a"],
                    "hardness_shore_d": r["hardness_shore_d"],
                    "ral_code": r["ral_code"],
                    "secondary_colors": split_csv(r["secondary_colors"]),
                    "certifications": split_csv(r["certifications"]),
                    "tags": split_csv(r["tags"]),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )

        # Favourites & list filters
        favourites = []
        if "favourites" in tables:
            for r in conn.execute(
                "SELECT principal, material_uuid, starred_at FROM favourites ORDER BY principal, starred_at"
            ):
                favourites.append(
                    {
                        "principal": r["principal"],
                        "material_uuid": r["material_uuid"],
                        "starred_at": r["starred_at"],
                    }
                )

        list_filters: list[dict[str, Any]] = []
        if "list_filters" in tables:
            for r in conn.execute("SELECT principal, filters, updated_at FROM list_filters"):
                try:
                    parsed_f = json.loads(r["filters"])
                except ValueError:
                    parsed_f = {}
                list_filters.append(
                    {
                        "principal": r["principal"],
                        "filters": parsed_f,
                        "updated_at": r["updated_at"],
                    }
                )

        # Spools (non-deleted: both active and archived)
        spool_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(spools)")
        }
        has_assignments = "assignments" in tables
        has_consumed = "consumed_weight_g" in spool_cols

        query = f"""
            SELECT s.*,
                   COALESCE(l.name, '') AS location_name,
                   {"a.device_id AS tool_device_id, a.tool_id AS tool_slot_id" if has_assignments else "NULL AS tool_device_id, NULL AS tool_slot_id"}
            FROM spools s
            LEFT JOIN locations l ON l.id = s.location_id
            {"LEFT JOIN assignments a ON a.spool_id = s.id" if has_assignments else ""}
            WHERE s.deleted_at IS NULL
            ORDER BY s.id ASC
        """
        spools: list[dict[str, Any]] = []
        for r in conn.execute(query):
            keys = r.keys()
            tool_obj = None
            if r["tool_device_id"] is not None:
                tool_obj = {
                    "device_id": r["tool_device_id"],
                    "tool_id": r["tool_slot_id"],
                    "gone": False,
                }
            sp: dict[str, Any] = {
                "id": r["id"],
                "uuid": r["uuid"] if "uuid" in keys else "",
                "source": r["source"] if "source" in keys else "manual",
                "material_uuid": r["material_uuid"],
                "material_source": r["material_source"],
                "material_name": r["material_name"],
                "brand_name": r["brand_name"],
                "class": r["class"],
                "type": r["type"],
                "color_rgba": r["color_rgba"],
                "density": r["density"],
                "diameter_um": r["diameter_um"],
                "package_uuid": r["package_uuid"],
                "full_weight_g": r["full_weight_g"],
                "empty_weight_g": r["empty_weight_g"],
                "length_mm": r["length_mm"],
                "remaining_weight_g": float(r["remaining_weight_g"]),
                "odometer_consumed_weight_g": float(r["consumed_weight_g"]) if has_consumed else 0.0,
                "condition": r["condition"] if "condition" in keys else "new",
                "location_id": r["location_id"],
                "location": r["location_name"],
                "notes": r["notes"],
                "created_by": r["created_by"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "archived_at": r["archived_at"] if "archived_at" in keys else None,
                "archive_reason": (r["archive_reason"] or "") if "archive_reason" in keys else "",
                "archive_reason_note": (r["archive_reason_note"] or "") if "archive_reason_note" in keys else "",
                "tool": tool_obj,
            }
            compute_spool_consumption(sp, package_net_weights=pkg_weights)
            spools.append(sp)

        # Tool assignments (with odometer state)
        assignments: list[dict[str, Any]] = []
        if has_assignments:
            for r in conn.execute(
                """
                SELECT a.*, s.uuid AS spool_uuid
                FROM assignments a
                LEFT JOIN spools s ON s.id = a.spool_id
                ORDER BY a.device_id, a.tool_id
                """
            ):
                reported_obj = None
                if r["reported"]:
                    try:
                        reported_obj = json.loads(r["reported"])
                    except ValueError:
                        reported_obj = None
                assignments.append(
                    {
                        "device_id": r["device_id"],
                        "tool_id": r["tool_id"],
                        "material_uuid": r["material_uuid"],
                        "material_source": r["material_source"],
                        "material_name": r["material_name"],
                        "brand_name": r["brand_name"],
                        "type": r["type"],
                        "abbreviation": r["abbreviation"],
                        "color_rgba": r["color_rgba"],
                        "spool_id": r["spool_id"],
                        "spool_uuid": r["spool_uuid"],
                        "by": r["assigned_by"],
                        "reported": reported_obj,
                        "updated_at": r["updated_at"],
                        "odometer_e": r["odometer_e"],
                        "odometer_id": r["odometer_id"],
                    }
                )

    summary, usage_by_material = build_usage_rollup(spools)
    return {
        "schema": SCHEMA_VERSION,
        "exported_at": utc_now_iso(),
        "export_mode": "sqlite",
        "source": {"sqlite_db": str(db_path.expanduser().resolve())},
        "summary": summary,
        "usage_by_material": usage_by_material,
        "catalogue": catalogue,
        "locations": locations,
        "custom_materials": custom_materials,
        "spools": spools,
        "tool_assignments": assignments,
        "favourites": favourites,
        "list_filters": list_filters,
    }


# ---------------------------------------------------------------------------
# HTTP REST API Export
# ---------------------------------------------------------------------------


def fetch_paginated_offset(
    client: ConnectHttpClient,
    path: str,
    items_key: str,
    extra_query: dict[str, Any] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    offset = 0
    while True:
        q = dict(extra_query or {})
        q["limit"] = limit
        q["offset"] = offset
        page = client.request("GET", path, query=q)
        rows = page.get(items_key) or []
        results.extend(rows)
        next_offset = page.get("next")
        if next_offset is None or not rows:
            break
        offset = int(next_offset)
    return results


def fetch_spool_history_consumption(client: ConnectHttpClient) -> tuple[dict[int, float], bool]:
    """
    Query Core History API for `com.github.aux.prusa3d.materials.spool` events
    and sum weight decreases per `spool_id`. Gracefully handles 501 / 403.
    """
    cursor: str | None = None
    readings_by_spool: dict[int, list[float]] = {}
    while True:
        query: dict[str, Any] = {"types": SPOOL_EVENT_TYPE, "limit": 500}
        if cursor:
            query["cursor"] = cursor
        try:
            page = client.request("GET", "/api/v1/history/facts", query=query)
        except ApiError as exc:
            if exc.status in (403, 404, 501):
                return {}, False
            raise

        for item in page.get("items") or []:
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            sid = payload.get("spool_id")
            rem = payload.get("remaining_weight_g")
            if isinstance(sid, int) and isinstance(rem, (int, float)):
                readings_by_spool.setdefault(sid, []).append(float(rem))

        next_cursor = page.get("cursor")
        if not next_cursor or not (page.get("items") or []):
            break
        cursor = str(next_cursor)

    consumed_by_id: dict[int, float] = {}
    for sid, seq in readings_by_spool.items():
        drops = 0.0
        for prev_w, curr_w in zip(seq, seq[1:]):
            if prev_w > curr_w:
                drops += prev_w - curr_w
        if drops > 0.0:
            consumed_by_id[sid] = round(drops, 3)
    return consumed_by_id, True


def export_from_http(
    client: ConnectHttpClient,
    sqlite_db: pathlib.Path | None = None,
) -> dict[str, Any]:
    catalogue = client.request("GET", f"{MODULE_PREFIX}/catalogue")
    loc_resp = client.request("GET", f"{MODULE_PREFIX}/locations")
    locations = loc_resp.get("locations") or []

    # Custom materials visible to caller
    custom_rows = fetch_paginated_offset(
        client,
        f"{MODULE_PREFIX}/materials",
        "rows",
        extra_query={"custom": True},
    )
    custom_materials: list[dict[str, Any]] = []
    seen_custom_uuids: set[str] = set()
    for row in custom_rows:
        mat_uuid = row["uuid"]
        detail = client.request("GET", f"{MODULE_PREFIX}/materials/{mat_uuid}")
        custom_obj = detail.get("custom")
        if custom_obj:
            custom_materials.append(custom_obj)
            seen_custom_uuids.add(mat_uuid)

    # Favourites & list filters
    fav_rows = fetch_paginated_offset(
        client,
        f"{MODULE_PREFIX}/materials",
        "rows",
        extra_query={"favourites": True},
    )
    favourites = [{"material_uuid": r["uuid"]} for r in fav_rows]

    filters_resp = client.request("GET", f"{MODULE_PREFIX}/list-filters")
    list_filters = [{"filters": (filters_resp or {}).get("filters") or {}}]

    # All spools (active + archived)
    spools = fetch_paginated_offset(client, f"{MODULE_PREFIX}/spools", "spools")

    # Fetch package net_weight_g for any material whose spools reference a package_uuid
    package_net_weights: dict[str, float] = {}
    materials_needing_pkgs = {
        str(sp["material_uuid"])
        for sp in spools
        if sp.get("package_uuid") and sp.get("full_weight_g") is None
    }
    for mat_uuid in sorted(materials_needing_pkgs):
        try:
            detail = client.request("GET", f"{MODULE_PREFIX}/materials/{mat_uuid}")
            for pkg in detail.get("packages") or []:
                if pkg.get("uuid") and pkg.get("net_weight_g") is not None:
                    package_net_weights[pkg["uuid"]] = float(pkg["net_weight_g"])
        except ApiError as exc:
            if exc.status != 404:
                raise

    # Query Core History spool events (if history provider is configured)
    history_drops_by_id, history_available = fetch_spool_history_consumption(client)

    # Optional hybrid SQLite enrichment for exact `consumed_weight_g` & other users' custom materials
    sqlite_consumed_by_uuid: dict[str, float] = {}
    if sqlite_db is not None:
        sq_data = export_from_sqlite(sqlite_db)
        for sq_sp in sq_data["spools"]:
            if sq_sp.get("uuid"):
                sqlite_consumed_by_uuid[sq_sp["uuid"]] = float(
                    sq_sp.get("odometer_consumed_weight_g") or 0.0
                )
        for sq_cm in sq_data["custom_materials"]:
            if sq_cm["uuid"] not in seen_custom_uuids:
                custom_materials.append(sq_cm)
                seen_custom_uuids.add(sq_cm["uuid"])

    for sp in spools:
        compute_spool_consumption(
            sp,
            package_net_weights=package_net_weights,
            history_drops_by_id=history_drops_by_id,
            sqlite_consumed_by_uuid=sqlite_consumed_by_uuid,
        )

    # Printer tool assignments
    tool_assignments = fetch_paginated_offset(
        client, f"{MODULE_PREFIX}/printers/tools", "tools"
    )

    summary, usage_by_material = build_usage_rollup(spools)
    mode = "hybrid_http_and_sqlite" if sqlite_db is not None else "http"
    source_meta: dict[str, Any] = {
        "url": client.base_url,
        "core_history_available": history_available,
    }
    if sqlite_db is not None:
        source_meta["sqlite_db"] = str(sqlite_db.expanduser().resolve())

    return {
        "schema": SCHEMA_VERSION,
        "exported_at": utc_now_iso(),
        "export_mode": mode,
        "source": source_meta,
        "summary": summary,
        "usage_by_material": usage_by_material,
        "catalogue": catalogue,
        "locations": locations,
        "custom_materials": custom_materials,
        "spools": spools,
        "tool_assignments": tool_assignments,
        "favourites": favourites,
        "list_filters": list_filters,
    }


# ---------------------------------------------------------------------------
# Import: HTTP REST API & Direct SQLite
# ---------------------------------------------------------------------------

CUSTOM_MATERIAL_FIELDS = (
    "name",
    "brand_name",
    "class",
    "type",
    "abbreviation",
    "color_rgba",
    "diameter_um",
    "density",
    "min_print_temp",
    "max_print_temp",
    "preheat_temp",
    "min_bed_temp",
    "max_bed_temp",
    "chamber_temp",
    "drying_temp",
    "drying_time_min",
    "min_chamber_temp",
    "max_chamber_temp",
    "min_nozzle_diameter",
    "transmission_distance",
    "hardness_shore_a",
    "hardness_shore_d",
    "ral_code",
    "secondary_colors",
    "certifications",
    "tags",
)


def custom_signature(mat: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(mat.get("name") or "").strip().lower(),
        str(mat.get("brand_name") or "").strip().lower(),
        str(mat.get("class") or "FFF").strip().upper(),
        str(mat.get("type") or "").strip().lower(),
        str(mat.get("color_rgba") or "").strip().lower(),
    )


def build_custom_payload(mat: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in CUSTOM_MATERIAL_FIELDS:
        val = mat.get(key)
        if val is None or val == "" or val == []:
            continue
        payload[key] = val
    payload["name"] = str(mat.get("name") or "Imported Material")[:200]
    mat_class = str(mat.get("class") or "FFF").upper()
    payload["class"] = mat_class if mat_class in ("FFF", "SLA") else "FFF"
    return payload


def synthesize_custom_from_spool(sp: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": str(sp.get("material_name") or sp.get("type") or "Imported Material")[:200],
        "class": "SLA" if str(sp.get("class") or "").upper() == "SLA" else "FFF",
    }
    for key in ("brand_name", "type", "color_rgba"):
        if sp.get(key):
            payload[key] = str(sp[key])[:200]
    if sp.get("density") is not None:
        payload["density"] = float(sp["density"])
    if sp.get("diameter_um") is not None:
        payload["diameter_um"] = int(sp["diameter_um"])
    return payload


def ensure_catalogue_loaded(client: ConnectHttpClient, wait_seconds: int = 180) -> None:
    cat = client.request("GET", f"{MODULE_PREFIX}/catalogue")
    if cat.get("loaded"):
        return
    phase = (cat.get("import") or {}).get("phase")
    if phase != "running":
        print("Target catalogue is not loaded; starting POST /catalogue/download ...", file=sys.stderr)
        client.request("POST", f"{MODULE_PREFIX}/catalogue/download")
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(2.0)
        cat = client.request("GET", f"{MODULE_PREFIX}/catalogue")
        imp = cat.get("import") or {}
        if cat.get("loaded") and imp.get("phase") != "running":
            return
        if imp.get("phase") == "failed":
            raise RuntimeError(
                f"Target catalogue download failed (reason={imp.get('reason')})"
            )
    raise TimeoutError("Timed out waiting for target OpenPrintTag catalogue download")


def prepare_spool_weights_for_import(
    sp: dict[str, Any],
) -> tuple[float, float | None, float | None]:
    """
    Prepare `(remaining_weight_g, full_weight_g, empty_weight_g)` so that:
    1) Prusa's `invalidWeights` check (`remaining <= full - empty`) always passes, AND
    2) `consumed_weight_g` is preserved on the target server via `full - empty - remaining`
       even if the source spool originally had `full_weight_g = None`.
    """
    remaining = max(0.0, min(MAX_WEIGHT_G, float(sp.get("remaining_weight_g") or 0.0)))
    consumed = max(0.0, float(sp.get("consumed_weight_g") or 0.0))
    full_w = float(sp["full_weight_g"]) if sp.get("full_weight_g") is not None else None
    empty_w = float(sp["empty_weight_g"]) if sp.get("empty_weight_g") is not None else None

    if full_w is None and consumed > 0.0:
        empty_val = empty_w if empty_w is not None else 0.0
        full_w = min(MAX_WEIGHT_G, remaining + consumed + empty_val)
        empty_w = empty_val

    if full_w is not None:
        empty_val = empty_w if empty_w is not None else 0.0
        if remaining > (full_w - empty_val):
            full_w = min(MAX_WEIGHT_G, remaining + consumed + empty_val)

    return round(remaining, 3), round_g(full_w), round_g(empty_w)


def spool_dedupe_key(
    material_uuid: str,
    remaining_g: float,
    location_name: str,
    notes: str,
    is_archived: bool,
) -> tuple[str, float, str, str, bool]:
    return (
        material_uuid,
        round(float(remaining_g), 2),
        location_name.strip().lower(),
        notes.strip(),
        is_archived,
    )


def import_to_http(
    client: ConnectHttpClient,
    bundle: dict[str, Any],
    *,
    ensure_catalogue: bool = False,
    skip_existing: bool = True,
    dry_run: bool = False,
    patch_sqlite_db: pathlib.Path | None = None,
) -> dict[str, Any]:
    stats = {
        "locations_created_or_matched": 0,
        "custom_materials_created": 0,
        "custom_materials_matched": 0,
        "orphan_materials_synthesized": 0,
        "spools_created": 0,
        "spools_skipped_existing": 0,
        "spools_archived": 0,
        "favourites_restored": 0,
        "sqlite_spools_patched": 0,
    }

    if ensure_catalogue and not dry_run:
        ensure_catalogue_loaded(client)

    # 1. Locations (POST /locations is idempotent by case-insensitive name!)
    loc_name_to_id: dict[str, int] = {}
    existing_locs = (client.request("GET", f"{MODULE_PREFIX}/locations") or {}).get("locations") or []
    for loc in existing_locs:
        loc_name_to_id[str(loc["name"]).strip().lower()] = int(loc["id"])

    for loc in bundle.get("locations") or []:
        name = str(loc.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in loc_name_to_id:
            stats["locations_created_or_matched"] += 1
            continue
        if dry_run:
            loc_name_to_id[key] = -1
            stats["locations_created_or_matched"] += 1
            continue
        created = client.request("POST", f"{MODULE_PREFIX}/locations", body={"name": name[:200]})
        loc_name_to_id[key] = int(created["id"])
        stats["locations_created_or_matched"] += 1

    # 2. Custom materials (deduplicate against existing custom materials on target)
    material_uuid_map: dict[str, str] = {}
    existing_custom_by_sig: dict[tuple[str, str, str, str, str], str] = {}

    existing_custom_rows = fetch_paginated_offset(
        client, f"{MODULE_PREFIX}/materials", "rows", extra_query={"custom": True}
    )
    for row in existing_custom_rows:
        existing_custom_by_sig[custom_signature(row)] = str(row["uuid"])

    for cm in bundle.get("custom_materials") or []:
        old_uuid = str(cm["uuid"])
        sig = custom_signature(cm)
        if sig in existing_custom_by_sig:
            material_uuid_map[old_uuid] = existing_custom_by_sig[sig]
            stats["custom_materials_matched"] += 1
            continue
        if dry_run:
            material_uuid_map[old_uuid] = old_uuid
            stats["custom_materials_created"] += 1
            continue
        created_cm = client.request(
            "POST", f"{MODULE_PREFIX}/custom-materials", body=build_custom_payload(cm)
        )
        new_uuid = str(created_cm["uuid"])
        material_uuid_map[old_uuid] = new_uuid
        existing_custom_by_sig[sig] = new_uuid
        stats["custom_materials_created"] += 1

    # Snapshot existing target spools as a multiset so identical spools in a single
    # export all import cleanly, while re-running the import does not duplicate them.
    existing_spool_counts: collections.Counter[tuple[str, float, str, str, bool]] = (
        collections.Counter()
    )
    if skip_existing:
        for existing_sp in fetch_paginated_offset(client, f"{MODULE_PREFIX}/spools", "spools"):
            existing_spool_counts[
                spool_dedupe_key(
                    str(existing_sp.get("material_uuid") or ""),
                    float(existing_sp.get("remaining_weight_g") or 0.0),
                    str(existing_sp.get("location") or ""),
                    str(existing_sp.get("notes") or ""),
                    existing_sp.get("archived_at") is not None,
                )
            ] += 1

    # 3. Spools (active + archived via 2-step POST + PATCH)
    imported_id_to_source_spool: list[tuple[int, dict[str, Any]]] = []

    for sp in bundle.get("spools") or []:
        orig_mat_uuid = str(sp["material_uuid"])
        if orig_mat_uuid not in material_uuid_map and sp.get("material_source") == "custom":
            sig = custom_signature(synthesize_custom_from_spool(sp))
            if sig in existing_custom_by_sig:
                material_uuid_map[orig_mat_uuid] = existing_custom_by_sig[sig]
        target_mat_uuid = material_uuid_map.get(orig_mat_uuid, orig_mat_uuid)
        is_mapped_custom = orig_mat_uuid in material_uuid_map or sp.get("material_source") == "custom"

        remaining_g, full_g, empty_g = prepare_spool_weights_for_import(sp)
        loc_raw = str(sp.get("location") or "")
        notes_raw = str(sp.get("notes") or "")[:2000]
        is_arch = sp.get("archived_at") is not None

        if skip_existing:
            dkey = spool_dedupe_key(target_mat_uuid, remaining_g, loc_raw, notes_raw, is_arch)
            if existing_spool_counts[dkey] > 0:
                existing_spool_counts[dkey] -= 1
                stats["spools_skipped_existing"] += 1
                continue
        draft: dict[str, Any] = {
            "material_uuid": target_mat_uuid,
            "remaining_weight_g": remaining_g,
            "quantity": 1,
        }
        if full_g is not None:
            draft["full_weight_g"] = full_g
        if empty_g is not None:
            draft["empty_weight_g"] = empty_g
        if sp.get("length_mm") is not None:
            draft["length_mm"] = int(sp["length_mm"])
        if sp.get("package_uuid") and not is_mapped_custom:
            draft["package_uuid"] = sp["package_uuid"]
        if sp.get("notes"):
            draft["notes"] = str(sp["notes"])[:2000]

        loc_name = str(sp.get("location") or "").strip().lower()
        if loc_name and loc_name in loc_name_to_id and loc_name_to_id[loc_name] > 0:
            draft["location_id"] = loc_name_to_id[loc_name]

        if dry_run:
            stats["spools_created"] += 1
            if sp.get("archived_at"):
                stats["spools_archived"] += 1
            continue

        try:
            added = client.request("POST", f"{MODULE_PREFIX}/spools", body=draft)
        except ApiError as exc:
            # Fallback 1: package dropped from newer catalogue -> retry without package_uuid
            if exc.code == "materials/NOT_FOUND" and "package_uuid" in draft:
                draft.pop("package_uuid", None)
                try:
                    added = client.request("POST", f"{MODULE_PREFIX}/spools", body=draft)
                except ApiError as exc2:
                    exc = exc2
                else:
                    exc = None  # type: ignore[assignment]
            # Fallback 2: material missing (e.g. another user's custom material or dropped catalogue entry)
            if exc is not None and exc.code == "materials/NOT_FOUND":
                synth_payload = synthesize_custom_from_spool(sp)
                sig = custom_signature(synth_payload)
                if sig in existing_custom_by_sig:
                    new_mat_uuid = existing_custom_by_sig[sig]
                else:
                    created_cm = client.request(
                        "POST", f"{MODULE_PREFIX}/custom-materials", body=synth_payload
                    )
                    new_mat_uuid = str(created_cm["uuid"])
                    existing_custom_by_sig[sig] = new_mat_uuid
                    stats["orphan_materials_synthesized"] += 1
                material_uuid_map[orig_mat_uuid] = new_mat_uuid
                draft["material_uuid"] = new_mat_uuid
                draft.pop("package_uuid", None)
                added = client.request("POST", f"{MODULE_PREFIX}/spools", body=draft)
            elif exc is not None:
                raise exc

        new_spool_id = int(added["ids"][0])
        stats["spools_created"] += 1
        imported_id_to_source_spool.append((new_spool_id, sp))

        if sp.get("archived_at") is not None:
            patch_body: dict[str, Any] = {"archived": True}
            if sp.get("archive_reason"):
                patch_body["archive_reason"] = sp["archive_reason"]
            if sp.get("archive_reason_note"):
                patch_body["archive_reason_note"] = str(sp["archive_reason_note"])[:1000]
            client.request("PATCH", f"{MODULE_PREFIX}/spools/{new_spool_id}", body=patch_body)
            stats["spools_archived"] += 1

    # 4. Favourites
    for fav in bundle.get("favourites") or []:
        orig_uuid = str(fav.get("material_uuid") or "")
        if not orig_uuid:
            continue
        target_uuid = material_uuid_map.get(orig_uuid, orig_uuid)
        if dry_run:
            stats["favourites_restored"] += 1
            continue
        try:
            client.request("PUT", f"{MODULE_PREFIX}/favourites/{target_uuid}")
            stats["favourites_restored"] += 1
        except ApiError as exc:
            if exc.status != 404 and exc.code != "materials/NOT_FOUND":
                raise

    # 5. Optional hybrid SQLite post-patch: stamp exact `consumed_weight_g`, `uuid`, `source`
    if patch_sqlite_db is not None and not dry_run and imported_id_to_source_spool:
        with open_sqlite_rw(patch_sqlite_db) as conn:
            for new_id, sp in imported_id_to_source_spool:
                consumed_val = float(
                    sp.get("odometer_consumed_weight_g")
                    if sp.get("odometer_consumed_weight_g") is not None
                    else (sp.get("consumed_weight_g") or 0.0)
                )
                src_uuid = str(sp.get("uuid") or "")
                src_source = sp.get("source") if sp.get("source") in ("manual", "printer") else "manual"
                # Avoid UNIQUE constraint collision if UUID already exists
                uuid_taken = False
                if src_uuid:
                    row = conn.execute(
                        "SELECT id FROM spools WHERE uuid = ? AND id != ?",
                        (src_uuid, new_id),
                    ).fetchone()
                    uuid_taken = row is not None
                if src_uuid and not uuid_taken:
                    conn.execute(
                        "UPDATE spools SET consumed_weight_g = ?, uuid = ?, source = ? WHERE id = ?",
                        (consumed_val, src_uuid, src_source, new_id),
                    )
                else:
                    conn.execute(
                        "UPDATE spools SET consumed_weight_g = ? WHERE id = ?",
                        (consumed_val, new_id),
                    )
                stats["sqlite_spools_patched"] += 1
            conn.commit()

    return stats


def import_to_sqlite(
    db_path: pathlib.Path,
    bundle: dict[str, Any],
    *,
    default_owner: str = "admin",
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Direct lossless import into a stopped target's `database.sqlite`.
    Preserves `consumed_weight_g`, NFC `uuid`s, `source`, `custom_materials`
    across owners, `locations`, `archived_at`, and `favourites`.
    """
    stats = {
        "locations_created_or_matched": 0,
        "custom_materials_upserted": 0,
        "spools_inserted": 0,
        "spools_skipped_existing_uuid": 0,
        "favourites_upserted": 0,
    }
    now = utc_now_iso()

    if dry_run:
        return {
            "locations_created_or_matched": len(bundle.get("locations") or []),
            "custom_materials_upserted": len(bundle.get("custom_materials") or []),
            "spools_inserted": len(bundle.get("spools") or []),
            "spools_skipped_existing_uuid": 0,
            "favourites_upserted": len(bundle.get("favourites") or []),
        }

    with open_sqlite_rw(db_path) as conn:
        # 1. Locations
        loc_name_to_id: dict[str, int] = {}
        for r in conn.execute("SELECT id, name FROM locations"):
            loc_name_to_id[str(r["name"]).strip().lower()] = int(r["id"])

        for loc in bundle.get("locations") or []:
            name = str(loc.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key not in loc_name_to_id:
                cur = conn.execute(
                    "INSERT INTO locations (name, created_at, updated_at) VALUES (?, ?, ?)",
                    (name[:200], loc.get("created_at") or now, loc.get("updated_at") or now),
                )
                loc_name_to_id[key] = int(cur.lastrowid)  # type: ignore[arg-type]
            stats["locations_created_or_matched"] += 1

        # 2. Custom materials (preserves exact UUID!)
        for cm in bundle.get("custom_materials") or []:
            cm_uuid = str(cm.get("uuid") or uuid.uuid4())
            owner = str(cm.get("owner") or default_owner)
            rgba = str(cm.get("color_rgba") or "")
            conn.execute(
                """
                INSERT INTO custom_materials (
                    uuid, owner, name, brand_name, class, type, abbreviation,
                    color_rgba, color_family, diameter_um, density,
                    min_print_temp, max_print_temp, preheat_temp, min_bed_temp, max_bed_temp,
                    chamber_temp, min_chamber_temp, max_chamber_temp, drying_temp, drying_time_min,
                    min_nozzle_diameter, transmission_distance, hardness_shore_a, hardness_shore_d,
                    ral_code, secondary_colors, certifications, tags, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(uuid) DO UPDATE SET
                    name = excluded.name,
                    brand_name = excluded.brand_name,
                    class = excluded.class,
                    type = excluded.type,
                    abbreviation = excluded.abbreviation,
                    color_rgba = excluded.color_rgba,
                    color_family = excluded.color_family,
                    diameter_um = excluded.diameter_um,
                    density = excluded.density,
                    updated_at = excluded.updated_at
                """,
                (
                    cm_uuid,
                    owner,
                    str(cm.get("name") or "Imported Material")[:200],
                    str(cm.get("brand_name") or "")[:200],
                    "SLA" if str(cm.get("class") or "").upper() == "SLA" else "FFF",
                    str(cm.get("type") or "")[:200],
                    str(cm.get("abbreviation") or "")[:200],
                    rgba,
                    color_family(rgba),
                    cm.get("diameter_um"),
                    cm.get("density"),
                    cm.get("min_print_temp"),
                    cm.get("max_print_temp"),
                    cm.get("preheat_temp"),
                    cm.get("min_bed_temp"),
                    cm.get("max_bed_temp"),
                    cm.get("chamber_temp"),
                    cm.get("min_chamber_temp"),
                    cm.get("max_chamber_temp"),
                    cm.get("drying_temp"),
                    cm.get("drying_time_min"),
                    cm.get("min_nozzle_diameter"),
                    cm.get("transmission_distance"),
                    cm.get("hardness_shore_a"),
                    cm.get("hardness_shore_d"),
                    str(cm.get("ral_code") or "")[:200],
                    join_csv(cm.get("secondary_colors")),
                    join_csv(cm.get("certifications")),
                    join_csv(cm.get("tags")),
                    cm.get("created_at") or now,
                    cm.get("updated_at") or now,
                ),
            )
            stats["custom_materials_upserted"] += 1

        # 3. Spools (idempotent by spool UUID; preserves exact `consumed_weight_g` & NFC `source`)
        existing_uuids = {
            str(r["uuid"]) for r in conn.execute("SELECT uuid FROM spools WHERE uuid != ''")
        }
        for sp in bundle.get("spools") or []:
            sp_uuid = str(sp.get("uuid") or uuid.uuid4())
            if sp_uuid in existing_uuids:
                stats["spools_skipped_existing_uuid"] += 1
                continue

            loc_id = None
            loc_name = str(sp.get("location") or "").strip().lower()
            if loc_name and loc_name in loc_name_to_id:
                loc_id = loc_name_to_id[loc_name]

            rgba = str(sp.get("color_rgba") or "")
            consumed_g = float(
                sp.get("odometer_consumed_weight_g")
                if sp.get("odometer_consumed_weight_g") is not None
                else (sp.get("consumed_weight_g") or 0.0)
            )
            remaining_g, full_g, empty_g = prepare_spool_weights_for_import(sp)
            src = sp.get("source") if sp.get("source") in ("manual", "printer") else "manual"
            cond = sp.get("condition") if sp.get("condition") in ("new", "partially_used") else "new"
            archived_at = sp.get("archived_at")
            arch_reason = (sp.get("archive_reason") or None) if archived_at else None
            arch_note = (sp.get("archive_reason_note") or None) if archived_at else None

            conn.execute(
                """
                INSERT INTO spools (
                    uuid, source, material_uuid, material_source, material_name, brand_name,
                    class, type, color_rgba, color_family, density, diameter_um, package_uuid,
                    full_weight_g, empty_weight_g, length_mm, remaining_weight_g, consumed_weight_g,
                    condition, location_id, notes, created_by, created_at, updated_at,
                    archived_at, archive_reason, archive_reason_note
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    sp_uuid,
                    src,
                    str(sp.get("material_uuid") or ""),
                    str(sp.get("material_source") or "catalogue"),
                    str(sp.get("material_name") or ""),
                    str(sp.get("brand_name") or ""),
                    str(sp.get("class") or "FFF"),
                    str(sp.get("type") or ""),
                    rgba,
                    color_family(rgba),
                    sp.get("density"),
                    sp.get("diameter_um"),
                    sp.get("package_uuid"),
                    full_g,
                    empty_g,
                    sp.get("length_mm"),
                    remaining_g,
                    consumed_g,
                    cond,
                    loc_id,
                    str(sp.get("notes") or "")[:2000],
                    str(sp.get("created_by") or default_owner),
                    sp.get("created_at") or now,
                    sp.get("updated_at") or now,
                    archived_at,
                    arch_reason,
                    arch_note,
                ),
            )
            existing_uuids.add(sp_uuid)
            stats["spools_inserted"] += 1

        # 4. Favourites
        for fav in bundle.get("favourites") or []:
            mat_uuid = str(fav.get("material_uuid") or "")
            if not mat_uuid:
                continue
            principal = str(fav.get("principal") or default_owner)
            conn.execute(
                """
                INSERT INTO favourites (principal, material_uuid, starred_at)
                VALUES (?, ?, ?)
                ON CONFLICT(principal, material_uuid) DO NOTHING
                """,
                (principal, mat_uuid, fav.get("starred_at") or now),
            )
            stats["favourites_upserted"] += 1

        conn.commit()

    return stats


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def build_client_from_args(args: argparse.Namespace) -> ConnectHttpClient:
    token = args.token or os.environ.get("PRUSA_CONNECT_TOKEN")
    client = ConnectHttpClient(
        base_url=args.url,
        token=token,
        insecure=args.insecure,
        ca_file=args.ca_file,
        timeout=args.timeout,
    )
    if args.username and not token:
        password = (
            args.password
            or os.environ.get("PRUSA_CONNECT_PASSWORD")
            or getpass.getpass(f"Password for {args.username}: ")
        )
        client.login_password(args.username, password)
    return client


def add_common_conn_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        help="Prusa Connect instance URL, e.g. http://192.168.1.100:8080",
    )
    parser.add_argument(
        "--token",
        help="Bearer API token (pcl_...), or set PRUSA_CONNECT_TOKEN env var",
    )
    parser.add_argument(
        "--username",
        help="Local account username (logs in via POST /api/v1/auth/login if --token is omitted)",
    )
    parser.add_argument(
        "--password",
        help="Local account password, or set PRUSA_CONNECT_PASSWORD env var",
    )
    parser.add_argument(
        "--sqlite-db",
        type=pathlib.Path,
        help=(
            "Path to the Materials module SQLite file "
            "(.../com.github.aux.prusa3d.materials/database.sqlite). "
            "Can be used standalone offline or combined with --url."
        ),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification for self-signed HTTPS instances",
    )
    parser.add_argument(
        "--ca-file",
        help="Custom CA certificate PEM path for HTTPS verification",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP request timeout in seconds (default: 30)",
    )


def cmd_export(args: argparse.Namespace) -> int:
    if not args.url and not args.sqlite_db:
        print("Error: specify --url, --sqlite-db, or both.", file=sys.stderr)
        return 2

    if args.url:
        client = build_client_from_args(args)
        bundle = export_from_http(client, sqlite_db=args.sqlite_db)
    else:
        bundle = export_from_sqlite(args.sqlite_db)

    encoded = json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"
    if args.output == "-":
        sys.stdout.write(encoded)
    else:
        out_path = pathlib.Path(args.output)
        out_path.write_text(encoded, encoding="utf-8")
        s = bundle["summary"]
        print(
            f"Exported to {out_path} ({bundle['export_mode']} mode):\n"
            f"  Materials with spools : {s['distinct_materials_with_spools']}\n"
            f"  Custom materials      : {len(bundle['custom_materials'])}\n"
            f"  Locations             : {len(bundle['locations'])}\n"
            f"  Spools                : {s['total_spools']} "
            f"({s['active_spools']} active, {s['archived_spools']} archived)\n"
            f"  Remaining filament    : {s['active_remaining_weight_g']} g\n"
            f"  Consumed filament     : {s['total_consumed_weight_g']} g",
            file=sys.stderr,
        )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    if not args.url and not args.sqlite_db:
        print("Error: specify --url (HTTP import) or --sqlite-db (direct SQLite import).", file=sys.stderr)
        return 2

    in_path = pathlib.Path(args.input)
    bundle = json.loads(in_path.read_text(encoding="utf-8"))
    if not isinstance(bundle, dict) or "spools" not in bundle:
        print(f"Error: {in_path} is not a valid materials export JSON file.", file=sys.stderr)
        return 2

    if args.url:
        client = build_client_from_args(args)
        stats = import_to_http(
            client,
            bundle,
            ensure_catalogue=args.ensure_catalogue,
            skip_existing=not args.no_skip_existing,
            dry_run=args.dry_run,
            patch_sqlite_db=args.sqlite_db,
        )
        mode_label = "DRY RUN (HTTP)" if args.dry_run else "HTTP Import"
    else:
        stats = import_to_sqlite(
            args.sqlite_db,
            bundle,
            default_owner=args.default_owner,
            dry_run=args.dry_run,
        )
        mode_label = "DRY RUN (SQLite)" if args.dry_run else "SQLite Import"

    print(f"{mode_label} summary:", file=sys.stderr)
    for k, v in stats.items():
        print(f"  {k:32s}: {v}", file=sys.stderr)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    in_path = pathlib.Path(args.input)
    bundle = json.loads(in_path.read_text(encoding="utf-8"))
    summary = bundle.get("summary") or {}
    rows = bundle.get("usage_by_material") or []

    print(f"Export file : {in_path}")
    print(f"Exported at : {bundle.get('exported_at')} (mode: {bundle.get('export_mode')})")
    print(
        f"Totals      : {summary.get('total_spools', 0)} spools "
        f"({summary.get('active_spools', 0)} active, {summary.get('archived_spools', 0)} archived) | "
        f"Remaining: {summary.get('active_remaining_weight_g', 0)} g | "
        f"Consumed: {summary.get('total_consumed_weight_g', 0)} g"
    )
    print()
    header = f"{'Brand':18s} {'Material':28s} {'Type':8s} {'Active':>6s} {'Arch':>4s} {'Remain(g)':>10s} {'Consumed(g)':>12s}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{str(r.get('brand_name') or '-')[:18]:18s} "
            f"{str(r.get('material_name') or '-')[:28]:28s} "
            f"{str(r.get('type') or '-')[:8]:8s} "
            f"{int(r.get('active_spools') or 0):6d} "
            f"{int(r.get('archived_spools') or 0):4d} "
            f"{float(r.get('active_remaining_weight_g') or 0.0):10.1f} "
            f"{float(r.get('consumed_weight_g') or 0.0):12.1f}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prusa_materials_sync.py",
        description="Export and import Prusa Connect filament materials, spools, and consumed weights.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("export", help="Download all filament materials, spools, and usage to JSON")
    add_common_conn_args(p_exp)
    p_exp.add_argument(
        "-o",
        "--output",
        default="prusa-materials-export.json",
        help="Output JSON file path, or '-' for stdout (default: prusa-materials-export.json)",
    )
    p_exp.set_defaults(func=cmd_export)

    p_imp = sub.add_parser("import", help="Import a JSON export into another Prusa Connect instance")
    add_common_conn_args(p_imp)
    p_imp.add_argument(
        "-i",
        "--input",
        required=True,
        help="Input JSON file previously produced by `export`",
    )
    p_imp.add_argument(
        "--ensure-catalogue",
        action="store_true",
        help="Trigger POST /catalogue/download on target if OpenPrintTag catalogue is not yet loaded",
    )
    p_imp.add_argument(
        "--default-owner",
        default="admin",
        help="Fallback owner principal for custom_materials in offline --sqlite-db import (default: admin)",
    )
    p_imp.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Do not skip spools that already match existing spools on the target HTTP instance",
    )
    p_imp.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be created without modifying the target instance",
    )
    p_imp.set_defaults(func=cmd_import)

    p_sum = sub.add_parser("summary", help="Print a per-material filament consumption table from an export JSON")
    p_sum.add_argument("input", help="Path to export JSON file")
    p_sum.set_defaults(func=cmd_summary)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ApiError, RuntimeError, FileNotFoundError, TimeoutError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
