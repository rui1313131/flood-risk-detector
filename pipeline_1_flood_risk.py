"""
Pipeline 1 — QGIS FillSink (Wang & Liu 2006) algorithm + housing data.

Maps to the original spec:
    * RichDEM / pysheds / SAGA Wang & Liu  → ``sink_detection.fill_sinks``
      (default backend: pure-Python Wang & Liu, identical math; SAGA selected
      automatically when present for QGIS bit-compat)
    * 「埋め立て後 - 元」差分 → 浸水深ラスタ → ``compute_depth`` + ``depth.tif``
    * OSMnx + 国土地理院 GML/Shapefile → ``building_loader``
    * GeoPandas で空間結合 → ``flood_analysis.find_at_risk_buildings``
    * PNG 地図 + CSV 出力      → ``flood_analysis.render_map`` / ``export_csv``

Reproducibility
---------------
Every run writes ``manifest.json`` recording: input DEM sha256, building
source descriptor, all parameters, library versions, output sha256s, run
timestamp, git commit. A re-run with the same inputs is byte-identical for
the geometric steps.

Range specification (CUI, all flags shared with pipelines 2 and 3)
    pipeline_1_flood_risk.py dem.tif
    pipeline_1_flood_risk.py --bbox "lon_min,lat_min,lon_max,lat_max"
    pipeline_1_flood_risk.py --place "箱根町" --country jp --max-side 0.05
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import rasterio

from sink_detection import detect_sinks
from building_loader import load_buildings_for_dem
from flood_analysis import (
    find_at_risk_buildings, rank_sinks_by_risk, export_csv, render_map,
    render_per_sink_maps,
)
from manifest import write_manifest
from pipeline_common import (
    add_input_flags, acquire_dem, resolve_bbox, OutLayout, auto_run_name,
)


def run(
    dem_path: str | None,
    out_dir: str,
    *,
    bbox_wgs84: tuple[float, float, float, float] | None = None,
    dem_zoom: int = 0,
    source: str = "osm",
    gsi_path: str | None = None,
    min_depth: float = 0.10,
    min_area_m2: float = 50.0,
    max_depth: float | None = None,
    max_area_m2: float | None = None,
    backend: str = "auto",
    min_slope_deg: float = 0.1,
    top: int | None = None,
    basemap: bool = False,
    skip_buildings: bool = False,
    per_sink_images: bool = True,
) -> dict:
    layout = OutLayout(out_dir).make()
    produced: list[Path] = []

    # ---- 0) Acquire DEM (file | bbox | place) ----
    dem_path, fetched_bbox, fetched_files = acquire_dem(
        dem_path, bbox_wgs84, layout.rasters, dem_zoom=dem_zoom,
    )
    produced += fetched_files

    # ---- 1) FillSink (Wang & Liu) ----
    print(f"[1/4] FillSink on {dem_path} (backend={backend}, min_slope={min_slope_deg}°)")
    sinks_gdf, rasters, sink_info = detect_sinks(
        dem_path,
        min_depth=min_depth, min_area_m2=min_area_m2,
        max_depth=max_depth, max_area_m2=max_area_m2,
        backend=backend, min_slope_deg=min_slope_deg,
        depth_out=layout.rasters / "depth.tif",
        sinks_out=layout.meta / "sinks.geojson",
    )
    produced += [layout.rasters / "depth.tif", layout.meta / "sinks.geojson"]
    print(f"      backend_used={sink_info['backend_used']}  → {len(sinks_gdf)} sink(s)")

    # ---- 2) Buildings ----
    if skip_buildings:
        print("[2/4] Buildings: skipped (--skip-buildings)")
        buildings = sinks_gdf.iloc[0:0][["geometry"]].copy()
        buildings["building_id"] = []
        buildings["source"] = []
    else:
        print(f"[2/4] Loading buildings (source={source})")
        with rasterio.open(dem_path) as src:
            dem_bounds = src.bounds
            dem_crs = src.crs
        buildings = load_buildings_for_dem(
            dem_bounds, dem_crs, source=source, gsi_path=gsi_path,
        )
        print(f"      → {len(buildings)} building footprint(s)")

    # ---- 3) Overlay + ranking ----
    print("[3/4] Spatial overlay + risk ranking")
    at_risk = find_at_risk_buildings(sinks_gdf, buildings, rasters)
    ranked = rank_sinks_by_risk(sinks_gdf, at_risk)
    if top is not None:
        ranked = ranked.head(top).copy()
        keep = set(ranked["sink_id"].tolist())
        sinks_gdf = sinks_gdf[sinks_gdf["sink_id"].isin(keep)].reset_index(drop=True)
        at_risk = at_risk[at_risk["sink_id"].isin(keep)].reset_index(drop=True)
    print(f"      → {len(at_risk)} at-risk building(s)")

    # ---- 4) Outputs (organized into sub-folders) ----
    print("[4/4] Writing outputs")
    csv_at_risk = layout.coords / "at_risk_buildings.csv"
    csv_ranked = layout.coords / "sinks_ranked.csv"
    geojson_ranked = layout.coords / "sinks_ranked.geojson"
    overview_png = layout.images / "overview.png"

    export_csv(at_risk, csv_at_risk)
    ranked.drop(columns="geometry").to_csv(csv_ranked, index=False)
    ranked.to_file(geojson_ranked, driver="GeoJSON")
    render_map(sinks_gdf, buildings, at_risk, overview_png, basemap=basemap)
    produced += [csv_at_risk, csv_ranked, geojson_ranked, overview_png]

    if per_sink_images and not at_risk.empty:
        print(f"      Rendering per-sink images → {layout.per_sink}")
        # Iterate ranked order so file numbering matches the CSV ranking
        ranked_sinks_for_imgs = ranked.copy()
        per_files = render_per_sink_maps(
            ranked_sinks_for_imgs, buildings, at_risk, rasters,
            layout.per_sink, only_with_buildings=True, basemap=basemap,
        )
        produced += per_files
        print(f"      → {len(per_files)} per-sink image(s)")

    # ---- Manifest ----
    inputs: dict = {"dem": dem_path}
    if fetched_bbox is not None:
        inputs["dem_source"] = {"type": "gsi", "zoom_used": sink_info.get("zoom"),
                                 "bbox_wgs84": list(fetched_bbox)}
    if source == "gsi" and gsi_path:
        inputs["gsi_buildings"] = gsi_path
    elif not skip_buildings and source == "osm":
        inputs["osm_buildings"] = "OpenStreetMap (Overpass) — fetched at run time"

    manifest_path = write_manifest(
        layout.meta,
        inputs=inputs,
        parameters={
            "pipeline": "1_flood_risk",
            "backend_requested": backend,
            "backend_used": sink_info["backend_used"],
            "min_slope_deg": min_slope_deg,
            "min_depth": min_depth, "min_area_m2": min_area_m2,
            "max_depth": max_depth, "max_area_m2": max_area_m2,
            "top": top,
            "building_source": None if skip_buildings else source,
            "per_sink_images": per_sink_images,
        },
        output_files=produced,
        extra={"summary": {
            "n_sinks_total": int(sink_info["n_sinks"]),
            "n_sinks_kept": int(len(sinks_gdf)),
            "n_buildings": int(len(buildings)),
            "n_at_risk": int(len(at_risk)),
            "cell_size_m": sink_info["cell_size_m"],
            "crs": sink_info["crs"],
        }},
    )
    print(f"      Manifest: {manifest_path}")
    print(f"\nResults laid out under: {layout.root}")
    print(f"  coordinates/   {csv_at_risk.name}, {csv_ranked.name}, {geojson_ranked.name}")
    print(f"  images/        overview.png + per_sink/")
    print(f"  rasters/       dem_utm.tif, depth.tif")
    print(f"  meta/          manifest.json, sinks.geojson")
    print("Done.")
    return json.loads(Path(manifest_path).read_text())


def main() -> None:
    p = argparse.ArgumentParser(
        description="Pipeline 1 — FillSink (Wang & Liu) + housing data → at-risk CSV/PNG.",
    )
    add_input_flags(p)
    p.add_argument("--source", choices=("osm", "gsi"), default="osm")
    p.add_argument("--gsi-path", help="Path to GSI GML/Shapefile (when --source gsi).")
    p.add_argument("--backend", choices=("auto", "richdem", "saga", "pure"), default="auto")
    p.add_argument("--min-slope", type=float, default=0.1)
    p.add_argument("--min-depth", type=float, default=0.10)
    p.add_argument("--min-area", type=float, default=50.0)
    p.add_argument("--max-depth", type=float, default=None)
    p.add_argument("--max-area", type=float, default=None)
    p.add_argument("--top", type=int, default=None)
    p.add_argument("--basemap", action="store_true")
    p.add_argument("--skip-buildings", action="store_true")
    p.add_argument("--no-per-sink-images", action="store_true",
                   help="Skip the per-sink PNG rendering (overview only).")
    args = p.parse_args()

    bbox = resolve_bbox(args)
    out_dir = args.out_dir or str(Path("results") / auto_run_name(args, bbox))

    run(
        args.dem, out_dir,
        bbox_wgs84=bbox, dem_zoom=args.dem_zoom,
        source=args.source, gsi_path=args.gsi_path,
        min_depth=args.min_depth, min_area_m2=args.min_area,
        max_depth=args.max_depth, max_area_m2=args.max_area,
        backend=args.backend, min_slope_deg=args.min_slope,
        top=args.top, basemap=args.basemap,
        skip_buildings=args.skip_buildings,
        per_sink_images=not args.no_per_sink_images,
    )


if __name__ == "__main__":
    main()
