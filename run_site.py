"""
End-to-end single-site runner: take one (lat, lon) and produce
flood-risk PNGs + report.

    python3 run_site.py <lat> <lon> [--label "..."] [--slug ...]
                                    [--half-side 0.02] [--skip-basemap]

Pipeline (one shot, idempotent within prefetch/<slug>/):
  1. DEM (GSI dem5a_png z15) + OSM building footprints  → prefetch/<slug>/
  2. GSI 標準地図 basemap tiles (z17)                   → basemap_utm.tif
  3. Sink detection (SAGA Wang & Liu) + at-risk buildings + PNGs + report.txt

Slug の決まり方 (優先順):
  1. --slug "..." を明示指定  → そのまま使う
  2. --label "..." を指定     → ラベルから日本語フォルダ名を生成
                                 例: "茨城県日立市久慈町三丁目"
  3. どちらも無し              → 座標ベース  例: site_36p497N_140p613E
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Force UTF-8 on Windows consoles so non-ASCII labels and ✓ marks don't crash
# (default cp932/Shift-JIS can't encode U+2713 etc.).
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from prefetch_test_sites import prefetch_site
from prefetch_basemaps import BASEMAP_ZOOM
from basemap_fetcher import fetch_gsi_basemap
from analyze_sites import analyse_site
from pipeline_common import safe_dir_name


def _coord_slug(lat: float, lon: float) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return (f"site_{abs(lat):.3f}{ns}_{abs(lon):.3f}{ew}"
            .replace(".", "p"))


def _auto_slug(lat: float, lon: float, label: str | None) -> str:
    """ラベルがあれば日本語そのままフォルダ名へ。無ければ座標ベース。"""
    if label:
        return safe_dir_name(label)
    return _coord_slug(lat, lon)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run flood-risk pipeline for a single (lat, lon).")
    ap.add_argument("lat", type=float, help="Latitude (WGS84, e.g. 36.496985)")
    ap.add_argument("lon", type=float, help="Longitude (WGS84, e.g. 140.613220)")
    ap.add_argument("--label", default=None,
                    help="Display label (default: '<lat>,<lon>')")
    ap.add_argument("--slug", default=None,
                    help="Output folder name under prefetch/ "
                         "(default: auto-generated from coords)")
    ap.add_argument("--half-side", type=float, default=0.02,
                    help="Half bbox side in degrees (default 0.02 ≈ 4.4 km)")
    ap.add_argument("--skip-basemap", action="store_true",
                    help="Skip GSI 標準地図 download (overlay-only PNG)")
    ap.add_argument("--scalebar-m", type=float, default=None,
                    help="Force scalebar length in metres (e.g. 100). "
                         "Default: auto (≈ 12%% of figure width, snapped to "
                         "{10,20,50,100,200,500,1000,...} m)")
    args = ap.parse_args()

    slug = args.slug or _auto_slug(args.lat, args.lon, args.label)
    label = args.label or f"{args.lat:.6f}°N, {args.lon:.6f}°E"

    project_root = Path(__file__).resolve().parent
    root = project_root / "prefetch"
    root.mkdir(exist_ok=True)
    site_dir = root / slug

    # ---- 1. DEM + OSM buildings (skip if site.json already there) ----
    if (site_dir / "site.json").exists():
        print(f"=== {slug}: site.json exists → reuse prefetched DEM/OSM")
        info = json.loads((site_dir / "site.json").read_text(encoding="utf-8"))
    else:
        # prefetch_test_sites.HALF_SIDE_DEG is module-level; override here
        # by patching the module constant before calling prefetch_site so the
        # bbox math respects --half-side.
        import prefetch_test_sites as _pts
        _pts.HALF_SIDE_DEG = args.half_side
        site = {"slug": slug, "label": label,
                "lat": args.lat, "lon": args.lon}
        info = prefetch_site(site, root)

    # ---- 2. GSI 標準地図 basemap ----
    basemap_path = site_dir / "basemap_utm.tif"
    if args.skip_basemap:
        print(f"=== {slug}: --skip-basemap, overlay-only render")
    elif basemap_path.exists():
        print(f"=== {slug}: basemap_utm.tif exists → reuse")
    else:
        print(f"=== {slug}: fetching GSI 標準地図 (z{BASEMAP_ZOOM})")
        fetch_gsi_basemap(tuple(info["bbox_wgs84"]), site_dir,
                          zoom=BASEMAP_ZOOM)

    # ---- 3. Analysis (sinks + at-risk buildings + PNGs + report.txt) ----
    if args.scalebar_m is not None:
        import analyze_sites as _as
        _as.SCALEBAR_M_OVERRIDE = float(args.scalebar_m)
    result = analyse_site(site_dir)

    print("\n--------------------------------------------------------------")
    print(f"DONE: {slug}  ({label})")
    print(f"  検出窪地     : {result['n_sinks_total']} 件 "
          f"(対象 {result['n_target_sinks']} 件 = 住宅含む)")
    print(f"  建物         : {result['n_buildings']:,} 棟 "
          f"(浸水リスク {result['n_at_risk']:,} 棟)")
    print(f"  元画像   : {result['png_classic_with_basemap']}")
    print(f"  検出後   : {result['png_layered_with_basemap']}")
    print(f"  Report   : {result['report']}")


if __name__ == "__main__":
    main()
