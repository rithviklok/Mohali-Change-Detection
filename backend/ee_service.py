"""
ee_service.py — Google Earth Engine computation for Mohali Change Detection.

Faithful Python translation of the GIS Analyst's AlphaEarth + NDBI + NDVI
change detection script (Change_Detection_GEE.txt).

Pipeline
--------
1. AlphaEarth embeddings  → semantic change mask  (available 2017+)
2. Landsat 8 NDBI         → baseline built-up index
3. Sentinel-2 NDBI/NDVI   → current built-up + vegetation
4. Combine into urbanGrowth raster mask
5. reduceToVectors → polygon clusters
6. Calculate area_sqm / area_ha per cluster
7. Filter clusters > 0.5 ha
8. Return as GeoJSON (for map display) + SHP download URL
"""

from __future__ import annotations

import os
import json
import ee
import google.auth
from google.oauth2 import service_account


# ── Constants ──────────────────────────────────────────────────────────────────

EE_SCOPES = ["https://www.googleapis.com/auth/earthengine"]
GEE_PROJECT = "change-detection-494607"


# ── Initialization ─────────────────────────────────────────────────────────────

def initialize_ee() -> None:
    """
    Initialize Earth Engine.
    Priority: Service Account key file → ADC → cached user credentials.
    """
    project = os.environ.get("GEE_PROJECT_ID", GEE_PROJECT)
    key_path = os.environ.get("GEE_SERVICE_ACCOUNT_KEY_PATH")

    try:
        # 1. Service Account key file (server deployments)
        if key_path and os.path.exists(key_path):
            creds = service_account.Credentials.from_service_account_file(
                key_path, scopes=EE_SCOPES
            )
            ee.Initialize(creds, project=project)
            print(f"[OK] EE initialized (Service Account) — project: {project}")
            return

        # 2. Application Default Credentials (Cloud Run)
        creds, detected = google.auth.default(scopes=EE_SCOPES)
        ee.Initialize(creds, project=project or detected)
        print(f"[OK] EE initialized (ADC) — project: {project or detected}")

    except Exception:
        # 3. Cached user credentials (local dev via `earthengine authenticate`)
        try:
            ee.Initialize(project=project)
            print(f"[OK] EE initialized (user auth) — project: {project}")
        except Exception as exc:
            raise RuntimeError(f"EE init failed: {exc}") from exc


# ── Boundary loader ───────────────────────────────────────────────────────────

def load_boundary() -> ee.Geometry:
    """Load the Mohali municipal boundary from the GeoJSON file."""
    path = os.path.join(os.path.dirname(__file__), "boundaries", "Mohali.geojson")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    geom = data["features"][0]["geometry"]
    gtype = geom["type"]
    coords = geom["coordinates"]

    if gtype == "Polygon":
        return ee.Geometry.Polygon(coords)
    elif gtype == "MultiPolygon":
        return ee.Geometry.MultiPolygon(coords)
    else:
        raise ValueError(f"Unsupported geometry type: {gtype}")


# ── Core computation ──────────────────────────────────────────────────────────

def _build_urban_growth(year1: int, year2: int, aoi: ee.Geometry) -> ee.Image:
    """
    Build the binary urban growth mask. Exact translation of the JS script.
    """

    # ── Landsat 8 — Baseline Year NDBI ────────────────────────────────────
    l8 = (ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
          .filterBounds(aoi)
          .filterDate(f"{year1}-01-01", f"{year1}-12-31")
          .filter(ee.Filter.lt("CLOUD_COVER", 20))
          .median()
          .clip(aoi))

    ndbi_y1 = l8.normalizedDifference(["SR_B6", "SR_B5"]).rename("NDBI_Y1")

    # ── Sentinel-2 — Current Year NDBI & NDVI ─────────────────────────────
    s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
          .filterBounds(aoi)
          .filterDate(f"{year2}-01-01", f"{year2}-12-31")
          .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
          .median()
          .clip(aoi))

    ndbi_y2 = s2.normalizedDifference(["B11", "B8"]).rename("NDBI_Y2")
    ndvi_y2 = s2.normalizedDifference(["B8", "B4"]).rename("NDVI_Y2")

    # ── Built-up change ───────────────────────────────────────────────────
    built_change = ndbi_y2.subtract(ndbi_y1)

    # ── Final mask ────────────────────────────────────────────────────────
    # AlphaEarth embeddings only exist from 2017 onwards.
    if year1 >= 2017 and year2 >= 2017:
        alpha = ee.ImageCollection("GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL")
        a1 = alpha.filterDate(f"{year1}-01-01", f"{year1 + 1}-01-01").filterBounds(aoi).first()
        a2 = alpha.filterDate(f"{year2}-01-01", f"{year2 + 1}-01-01").filterBounds(aoi).first()
        similarity = a1.multiply(a2).reduce(ee.Reducer.sum()).clip(aoi)
        alpha_change = ee.Image(1).subtract(similarity)

        urban_growth = (alpha_change.gt(0.05)
                        .And(built_change.gt(0.05))
                        .And(ndvi_y2.lt(0.5)))
    else:
        # Pre-2017 fallback: stricter NDBI-only thresholds
        urban_growth = (built_change.gt(0.1)
                        .And(ndvi_y2.lt(0.4)))

    return urban_growth


# ── Public API functions ──────────────────────────────────────────────────────

def get_change_tiles(year1: int, year2: int) -> str:
    """
    Returns a GEE tile URL for the final filtered built-up clusters (> 0.5 ha).
    We rasterize the polygons so they can be perfectly sliced by the frontend slider.
    """
    aoi = load_boundary()
    urban_growth = _build_urban_growth(year1, year2, aoi)
    
    # 1. Convert to polygons
    clusters = urban_growth.selfMask().reduceToVectors(
        geometry=aoi,
        scale=20,
        geometryType="polygon",
        eightConnected=True,
        labelProperty="builtup",
        reducer=ee.Reducer.countEvery(),
    )

    # 2. Filter area > 0.5 ha
    def calc_area(feature):
        geom = feature.geometry().transform("EPSG:4326", 1)
        area = geom.area(maxError=1)
        return feature.set({"area_ha": ee.Number(area).divide(10000)})

    large_clusters = clusters.map(calc_area).filter(ee.Filter.gt("area_ha", 0.5))

    # 3. Paint polygons to a blank image
    empty = ee.Image().byte()
    cluster_img = empty.paint(large_clusters, 1, 2)  # Color 1, width 2
    filled_img = empty.paint(large_clusters, 1)      # Fill

    # Combine borders and fill
    final_img = filled_img.updateMask(filled_img.eq(1))

    # 4. Generate map tiles
    map_id = final_img.getMapId({"palette": ["red"]})
    return map_id["tile_fetcher"].url_format


def get_rgb_tiles(year: int) -> str:
    """
    Returns a GEE tile URL for true-color RGB satellite imagery of a given year.
    Uses Sentinel-2 for 2017+ and Landsat 8 for 2013-2016.
    """
    aoi = load_boundary()

    if year >= 2017:
        # Sentinel-2: B4=Red, B3=Green, B2=Blue
        img = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
               .filterBounds(aoi)
               .filterDate(f"{year}-01-01", f"{year}-12-31")
               .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
               .median()
               .clip(aoi))

        vis = {"bands": ["B4", "B3", "B2"], "min": 0, "max": 3000}
    else:
        # Landsat 8 TOA for better visual RGB: B4=Red, B3=Green, B2=Blue
        img = (ee.ImageCollection("LANDSAT/LC08/C02/T1_TOA")
               .filterBounds(aoi)
               .filterDate(f"{year}-01-01", f"{year}-12-31")
               .filter(ee.Filter.lt("CLOUD_COVER", 20))
               .median()
               .clip(aoi))

        vis = {"bands": ["B4", "B3", "B2"], "min": 0.0, "max": 0.3}

    map_id = img.getMapId(vis)
    return map_id["tile_fetcher"].url_format


def get_change_polygons(year1: int, year2: int) -> dict:
    """
    Full pipeline: raster → vector polygons → area calc → filter > 0.5 ha.
    Returns GeoJSON FeatureCollection of the large built-up clusters.
    """
    aoi = load_boundary()
    urban_growth = _build_urban_growth(year1, year2, aoi)

    # Convert raster to polygon clusters (Part 5.1 of JS script)
    clusters = urban_growth.selfMask().reduceToVectors(
        geometry=aoi,
        scale=20,
        geometryType="polygon",
        eightConnected=True,
        labelProperty="builtup",
        reducer=ee.Reducer.countEvery(),
    )

    # Calculate area per cluster (Part: Calculate cluster area)
    def calc_area(feature):
        geom = feature.geometry().transform("EPSG:4326", 1)
        area = geom.area(maxError=1)
        return feature.set({
            "area_sqm": area,
            "area_ha": ee.Number(area).divide(10000),
        })

    clusters = clusters.map(calc_area)

    # Filter: keep only clusters > 0.5 ha
    large = clusters.filter(ee.Filter.gt("area_ha", 0.5))

    # Return as GeoJSON dict
    return large.getInfo()


def get_shapefile_url(year1: int, year2: int) -> str:
    """
    Same as get_change_polygons but returns a direct download URL
    for a .zip containing the Shapefile (.shp/.shx/.dbf/.prj).
    """
    aoi = load_boundary()
    urban_growth = _build_urban_growth(year1, year2, aoi)

    clusters = urban_growth.selfMask().reduceToVectors(
        geometry=aoi,
        scale=20,
        geometryType="polygon",
        eightConnected=True,
        labelProperty="builtup",
        reducer=ee.Reducer.countEvery(),
    )

    def calc_area(feature):
        geom = feature.geometry().transform("EPSG:4326", 1)
        area = geom.area(maxError=1)
        return feature.set({
            "area_sqm": area,
            "area_ha": ee.Number(area).divide(10000),
        })

    clusters = clusters.map(calc_area)
    large = clusters.filter(ee.Filter.gt("area_ha", 0.5))

    return large.getDownloadURL(
        filetype="SHP",
        filename=f"Mohali_Change_Detection_{year1}_{year2}",
    )
