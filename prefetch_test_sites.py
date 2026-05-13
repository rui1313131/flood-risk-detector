"""
Pre-fetch DEM + OSM building footprints for the 3 user-specified test sites.

Each site gets a tight bbox (half_side = 0.02° → ~4.4 km × 3.6 km at mid-Japan
latitudes) so the download stays light: ~16-25 GSI z14 tiles + Overpass query
per site. After this script finishes, pipeline_1 can be run pointing at the
local DEM and OSM cache will be re-used (osmnx default cache → ./cache).

Run:
    python3 prefetch_test_sites.py

Outputs:
    prefetch/<site_slug>/dem_utm.tif        (UTM-reprojected DEM)
    prefetch/<site_slug>/dem_mercator.tif   (intermediate Web-Mercator mosaic)
    prefetch/<site_slug>/buildings.geojson  (OSM footprints, DEM CRS)
    prefetch/<site_slug>/site.json          (record of inputs + bbox)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import rasterio

from dem_fetcher import fetch_dem_for_bbox
from building_loader import load_buildings_for_dem


HALF_SIDE_DEG = 0.02   # full bbox = 0.04° × 0.04°  ≈ 4.4 km × 3.6 km
DEM_LAYER = "dem5a_png"  # GSI 5 m photogrammetric DEM (urban / lowland)
DEM_ZOOM = 15            # max for dem5a_png — UTM pixel ≈ 2.4 m at mid-Japan

SITES = [
    {
        "slug": "hitachi_kuji",
        "label": "茨城県日立市久慈町三丁目",
        "lat": 36.496985,
        "lon": 140.613220,
    },
    {
        "slug": "sakata_takasago",
        "label": "山形県酒田市高砂三丁目",
        "lat": 38.944065,
        "lon": 139.825337,
    },
    {
        "slug": "kirishima_hayato",
        "label": "鹿児島県霧島市隼人町神宮六丁目",
        "lat": 31.750034,
        "lon": 130.745362,
    },
]


def site_bbox(lat: float, lon: float, half: float | None = None):
    # ``HALF_SIDE_DEG`` を関数呼び出し時に参照する (= モジュール変数を後から
    # 書き換えた場合でも反映される)。Python のデフォルト引数は def 時に
    # 評価されるため、`half: float = HALF_SIDE_DEG` だと run_site.py が
    # _pts.HALF_SIDE_DEG = ... と書き換えても無視されてしまう。
    if half is None:
        half = HALF_SIDE_DEG
    return (lon - half, lat - half, lon + half, lat + half)


def prefetch_site(site: dict, root: Path) -> dict:
    out_dir = root / site["slug"]
    out_dir.mkdir(parents=True, exist_ok=True)

    bbox = site_bbox(site["lat"], site["lon"])
    print(f"\n=== {site['slug']}  ({site['label']}) ===")
    print(f"   center = ({site['lat']}, {site['lon']})")
    print(f"   bbox   = {bbox}")

    # ---- DEM ----
    t0 = time.time()
    dem_path = fetch_dem_for_bbox(
        bbox, out_dir, zoom=DEM_ZOOM, source="gsi", gsi_layer=DEM_LAYER,
    )
    dem_secs = time.time() - t0
    dem_size_mb = dem_path.stat().st_size / 1e6
    print(f"   DEM   ✓ {dem_path.name}  ({dem_size_mb:.2f} MB, {dem_secs:.1f}s)")

    # Sanity: open and check stats
    with rasterio.open(dem_path) as src:
        arr = src.read(1, masked=True)
        dem_crs = str(src.crs)
        dem_bounds = src.bounds
    if arr.count() == 0:
        raise RuntimeError(f"DEM has no valid pixels: {dem_path}")
    print(f"   DEM   range = {float(arr.min()):.1f}..{float(arr.max()):.1f} m"
          f"  ({arr.count()} valid px)")

    # ---- Buildings (OSM) — populates osmnx cache for re-use ----
    t0 = time.time()
    buildings = load_buildings_for_dem(dem_bounds, dem_crs, source="osm")
    bld_secs = time.time() - t0
    bld_path = out_dir / "buildings.geojson"
    if len(buildings) > 0:
        buildings.to_file(bld_path, driver="GeoJSON")
        bld_size_kb = bld_path.stat().st_size / 1e3
        print(f"   OSM   ✓ {len(buildings)} buildings → {bld_path.name}"
              f"  ({bld_size_kb:.1f} KB, {bld_secs:.1f}s)")
    else:
        print(f"   OSM   ⚠ 0 buildings returned ({bld_secs:.1f}s) — check bbox.")

    # ---- Record metadata ----
    info = {
        "slug": site["slug"],
        "label": site["label"],
        "center_lat": site["lat"],
        "center_lon": site["lon"],
        "half_side_deg": HALF_SIDE_DEG,
        "bbox_wgs84": list(bbox),
        "dem_layer": DEM_LAYER,
        "dem_zoom": DEM_ZOOM,
        "dem_path": str(dem_path.relative_to(root.parent)),
        "dem_crs": dem_crs,
        "dem_bounds_native": list(dem_bounds),
        "n_buildings_cached": int(len(buildings)),
        "elev_min_m": float(arr.min()),
        "elev_max_m": float(arr.max()),
    }
    (out_dir / "site.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    return info


def main() -> None:
    project_root = Path(__file__).resolve().parent
    root = project_root / "prefetch"
    root.mkdir(exist_ok=True)

    summary = []
    for site in SITES:
        try:
            summary.append(prefetch_site(site, root))
        except Exception as e:
            print(f"   ✗ FAILED for {site['slug']}: {e}")
            summary.append({"slug": site["slug"], "error": str(e)})

    (root / "prefetch_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    print(f"\nSummary written: {root / 'prefetch_summary.json'}")


if __name__ == "__main__":
    main()
