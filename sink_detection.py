"""
Sink (depression) detection from a DEM.

Workflow:
    1. Read GeoTIFF DEM with rasterio.
    2. Run RichDEM's Priority-Flood depression-filling.
    3. Compute water-depth = filled - original.
    4. Threshold + vectorize the depth raster into polygons (GeoDataFrame).
    5. Aggregate per-sink statistics (max depth, mean depth, area).

The module exposes a single high-level entry point, ``detect_sinks``, plus
smaller helpers that can be composed if a caller wants intermediate rasters.
"""

from __future__ import annotations

import heapq
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import rasterio
from rasterio import features
from rasterio.transform import Affine
import geopandas as gpd
from shapely.geometry import shape

try:                                    # richdem is optional — pure backend works without it
    import richdem as rd
    _HAS_RICHDEM = True
except ImportError:                     # pragma: no cover
    rd = None
    _HAS_RICHDEM = False


FillBackend = Literal["richdem", "saga", "pure"]


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class DEMRasters:
    """Container for the rasters produced during sink detection."""
    elevation: np.ndarray          # original DEM
    filled: np.ndarray             # depression-filled DEM
    depth: np.ndarray              # filled - elevation (>=0)
    transform: Affine
    crs: rasterio.crs.CRS
    nodata: Optional[float]


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #

def read_dem(path: str | Path) -> tuple[np.ndarray, Affine, rasterio.crs.CRS, Optional[float]]:
    """Load a single-band GeoTIFF DEM."""
    with rasterio.open(path) as src:
        if src.count != 1:
            raise ValueError(f"DEM must be single-band, got {src.count} bands")
        elev = src.read(1).astype("float64")
        return elev, src.transform, src.crs, src.nodata


def write_geotiff(
    path: str | Path,
    array: np.ndarray,
    transform: Affine,
    crs: rasterio.crs.CRS,
    nodata: Optional[float] = None,
) -> None:
    """Write a 2-D float array as a single-band GeoTIFF."""
    profile = {
        "driver": "GTiff",
        "height": array.shape[0],
        "width": array.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": transform,
        "compress": "lzw",
    }
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array.astype("float32"), 1)


# --------------------------------------------------------------------------- #
# Core algorithms
# --------------------------------------------------------------------------- #

def _fill_sinks_richdem(elevation: np.ndarray, nodata: Optional[float]) -> np.ndarray:
    """RichDEM Priority-Flood (Wang & Liu 2006 equivalent)."""
    if not _HAS_RICHDEM:
        raise RuntimeError(
            "richdem is not installed. Install it (`pip install richdem`) or "
            "use backend='pure' / backend='saga'."
        )
    rd_arr = rd.rdarray(elevation.astype("float64"), no_data=nodata if nodata is not None else -9999.0)
    filled = rd.FillDepressions(rd_arr, in_place=False)
    return np.asarray(filled, dtype="float64")


def _fill_sinks_pure(
    elevation: np.ndarray,
    nodata: Optional[float],
    *,
    cell_size: float = 1.0,
    min_slope_deg: float = 0.1,
) -> np.ndarray:
    """
    Faithful Wang & Liu (2006) "Fill Sinks" in pure Python.

    Matches QGIS's "Fill sinks (Wang & Liu)" tool exposed via SAGA
    (``ta_preprocessor 4``).

    Parameters
    ----------
    cell_size : float
        Pixel size in the DEM's horizontal units (metres for projected CRS).
    min_slope_deg : float
        Minimum slope between filled cell and its parent, in degrees.
        SAGA's default is 0.1°. Set to 0 to recover plain Priority-Flood.

    NoData handling
    ---------------
    SAGA treats NoData cells as "outside the DEM"; water flows out through
    them. To match that we (1) mark NoData cells as closed so the heap
    skips them, and (2) seed every NoData-adjacent valid cell as a boundary
    outlet — without this the algorithm would only drain through the four
    rectangular DEM edges and over-fill closed basins that touch a NoData
    region (e.g., the corners produced by reprojection).

    Heap entries carry a monotonic counter as tie-breaker.
    """
    h, w = elevation.shape
    filled = elevation.astype("float64").copy()
    closed = np.zeros((h, w), dtype=bool)

    # Robust NoData detection: explicit NoData value AND NaN.
    is_nodata = np.isnan(elevation)
    if nodata is not None and not (isinstance(nodata, float) and np.isnan(nodata)):
        is_nodata |= (elevation == nodata)
    closed |= is_nodata

    # Increment per orthogonal step (SAGA/QGIS default's tan(MINSLOPE) * cellsize)
    eps_ortho = float(np.tan(np.deg2rad(min_slope_deg)) * cell_size)
    eps_diag = eps_ortho * float(np.sqrt(2.0))

    pq: list[tuple[float, int, int, int]] = []
    counter = 0

    def push(i: int, j: int) -> None:
        nonlocal counter
        if not closed[i, j]:
            heapq.heappush(pq, (float(filled[i, j]), counter, i, j))
            counter += 1
            closed[i, j] = True

    # Seed: rectangular DEM edges
    for j in range(w):
        push(0, j); push(h - 1, j)
    for i in range(h):
        push(i, 0); push(i, w - 1)

    # Seed: every valid cell adjacent to a NoData cell — water can exit there.
    if is_nodata.any():
        # 8-neighbour dilation of the NoData mask gives the "shore" cells.
        from scipy.ndimage import binary_dilation
        shore = binary_dilation(is_nodata, structure=np.ones((3, 3), bool))
        shore &= ~is_nodata           # only valid cells
        ys, xs = np.where(shore)
        for i, j in zip(ys, xs):
            push(int(i), int(j))

    nbrs = (
        (-1, -1, eps_diag), (-1, 0, eps_ortho), (-1, 1, eps_diag),
        (0, -1, eps_ortho),                     (0, 1, eps_ortho),
        (1, -1, eps_diag),  (1, 0, eps_ortho),  (1, 1, eps_diag),
    )

    while pq:
        e, _, i, j = heapq.heappop(pq)
        for di, dj, eps in nbrs:
            ni, nj = i + di, j + dj
            if 0 <= ni < h and 0 <= nj < w and not closed[ni, nj]:
                min_elev = e + eps
                if filled[ni, nj] < min_elev:
                    filled[ni, nj] = min_elev
                heapq.heappush(pq, (float(filled[ni, nj]), counter, ni, nj))
                counter += 1
                closed[ni, nj] = True

    # Restore NaN where the input had NoData, so downstream comparisons stay clean.
    if is_nodata.any():
        filled[is_nodata] = float("nan")
    return filled


def _fill_sinks_saga(
    elevation: np.ndarray,
    transform: Affine,
    crs: rasterio.crs.CRS,
    nodata: Optional[float],
    *,
    min_slope_deg: float = 0.1,
) -> np.ndarray:
    """
    True SAGA "Fill Sinks (Wang & Liu)" — same tool QGIS exposes.
    Requires ``saga_cmd`` on PATH (apt: libsaga, saga).
    """
    if shutil.which("saga_cmd") is None:
        raise RuntimeError(
            "SAGA backend requested but `saga_cmd` is not on PATH. "
            "Install with `sudo apt install saga` or pick backend='richdem'."
        )
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        in_tif = tmp / "in.tif"
        out_sdat = tmp / "out.sdat"  # SAGA grid
        write_geotiff(in_tif, elevation, transform, crs, nodata=nodata)
        # ta_preprocessor 4 = Fill Sinks (Wang & Liu)
        subprocess.run(
            ["saga_cmd", "ta_preprocessor", "4",
             "-ELEV", str(in_tif), "-FILLED", str(out_sdat),
             "-MINSLOPE", str(min_slope_deg)],
            check=True, capture_output=True,
        )
        with rasterio.open(out_sdat) as src:
            return src.read(1).astype("float64")


def auto_select_backend() -> FillBackend:
    """Pick the most QGIS-faithful backend that's actually available."""
    if shutil.which("saga_cmd") is not None:
        return "saga"
    if _HAS_RICHDEM:
        return "richdem"
    return "pure"


def fill_sinks(
    elevation: np.ndarray,
    nodata: Optional[float],
    *,
    backend: FillBackend | Literal["auto"] = "auto",
    transform: Optional[Affine] = None,
    crs: Optional[rasterio.crs.CRS] = None,
    min_slope_deg: float = 0.1,
) -> tuple[np.ndarray, FillBackend]:
    """
    Fill sinks (depressions) in a DEM — Wang & Liu (2006).

    Parameters
    ----------
    backend : "auto" | "saga" | "richdem" | "pure"
        ``auto`` (default) prefers SAGA (matches QGIS bit-for-bit), then
        RichDEM, then the pure-Python Wang & Liu fallback.
    min_slope_deg : float
        ε for the Wang & Liu downhill-flow guarantee. SAGA default = 0.01°.
    transform, crs : required only for the SAGA backend (it serializes a
        GeoTIFF to disk for ``saga_cmd``).

    Returns
    -------
    (filled, backend_used) — float64 surface and the backend actually used.
    """
    if backend == "auto":
        backend = auto_select_backend()

    if backend == "richdem":
        out = _fill_sinks_richdem(elevation, nodata)
    elif backend == "saga":
        if transform is None or crs is None:
            raise ValueError("SAGA backend requires transform and crs.")
        out = _fill_sinks_saga(elevation, transform, crs, nodata,
                               min_slope_deg=min_slope_deg)
    elif backend == "pure":
        cell_size = float(abs(transform.a)) if transform is not None else 1.0
        out = _fill_sinks_pure(elevation, nodata,
                               cell_size=cell_size,
                               min_slope_deg=min_slope_deg)
    else:
        raise ValueError(f"Unknown backend: {backend!r}")

    if nodata is not None:
        out[elevation == nodata] = nodata
    return out, backend


def compute_depth(elevation: np.ndarray, filled: np.ndarray, nodata: Optional[float]) -> np.ndarray:
    """
    Water-depth raster: how much each cell would be flooded if the sink filled.

    NoData cells (explicit value or NaN in either input) become 0.
    """
    depth = filled - elevation
    mask = np.isnan(elevation) | np.isnan(filled)
    if nodata is not None and not (isinstance(nodata, float) and np.isnan(nodata)):
        mask |= (elevation == nodata) | (filled == nodata)
    depth[mask] = 0.0
    # Numerical noise can produce tiny negatives — clamp.
    depth[depth < 0] = 0.0
    return depth


def vectorize_sinks(
    depth: np.ndarray,
    transform: Affine,
    crs: rasterio.crs.CRS,
    min_depth: float = 0.10,
    min_area_m2: float = 50.0,
    max_depth: float | None = None,
    max_area_m2: float | None = None,
) -> gpd.GeoDataFrame:
    """
    Convert the depth raster into a GeoDataFrame of sink polygons.

    Parameters
    ----------
    min_depth, max_depth : float | None
        Keep only cells / sinks whose max depth is within ``[min_depth, max_depth]``.
        ``max_depth`` is useful for excluding entire watershed-scale basins
        (e.g., volcanic calderas) that aren't a meaningful flood-risk signal.
    min_area_m2, max_area_m2 : float | None
        Drop sinks with area outside ``[min_area_m2, max_area_m2]`` (m²).
        ``max_area_m2`` complements ``max_depth`` — for closed basins, area is
        often the more discriminating bound.
    """
    mask = (depth >= min_depth).astype("uint8")
    if not mask.any():
        return gpd.GeoDataFrame(
            columns=["sink_id", "max_depth", "mean_depth", "area_m2", "geometry"],
            geometry="geometry",
            crs=crs,
        )

    # features.shapes yields (geom, value) pairs of connected pixel groups
    polys, depths_max, depths_mean, areas = [], [], [], []
    for geom, val in features.shapes(mask, mask=mask.astype(bool), transform=transform):
        if val != 1:
            continue
        poly = shape(geom)
        area = float(poly.area)
        if area < min_area_m2:
            continue
        if max_area_m2 is not None and area > max_area_m2:
            continue

        # Per-polygon depth stats: rasterize the polygon back to a mask
        poly_mask = features.geometry_mask(
            [geom], out_shape=depth.shape, transform=transform, invert=True
        )
        depth_in_poly = depth[poly_mask]
        if depth_in_poly.size == 0:
            continue

        d_max = float(depth_in_poly.max())
        if max_depth is not None and d_max > max_depth:
            continue

        polys.append(poly)
        depths_max.append(d_max)
        depths_mean.append(float(depth_in_poly.mean()))
        areas.append(area)

    gdf = gpd.GeoDataFrame(
        {
            "sink_id": np.arange(len(polys), dtype=int),
            "max_depth": depths_max,
            "mean_depth": depths_mean,
            "area_m2": areas,
            "geometry": polys,
        },
        geometry="geometry",
        crs=crs,
    )
    return gdf.sort_values("max_depth", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# High-level entry point
# --------------------------------------------------------------------------- #

def detect_sinks(
    dem_path: str | Path,
    *,
    min_depth: float = 0.10,
    min_area_m2: float = 50.0,
    max_depth: float | None = None,
    max_area_m2: float | None = None,
    backend: FillBackend | Literal["auto"] = "auto",
    min_slope_deg: float = 0.1,
    depth_out: Optional[str | Path] = None,
    sinks_out: Optional[str | Path] = None,
) -> tuple[gpd.GeoDataFrame, DEMRasters, dict]:
    """
    End-to-end sink extraction.

    Returns
    -------
    sinks_gdf : GeoDataFrame
        One row per sink polygon with depth/area attributes.
    rasters : DEMRasters
        Original / filled / depth arrays and georeferencing info.
    """
    elevation, transform, crs, nodata = read_dem(dem_path)
    if crs is None or not crs.is_projected:
        # We need metric area — caller should reproject upstream, but warn loudly.
        raise ValueError(
            "DEM CRS must be projected (metric). Reproject to e.g. EPSG:3857 "
            "or a local UTM zone before running detect_sinks()."
        )

    filled, backend_used = fill_sinks(
        elevation, nodata,
        backend=backend, transform=transform, crs=crs,
        min_slope_deg=min_slope_deg,
    )
    depth = compute_depth(elevation, filled, nodata)
    sinks = vectorize_sinks(
        depth, transform, crs,
        min_depth=min_depth, min_area_m2=min_area_m2,
        max_depth=max_depth, max_area_m2=max_area_m2,
    )

    if depth_out is not None:
        write_geotiff(depth_out, depth, transform, crs, nodata=0.0)
    if sinks_out is not None:
        sinks.to_file(sinks_out, driver="GeoJSON" if str(sinks_out).endswith(".geojson") else None)

    rasters = DEMRasters(elevation=elevation, filled=filled, depth=depth,
                        transform=transform, crs=crs, nodata=nodata)
    info = {
        "backend_used": backend_used,
        "min_slope_deg": min_slope_deg,
        "min_depth": min_depth,
        "min_area_m2": min_area_m2,
        "max_depth": max_depth,
        "max_area_m2": max_area_m2,
        "n_sinks": int(len(sinks)),
        "cell_size_m": float(abs(transform.a)),
        "crs": str(crs),
    }
    return sinks, rasters, info


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Detect sinks (depressions) in a DEM.")
    p.add_argument("dem", help="Input GeoTIFF DEM (must be in a projected CRS).")
    p.add_argument("--backend", choices=("auto", "richdem", "saga", "pure"), default="auto",
                   help="FillSink backend (default: auto — saga > richdem > pure).")
    p.add_argument("--min-slope", type=float, default=0.1,
                   help="Wang & Liu min-slope in degrees (default: 0.1, SAGA/QGIS default).")
    p.add_argument("--min-depth", type=float, default=0.10,
                   help="Minimum depth threshold in DEM vertical units (default: 0.10).")
    p.add_argument("--min-area", type=float, default=50.0,
                   help="Minimum sink area in m^2 (default: 50).")
    p.add_argument("--depth-out", help="Optional output GeoTIFF for the depth raster.")
    p.add_argument("--sinks-out", help="Optional output GeoJSON/Shapefile for sink polygons.")
    args = p.parse_args()

    sinks, _, info = detect_sinks(
        args.dem,
        min_depth=args.min_depth,
        min_area_m2=args.min_area,
        backend=args.backend,
        min_slope_deg=args.min_slope,
        depth_out=args.depth_out,
        sinks_out=args.sinks_out,
    )
    print(f"Backend used: {info['backend_used']}")
    print(f"Detected {len(sinks)} sink(s).")
    if len(sinks):
        print(sinks[["sink_id", "max_depth", "mean_depth", "area_m2"]].head(20).to_string(index=False))
