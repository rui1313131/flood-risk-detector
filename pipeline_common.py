"""
Shared CLI / DEM-input helpers used by pipelines 1, 2, 3.

Every pipeline accepts the same range-specification surface:

    * positional ``dem``       — path to a local GeoTIFF DEM
    * ``--bbox`` "minx,miny,maxx,maxy" — WGS84 bbox; DEM is auto-fetched (GSI)
    * ``--place`` "<name>"     — geocoded via Nominatim → bbox

This module factors that out so the three entry points stay short and
the user always has the same flags available regardless of which pipeline
they invoke.
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path
from typing import Optional

from dem_fetcher import fetch_dem_for_bbox
from geocoder import geocode, shrink_bbox


# --------------------------------------------------------------------------- #
# Output layout
# --------------------------------------------------------------------------- #

class OutLayout:
    """
    Standard sub-folder layout used by every pipeline so users find files
    by purpose, not by chronology:

        results/<run_name>/
        ├── coordinates/   — CSV / GeoJSON (text-based location data)
        ├── images/        — PNG maps (overview + per_sink/)
        ├── rasters/       — GeoTIFF (DEM, depth)
        └── meta/          — manifest.json, sinks.geojson
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.coords = self.root / "coordinates"
        self.images = self.root / "images"
        self.per_sink = self.images / "per_sink"
        self.rasters = self.root / "rasters"
        self.meta = self.root / "meta"

    def make(self) -> "OutLayout":
        for d in (self.coords, self.images, self.per_sink, self.rasters, self.meta):
            d.mkdir(parents=True, exist_ok=True)
        return self


def safe_dir_name(name: str) -> str:
    """
    Sanitize a place name into a filesystem-safe folder name.
    Keeps Japanese characters, replaces whitespace and shell-unfriendly chars.
    """
    s = unicodedata.normalize("NFKC", name).strip()
    s = re.sub(r"[\s/\\:*?\"<>|]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_.")
    return s or "run"


def auto_run_name(args, bbox: Optional[tuple[float, float, float, float]]) -> str:
    """Generate a sensible run-name from --place, --bbox, or DEM filename."""
    if getattr(args, "place", None):
        return safe_dir_name(args.place)
    if bbox is not None:
        a, b, c, d = bbox
        return f"bbox_{a:.4f}_{b:.4f}_{c:.4f}_{d:.4f}".replace(".", "p")
    if getattr(args, "dem", None):
        return Path(args.dem).stem
    return "run"


# --------------------------------------------------------------------------- #
# CLI flags
# --------------------------------------------------------------------------- #


def add_input_flags(p: argparse.ArgumentParser) -> None:
    """Range-specification flags shared by every pipeline CLI."""
    p.add_argument("dem", nargs="?", default=None,
                   help="Input DEM GeoTIFF (projected CRS, metres). "
                        "Omit and use --bbox or --place to auto-fetch.")
    p.add_argument("--bbox",
                   help='WGS84 bbox "lon_min,lat_min,lon_max,lat_max"; '
                        "DEM is downloaded from GSI.")
    p.add_argument("--place",
                   help='Place name (e.g., "箱根町", "神奈川県") — geocoded '
                        "via Nominatim and combined with --max-side to crop.")
    p.add_argument("--max-side", type=float, default=0.30,
                   help="Cap bbox side in degrees for --place "
                        "(default 0.30° ≈ 33 km — fits a small city / town). "
                        "Use 0.10° for a ward, 1.0° for a small prefecture.")
    p.add_argument("--country", default=None,
                   help="ISO country code to constrain --place lookup (e.g., jp).")
    p.add_argument("--dem-zoom", type=int, default=0,
                   help="GSI tile zoom level when auto-fetching. "
                        "0 = auto. At ~lat 35°: z14≈8m, z13≈16m, z12≈31m, z11≈63m. "
                        "Auto picks z14 for ≤0.15°, z13 for ≤0.40°, "
                        "z12 for ≤1.0°, z11 above. GSI's max is z14.")
    p.add_argument("--out-dir", default=None,
                   help="Output directory. If omitted, auto-named from "
                        "--place or --bbox under ./results/.")


def auto_zoom(bbox_wgs84: tuple[float, float, float, float]) -> int:
    """Pick a GSI tile zoom level from the bbox size to bound RAM / network."""
    side = max(bbox_wgs84[2] - bbox_wgs84[0], bbox_wgs84[3] - bbox_wgs84[1])
    if side <= 0.15:
        return 14   # ~10 m
    if side <= 0.40:
        return 13   # ~20 m
    if side <= 1.00:
        return 12   # ~40 m
    return 11       # ~80 m


def resolve_bbox(args) -> Optional[tuple[float, float, float, float]]:
    """
    Convert ``--bbox`` / ``--place`` into a WGS84 bbox or return None
    (meaning the user passed a positional DEM file instead).
    """
    if args.bbox:
        parts = [float(v) for v in args.bbox.split(",")]
        if len(parts) != 4:
            raise SystemExit("--bbox needs 4 comma-separated values")
        return tuple(parts)  # type: ignore[return-value]
    if args.place:
        hit = geocode(args.place, country=args.country)
        bbox = shrink_bbox(hit["bbox_wgs84"], args.max_side)
        print(f'[geocode] {args.place!r} → {hit["display_name"]}')
        print(f'           bbox (cropped to ≤{args.max_side}°) = '
              f'{bbox[0]:.4f},{bbox[1]:.4f},{bbox[2]:.4f},{bbox[3]:.4f}')
        return bbox
    return None


def acquire_dem(
    dem_path: Optional[str],
    bbox_wgs84: Optional[tuple[float, float, float, float]],
    out_dir: Path,
    *,
    dem_zoom: int = 0,
) -> tuple[str, Optional[tuple[float, float, float, float]], list[Path]]:
    """
    Resolve the DEM path: either a user-provided file or a freshly fetched
    Web-Mercator → UTM GeoTIFF.

    ``dem_zoom=0`` requests automatic zoom selection from bbox size.
    """
    if dem_path is not None:
        return dem_path, None, []

    if bbox_wgs84 is None:
        raise SystemExit("Either a positional DEM, --bbox, or --place is required.")

    if dem_zoom == 0:
        dem_zoom = auto_zoom(bbox_wgs84)
        print(f"[0] Auto-zoom = {dem_zoom} for bbox side "
              f"{max(bbox_wgs84[2]-bbox_wgs84[0], bbox_wgs84[3]-bbox_wgs84[1]):.3f}°")

    # Tile-count guardrail: warn loudly above ~5000 tiles (~30 GB pixel array)
    from dem_fetcher import tiles_for_bbox
    rng = tiles_for_bbox(bbox_wgs84, dem_zoom)
    n = (rng[2] - rng[0] + 1) * (rng[3] - rng[1] + 1)
    if n > 5000:
        print(f"  WARNING: this bbox covers {n} tiles at zoom {dem_zoom}. "
              "Consider --dem-zoom 11 or a smaller bbox.")
    elif n > 1000:
        print(f"  Note: {n} tiles to download, may take a couple of minutes.")

    print(f"[0] Fetching DEM (source=gsi, zoom={dem_zoom})")
    fetched = fetch_dem_for_bbox(bbox_wgs84, out_dir, zoom=dem_zoom,
                                 source="gsi", reproject=True)
    return str(fetched), bbox_wgs84, [out_dir / "dem_mercator.tif", Path(fetched)]
