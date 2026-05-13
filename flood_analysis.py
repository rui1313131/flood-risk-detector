"""
Spatial overlay of sinks and buildings + output (CSV + PNG map).

Given:
    - sinks GeoDataFrame from ``sink_detection.detect_sinks``
    - buildings GeoDataFrame from ``building_loader``
    - depth raster (DEMRasters.depth) for per-building depth lookup

Produces:
    - at_risk_buildings : GeoDataFrame of buildings inside any sink, with
      estimated water depth (max depth sampled inside the building footprint).
    - CSV   : building_id, lon, lat, x, y, sink_id, est_depth_m
    - PNG   : map showing sinks (semi-transparent), at-risk buildings (red),
      other buildings (grey), and an optional basemap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from rasterio import features
from rasterio.transform import Affine, rowcol

from sink_detection import DEMRasters


# --------------------------------------------------------------------------- #
# Per-building depth sampling
# --------------------------------------------------------------------------- #

def _max_in_window(
    depth: np.ndarray, transform: Affine, x: float, y: float, radius: int = 1
) -> float:
    """
    Max depth in a (2*radius+1) × (2*radius+1) cell window around (x, y).

    Returns 0 if the centre falls outside the raster.
    """
    row, col = rowcol(transform, x, y)
    h, w = depth.shape
    if not (0 <= row < h and 0 <= col < w):
        return 0.0
    r0, r1 = max(0, row - radius), min(h, row + radius + 1)
    c0, c1 = max(0, col - radius), min(w, col + radius + 1)
    win = depth[r0:r1, c0:c1]
    return float(win.max()) if win.size else 0.0


def _sample_max_depth(
    geom, depth: np.ndarray, transform: Affine
) -> float:
    """
    Max depth under a polygon, robust to sub-pixel buildings.

    Strategy:
      1. Polygon mask  — works when the footprint is ≥ ~1 pixel.
      2. Centroid 3×3 — sub-pixel footprints (typical OSM buildings ~5–10 m
         on ~8 m pixels) commonly rasterize to an empty mask, *or* land their
         centroid on a 0-depth boundary cell while their actual footprint
         overlaps deep cells just next door. A 3×3 window catches that.
    Returns the max of both attempts so the polygon mask is never lost.
    """
    poly_max = 0.0
    mask = features.geometry_mask(
        [geom], out_shape=depth.shape, transform=transform, invert=True
    )
    if mask.any():
        vals = depth[mask]
        if vals.size:
            poly_max = float(vals.max())

    c = geom.centroid
    win_max = _max_in_window(depth, transform, c.x, c.y, radius=1)

    return max(poly_max, win_max)


# --------------------------------------------------------------------------- #
# Overlay
# --------------------------------------------------------------------------- #

def rank_sinks_by_risk(
    sinks: gpd.GeoDataFrame,
    at_risk: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """
    Add a deterministic risk score and sort sinks descending.

    Score = max_depth * log1p(area_m2) * (1 + n_buildings)

    All three factors are non-negative, so the score is monotone in each.
    The log on area damps very large flat sinks dominating just by extent.
    """
    sinks = sinks.copy()
    if at_risk.empty or "sink_id" not in at_risk.columns:
        sinks["n_buildings"] = 0
    else:
        counts = at_risk.groupby("sink_id").size().rename("n_buildings")
        sinks = sinks.merge(counts, left_on="sink_id", right_index=True, how="left")
        sinks["n_buildings"] = sinks["n_buildings"].fillna(0).astype(int)

    sinks["risk_score"] = (
        sinks["max_depth"].astype(float)
        * np.log1p(sinks["area_m2"].astype(float))
        * (1.0 + sinks["n_buildings"].astype(float))
    )
    return sinks.sort_values("risk_score", ascending=False).reset_index(drop=True)


def find_at_risk_buildings(
    sinks: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    rasters: DEMRasters,
) -> gpd.GeoDataFrame:
    """
    Buildings whose footprint intersects any sink polygon.

    Adds:
        sink_id      : id of the (largest-overlap) sink they fall in
        est_depth_m  : max depth raster value under the footprint
        lon, lat     : centroid in WGS84 for the CSV
    """
    if buildings.empty or sinks.empty:
        return gpd.GeoDataFrame(
            columns=["building_id", "sink_id", "est_depth_m", "geometry"],
            geometry="geometry",
            crs=buildings.crs if not buildings.empty else sinks.crs,
        )

    # Spatial join: buildings × sinks (intersect predicate)
    joined = gpd.sjoin(
        buildings, sinks[["sink_id", "geometry"]],
        how="inner", predicate="intersects",
    ).drop(columns=["index_right"])

    if joined.empty:
        return gpd.GeoDataFrame(
            columns=["building_id", "sink_id", "est_depth_m", "geometry"],
            geometry="geometry",
            crs=buildings.crs,
        )

    # If a building straddles two sinks, keep the deeper sink (higher max_depth).
    joined = joined.merge(
        sinks[["sink_id", "max_depth"]], on="sink_id", how="left",
    )
    joined = (
        joined.sort_values("max_depth", ascending=False)
              .drop_duplicates("building_id", keep="first")
              .drop(columns=["max_depth"])
    )

    # Sample per-building depth from the raster
    joined["est_depth_m"] = [
        _sample_max_depth(g, rasters.depth, rasters.transform)
        for g in joined.geometry
    ]

    # Centroid in projected CRS, then to WGS84 for the CSV
    cent = joined.geometry.centroid
    joined["x"] = cent.x
    joined["y"] = cent.y
    cent_wgs = gpd.GeoSeries(cent, crs=buildings.crs).to_crs(4326)
    joined["lon"] = cent_wgs.x
    joined["lat"] = cent_wgs.y

    cols = ["building_id", "source", "sink_id", "est_depth_m",
            "lon", "lat", "x", "y", "geometry"]
    return joined[[c for c in cols if c in joined.columns]].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #

def export_csv(at_risk: gpd.GeoDataFrame, path: str | Path) -> None:
    """Drop geometry and write the at-risk-building table."""
    df = pd.DataFrame(at_risk.drop(columns="geometry"))
    df.to_csv(path, index=False)


def render_per_sink_maps(
    sinks: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    at_risk: gpd.GeoDataFrame,
    rasters,                              # DEMRasters
    out_dir: str | Path,
    *,
    padding: float = 0.4,
    only_with_buildings: bool = True,
    basemap: bool = False,
    figsize: tuple[float, float] = (8, 8),
    dpi: int = 180,
) -> list[Path]:
    """
    Render one PNG per sink, cropped to the sink's geometry plus padding.

    Filename pattern (sortable + descriptive):
        sink_{rank:03d}_id{sink_id}_depth_{max_depth_dm:04d}dm_bld_{n}.png

    where ``max_depth_dm`` is the depth in decimetres (i.e. ``round(depth*10)``)
    so a sink with 16.08 m depth becomes ``depth_0161dm``.

    Parameters
    ----------
    padding : float
        Extra margin around the sink, fraction of bbox side (default 0.4 → 40%).
    only_with_buildings : bool
        If True, only sinks with ≥1 at-risk building are rendered.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if sinks.empty:
        return []

    # n_buildings per sink (joined from at_risk)
    if at_risk.empty or "sink_id" not in at_risk.columns:
        n_by_sink: dict[int, int] = {}
    else:
        n_by_sink = at_risk.groupby("sink_id").size().to_dict()

    # Reset transform/depth references for cropping
    transform = rasters.transform
    depth = rasters.depth

    written: list[Path] = []
    # Iterate in given order (caller is expected to pass ranked sinks)
    for rank, (_, s) in enumerate(sinks.iterrows(), 1):
        sid = int(s["sink_id"])
        n = int(n_by_sink.get(sid, 0))
        if only_with_buildings and n == 0:
            continue

        max_depth = float(s["max_depth"])
        # bbox + padding
        minx, miny, maxx, maxy = s.geometry.bounds
        side = max(maxx - minx, maxy - miny)
        pad = max(side * padding, 30.0)  # at least 30 m of context
        view = (minx - pad, miny - pad, maxx + pad, maxy + pad)

        # Clip the depth raster to the view window for the heatmap background
        from rasterio.windows import from_bounds
        win = from_bounds(*view, transform=transform)
        r0, r1 = max(0, int(win.row_off)), min(depth.shape[0], int(win.row_off + win.height))
        c0, c1 = max(0, int(win.col_off)), min(depth.shape[1], int(win.col_off + win.width))
        depth_crop = depth[r0:r1, c0:c1]
        # Pixel coords → map coords for the cropped extent
        from rasterio.transform import xy as rio_xy
        x0, y0 = rio_xy(transform, r0, c0, offset="ul")
        x1, y1 = rio_xy(transform, r1, c1, offset="ul")
        extent = (x0, x1, y1, y0)  # matplotlib expects (left, right, bottom, top)

        # Plot
        fig, ax = plt.subplots(figsize=figsize)
        if depth_crop.size:
            im = ax.imshow(
                depth_crop, extent=extent, origin="upper",
                cmap="Blues", alpha=0.7,
                vmin=0, vmax=max(1.0, float(depth_crop.max())),
            )
            cbar = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
            cbar.set_label("depth (m)")

        # Buildings inside the view (gray) + at-risk for this sink (red)
        if not buildings.empty:
            inview = buildings.cx[view[0]:view[2], view[1]:view[3]]
            inview.plot(ax=ax, facecolor="none", edgecolor="#6b7280", linewidth=0.6)
        if not at_risk.empty:
            this_sink = at_risk[at_risk["sink_id"] == sid]
            if not this_sink.empty:
                this_sink.plot(ax=ax, facecolor="#ef4444",
                               edgecolor="#7f1d1d", linewidth=0.8)

        # Sink polygon outline
        gpd.GeoSeries([s.geometry], crs=sinks.crs).plot(
            ax=ax, facecolor="none", edgecolor="#1d4ed8", linewidth=1.6,
        )

        if basemap:
            try:
                import contextily as cx
                cx.add_basemap(ax, crs=sinks.crs, source=cx.providers.OpenStreetMap.Mapnik)
            except Exception:
                pass  # quietly skip; the overview render warns once already

        ax.set_xlim(view[0], view[2])
        ax.set_ylim(view[1], view[3])
        ax.set_aspect("equal")
        ax.set_title(
            f"Sink #{sid}  max_depth={max_depth:.2f} m  area={float(s['area_m2']):.0f} m²  "
            f"buildings={n}"
        )
        ax.set_xlabel("X (m, projected)")
        ax.set_ylabel("Y (m, projected)")

        depth_dm = max(0, int(round(max_depth * 10)))
        fname = f"sink_{rank:03d}_id{sid}_depth_{depth_dm:04d}dm_bld_{n}.png"
        out_path = out_dir / fname
        fig.tight_layout()
        fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        written.append(out_path)

    return written


def render_map(
    sinks: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    at_risk: gpd.GeoDataFrame,
    out_path: str | Path,
    *,
    basemap: bool = False,
    figsize: tuple[float, float] = (10, 10),
    dpi: int = 200,
) -> None:
    """
    Render a PNG map.

    sinks         : translucent blue
    buildings     : grey outline (background context)
    at-risk       : filled red (highlight)
    """
    fig, ax = plt.subplots(figsize=figsize)

    if not sinks.empty:
        sinks.plot(ax=ax, color="#3b82f6", alpha=0.35, edgecolor="#1e3a8a", linewidth=0.5)
    if not buildings.empty:
        buildings.plot(ax=ax, facecolor="none", edgecolor="#9ca3af", linewidth=0.4)
    if not at_risk.empty:
        at_risk.plot(ax=ax, facecolor="#ef4444", edgecolor="#7f1d1d", linewidth=0.6)

    if basemap:
        try:
            import contextily as cx
            cx.add_basemap(ax, crs=sinks.crs, source=cx.providers.OpenStreetMap.Mapnik)
        except Exception as e:  # contextily is optional
            print(f"  (basemap skipped: {e})")

    ax.set_axis_off()
    ax.set_title(
        f"Flood-risk buildings: {len(at_risk)} / {len(buildings)} "
        f"in {len(sinks)} sink(s)"
    )
    legend = [
        Patch(facecolor="#3b82f6", alpha=0.35, edgecolor="#1e3a8a", label="Sink (depression)"),
        Patch(facecolor="#ef4444", edgecolor="#7f1d1d", label="At-risk building"),
        Patch(facecolor="none", edgecolor="#9ca3af", label="Other building"),
    ]
    ax.legend(handles=legend, loc="lower right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
