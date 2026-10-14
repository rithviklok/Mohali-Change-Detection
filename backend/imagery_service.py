"""
imagery_service.py — High-resolution historical satellite imagery for visual verification.

Source: Esri World Imagery Wayback (Maxar / DigitalGlobe / Vantor).
Resolution: ~0.3 - 0.5 metres per pixel (sub-metre, razor-sharp).

Why: Landsat 8 (30 m) and Sentinel-2 (10 m) are what the Earth Engine change
detection algorithm uses for computing changes, but for visual comparison on
the map, sub-metre imagery allows users to zoom into individual plots and
see roofs, walls, and roads with crystal clarity.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CACHE_DB = Path(__file__).parent / "tile_cache.db"
MAX_NATIVE_ZOOM = 19

# Verified high-resolution imagery releases for Mohali with actual capture dates.
# Resolution is 0.3m (30 cm) - 0.5m (50 cm) per pixel.
MOHALI_WAYBACK_CATALOG = [
    {
        "year": 2014,
        "release": 6984,
        "release_date": "2016-09-14",
        "capture_date": "2014-04-28",
        "capture_ms": 1398643200000,
        "provider": "DigitalGlobe",
        "resolution_m": 0.50,
        "max_native_zoom": 17,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/6984/{z}/{y}/{x}",
    },
    {
        "year": 2016,
        "release": 25379,
        "release_date": "2017-08-30",
        "capture_date": "2016-10-08",
        "capture_ms": 1475884800000,
        "provider": "DigitalGlobe",
        "resolution_m": 0.50,
        "max_native_zoom": 19,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/25379/{z}/{y}/{x}",
    },
    {
        "year": 2017,
        "release": 32337,
        "release_date": "2018-05-16",
        "capture_date": "2017-06-13",
        "capture_ms": 1497312000000,
        "provider": "DigitalGlobe",
        "resolution_m": 0.31,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/32337/{z}/{y}/{x}",
    },
    {
        "year": 2018,
        "release": 4756,
        "release_date": "2019-12-12",
        "capture_date": "2018-03-05",
        "capture_ms": 1520208000000,
        "provider": "DigitalGlobe",
        "resolution_m": 0.31,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/4756/{z}/{y}/{x}",
    },
    {
        "year": 2019,
        "release": 4756,
        "release_date": "2019-12-12",
        "capture_date": "2018-03-05",
        "capture_ms": 1520208000000,
        "provider": "DigitalGlobe",
        "resolution_m": 0.31,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/4756/{z}/{y}/{x}",
    },
    {
        "year": 2020,
        "release": 119,
        "release_date": "2020-10-14",
        "capture_date": "2020-02-13",
        "capture_ms": 1581552000000,
        "provider": "Maxar",
        "resolution_m": 0.31,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/119/{z}/{y}/{x}",
    },
    {
        "year": 2021,
        "release": 27659,
        "release_date": "2021-04-28",
        "capture_date": "2020-10-09",
        "capture_ms": 1602201600000,
        "provider": "Maxar",
        "resolution_m": 0.31,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/27659/{z}/{y}/{x}",
    },
    {
        "year": 2022,
        "release": 11475,
        "release_date": "2023-01-11",
        "capture_date": "2022-02-13",
        "capture_ms": 1644731436885,
        "provider": "Maxar",
        "resolution_m": 0.30,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/11475/{z}/{y}/{x}",
    },
    {
        "year": 2023,
        "release": 11475,
        "release_date": "2023-01-11",
        "capture_date": "2022-02-13",
        "capture_ms": 1644731436885,
        "provider": "Maxar",
        "resolution_m": 0.30,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/11475/{z}/{y}/{x}",
    },
    {
        "year": 2024,
        "release": 32553,
        "release_date": "2024-08-15",
        "capture_date": "2024-04-08",
        "capture_ms": 1712534400000,
        "provider": "Maxar",
        "resolution_m": 0.30,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/32553/{z}/{y}/{x}",
    },
    {
        "year": 2025,
        "release": 48925,
        "release_date": "2025-06-26",
        "capture_date": "2025-02-02",
        "capture_ms": 1738454400000,
        "provider": "Maxar",
        "resolution_m": 0.30,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/48925/{z}/{y}/{x}",
    },
    {
        "year": 2026,
        "release": 22869,
        "release_date": "2026-03-26",
        "capture_date": "2025-10-19",
        "capture_ms": 1760832000000,
        "provider": "Vantor",
        "resolution_m": 0.30,
        "tile_url": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/22869/{z}/{y}/{x}",
    },
]


def _init_db():
    conn = sqlite3.connect(str(CACHE_DB))
    # If legacy table without 'year' column exists, drop and recreate
    cur = conn.execute("PRAGMA table_info(wayback_meta)")
    cols = {row[1] for row in cur.fetchall()}
    if cols and "year" not in cols:
        conn.execute("DROP TABLE wayback_meta")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS wayback_meta (
            year         INTEGER,
            release      INTEGER PRIMARY KEY,
            release_date TEXT NOT NULL,
            capture_date TEXT NOT NULL,
            capture_ms   INTEGER,
            provider     TEXT,
            res_m        REAL,
            tile_url     TEXT
        )
    """)
    for item in MOHALI_WAYBACK_CATALOG:
        conn.execute("""
            INSERT OR REPLACE INTO wayback_meta
            (year, release, release_date, capture_date, capture_ms, provider, res_m, tile_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item["year"], item["release"], item["release_date"],
            item["capture_date"], item["capture_ms"], item["provider"],
            item["resolution_m"], item["tile_url"]
        ))
    conn.commit()
    conn.close()


def start_background_build() -> None:
    _init_db()


def available_captures() -> list[dict]:
    _init_db()
    return MOHALI_WAYBACK_CATALOG


def get_wayback_for_year(year: int) -> dict:
    """
    Pick the highest-resolution Wayback capture for the given year.
    If exact year not in catalog, picks the closest year.
    """
    _init_db()
    exact_match = next((item for item in MOHALI_WAYBACK_CATALOG if item["year"] == year), None)
    if exact_match:
        return {
            "tile_url": exact_match["tile_url"],
            "year": year,
            "source": "esri_wayback",
            "release": exact_match["release"],
            "release_date": exact_match["release_date"],
            "capture_date": exact_match["capture_date"],
            "provider": exact_match["provider"],
            "resolution_m": exact_match["resolution_m"],
            "exact_year": True,
            "note": None,
            "max_native_zoom": exact_match.get("max_native_zoom", MAX_NATIVE_ZOOM),
            "attribution": f"Esri World Imagery Wayback ({exact_match['capture_date']} · {exact_match['provider']})",
        }

    # Closest year
    closest = min(MOHALI_WAYBACK_CATALOG, key=lambda item: abs(item["year"] - year))
    return {
        "tile_url": closest["tile_url"],
        "year": year,
        "source": "esri_wayback",
        "release": closest["release"],
        "release_date": closest["release_date"],
        "capture_date": closest["capture_date"],
        "provider": closest["provider"],
        "resolution_m": closest["resolution_m"],
        "exact_year": False,
        "note": f"No direct capture in {year}; showing closest capture ({closest['capture_date']})",
        "max_native_zoom": closest.get("max_native_zoom", MAX_NATIVE_ZOOM),
        "attribution": f"Esri World Imagery Wayback ({closest['capture_date']} · {closest['provider']})",
    }
