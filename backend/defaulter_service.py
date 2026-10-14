"""
defaulter_service.py — Focused Under-Assessment Detection.

Loads the audited master register from Mohali_Defaulters.csv and cross-references
with mseva.csv to identify IN_TAX_NET properties recorded as 'Open / Vacant Land'
(in GIS or mSeva) that have new satellite-detected construction.

Key Design Decisions:
1. Scenario 1 (IN_TAX_NET + Vacant Land):
   - Only considers properties in the tax net.
   - Evaluates BOTH GIS ('property_type' == 'Open / Vacant Land') and mSeva
     ('Property_Type' == 'Vacant Land') to catch under-reported accounts.
2. Scenario 2 (SUSPECTED):
   - Excluded: SUSPECTED parcels already have no tax record and are already
     targeted for field inspection under the master Defaulters register.
3. Scenario 3 (EXEMPT):
   - Excluded: Legally tax-exempt properties incur no tax liability.
4. Ghost Properties:
   - Excluded: Polygons outside the surveyed parcel grid require cadastral demarcation.
"""

from __future__ import annotations

from pathlib import Path
from collections import Counter
import geopandas as gpd
import pandas as pd
from shapely.geometry import shape, Point

import os
import json

# ── Data Paths ─────────────────────────────────────────────────────────────────

# Supports configuration via environment variable for portable deployment.
# Falls back to local workspace path if not set.
DEFAULT_DATA_DIR = Path(
    r"C:\Users\RithvikLokesh\Airawat Research Foundation-20260316T051131Z-3-001"
    r"\Airawat Research Foundation\AI PGR\Property Tax\Mohali\MohaliDataset"
)
DATA_DIR = Path(os.environ.get("MOHALI_DATA_DIR", os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)))

DEFAULTERS_CSV = Path(os.environ.get("DEFAULTERS_CSV_PATH", DATA_DIR / "Mohali_Defaulters.csv"))
DEFAULTERS_GEOJSON = Path(os.environ.get("DEFAULTERS_GEOJSON_PATH", DATA_DIR / "Mohali_Defaulters.geojson"))
MSEVA_CSV = Path(os.environ.get("MSEVA_CSV_PATH", DATA_DIR / "mseva.csv"))

# ── Caching ────────────────────────────────────────────────────────────────────

_mseva_cache: dict | None = None
_candidate_cache: gpd.GeoDataFrame | None = None
_all_parcels_gdf: gpd.GeoDataFrame | None = None


def _load_mseva_lookup() -> dict:
    """
    Load MSEVA CSV and return a dict keyed by surveyid with financial
    and billing property types. Multi-account properties are aggregated.
    """
    global _mseva_cache
    if _mseva_cache is not None:
        return _mseva_cache

    df = pd.read_csv(
        str(MSEVA_CSV),
        encoding="latin1",
        usecols=[
            "surveyid", "total_demand_amount", "total_collected_amount",
            "total_outstanding_amount", "Property_Type",
        ],
        dtype=str,
    )
    df = df.dropna(subset=["surveyid"])
    df["surveyid"] = (
        df["surveyid"]
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )
    for col in ("total_demand_amount", "total_collected_amount", "total_outstanding_amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["Property_Type"] = df["Property_Type"].fillna("").astype(str)

    agg = df.groupby("surveyid").agg(
        demand=("total_demand_amount", "sum"),
        collected=("total_collected_amount", "sum"),
        outstanding=("total_outstanding_amount", "sum"),
        mseva_type=(
            "Property_Type",
            lambda s: ", ".join(sorted(set(x.strip() for x in s if x.strip()))),
        ),
    )
    _mseva_cache = agg.to_dict(orient="index")
    return _mseva_cache


def load_candidate_parcels() -> gpd.GeoDataFrame:
    """
    Load candidate parcels directly from Mohali_Defaulters.csv.
    
    Candidate Pool for Satellite Change Detection:
      1. Set 2 Candidates: tax_status == 'SUSPECTED' and present_status in
         ('VACANT', 'GREEN_BELT', 'PARK', 'OTHER') or property_type == 'Open / Vacant Land'
      2. Under-assessed Candidates: tax_status == 'IN_TAX_NET' and (property_type == 'Open / Vacant Land'
         or mseva_type contains 'Vacant Land' or present_status != 'BUILT_UP')
    """
    global _candidate_cache
    if _candidate_cache is not None:
        return _candidate_cache

    print("[defaulter] Loading candidate parcels from Mohali_Defaulters.csv...")
    df = pd.read_csv(str(DEFAULTERS_CSV), dtype=str)
    total_parcels = len(df)
    legacy_count = int(((df["tax_status"] == "SUSPECTED") & (df["present_status"] == "BUILT_UP")).sum())

    # Attach mSeva billing and financial data
    mseva = _load_mseva_lookup()
    df["UID_str"] = (
        df["gis_uid"]
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )

    mseva_df = pd.DataFrame([
        {
            "UID_str": uid,
            "demand": v["demand"],
            "collected": v["collected"],
            "outstanding": v["outstanding"],
            "mseva_type": v["mseva_type"],
        }
        for uid, v in mseva.items()
    ])
    df = df.merge(mseva_df, on="UID_str", how="left")
    df["demand"] = df["demand"].fillna(0.0)
    df["collected"] = df["collected"].fillna(0.0)
    df["outstanding"] = df["outstanding"].fillna(0.0)
    df["mseva_type"] = df["mseva_type"].fillna("")

    # 2013 GIS Land Usage for Vacant Land (strictly non-exempt)
    is_vacant_gis = (df["property_type"] == "Open / Vacant Land") | (df["present_status"] == "VACANT")
    is_vacant_mseva = df["mseva_type"].str.contains(r"\bVacant Land\b", case=False, na=False)

    # Strictly require that the property is TAXABLE (exclude statutory EXEMPT)
    is_taxable = df["tax_status"] != "EXEMPT"

    # ── Candidate Pools for Satellite Change Detection ─────────────
    # Pool A: SUSPECTED vacant (Candidates for Set 2: New Construction Defaulters)
    # Vacant in 2013 GIS survey, never registered on mSeva. When satellite detects built-up,
    # they are unregistered property tax evaders.
    susp_vacant = df[(df["tax_status"] == "SUSPECTED") & is_vacant_gis & is_taxable].copy()

    # Pool B: IN_TAX_NET vacant (Candidates for Under-Assessed Properties)
    # Registered on mSeva as Vacant / Zero-Tax Land (or vacant in 2013 GIS), but when satellite detects built-up,
    # they are paying ₹0 when they should be paying built-up tax!
    in_tax_vacant = df[(df["tax_status"] == "IN_TAX_NET") & (is_vacant_gis | is_vacant_mseva) & is_taxable].copy()

    candidates = pd.concat([susp_vacant, in_tax_vacant], ignore_index=True)

    candidates["lat"] = pd.to_numeric(candidates["latitude"], errors="coerce")
    candidates["lon"] = pd.to_numeric(candidates["longitude"], errors="coerce")
    candidates = candidates.dropna(subset=["lat", "lon"])

    cand_gdf = gpd.GeoDataFrame(
        candidates,
        geometry=[Point(xy) for xy in zip(candidates["lon"], candidates["lat"])],
        crs="EPSG:4326",
    )
    cand_gdf.attrs["total_parcels"] = total_parcels
    cand_gdf.attrs["total_in_tax_net"] = int((df["tax_status"] == "IN_TAX_NET").sum())
    cand_gdf.attrs["total_suspected"] = int((df["tax_status"] == "SUSPECTED").sum())
    cand_gdf.attrs["legacy_built_up_defaulters"] = legacy_count
    cand_gdf.attrs["candidate_pool_size"] = len(cand_gdf)

    print(
        f"[defaulter] {total_parcels:,} total parcels -> "
        f"Legacy Defaulters (Set 1): {legacy_count:,} -> "
        f"Change Detection Candidates: {len(cand_gdf):,} vacant plots "
        f"({len(susp_vacant):,} unregistered + {len(in_tax_vacant):,} zero-tax registered)"
    )

    _candidate_cache = cand_gdf
    return cand_gdf


# ── Core Spatial Analysis ──────────────────────────────────────────────────────

def identify_under_assessed(change_geojson: dict) -> dict:
    """
    Focused analysis: intersect satellite change polygons with vacant parcels
    to identify:
      1. New Construction Defaulters (Set 2 - Orange): Unregistered on mSeva with new built-up.
      2. Under-Assessed (In Tax Net - Yellow): Registered as Vacant/Zero Tax Land with new built-up.
    """
    candidates = load_candidate_parcels()

    features = change_geojson.get("features", [])
    if not features:
        return _empty_result(candidates)

    # Build change polygon GeoDataFrame
    change_records = []
    for i, feat in enumerate(features):
        geom = shape(feat["geometry"])
        props = feat.get("properties", {})
        change_records.append({
            "change_id": i,
            "geometry": geom,
            "area_sqm": props.get("area_sqm", 0),
            "area_ha": props.get("area_ha", 0),
        })

    change_gdf = gpd.GeoDataFrame(change_records, crs="EPSG:4326")

    # Spatial join: candidate points within change polygons
    joined = gpd.sjoin(
        candidates, change_gdf, how="inner", predicate="within"
    ).drop_duplicates(subset="gis_uid")

    def _s(v) -> str:
        return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()

    def _f(v) -> float:
        try:
            val = float(v)
            return 0.0 if pd.isna(val) else round(val, 2)
        except (TypeError, ValueError):
            return 0.0

    defaulters = []
    for _, row in joined.iterrows():
        tax_status = _s(row.get("tax_status"))
        gis_ptype = _s(row.get("property_type"))
        pres_st = _s(row.get("present_status"))
        mseva_type = _s(row.get("mseva_type"))

        if tax_status == "SUSPECTED":
            category = "NEW_CONSTRUCTION_DEFAULTER"
            signal = "NEW_CONSTRUCTION"
            color = "#f97316"
            reason = (
                f'New Construction Defaulter (Set 2): Recorded as {pres_st or "Vacant"} in 2013 survey, '
                f'never registered on mSeva. Satellite detected new built-up construction.'
            )
        else:
            category = "UNDER_ASSESSED"
            signal = "UNDER_ASSESSED"
            color = "#eab308"
            reason = (
                f'Under-Assessed (In Tax Net): Registered on mSeva as {mseva_type or gis_ptype or "Vacant Land"} (Zero Tax Land), '
                f'but satellite detected new built-up construction.'
            )

        defaulters.append({
            "uid": _s(row["gis_uid"]),
            "ward": _s(row.get("ward_id")),
            "owner": _s(row.get("gis_owner_name")),
            "mobile": _s(row.get("gis_mobile")),
            "locality": _s(row.get("gis_locality")),
            "gis_usage": gis_ptype,
            "present_status": pres_st,
            "tax_status": tax_status,
            "category": category,
            "color": color,
            "mseva_type": mseva_type,
            "signal": signal,
            "current_demand": _f(row.get("demand")),
            "collected": _f(row.get("collected")),
            "outstanding": _f(row.get("outstanding")),
            "centroid_lng": round(row.geometry.x, 6),
            "centroid_lat": round(row.geometry.y, 6),
            "change_polygon_id": int(row["change_id"]),
            "reason": reason,
        })

    usage_counts = Counter(d["gis_usage"] for d in defaulters)
    ward_counts = Counter(d["ward"] for d in defaulters)
    new_def = [d for d in defaulters if d["category"] == "NEW_CONSTRUCTION_DEFAULTER"]
    under_assessed = [d for d in defaulters if d["category"] == "UNDER_ASSESSED"]

    return {
        "defaulters": defaulters,
        "under_assessed": defaulters,  # Kept for frontend backwards compatibility
        "summary": {
            "total_new_construction_defaulters": len(new_def),
            "total_legacy_defaulters": candidates.attrs.get("legacy_built_up_defaulters", 10858),
            "total_under_assessed": len(under_assessed),
            "total_flagged": len(defaulters),
            "total_candidates_checked": len(candidates),
            "total_in_tax_net": candidates.attrs.get("total_in_tax_net", 0),
            "total_suspected": candidates.attrs.get("total_suspected", 0),
            "total_parcels": candidates.attrs.get("total_parcels", 0),
            "total_change_polygons": len(features),
            "current_demand_total": round(sum(d["current_demand"] for d in defaulters), 2),
            "by_usage": dict(usage_counts),
            "by_ward": dict(sorted(ward_counts.items())),
        },
    }


def _empty_result(candidates: gpd.GeoDataFrame) -> dict:
    return {
        "defaulters": [],
        "under_assessed": [],
        "summary": {
            "total_new_construction_defaulters": 0,
            "total_legacy_defaulters": candidates.attrs.get("legacy_built_up_defaulters", 10858),
            "total_under_assessed": 0,
            "total_flagged": 0,
            "total_candidates_checked": len(candidates),
            "total_in_tax_net": candidates.attrs.get("total_in_tax_net", 0),
            "total_suspected": candidates.attrs.get("total_suspected", 0),
            "total_parcels": candidates.attrs.get("total_parcels", 0),
            "total_change_polygons": 0,
            "current_demand_total": 0.0,
            "by_usage": {},
            "by_ward": {},
        },
    }


# ── Approach A: Change-Intersected Parcels Layer ───────────────────────────────

def load_all_parcels() -> gpd.GeoDataFrame:
    """
    Load all GIS survey parcels with full polygon geometries and pre-computed centroids.
    Cached in memory for instant spatial operations.
    """
    global _all_parcels_gdf
    if _all_parcels_gdf is not None:
        return _all_parcels_gdf

    print("[defaulter] Loading all survey parcels for change overlay...")
    if DEFAULTERS_GEOJSON.exists():
        gdf = gpd.read_file(str(DEFAULTERS_GEOJSON))
    else:
        shp_path = DATA_DIR / "mohaligis.shp"
        gdf = gpd.read_file(str(shp_path)).to_crs(epsg=4326)

    # Precompute centroids
    gdf["centroid"] = gdf.geometry.centroid
    _all_parcels_gdf = gdf
    print(f"[defaulter] Cached {len(gdf):,} survey parcel polygons in memory.")
    return _all_parcels_gdf


def get_change_parcels_geojson(change_geojson: dict) -> str:
    """
    Approach A: Extract all GIS survey parcels that intersect the detected
    change polygons, with tax status classification and color coding:
      - NEW_CONSTRUCTION_DEFAULTER (Orange: #f97316)
      - LEGACY_BUILT_UP_DEFAULTER  (Red:    #ef4444)
      - UNDER_ASSESSED             (Yellow: #eab308)
      - IN_TAX_NET_BUILT_UP        (Green:  #22c55e)
      - EXEMPT                     (Gray:   #94a3b8)

    Returns a GeoJSON FeatureCollection string.
    """
    features = change_geojson.get("features", [])
    if not features:
        return json.dumps({"type": "FeatureCollection", "features": []})

    all_parcels = load_all_parcels()
    mseva = _load_mseva_lookup()

    change_geoms = [shape(f["geometry"]) for f in features]
    change_gdf = gpd.GeoDataFrame(
        {"change_id": range(len(change_geoms))},
        geometry=change_geoms,
        crs="EPSG:4326",
    )

    # Fast spatial join on centroids
    pts_gdf = all_parcels.copy().set_geometry("centroid")
    joined = gpd.sjoin(
        pts_gdf, change_gdf, how="inner", predicate="within"
    ).drop_duplicates(subset="gis_uid")

    if joined.empty:
        return json.dumps({"type": "FeatureCollection", "features": []})

    matched = all_parcels.loc[joined.index].copy()
    # Light simplification (~1m) keeps GeoJSON compact and transfer fast
    matched["geometry"] = matched["geometry"].simplify(0.00001)

    geojson_features = []
    for _, row in matched.iterrows():
        uid = str(row.get("gis_uid", "")).strip().replace(".0", "")
        tax_status = str(row.get("tax_status", "")).strip()
        ptype = str(row.get("property_type", "")).strip()
        pusage = str(row.get("property_usage", "")).strip()
        pres_st = str(row.get("present_status", "")).strip()
        owner = str(row.get("gis_owner_name", "")).strip()
        mobile = str(row.get("gis_mobile", "")).strip()
        ward = str(row.get("ward_id", "")).strip()
        locality = str(row.get("gis_locality", "")).strip()

        mseva_info = mseva.get(uid, {})
        mseva_type = mseva_info.get("mseva_type", "")

        is_vacant_gis = (ptype == "Open / Vacant Land") or (pres_st == "VACANT")
        is_vacant_mseva = "vacant" in mseva_type.lower()

        if tax_status == "IN_TAX_NET" and (is_vacant_gis or is_vacant_mseva):
            category = "UNDER_ASSESSED"
            color = "#eab308"
            reason = f"Under-Assessed: Registered on mSeva as {mseva_type or ptype or 'Vacant Land'} (Zero Tax Land), but satellite detected new built-up construction."
        elif tax_status == "SUSPECTED":
            if is_vacant_gis:
                category = "NEW_CONSTRUCTION_DEFAULTER"
                color = "#f97316"
                reason = f"New Construction Defaulter (Set 2): Vacant plot in 2013 survey ({ptype}), never registered on mSeva. Satellite detected new built-up construction."
            else:
                category = "LEGACY_BUILT_UP_DEFAULTER"
                color = "#ef4444"
                reason = f"Legacy Built-Up Defaulter (Set 1): Physical structure ({ptype or 'Built-Up'}) already existed in 2013 survey, never registered in mSeva."
        elif tax_status == "IN_TAX_NET":
            category = "IN_TAX_NET_BUILT_UP"
            color = "#22c55e"
            reason = f"Compliant: In tax net and paying property tax as {mseva_type or ptype or 'Built-Up'}."
        elif tax_status == "EXEMPT":
            category = "EXEMPT"
            color = "#94a3b8"
            reason = "Exempt Property: Marked as legally exempt from municipal property tax."
        else:
            category = "OTHER"
            color = "#38bdf8"
            reason = f"Tax Status: {tax_status}"

        geom_json = row.geometry.__geo_interface__

        geojson_features.append({
            "type": "Feature",
            "geometry": geom_json,
            "properties": {
                "uid": uid,
                "owner": owner,
                "mobile": mobile,
                "ward": ward,
                "locality": locality,
                "property_type": ptype,
                "property_usage": pusage,
                "present_status": pres_st,
                "tax_status": tax_status,
                "category": category,
                "color": color,
                "reason": reason,
                "mseva_type": mseva_type,
            }
        })

    return json.dumps({
        "type": "FeatureCollection",
        "features": geojson_features
    })

