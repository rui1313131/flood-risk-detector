"""
DEM auto-fetcher.

Pulls free public DEM data for a WGS84 bounding box and writes a single
GeoTIFF that the rest of the pipeline can consume.

Currently supported sources
---------------------------
* ``gsi`` — 国土地理院「標高タイル」(``dem_png``, 10 m DEM, ≤ zoom 14).
  Free, no API key. Produces a Web-Mercator GeoTIFF that is then reprojected
  to a local UTM zone (metres) for area-correct downstream analysis.

Endpoint
--------
    https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png

Encoding (per GSI spec): ``elev = u * 0.01`` where ``u`` is the signed 24-bit
integer ``r*65536 + g*256 + b`` (``u == 2**23`` is the NoData marker).
"""

from __future__ import annotations

import io
import math
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import Resampling, calculate_default_transform, reproject
from PIL import Image


GSI_DEM_URL = "https://cyberjapandata.gsi.go.jp/xyz/{layer}/{z}/{x}/{y}.png"
WEB_MERCATOR_HALF = 20037508.342789244   # Web Mercator world half-extent (m)

# Per-layer max usable zoom on the GSI tile server.
# dem_png   = 10 m DEM (nation-wide), z ≤ 14
# dem5a_png = 5 m DEM photogrammetry (urban / lowland coverage), z ≤ 15
# dem5b_png = 5 m DEM laser (mountain coverage),                 z ≤ 15
# dem5c_png = 5 m DEM mixed,                                     z ≤ 15
GSI_LAYER_MAX_ZOOM = {
    "dem_png": 14, "dem5a_png": 15, "dem5b_png": 15, "dem5c_png": 15,
}


# --------------------------------------------------------------------------- #
# Tile math
# --------------------------------------------------------------------------- #

def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """WGS84 lon/lat → XYZ tile indices (Web Mercator)."""
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_to_mercator_bounds(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    """Tile XYZ → Web-Mercator (EPSG:3857) bounds (left, bottom, right, top)."""
    n = 2 ** zoom
    tile_size = 2 * WEB_MERCATOR_HALF / n
    left = -WEB_MERCATOR_HALF + x * tile_size
    right = left + tile_size
    top = WEB_MERCATOR_HALF - y * tile_size
    bottom = top - tile_size
    return left, bottom, right, top


def tiles_for_bbox(
    bbox_wgs84: tuple[float, float, float, float], zoom: int
) -> tuple[int, int, int, int]:
    """Inclusive tile-index ranges (x_min, y_min, x_max, y_max) covering bbox."""
    minx, miny, maxx, maxy = bbox_wgs84
    x0, y0 = lonlat_to_tile(minx, maxy, zoom)   # NW corner
    x1, y1 = lonlat_to_tile(maxx, miny, zoom)   # SE corner
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


# --------------------------------------------------------------------------- #
# PNG → elevation
# --------------------------------------------------------------------------- #

def decode_gsi_png(png_bytes: bytes) -> np.ndarray:
    """
    Decode a GSI elevation PNG into a (256, 256) float32 array of metres.
    NoData → NaN.
    """
    img = np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"))
    r = img[..., 0].astype(np.int64)
    g = img[..., 1].astype(np.int64)
    b = img[..., 2].astype(np.int64)
    u = r * 65536 + g * 256 + b

    elev = np.where(u < (1 << 23), u, u - (1 << 24)).astype(np.float64) * 0.01
    elev[u == (1 << 23)] = np.nan        # GSI NoData marker
    return elev.astype(np.float32)


# --------------------------------------------------------------------------- #
# Tile download
# --------------------------------------------------------------------------- #

def _download(url: str, *, timeout: float = 10.0, retries: int = 3) -> Optional[bytes]:
    """GET with simple retry; returns bytes or None on 404."""
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "flood-risk-detector/0.1"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # tile genuinely missing (e.g., over ocean)
            last_exc = e
        except Exception as e:                # network blip — retry
            last_exc = e
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Tile download failed after {retries} retries: {url} ({last_exc})")


def fetch_gsi_tiles(
    tile_range: tuple[int, int, int, int],
    zoom: int,
    *,
    layer: str = "dem_png",
    max_workers: int = 8,
    progress: bool = True,
) -> dict[tuple[int, int], np.ndarray]:
    """
    Download every tile in ``(x_min, y_min, x_max, y_max)``.

    Returns a dict ``{(x, y): elevation_array}``. Missing tiles (e.g., outside
    the GSI domain) are filled with all-NaN arrays so the mosaic still tiles
    cleanly.
    """
    x0, y0, x1, y1 = tile_range
    coords = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    out: dict[tuple[int, int], np.ndarray] = {}

    def work(xy):
        x, y = xy
        url = GSI_DEM_URL.format(layer=layer, z=zoom, x=x, y=y)
        data = _download(url)
        if data is None:
            return xy, np.full((256, 256), np.nan, dtype=np.float32)
        return xy, decode_gsi_png(data)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(work, xy) for xy in coords]
        for i, fut in enumerate(as_completed(futures), 1):
            xy, arr = fut.result()
            out[xy] = arr
            if progress and (i % 10 == 0 or i == len(futures)):
                print(f"      tiles: {i}/{len(futures)}")

    return out


# --------------------------------------------------------------------------- #
# Mosaic
# --------------------------------------------------------------------------- #

def mosaic_to_geotiff(
    tiles: dict[tuple[int, int], np.ndarray],
    tile_range: tuple[int, int, int, int],
    zoom: int,
    out_path: str | Path,
) -> Path:
    """Stitch tiles into one Web-Mercator GeoTIFF (EPSG:3857)."""
    x0, y0, x1, y1 = tile_range
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    h, w = ny * 256, nx * 256

    canvas = np.full((h, w), np.nan, dtype=np.float32)
    for (x, y), arr in tiles.items():
        ix, iy = x - x0, y - y0
        canvas[iy * 256:(iy + 1) * 256, ix * 256:(ix + 1) * 256] = arr

    # Geo-extent from the NW (top-left) tile
    left, _, _, top = tile_to_mercator_bounds(x0, y0, zoom)
    n = 2 ** zoom
    pixel_size = (2 * WEB_MERCATOR_HALF / n) / 256
    transform = Affine.translation(left, top) * Affine.scale(pixel_size, -pixel_size)

    out_path = Path(out_path)
    with rasterio.open(
        out_path, "w",
        driver="GTiff", height=h, width=w, count=1,
        dtype="float32", crs="EPSG:3857", transform=transform,
        nodata=float("nan"), compress="lzw",
    ) as dst:
        dst.write(canvas, 1)
    return out_path


# --------------------------------------------------------------------------- #
# UTM auto-reprojection
# --------------------------------------------------------------------------- #

def utm_epsg_for_bbox(bbox_wgs84: tuple[float, float, float, float]) -> int:
    """Pick the UTM EPSG matching the bbox centre (north EPSG 326xx, south 327xx)."""
    minx, miny, maxx, maxy = bbox_wgs84
    lon_c = (minx + maxx) / 2
    lat_c = (miny + maxy) / 2
    zone = int((lon_c + 180.0) // 6) + 1
    return (32600 if lat_c >= 0 else 32700) + zone


def reproject_to_utm(
    in_path: str | Path,
    out_path: str | Path,
    target_epsg: int,
    *,
    resampling: Resampling = Resampling.bilinear,
) -> Path:
    """Reproject a GeoTIFF to a UTM zone (metres)."""
    with rasterio.open(in_path) as src:
        transform, w, h = calculate_default_transform(
            src.crs, f"EPSG:{target_epsg}", src.width, src.height, *src.bounds,
        )
        profile = src.profile.copy()
        profile.update(
            crs=f"EPSG:{target_epsg}", transform=transform,
            width=w, height=h, compress="lzw",
        )
        with rasterio.open(out_path, "w", **profile) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=transform, dst_crs=f"EPSG:{target_epsg}",
                resampling=resampling,
            )
    return Path(out_path)


# --------------------------------------------------------------------------- #
# High-level entry point
# --------------------------------------------------------------------------- #

def fetch_dem_for_bbox(
    bbox_wgs84: tuple[float, float, float, float],
    out_dir: str | Path,
    *,
    zoom: int = 14,
    source: str = "gsi",
    gsi_layer: str = "dem_png",
    reproject: bool = True,
    target_epsg: Optional[int] = None,
) -> Path:
    """
    Download a DEM covering ``bbox_wgs84`` and save it as a metric GeoTIFF.

    Returns the path of the final (UTM-reprojected) DEM. Set
    ``reproject=False`` to keep Web Mercator. ``gsi_layer`` selects which GSI
    tile layer to pull (e.g. ``dem_png`` for 10 m, ``dem5a_png`` for 5 m).
    """
    if source != "gsi":
        raise ValueError(f"Unsupported DEM source: {source!r} (only 'gsi' for now)")
    if gsi_layer not in GSI_LAYER_MAX_ZOOM:
        raise ValueError(f"Unknown gsi_layer {gsi_layer!r}; "
                         f"choose from {sorted(GSI_LAYER_MAX_ZOOM)}")
    max_z = GSI_LAYER_MAX_ZOOM[gsi_layer]
    if zoom > max_z:
        raise ValueError(f"zoom={zoom} exceeds max for layer {gsi_layer!r} (z<={max_z})")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"      bbox WGS84  = {bbox_wgs84}")
    print(f"      layer       = {gsi_layer}  (max z={max_z})")
    print(f"      zoom        = {zoom}")

    rng = tiles_for_bbox(bbox_wgs84, zoom)
    n_tiles = (rng[2] - rng[0] + 1) * (rng[3] - rng[1] + 1)
    print(f"      tile range  = x[{rng[0]}..{rng[2]}] y[{rng[1]}..{rng[3]}]  ({n_tiles} tiles)")

    tiles = fetch_gsi_tiles(rng, zoom, layer=gsi_layer)
    merc_path = out_dir / "dem_mercator.tif"
    mosaic_to_geotiff(tiles, rng, zoom, merc_path)
    print(f"      mosaic      → {merc_path}")

    if not reproject:
        return merc_path

    if target_epsg is None:
        target_epsg = utm_epsg_for_bbox(bbox_wgs84)
    utm_path = out_dir / "dem_utm.tif"
    reproject_to_utm(merc_path, utm_path, target_epsg)
    print(f"      reprojected → EPSG:{target_epsg} → {utm_path}")
    return utm_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Fetch a DEM for a WGS84 bbox.")
    p.add_argument("--bbox", required=True,
                   help='WGS84 bbox "lon_min,lat_min,lon_max,lat_max"')
    p.add_argument("--zoom", type=int, default=14)
    p.add_argument("--out-dir", default=".")
    p.add_argument("--source", default="gsi", choices=("gsi",))
    p.add_argument("--no-reproject", action="store_true")
    args = p.parse_args()

    bbox = tuple(float(v) for v in args.bbox.split(","))
    if len(bbox) != 4:
        raise SystemExit("--bbox needs 4 comma-separated values")
    fetch_dem_for_bbox(
        bbox, args.out_dir,
        zoom=args.zoom, source=args.source,
        reproject=not args.no_reproject,
    )
