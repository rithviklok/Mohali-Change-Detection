"""
main.py — FastAPI backend for Mohali Change Detection.

Endpoints
---------
GET  /health              → service status
POST /rgb-tiles           → tile URL for a year: sub-metre Esri Wayback (default) or Landsat/S2
GET  /imagery-captures    → real high-res capture dates available over Mohali
POST /change-tiles        → EE tile URL (rasterized cluster polygons, right-side only)
POST /change-polygons     → GeoJSON FeatureCollection (polygons on map)
POST /change-shapefile    → direct .zip download URL for Shapefile
POST /identify-defaulters → IN_TAX_NET vacant parcels inside change polygons → under-assessed
"""

from __future__ import annotations

import time
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import ee_service
import imagery_service

# ── Constants ──────────────────────────────────────────────────────────────────

CACHE_DB = Path(__file__).parent / "tile_cache.db"
CACHE_TTL_HOURS = 20  # EE URLs expire after ~24h; evict at 20h to be safe


# ── Models ─────────────────────────────────────────────────────────────────────

class YearRange(BaseModel):
    year1: int = Field(..., ge=2013, le=2030, description="Baseline year")
    year2: int = Field(..., ge=2013, le=2030, description="Current / comparison year")


class SingleYear(BaseModel):
    year: int = Field(..., ge=2013, le=2030)
    source: Literal["highres", "analysis"] = "highres"


# ── Cache with TTL ─────────────────────────────────────────────────────────────

def _init_cache():
    """Create the cache table if it doesn't exist."""
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tile_cache (
            key        TEXT PRIMARY KEY,
            url        TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _cached_tile(key: str, compute_fn) -> str:
    """
    Return a cached EE tile URL if fresh (< 20 hours old),
    otherwise recompute, store, and return.
    """
    conn = sqlite3.connect(str(CACHE_DB))
    now = time.time()

    # Check cache
    row = conn.execute(
        "SELECT url, created_at FROM tile_cache WHERE key = ?", (key,)
    ).fetchone()

    if row:
        url, created_at = row
        age_hours = (now - created_at) / 3600
        if age_hours < CACHE_TTL_HOURS:
            conn.close()
            return url
        # Stale → evict
        conn.execute("DELETE FROM tile_cache WHERE key = ?", (key,))

    # Compute fresh URL
    url = compute_fn()

    conn.execute(
        "INSERT OR REPLACE INTO tile_cache (key, url, created_at) VALUES (?, ?, ?)",
        (key, url, now),
    )
    conn.commit()
    conn.close()
    return url


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Mohali Change Detection",
    description="On-the-fly urban growth detection using AlphaEarth + NDBI/NDVI, "
                "with satellite-driven defaulter identification.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_init_cache()
ee_service.initialize_ee()
imagery_service.start_background_build()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "city": "Mohali", "version": "2.1.0"}


@app.post("/rgb-tiles")
def rgb_tiles(req: SingleYear):
    """
    Return a map tile URL for true-colour imagery of a specific year.

    source="highres"  -> Esri World Imagery Wayback (~0.3-0.5 m), picked by
                         real capture date over Mohali. For visual verification.
    source="analysis" -> Landsat 8 (pre-2017) / Sentinel-2 composite (10-30 m),
                         i.e. the imagery the change detection actually runs on.
    """
    try:
        if req.source == "highres":
            return imagery_service.get_wayback_for_year(req.year)
        key = f"rgb:{req.year}"
        url = _cached_tile(key, lambda: ee_service.get_rgb_tiles(req.year))
        return {
            "tile_url": url, "year": req.year, "source": "analysis",
            "capture_date": f"{req.year} composite",
            "resolution_m": 30 if req.year < 2017 else 10,
            "provider": "Landsat 8" if req.year < 2017 else "Sentinel-2",
            "max_native_zoom": 18, "exact_year": True, "note": None,
            "attribution": "Google Earth Engine | USGS / ESA Copernicus",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/imagery-captures")
def imagery_captures():
    """List distinct high-res capture dates available over Mohali."""
    try:
        return {"captures": imagery_service.available_captures()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/change-tiles")
def change_tiles(req: YearRange):
    """Return a map tile URL for the rasterized built-up cluster polygons (> 0.5 ha)."""
    try:
        key = f"change:{req.year1}:{req.year2}"
        url = _cached_tile(key, lambda: ee_service.get_change_tiles(req.year1, req.year2))
        return {"tile_url": url, "year1": req.year1, "year2": req.year2}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/change-polygons")
def change_polygons(req: YearRange):
    """Return GeoJSON FeatureCollection of new built-up clusters (> 0.5 ha)."""
    try:
        key = f"polygons:{req.year1}:{req.year2}"
        # Cache the serialized GeoJSON too (recomputes are expensive)
        import json
        conn = sqlite3.connect(str(CACHE_DB))
        now = time.time()
        row = conn.execute(
            "SELECT url, created_at FROM tile_cache WHERE key = ?", (key,)
        ).fetchone()

        if row:
            cached_json, created_at = row
            if (now - created_at) / 3600 < CACHE_TTL_HOURS:
                conn.close()
                geojson = json.loads(cached_json)
                return {
                    "type": "FeatureCollection",
                    "features": geojson.get("features", []),
                    "year1": req.year1,
                    "year2": req.year2,
                    "cluster_count": len(geojson.get("features", [])),
                }
            conn.execute("DELETE FROM tile_cache WHERE key = ?", (key,))

        geojson = ee_service.get_change_polygons(req.year1, req.year2)
        conn.execute(
            "INSERT OR REPLACE INTO tile_cache (key, url, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(geojson), now),
        )
        conn.commit()
        conn.close()

        return {
            "type": "FeatureCollection",
            "features": geojson.get("features", []),
            "year1": req.year1,
            "year2": req.year2,
            "cluster_count": len(geojson.get("features", [])),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/change-shapefile")
def change_shapefile(req: YearRange):
    """Return a direct download URL for the Shapefile (.zip)."""
    try:
        key = f"shp:{req.year1}:{req.year2}"
        url = _cached_tile(key, lambda: ee_service.get_shapefile_url(req.year1, req.year2))
        return {"download_url": url, "year1": req.year1, "year2": req.year2}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/identify-defaulters")
def identify_defaulters(req: YearRange):
    """
    Focused pipeline: run change detection → intersect with vacant parcels
    to identify:
      1. New Construction Defaulters (Set 2 - Orange): Unregistered on mSeva with new built-up.
      2. Under-Assessed (Yellow): Registered on mSeva as Vacant / Zero-Tax Land with new built-up.
    """
    try:
        # Step 1: Get change polygons (from cache or GEE)
        import json
        key = f"polygons:{req.year1}:{req.year2}"
        conn = sqlite3.connect(str(CACHE_DB))
        now = time.time()
        row = conn.execute(
            "SELECT url, created_at FROM tile_cache WHERE key = ?", (key,)
        ).fetchone()

        if row and (now - row[1]) / 3600 < CACHE_TTL_HOURS:
            change_geojson = json.loads(row[0])
        else:
            change_geojson = ee_service.get_change_polygons(req.year1, req.year2)
            conn.execute(
                "INSERT OR REPLACE INTO tile_cache (key, url, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(change_geojson), now),
            )
            conn.commit()
        conn.close()

        # Step 2: Spatial intersection with vacant parcels
        from defaulter_service import identify_under_assessed
        result = identify_under_assessed(change_geojson)
        result["year1"] = req.year1
        result["year2"] = req.year2
        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/change-parcels")
def change_parcels(req: YearRange):
    """
    Approach A: Return GeoJSON FeatureCollection of all GIS survey parcels that
    fall within the detected change polygons, categorized by tax status:
      - NEW_CONSTRUCTION_DEFAULTER (#f97316)
      - LEGACY_BUILT_UP_DEFAULTER  (#ef4444)
      - UNDER_ASSESSED             (#eab308)
      - IN_TAX_NET_BUILT_UP        (#22c55e)
      - EXEMPT                     (#94a3b8)
    """
    try:
        import json
        key = f"polygons:{req.year1}:{req.year2}"
        conn = sqlite3.connect(str(CACHE_DB))
        now = time.time()
        row = conn.execute(
            "SELECT url, created_at FROM tile_cache WHERE key = ?", (key,)
        ).fetchone()

        if row and (now - row[1]) / 3600 < CACHE_TTL_HOURS:
            change_geojson = json.loads(row[0])
        else:
            change_geojson = ee_service.get_change_polygons(req.year1, req.year2)
            conn.execute(
                "INSERT OR REPLACE INTO tile_cache (key, url, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(change_geojson), now),
            )
            conn.commit()
        conn.close()

        import defaulter_service
        geojson_str = defaulter_service.get_change_parcels_geojson(change_geojson)
        return Response(content=geojson_str, media_type="application/json")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/clear-cache")
def clear_cache():
    """Manually clear all cached tile URLs."""
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute("DELETE FROM tile_cache")
    conn.commit()
    conn.close()
    return {"status": "cleared"}
