"""
Fetch GSI 標準地図 (standard topographic map) tiles for a WGS84 bbox and
assemble a RGB GeoTIFF reprojected into the local UTM zone.

URL: https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png
    * RGB raster, 256×256, max z=18, free, no API key.
    * Shows roads, place names, building outlines, station names, river
      names, map symbols, JIS contour lines — i.e. the same content rendered
      on https://maps.gsi.go.jp/.

Designed to be reused as a basemap underneath a 色別標高図 overlay so the
final figure mirrors the GSI website layer stack.
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import Affine
from rasterio.warp import Resampling, calculate_default_transform, reproject

from dem_fetcher import (
    WEB_MERCATOR_HALF,
    _download,
    tile_to_mercator_bounds,
    tiles_for_bbox,
    utm_epsg_for_bbox,
)


GSI_STD_URL = "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png"


def _decode_rgb(data: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(data)).convert("RGB"))


def fetch_gsi_basemap(
    bbox_wgs84: tuple[float, float, float, float],
    out_dir: str | Path,
    *,
    zoom: int = 16,
    target_epsg: Optional[int] = None,
    max_workers: int = 8,
    progress: bool = True,
    bbox_margin_deg: float = 0.012,
) -> Path:
    """Download GSI std tiles for ``bbox_wgs84`` and write a UTM RGB GeoTIFF.

    ``bbox_margin_deg`` extends the bbox before tile snapping so the basemap
    fully covers the DEM extent (DEM z15 タイルスナップは z16 より粗いため、
    margin 無しだと DEM 縁の数百 m 分が basemap 範囲外になり描画が崩れる)。
    既定 0.012° ≈ 1.3 km は z15 タイル 1 枚分を上回るので確実にカバーする。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if bbox_margin_deg > 0:
        west, south, east, north = bbox_wgs84
        bbox_wgs84 = (west - bbox_margin_deg, south - bbox_margin_deg,
                      east + bbox_margin_deg, north + bbox_margin_deg)

    rng = tiles_for_bbox(bbox_wgs84, zoom)
    x0, y0, x1, y1 = rng
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    n_tiles = nx * ny
    print(f"      basemap layer = std  zoom = {zoom}  tiles = {n_tiles}")

    canvas = np.full((ny * 256, nx * 256, 3), 255, dtype=np.uint8)
    coords = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

    def work(xy):
        x, y = xy
        data = _download(GSI_STD_URL.format(z=zoom, x=x, y=y))
        if data is None:
            return xy, None
        return xy, _decode_rgb(data)

    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(work, c) for c in coords]
        for fut in as_completed(futures):
            xy, arr = fut.result()
            done += 1
            if arr is not None:
                x, y = xy
                ix, iy = x - x0, y - y0
                canvas[iy * 256:(iy + 1) * 256, ix * 256:(ix + 1) * 256] = arr
            if progress and (done % 20 == 0 or done == n_tiles):
                print(f"      basemap tiles: {done}/{n_tiles}")

    # Web-Mercator extent from NW tile
    left, _, _, top = tile_to_mercator_bounds(x0, y0, zoom)
    n = 2 ** zoom
    pixel_size = (2 * WEB_MERCATOR_HALF / n) / 256
    transform = Affine.translation(left, top) * Affine.scale(pixel_size, -pixel_size)

    merc_path = out_dir / "basemap_mercator.tif"
    with rasterio.open(
        merc_path, "w",
        driver="GTiff", height=ny * 256, width=nx * 256, count=3,
        dtype="uint8", crs="EPSG:3857", transform=transform,
        photometric="rgb", compress="lzw",
    ) as dst:
        for band in range(3):
            dst.write(canvas[..., band], band + 1)

    if target_epsg is None:
        target_epsg = utm_epsg_for_bbox(bbox_wgs84)
    utm_path = out_dir / "basemap_utm.tif"
    with rasterio.open(merc_path) as src:
        transform2, w, h = calculate_default_transform(
            src.crs, f"EPSG:{target_epsg}", src.width, src.height, *src.bounds,
        )
        profile = src.profile.copy()
        profile.update(
            crs=f"EPSG:{target_epsg}", transform=transform2,
            width=w, height=h, compress="lzw", photometric="rgb",
        )
        with rasterio.open(utm_path, "w", **profile) as dst:
            for band_i in range(1, 4):
                reproject(
                    source=rasterio.band(src, band_i),
                    destination=rasterio.band(dst, band_i),
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=transform2, dst_crs=f"EPSG:{target_epsg}",
                    resampling=Resampling.bilinear,
                )
    print(f"      basemap     → {utm_path.name}  (EPSG:{target_epsg})")
    return utm_path
