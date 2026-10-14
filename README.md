# Mohali Change Detection & Under-Assessment Service

Standalone reference application for on-the-fly urban growth detection and property tax under-assessment identification for Mohali, Punjab.

Built as a functional reference for integrating into the **Punjab Property Tax Dashboard**.

---

## What It Does

1. **High-Resolution Swipe Map:**
   - Lets officers visually compare historical vs. current satellite imagery side-by-side using **Esri World Imagery Wayback** (0.3m – 0.5m sub-metre resolution).
   - Allows zooming directly into individual plots to inspect roofs, boundary walls, and roads with crystal clarity.

2. **Google Earth Engine (GEE) Built-Up Detection:**
   - Runs a spectral change detection algorithm (NDBI built-up index increase + NDVI vegetation decrease) over the municipal area between any baseline year (e.g. 2014) and comparison year (e.g. 2025/2026).
   - Outputs raster overlays and vectorized polygon clusters (> 0.5 ha).

3. **Focused Under-Assessment Identification:**
   - Evaluates properties in the tax net (`IN_TAX_NET`) that are recorded in the GIS survey or mSeva as **`Open / Vacant Land`**.
   - Intersects parcel centroids with satellite change polygons to detect parcels that are paying vacant plot tax (or zero tax) while satellite confirms new construction.
   - Flags them with full owner details, ward, mobile, demand, and map markers.
   - Provides 1-click CSV export of the flagged properties for field enforcement.

---

## Project Structure

```
Mohali Change Detection/
├── backend/
│   ├── main.py                  ← FastAPI entry point and routing
│   ├── ee_service.py            ← Google Earth Engine algorithms (NDBI, NDVI, clustering)
│   ├── imagery_service.py       ← Sub-metre Esri Wayback imagery tile catalog & provider
│   ├── defaulter_service.py     ← Focused under-assessment spatial join engine
│   ├── requirements.txt         ← Python dependencies
│   ├── tile_cache.db            ← Local SQLite tile and polygon cache
│   └── boundaries/
│       └── Mohali.geojson       ← Municipal boundary polygon
│
├── frontend/
│   ├── index.html               ← Interactive Leaflet side-by-side comparison UI
│   └── Mohali_boundary.geojson  ← Boundary layer for map display
│
└── README.md
```

---

## How to Run Locally

### Prerequisites
- Python 3.10+
- Google Earth Engine access (authenticated via `earthengine authenticate` or a GCP service account).

### Terminal 1: Backend (FastAPI on Port 8000)

```powershell
cd "Mohali Change Detection\backend"
pip install -r requirements.txt
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

### Terminal 2: Frontend (HTTP on Port 5500)

```powershell
cd "Mohali Change Detection\frontend"
python -m http.server 5500
```

Open your browser to: **`http://localhost:5500`**

---

## Configuration & Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `GEE_PROJECT_ID` | `change-detection-494607` | Google Cloud / Earth Engine Project ID |
| `GEE_SERVICE_ACCOUNT_KEY_PATH` | `None` (uses ADC or cached credentials) | Path to GCP Service Account JSON key for server deployment |
| `MOHALI_DATA_DIR` or `DATA_DIR` | Local workspace path | Directory containing `Mohali_Defaulters.csv` and `mseva.csv` |
| `DEFAULTERS_CSV_PATH` | `$DATA_DIR/Mohali_Defaulters.csv` | Path to master Defaulters CSV |
| `MSEVA_CSV_PATH` | `$DATA_DIR/mseva.csv` | Path to mSeva CSV ledger |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Service health status and version |
| `POST` | `/rgb-tiles` | High-resolution satellite tile URL for a selected year (Esri Wayback 0.3m default) |
| `GET` | `/imagery-captures` | Available sub-metre imagery releases and real capture dates over Mohali |
| `POST` | `/change-tiles` | GEE raster tile URL (red built-up change overlay) |
| `POST` | `/change-polygons` | GeoJSON FeatureCollection of detected new construction clusters |
| `POST` | `/change-shapefile` | Direct .zip download URL for Shapefile of change clusters |
| `POST` | `/identify-defaulters` | Intersects change polygons with `IN_TAX_NET` vacant parcels → returns under-assessed properties |
| `POST` | `/clear-cache` | Clears cached tile URLs in SQLite |

---

## Developer Integration Guide (For Dashboard Integration)

If you are integrating this logic into the PostgreSQL / PostGIS backend of the Punjab Property Tax Dashboard:

### 1. Earth Engine Integration (`ee_service.py`)
- The GEE logic in `ee_service.py` is stateless.
- Call `ee_service.get_change_polygons(year1, year2)` to receive the GeoJSON clusters directly.
- Insert the resulting polygons into your existing PostgreSQL table: `public.satellite_change_polygons`.

### 2. Under-Assessment Query (`defaulter_service.py`)
In the PostgreSQL database, the spatial intersection in `defaulter_service.py` maps to a single PostGIS query against your existing `property_current` table:

```sql
SELECT 
    p.gis_uid,
    p.ward_id,
    p.owner_name,
    p.contact_mobile,
    p.property_type AS gis_property_type,
    p.property_usage,
    p.demand_amount,
    p.collected_amount,
    p.outstanding_amount,
    p.centroid_lng,
    p.centroid_lat,
    c.change_id,
    c.area_sqm
FROM public.property_current p
JOIN public.satellite_change_polygons c 
  ON ST_Within(p.geometry, c.geometry)
WHERE p.tax_status = 'IN_TAX_NET'
  AND (p.property_type = 'Open / Vacant Land' OR p.property_usage IS NULL);
```

### 3. Visual Verification Imagery (`imagery_service.py`)
- The sub-metre tiles are generated directly from the public Esri Wayback WMTS endpoints.
- No API keys or rate-limited subscriptions are required.
- The catalog in `imagery_service.py` provides verified capture dates over Mohali (2014, 2016, 2017, 2018, 2019, 2021, 2023, 2024, 2025).
