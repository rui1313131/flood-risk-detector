"""
Preview the elevation of every prefetched test site (QGIS-pseudocolor style,
high-detail variant).

Layer stack (mirrors a typical QGIS layer ordering):

    1. Continuous Pseudocolor 0..10 m, ≥10 m grey, NaN transparent       (QGIS: Pseudocolor)
    2. Hillshade multiply, az=315° alt=45° vert-exag=3                    (QGIS: Hillshade renderer / Multiply blend)
    3. Contour lines: 0.25 m thin + 1 m thick (labelled)                  (QGIS: Contour processing tool)
    4. OSM building footprints, semi-transparent outlines                 (QGIS: vector layer overlay)
    5. 300 m × 300 m red reference box + 1 km scale bar                   (QGIS: annotation layer)

Source DEM: GSI dem5a_png z15  (5 m photogrammetric, native ≈4 m UTM px).
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize, LightSource
from matplotlib.patches import Rectangle
import numpy as np
import geopandas as gpd
import rasterio
from rasterio.warp import transform as warp_transform


BOX_SIDE_M = 300            # rough 町丁目 footprint
COLORMAP_NAME = "turbo"
ELEV_MIN = 0.0
ELEV_MAX = 10.0             # ≥10 m → over-colour (grey)
CONTOUR_MINOR_STEP = 0.25   # very fine relief contours
CONTOUR_MAJOR_STEP = 1.0    # bold contours every 1 m, with elevation label
HILLSHADE_VERT_EXAG = 3.0
HILLSHADE_FLOOR = 0.50      # darkest value of the hillshade multiplier
DPI = 200
FIGSIZE_IN = 12             # square — 12 in × 200 dpi = 2400 px
BUILDING_EDGECOLOR = (0, 0, 0, 0.55)
BUILDING_FACECOLOR = (0, 0, 0, 0.10)
BUILDING_LINEWIDTH = 0.25


# ---- Use a CJK font if available so the annotation renders ----------------
# Windows defaults (Yu Gothic / Meiryo / MS Gothic) checked first.
for cand in ("Yu Gothic", "Meiryo", "MS Gothic",
             "Noto Sans CJK JP", "Noto Sans CJK HK", "Noto Sans CJK SC",
             "Hiragino Sans", "IPAexGothic", "TakaoGothic"):
    if any(f.name == cand for f in fm.fontManager.ttflist):
        plt.rcParams["font.family"] = cand
        break
plt.rcParams["axes.unicode_minus"] = False


def render_site(site_dir: Path) -> Path:
    info = json.loads((site_dir / "site.json").read_text(encoding="utf-8"))
    dem_path = site_dir / "dem_utm.tif"

    with rasterio.open(dem_path) as src:
        elev = src.read(1, masked=True).astype(np.float32)
        bounds = src.bounds
        crs = src.crs
        px, py = src.res
        xs, ys = warp_transform(
            "EPSG:4326", crs,
            [info["center_lon"]], [info["center_lat"]],
        )
        cx, cy = xs[0], ys[0]

    elev_arr = elev.filled(np.nan)
    valid_mask = ~np.isnan(elev_arr)

    # ---- Hillshade ----
    # Fill NaN with local mean so the slope-based shader doesn't propagate NaN.
    fill = float(np.nanmean(elev_arr)) if np.any(valid_mask) else 0.0
    elev_for_hill = np.where(valid_mask, elev_arr, fill)
    ls = LightSource(azdeg=315, altdeg=45)
    hill = ls.hillshade(
        elev_for_hill, vert_exag=HILLSHADE_VERT_EXAG, dx=float(px), dy=float(py),
    )
    hill_mul = HILLSHADE_FLOOR + (1.0 - HILLSHADE_FLOOR) * hill   # [floor, 1]

    # ---- Continuous elevation colour ----
    cmap = plt.get_cmap(COLORMAP_NAME).copy()
    cmap.set_over((0.55, 0.55, 0.55, 1.0))   # ≥10 m → mid-grey
    cmap.set_under(cmap(0.0))                 # negatives clamp to bin-0 colour
    cmap.set_bad((0.0, 0.0, 0.0, 0.0))        # NaN → transparent
    norm = Normalize(vmin=ELEV_MIN, vmax=ELEV_MAX, clip=False)

    elev_for_color = np.ma.array(elev_arr, mask=~valid_mask)
    rgba = cmap(norm(elev_for_color))         # H × W × 4

    # Shade the colour by hillshade (multiply RGB only)
    rgba[..., :3] *= hill_mul[..., None]
    # Force NaN cells to fully transparent
    rgba[~valid_mask] = (1.0, 1.0, 1.0, 0.0)

    # ---- Plot ----
    fig, ax = plt.subplots(figsize=(FIGSIZE_IN, FIGSIZE_IN), dpi=DPI)
    extent = (bounds.left, bounds.right, bounds.bottom, bounds.top)
    ax.imshow(rgba, extent=extent, origin="upper", interpolation="bilinear")

    # Coordinate grids for contour (cell centres)
    h, w = elev_arr.shape
    xs_grid = np.linspace(bounds.left + px / 2,
                          bounds.right - px / 2, w)
    ys_grid = np.linspace(bounds.top - py / 2,
                          bounds.bottom + py / 2, h)
    Xg, Yg = np.meshgrid(xs_grid, ys_grid)

    minor_levels = np.arange(ELEV_MIN, ELEV_MAX + 0.001, CONTOUR_MINOR_STEP)
    minor_levels = minor_levels[~np.isin(minor_levels,
                                          np.arange(ELEV_MIN, ELEV_MAX + 0.001,
                                                    CONTOUR_MAJOR_STEP))]
    major_levels = np.arange(ELEV_MIN, ELEV_MAX + 0.001, CONTOUR_MAJOR_STEP)

    elev_for_contour = np.where(valid_mask, elev_arr, np.nan)
    if minor_levels.size:
        ax.contour(Xg, Yg, elev_for_contour, levels=minor_levels,
                   colors="black", linewidths=0.18, alpha=0.30, antialiased=True)
    cs_major = ax.contour(Xg, Yg, elev_for_contour, levels=major_levels,
                          colors="black", linewidths=0.55, alpha=0.80,
                          antialiased=True)
    ax.clabel(cs_major, fmt="%dm", fontsize=7, inline=True)

    # ---- OSM building footprints ----
    bld_path = site_dir / "buildings.geojson"
    if bld_path.exists():
        bld = gpd.read_file(bld_path)
        if not bld.empty:
            if str(bld.crs) != str(crs):
                bld = bld.to_crs(crs)
            # Restrict to the visible bbox so we don't fight off-canvas geometry
            bld = bld.cx[bounds.left:bounds.right, bounds.bottom:bounds.top]
            bld.boundary.plot(ax=ax, color=BUILDING_EDGECOLOR,
                              linewidth=BUILDING_LINEWIDTH, zorder=8,
                              antialiased=True)
            bld.plot(ax=ax, facecolor=BUILDING_FACECOLOR,
                     edgecolor="none", zorder=7)
            n_bld = len(bld)
        else:
            n_bld = 0
    else:
        n_bld = 0

    # ---- Box on the residential reference point ----
    half = BOX_SIDE_M / 2
    ax.add_patch(Rectangle(
        (cx - half, cy - half), BOX_SIDE_M, BOX_SIDE_M,
        fill=False, edgecolor="red", linewidth=2.4, zorder=10,
    ))
    ax.plot(cx, cy, marker="+", color="red",
            markersize=14, mew=1.6, zorder=11)
    ax.annotate(
        info["label"],
        xy=(cx, cy + half + 40),
        ha="center", color="black", fontsize=10,
        bbox=dict(facecolor="white", edgecolor="red",
                  boxstyle="round,pad=0.35", alpha=0.95),
        zorder=12,
    )

    # ---- Scale bar (1 km) ----
    bar_m = 1000.0
    bar_x = bounds.left + (bounds.right - bounds.left) * 0.04
    bar_y = bounds.bottom + (bounds.top - bounds.bottom) * 0.05
    ax.add_patch(Rectangle((bar_x, bar_y), bar_m, 60,
                           facecolor="black", edgecolor="black", zorder=10))
    ax.text(bar_x + bar_m / 2, bar_y + 120, "1 km",
            ha="center", color="black", fontsize=9,
            bbox=dict(facecolor="white", edgecolor="none", alpha=0.85),
            zorder=11)

    # ---- Colorbar (continuous, fine ticks) ----
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, fraction=0.045, pad=0.02, extend="max",
                        ticks=np.arange(0, 10.5, 0.5))
    cbar.set_label("Elevation (m)  —  ≥10 m shown in grey")
    cbar.ax.tick_params(labelsize=8)

    # ---- Title ----
    elev_valid = elev_arr[valid_mask]
    pct_lt10 = 100.0 * np.sum(elev_valid < 10) / elev_valid.size
    ax.set_title(
        f"{info['slug']}  —  {info['label']}\n"
        f"DEM: GSI {info.get('dem_layer','dem_png')} z{info.get('dem_zoom','?')}"
        f"  ({px:.2f} m px)   bbox WGS84 "
        f"{tuple(round(v,4) for v in info['bbox_wgs84'])}\n"
        f"valid={elev_valid.size:,} px   range={elev_valid.min():.2f}..{elev_valid.max():.2f} m"
        f"   <10m: {pct_lt10:.1f}%   buildings={n_bld:,}   box=300 m"
    )
    ax.set_xlabel(f"Easting (m, {crs})")
    ax.set_ylabel(f"Northing (m, {crs})")
    ax.set_aspect("equal")
    fig.tight_layout()

    out = site_dir / "elevation_0_10m.png"
    fig.savefig(out)
    plt.close(fig)
    print(f"  {out.relative_to(site_dir.parent.parent)}  "
          f"({out.stat().st_size/1024:.0f} KB)   "
          f"px={px:.2f}m   <10m={pct_lt10:.1f}%")
    return out


def main() -> None:
    project_root = Path(__file__).resolve().parent
    root = project_root / "prefetch"
    for site_dir in sorted(root.iterdir()):
        if not site_dir.is_dir():
            continue
        if not (site_dir / "site.json").exists():
            continue
        print(f"\n=== {site_dir.name} ===")
        render_site(site_dir)


if __name__ == "__main__":
    main()
