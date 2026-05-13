"""
End-to-end analysis for every prefetched test site.

Per site this does, in order:

    1. Sink detection (Wang & Liu 2006 Priority-Flood, SAGA `ta_preprocessor 4`
       = QGIS の "Fill sinks (Wang & Liu)" と同一バイナリ).
    2. min_depth / min_area でフィルタ → 住宅を含む窪地を「対象」として残す.
    3. PNG を 2 枚出力. 両方とも「GSI 標準地図 + 1 m bin の色別標高図 overlay」
       共通ベースで、検出マーキングの有無だけが異なる:
         * elevation_gsi_classic.png  ← 元画像 (検出なし、純粋に地形参照)
         * map_overlay_with_sinks.png ← 検出後 (対象窪地=黒塗り、リスク建物=赤塗り、
                                              対象に深さ降順の ID 番号ラベル)
       入力座標まわりの赤枠 + クロスヘア + 4 分割 X は描かない.
       画像タイトルに「○件」型の集計は載せない.
    4. report.txt (日本語) に、画像の ID 番号と一致する順序で対象窪地ごとの
       説明 (中心座標, 含む建物数, 入力点からの方位距離, GSI 検索リンク,
       深さ・面積) を列挙.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
from matplotlib.colors import (
    BoundaryNorm, ListedColormap,
)
from matplotlib.patches import Rectangle
import numpy as np
import geopandas as gpd
import rasterio
from rasterio.warp import (
    Resampling, calculate_default_transform, reproject,
    transform as warp_transform,
)

from rasterio import features as _rio_features

from sink_detection import detect_sinks
from flood_analysis import find_at_risk_buildings


# --------------------------------------------------------------------------- #
# Tuning constants (top of file so they're easy to change)
# --------------------------------------------------------------------------- #

# 二段しきい値: ポリゴンの形状は FillSink (溢れ点まで満たした水域) を
# そのまま反映しつつ、明らかなノイズ窪地は最大深さで丸ごと捨てる。
SINK_MIN_DEPTH      = 0.05  # セル単位 — 深さがこれ未満のセルはポリゴンに含めない。
                            # DEM ノイズ底 (~3 cm) のすぐ上に設定。これで黒塗り
                            # ポリゴン = 「溢れ点まで満たした水域全体」と一致。
SINK_MIN_MAX_DEPTH  = 0.50  # 窪地単位 — 最深点がこれ未満の窪地は丸ごと捨てる。
                            # GSI 5m DEM 写真測量精度 (±30 cm) の倍を確保。
SINK_MIN_AREA       = 1000.0 # 窪地単位 — 約 32 m × 32 m。GSI 1/25,000 で視認可能。
SINK_BACKEND   = "saga"     # QGIS-faithful Wang & Liu (= FillSink そのもの)

# Drop sinks whose base (lowest interior elevation) is above this threshold.
# Matches the user-supplied 12-cell flood palette upper bound (≥10 m = "red").
# A depression sitting at >10 m elevation isn't a flood-risk target for the
# coastal/lowland scope this palette implies (e.g., quarries, mountain valleys).
SINK_MAX_BASE_ELEV_M = 10.0

# User-supplied flood-depth-style palette: 1 m bins from 0 m to 10 m, with
# a separate "≤0 m" colour and a "≥10 m" colour.  Order matches the legend
# image attached by the user (top→bottom).
USER_PALETTE_UNDER = "#0A246B"     # 0 m 以下  (sea / below datum)
USER_PALETTE_BINS = [
    "#0070FF",   # 0 m ～ 1 m 以下
    "#56A0DC",   # 1 m ～ 2 m 以下
    "#00FFFF",   # 2 m ～ 3 m 以下
    "#3FFFC4",   # 3 m ～ 4 m 以下
    "#00FF00",   # 4 m ～ 5 m 以下
    "#C8FF00",   # 5 m ～ 6 m 以下
    "#FFFF00",   # 6 m ～ 7 m 以下
    "#FFCC00",   # 7 m ～ 8 m 以下
    "#FF8C00",   # 8 m ～ 9 m 以下
    "#FF3300",   # 9 m ～ 10 m 以下
]
USER_PALETTE_OVER = "#FF0000"      # 10 m 以上
USER_PALETTE_LABELS = (
    ["0m以下"]
    + [f"{i}m～{i+1}m以下" for i in range(10)]
    + ["10m以上"]
)
DPI = 240
FIGSIZE_IN = 13

# At-risk buildings are NOT drawn on the map. They appear in
# at_risk_buildings.csv/.geojson and per-ID block in report.txt only.
# (User asked 2026-05-06 to remove the red building overlay — building
# footprint rectangles were dominating the map visually.)

# Layered map blend: alpha applied to the elevation overlay above the std map.
# 0.62 keeps the colour bands obvious without obscuring the GSI label text.
OVERLAY_ALPHA = 0.62

# Per-target ID label styling (white text with black halo so it's readable
# both inside the black-filled sink and against the colour overlay).
ID_LABEL_FONTSIZE = 9
ID_LABEL_COLOR    = "white"
ID_LABEL_HALO     = "black"

# CJK font selection
for _cand in ("Noto Sans CJK JP", "Noto Sans CJK HK", "Noto Sans CJK SC",
              "IPAexGothic", "TakaoGothic"):
    if any(f.name == _cand for f in fm.fontManager.ttflist):
        plt.rcParams["font.family"] = _cand
        break
plt.rcParams["axes.unicode_minus"] = False


# --------------------------------------------------------------------------- #
# Geo helpers
# --------------------------------------------------------------------------- #

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlam/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(p2)
    x = math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


_DIRS = ["北", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東",
         "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西"]


def bearing_label(deg: float) -> str:
    return _DIRS[int(round(deg / 22.5)) % 16]


def gsi_search_url(lat: float, lon: float, zoom: int = 17) -> str:
    return (f"https://maps.gsi.go.jp/#{zoom}/{lat:.6f}/{lon:.6f}/"
            f"&base=std&ls=std&disp=1")


# --------------------------------------------------------------------------- #
# Colormap
# --------------------------------------------------------------------------- #

def make_user_cmap() -> tuple[ListedColormap, BoundaryNorm, list, list]:
    """Discrete 12-cell palette matching the user's attached legend image."""
    all_colors = [USER_PALETTE_UNDER] + USER_PALETTE_BINS + [USER_PALETTE_OVER]
    cmap = ListedColormap(all_colors, name="user_legend")
    cmap.set_bad((0.0, 0.0, 0.0, 0.0))
    boundaries = [-50.0] + list(range(0, 11)) + [1000.0]
    norm = BoundaryNorm(boundaries, ncolors=12)
    tick_centers = [-25.0] + [i + 0.5 for i in range(10)] + [505.0]
    return cmap, norm, tick_centers, list(USER_PALETTE_LABELS)


def load_basemap_for_dem(site_dir: Path,
                         dem_crs, left, right, bottom, top, w, h
                         ) -> Optional[np.ndarray]:
    """
    Load prefetched GSI std basemap and resample it onto the DEM grid so that
    matplotlib `imshow` aligns the basemap pixel-perfect with the elevation.
    Returns RGB (H, W, 3) uint8, or None if no basemap is prefetched.
    """
    p = site_dir / "basemap_utm.tif"
    if not p.exists():
        return None
    # 白 (255) で初期化: 基盤地図 z17 と DEM z15 のタイルスナップ差で
    # DEM 範囲外 (= 再投影先で source なし) のピクセルは reproject に
    # 触られず初期値が残る。黒で初期化すると外周が暗く描画されるため白にする。
    out = np.full((3, h, w), 255, dtype=np.uint8)
    dst_transform = rasterio.transform.from_bounds(
        left, bottom, right, top, w, h
    )
    with rasterio.open(p) as src:
        for i in range(3):
            reproject(
                source=rasterio.band(src, i + 1),
                destination=out[i],
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=dst_transform, dst_crs=dem_crs,
                resampling=Resampling.bilinear,
                init_dest_nodata=False,  # 255 (白) 初期値を保持し、source 範囲外を黒く塗らない
            )
    return np.transpose(out, (1, 2, 0))


# --------------------------------------------------------------------------- #
# Per-site analysis
# --------------------------------------------------------------------------- #

def analyse_site(site_dir: Path) -> dict:
    info = json.loads((site_dir / "site.json").read_text())
    slug = info["slug"]
    dem_path = site_dir / "dem_utm.tif"
    print(f"\n=== {slug} ({info['label']}) ===")

    # ---- 1. Sink detection (Wang & Liu, SAGA backend = FillSink) -------- #
    #     min_depth はセル単位の包含しきい値 (= 溢れ点まで満たした水域の縁
    #     を取り込むため低めに設定)。窪地単位の「最深部が浅すぎる」フィルタ
    #     は下で別途実施。
    print(f"  [1/3] FillSink (backend={SINK_BACKEND}, "
          f"cell min_depth={SINK_MIN_DEPTH} m, min_area={SINK_MIN_AREA} m²)")
    sinks_gdf, rasters, sink_info = detect_sinks(
        dem_path,
        backend=SINK_BACKEND, min_slope_deg=0.1,
        min_depth=SINK_MIN_DEPTH, min_area_m2=SINK_MIN_AREA,
        depth_out=site_dir / "depth.tif",
        sinks_out=site_dir / "sinks.geojson",
    )
    print(f"        backend_used={sink_info['backend_used']}  "
          f"→ {len(sinks_gdf)} sink(s) detected (raw)")

    # 窪地単位のフィルタ: 最深点が浅い (= ノイズ由来の偽窪地) を捨てる
    if not sinks_gdf.empty:
        n_before = len(sinks_gdf)
        sinks_gdf = sinks_gdf[
            sinks_gdf["max_depth"] >= SINK_MIN_MAX_DEPTH
        ].reset_index(drop=True)
        print(f"        最深点 ≥ {SINK_MIN_MAX_DEPTH} m を満たすもの: "
              f"{len(sinks_gdf)} / {n_before} 件")

    # ---- 2. Cross-reference with OSM buildings -------------------------- #
    bld_path = site_dir / "buildings.geojson"
    if bld_path.exists() and not sinks_gdf.empty:
        bld = gpd.read_file(bld_path)
        if str(bld.crs) != str(rasters.crs):
            bld = bld.to_crs(rasters.crs)
        at_risk = find_at_risk_buildings(sinks_gdf, bld, rasters)
        at_risk.drop(columns=["geometry"]).to_csv(
            site_dir / "at_risk_buildings.csv", index=False
        )
        if not at_risk.empty:
            at_risk.to_file(site_dir / "at_risk_buildings.geojson",
                            driver="GeoJSON")
        print(f"        住居データ照合: {len(bld)} 棟 → "
              f"窪地内 {len(at_risk)} 棟 (= 浸水リスクあり)")
    else:
        bld = gpd.GeoDataFrame(columns=["building_id", "geometry"],
                                geometry="geometry", crs=rasters.crs)
        at_risk = bld.copy()

    # ---- 2b. Filter sinks to *target* set: depressions that actually
    #     contain residential footprints (the user's definition of "対象") ---- #
    n_sinks_total = len(sinks_gdf)
    if not sinks_gdf.empty and not at_risk.empty:
        sink_ids_with_bld = set(at_risk["sink_id"].unique().tolist())
        target_sinks = sinks_gdf[
            sinks_gdf["sink_id"].isin(sink_ids_with_bld)
        ].sort_values("max_depth", ascending=False).reset_index(drop=True)
    else:
        target_sinks = sinks_gdf.iloc[0:0].copy()

    # ---- 2c. Drop high-elevation depressions: out-of-scope of the 0–10 m
    #     palette (e.g., quarries, mountain valleys, hill-top depressions). ---- #
    n_targets_before_elev_filter = len(target_sinks)
    if not target_sinks.empty:
        elev = rasters.elevation.astype(np.float32).copy()
        if rasters.nodata is not None:
            elev[elev == rasters.nodata] = np.nan
        base_elev = target_sinks.geometry.apply(
            lambda g: _sink_base_elev(g, elev, rasters.transform)
        )
        target_sinks = target_sinks.assign(base_elev=base_elev)
        target_sinks = target_sinks[
            target_sinks["base_elev"] <= SINK_MAX_BASE_ELEV_M
        ].reset_index(drop=True)
        # Also drop their at-risk-building rows so the red overlay matches.
        kept_sink_ids = set(target_sinks["sink_id"].tolist())
        at_risk = at_risk[at_risk["sink_id"].isin(kept_sink_ids)].reset_index(drop=True)

    target_sinks.to_file(site_dir / "target_sinks.geojson", driver="GeoJSON")
    print(f"        対象 (住宅含む & 標高≤{SINK_MAX_BASE_ELEV_M:.0f}m): "
          f"{len(target_sinks)} / {n_targets_before_elev_filter} 件 "
          f"({n_sinks_total} 件中)")

    # ---- 3. Render 2 PNGs ----------------------------------------------- #
    #   元画像 (基盤地図 + 標高、検出なし)
    #   検出後 (基盤地図 + 標高 + 黒塗り+ID)
    print("  [3/4] Render 2 PNGs")
    out_classic_bm    = render_classic_with_basemap(site_dir, info, rasters)
    out_layered_bm, _ = render_layered_with_basemap(site_dir, info, target_sinks, rasters)

    # 旧版で生成された PNG が残っていれば削除
    for legacy_name in ("elevation_only.png", "detection_only.png"):
        legacy = site_dir / legacy_name
        if legacy.exists():
            legacy.unlink()

    # ---- 4. Report ------------------------------------------------------ #
    print("  [4/4] Write report")
    out_txt = write_report(site_dir, info, target_sinks, sink_info,
                            bld, at_risk, n_sinks_total)

    return {"slug": slug,
            "n_sinks_total": int(n_sinks_total),
            "n_target_sinks": int(len(target_sinks)),
            "n_buildings": int(len(bld)),
            "n_at_risk": int(len(at_risk)),
            "png_classic_with_basemap": str(out_classic_bm),
            "png_layered_with_basemap": str(out_layered_bm),
            "report": str(out_txt)}


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _elev_geometry(rasters):
    """Return (elev_arr, valid_mask, transform fields) for the DEM rasters."""
    elev_arr = rasters.elevation.astype(np.float32).copy()
    nodata = rasters.nodata
    if nodata is not None:
        elev_arr[elev_arr == nodata] = np.nan
    valid_mask = ~np.isnan(elev_arr)
    px = abs(rasters.transform.a)
    py = abs(rasters.transform.e)
    h, w = elev_arr.shape
    left = rasters.transform.c
    top = rasters.transform.f
    right = left + w * px
    bottom = top - h * py
    return elev_arr, valid_mask, px, py, left, right, bottom, top, h, w


def _ref_xy(info: dict, crs) -> tuple[float, float]:
    xs, ys = warp_transform("EPSG:4326", crs,
                            [info["center_lon"]], [info["center_lat"]])
    return xs[0], ys[0]


def _user_bbox_outer_in_dem_crs(site_dir: Path, dem_crs) -> Optional[tuple[float, float, float, float]]:
    """ユーザの WGS84 bbox を DEM CRS に投影し、4 隅を含む外接 (axis-aligned)
    矩形を (left, bottom, right, top) で返す。GSI Maps スクリーンショットと
    同一範囲を表示するためのクリップ範囲として使う。"""
    site_json = site_dir / "site.json"
    if not site_json.exists():
        return None
    info = json.loads(site_json.read_text())
    bbox = info.get("bbox_wgs84")
    if not bbox or len(bbox) != 4:
        return None
    west, south, east, north = bbox
    xs, ys = warp_transform(
        "EPSG:4326", dem_crs,
        [west,  east,  west,  east],
        [south, south, north, north],
    )
    return min(xs), min(ys), max(xs), max(ys)


def _user_bbox_in_dem_crs(site_dir: Path, dem_crs) -> Optional[tuple[float, float, float, float]]:
    """Return the *inscribed* axis-aligned rectangle of the user's WGS84 bbox
    after projection to DEM CRS, as (left, bottom, right, top). The user's
    bbox is a rectangle in WGS84/Mercator but becomes a slightly rotated
    parallelogram in UTM. Taking the bounding box would re-introduce the NaN
    corners; taking the inscribed rectangle (max over the two southerly points
    for `bottom`, etc.) guarantees every pixel inside is covered by the
    parallelogram, hence by valid DEM data."""
    site_json = site_dir / "site.json"
    if not site_json.exists():
        return None
    info = json.loads(site_json.read_text())
    bbox = info.get("bbox_wgs84")
    if not bbox or len(bbox) != 4:
        return None
    west, south, east, north = bbox
    # 4 corners in WGS84: SW, SE, NW, NE
    xs, ys = warp_transform(
        "EPSG:4326", dem_crs,
        [west,  east,  west,  east],
        [south, south, north, north],
    )
    sw = (xs[0], ys[0])
    se = (xs[1], ys[1])
    nw = (xs[2], ys[2])
    ne = (xs[3], ys[3])
    inscribed_left   = max(nw[0], sw[0])  # right-most of the two left edges
    inscribed_right  = min(ne[0], se[0])  # left-most of the two right edges
    inscribed_top    = min(nw[1], ne[1])  # lower of the two top edges
    inscribed_bottom = max(sw[1], se[1])  # upper of the two bottom edges
    return inscribed_left, inscribed_bottom, inscribed_right, inscribed_top


def _inscribed_rect(valid_mask: np.ndarray) -> tuple[int, int, int, int]:
    """Return (r0, r1, c0, c1) — the largest axis-aligned rectangle whose
    every pixel is True in ``valid_mask``. Used to crop NaN corners produced
    by Mercator → UTM reprojection so the rendered figure has elevation
    coverage on every visible pixel."""
    h, w = valid_mask.shape
    r0, r1 = 0, h
    c0, c1 = 0, w
    # Iterative shrink: trim whichever edge still hits a NaN. Bounded by
    # h + w iterations because each step cuts at least 1 row or column.
    for _ in range(h + w):
        if r0 >= r1 or c0 >= c1:
            break
        bad_top    = not valid_mask[r0,    c0:c1].all()
        bad_bot    = not valid_mask[r1-1,  c0:c1].all()
        bad_left   = not valid_mask[r0:r1, c0   ].all()
        bad_right  = not valid_mask[r0:r1, c1-1 ].all()
        if not (bad_top or bad_bot or bad_left or bad_right):
            break
        # Trim the worst edge first so we converge faster.
        candidates = []
        if bad_top:    candidates.append(("top",    int((~valid_mask[r0,    c0:c1]).sum())))
        if bad_bot:    candidates.append(("bot",    int((~valid_mask[r1-1,  c0:c1]).sum())))
        if bad_left:   candidates.append(("left",   int((~valid_mask[r0:r1, c0   ]).sum())))
        if bad_right:  candidates.append(("right",  int((~valid_mask[r0:r1, c1-1 ]).sum())))
        worst = max(candidates, key=lambda kv: kv[1])[0]
        if   worst == "top":    r0 += 1
        elif worst == "bot":    r1 -= 1
        elif worst == "left":   c0 += 1
        elif worst == "right":  c1 -= 1
    return r0, r1, c0, c1


def _sink_base_elev(geom, elevation, transform) -> float:
    """Lowest DEM elevation inside the polygon (= depression's bottom).
    Returns +inf if no valid pixels intersect the polygon."""
    mask = _rio_features.geometry_mask(
        [geom], out_shape=elevation.shape, transform=transform, invert=True
    )
    vals = elevation[mask]
    vals = vals[~np.isnan(vals)]
    if vals.size == 0:
        return float("inf")
    return float(vals.min())


# 実行時に外部から書き換える: run_site.py が `--scalebar-m 100` で上書きする。
SCALEBAR_M_OVERRIDE: float | None = None

_SCALEBAR_NICE_M = (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)


def _pick_scalebar_m(width_m: float) -> float:
    """図の幅 (m) から「いい感じ」のスケールバー長 (m) を選ぶ。
    バーが図幅の約 12% を占めるサイズを目標に、{10, 20, 50, 100, 200, 500, ...} m
    の中から最も近いものを返す。GSI マップが使う step に合わせている。"""
    target = width_m * 0.12
    return float(min(_SCALEBAR_NICE_M, key=lambda c: abs(c - target)))


def _draw_scalebar(ax, left, right, bottom, top, *, bar_m: float | None = None):
    width_m = right - left
    height_m = top - bottom
    if bar_m is None:
        bar_m = SCALEBAR_M_OVERRIDE if SCALEBAR_M_OVERRIDE is not None else _pick_scalebar_m(width_m)
    bar_h = height_m * 0.012
    bar_x = left + width_m * 0.04
    bar_y = bottom + height_m * 0.05
    # 窪地ポリゴン (zorder=11) や ID ラベル (zorder=15) より上に置く。
    # 窪地が左下を完全に覆うサイトでもスケールバーが必ず見えるようにする。
    SB_BG_Z = 19
    SB_RECT_Z = 20
    SB_TEXT_Z = 21
    # 白い角丸背景 (バー本体 + ラベル領域を覆うサイズ) を先に敷く
    pad_x = bar_m * 0.20
    pad_y = bar_h * 1.5
    ax.add_patch(Rectangle((bar_x - pad_x, bar_y - pad_y),
                           bar_m + 2 * pad_x, bar_h * 6,
                           facecolor="white", edgecolor="black",
                           linewidth=0.5, alpha=0.92, zorder=SB_BG_Z))
    ax.add_patch(Rectangle((bar_x, bar_y), bar_m, bar_h,
                           facecolor="black", edgecolor="black",
                           zorder=SB_RECT_Z))
    label = f"{bar_m:.0f} m" if bar_m < 1000 else f"{bar_m/1000:.0f} km"
    ax.text(bar_x + bar_m / 2, bar_y + bar_h * 3, label,
            ha="center", color="black", fontsize=9,
            zorder=SB_TEXT_Z)


# --------------------------------------------------------------------------- #
# Shared base: GSI 標準地図 + 色別標高図 overlay (12-cell user palette)
# --------------------------------------------------------------------------- #

def _draw_basemap_overlay(site_dir: Path, info: dict, rasters,
                          with_basemap: bool = True):
    """
    Shared base for all PNGs: optional GSI 標準地図 background + the user's
    discrete 1 m-bin elevation overlay + colorbar + scalebar.

    `with_basemap=True`  → 標準地図 + 色別標高図 overlay
    `with_basemap=False` → 色別標高図 のみ (背景は白)

    Returns (fig, ax, extent, crs, px, base_label) so callers can add markings.
    """
    elev_arr, valid_mask, px, py, left, right, bottom, top, h, w = _elev_geometry(rasters)
    crs = rasters.crs
    extent = (left, right, bottom, top)

    cmap, norm, cb_ticks, cb_labels = make_user_cmap()
    elev_masked = np.ma.array(elev_arr, mask=~valid_mask)
    rgba = cmap(norm(elev_masked))

    fig, ax = plt.subplots(figsize=(FIGSIZE_IN, FIGSIZE_IN), dpi=DPI)

    # 標高オーバーレイ用に NaN を最近傍補完で埋める。元の DEM (rasters.elevation)
    # は窪地検出で既に使用済みで結果は確定しているため、ここでの補完は表示専用。
    # 効果: Mercator → UTM 再投影で生じる平行四辺形外の NaN 三角や、海域などの
    # 大きな NaN 領域も、近傍 DEM の標高色で埋まり、図全体が標高で塗り分けられる。
    if not valid_mask.all() and valid_mask.any():
        from scipy.ndimage import distance_transform_edt
        nearest_idx = distance_transform_edt(
            ~valid_mask, return_distances=False, return_indices=True
        )
        elev_filled = elev_arr[tuple(nearest_idx)]
        rgba = cmap(norm(elev_filled))

    if with_basemap:
        basemap_rgb = load_basemap_for_dem(site_dir, crs, left, right, bottom, top, w, h)
        has_basemap = basemap_rgb is not None
        if has_basemap:
            ax.imshow(basemap_rgb, extent=extent, origin="upper",
                      interpolation="bilinear")
            rgba[..., 3] = OVERLAY_ALPHA
            base_label = "GSI 標準地図 + 色別標高図 overlay"
        else:
            rgba[..., 3] = 1.0
            base_label = "色別標高図 のみ (basemap_utm.tif 未取得)"
    else:
        # No basemap requested — full-opacity colour bands on white background.
        rgba[..., 3] = 1.0
        base_label = "色別標高図 のみ (標準地図なし)"

    ax.imshow(rgba, extent=extent, origin="upper", interpolation="bilinear")

    # 表示範囲をユーザの bbox_wgs84 を UTM へ投影した外接矩形に揃える。
    # 目的: GSI Maps のスクリーンショットと「同じ範囲・同じ縮尺」で並べて
    # 比較できるようにすること。DEM 自体はタイル境界スナップで bbox より
    # 大きく取得されているが、表示は厳密にユーザ指定の bbox に切り替える。
    # 平行四辺形外の NaN 隅は前段の最近傍補完で既に色が入っており欠落は無い。
    user_bbox = _user_bbox_outer_in_dem_crs(site_dir, crs)
    if user_bbox is not None:
        v_left, v_bottom, v_right, v_top = user_bbox
        ax.set_xlim(v_left, v_right)
        ax.set_ylim(v_bottom, v_top)
        sb_left, sb_right, sb_bottom, sb_top = v_left, v_right, v_bottom, v_top
    elif valid_mask.any():
        rows = np.where(valid_mask.any(axis=1))[0]
        cols = np.where(valid_mask.any(axis=0))[0]
        v_left   = left + cols.min() * px
        v_right  = left + (cols.max() + 1) * px
        v_top    = top  - rows.min() * py
        v_bottom = top  - (rows.max() + 1) * py
        ax.set_xlim(v_left, v_right)
        ax.set_ylim(v_bottom, v_top)
        sb_left, sb_right, sb_bottom, sb_top = v_left, v_right, v_bottom, v_top
    else:
        sb_left, sb_right, sb_bottom, sb_top = left, right, bottom, top

    _draw_scalebar(ax, sb_left, sb_right, sb_bottom, sb_top)

    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, fraction=0.055, pad=0.02, spacing="uniform")
    cbar.set_ticks(cb_ticks)
    cbar.set_ticklabels(cb_labels)
    cbar.set_label("標高 (m)")
    cbar.ax.tick_params(labelsize=8)

    ax.set_xlabel(f"Easting (m, {crs})")
    ax.set_ylabel(f"Northing (m, {crs})")
    ax.set_aspect("equal")

    return fig, ax, extent, crs, px, base_label


# --------------------------------------------------------------------------- #
# Label placement
# --------------------------------------------------------------------------- #

def _label_position(geom):
    """Return (x, y) to place an ID number. representative_point() is
    guaranteed inside even for irregular shapes; centroid can fall outside."""
    try:
        pt = geom.representative_point()
    except Exception:
        pt = geom.centroid
    return pt.x, pt.y


def _draw_target_sinks(ax, sinks_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Plot target sinks as solid black + per-target ID labels (depth rank).

    現在の軸 xlim/ylim から外れる窪地はレンダリングしない (ID 番号が図の外側
    の白余白に飛ぶのを防ぐ)。ID は元 GeoDataFrame の深さ降順ランクで固定。"""
    if sinks_gdf.empty:
        return sinks_gdf.copy()
    ranked = sinks_gdf.sort_values("max_depth", ascending=False).reset_index(drop=True)
    ranked["rank"] = np.arange(1, len(ranked) + 1)

    from shapely.geometry import box as _shp_box
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    view_box = _shp_box(xmin, ymin, xmax, ymax)
    visible_mask = ranked.geometry.intersects(view_box)
    visible = ranked[visible_mask]

    if visible.empty:
        return ranked

    visible.plot(ax=ax, facecolor="black", edgecolor="none",
                 zorder=11, antialiased=True)
    for _, row in visible.iterrows():
        rank = int(row["rank"])
        lx, ly = _label_position(row.geometry)
        # Always anchor each ID with a small black dot. Tiny sinks (< ~1000 m²)
        # render as just a few image pixels and would otherwise be hidden under
        # the label; for large sinks the dot is invisibly absorbed into the fill.
        ax.plot(lx, ly, marker="o", markersize=12,
                markerfacecolor="black", markeredgecolor="none",
                linestyle="none", zorder=12, clip_on=True)
        t = ax.text(lx, ly, str(rank),
                    color=ID_LABEL_COLOR, fontsize=ID_LABEL_FONTSIZE,
                    fontweight="bold", ha="center", va="center", zorder=15,
                    clip_on=True)
        t.set_path_effects([
            path_effects.Stroke(linewidth=2.5, foreground=ID_LABEL_HALO),
            path_effects.Normal(),
        ])
    return ranked


def _title_dem_line(info: dict, px: float) -> str:
    return (f"DEM: GSI {info.get('dem_layer','dem_png')} "
            f"z{info.get('dem_zoom','?')} ({px:.2f} m px)")


# --------------------------------------------------------------------------- #
# Render A: 元画像 (検出処理なし)
# --------------------------------------------------------------------------- #

def render_classic_with_basemap(site_dir: Path, info: dict, rasters) -> Path:
    """標準地図 + 色別標高図 overlay。検出マーキングなし。"""
    fig, ax, extent, crs, px, base_label = _draw_basemap_overlay(
        site_dir, info, rasters, with_basemap=True)
    ax.set_title(
        f"{info['slug']}  —  {info['label']}\n"
        f"{base_label}（検出処理なし、地形参照用）   {_title_dem_line(info, px)}"
    )
    fig.tight_layout()
    out = site_dir / "elevation_gsi_classic.png"
    fig.savefig(out)
    plt.close(fig)
    return out


# --------------------------------------------------------------------------- #
# Render B: 検出後 (対象窪地=黒塗り + ID 番号; 建物の赤フィルは描画しない)
# --------------------------------------------------------------------------- #

def render_layered_with_basemap(site_dir: Path, info: dict,
                                sinks_gdf: gpd.GeoDataFrame, rasters) -> tuple[Path, gpd.GeoDataFrame]:
    """標準地図 + 色別標高図 + 対象窪地=黒塗り + ID 番号。"""
    fig, ax, extent, crs, px, base_label = _draw_basemap_overlay(
        site_dir, info, rasters, with_basemap=True)
    ranked = _draw_target_sinks(ax, sinks_gdf)
    ax.set_title(
        f"{info['slug']}  —  {info['label']}\n"
        f"{base_label}   {_title_dem_line(info, px)}\n"
        f"FillSink (Wang & Liu, SAGA): 対象窪地=黒塗り + ID 番号"
    )
    fig.tight_layout()
    out = site_dir / "map_overlay_with_sinks.png"
    fig.savefig(out)
    plt.close(fig)
    return out, ranked




# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

METHOD_BLOCK = """\
■ 手法
本検出は GSI dem5a_png（5 m 写真測量 DEM）を Web メルカトル → 局所 UTM に
再投影したラスタに対し、Wang & Liu (2006) "Priority-Flood" 窪地充填
アルゴリズムを SAGA GIS の `ta_preprocessor 4`（QGIS の "Fill sinks
(Wang & Liu)" が内部で呼ぶ実装と同一バイナリ）で実行する。

  1. Fill: 窪地を最低流出標高まで埋め、わずかな勾配 (0.1°) を保持して
     充填DEM を得る。
  2. Depth = 充填DEM − 元DEM。深さ 0 m 以上の連続領域を rasterio.features
     でポリゴン化。
  3. 各ポリゴンについて max_depth, mean_depth, area_m² を集計し、
     min_depth ≥ {min_depth} m, min_area ≥ {min_area:.0f} m² でフィルタ。

検出された各ポリゴンが「降った雨が逃げ場を失って溜まる窪地（せき止め
地形）」に対応する。これは QGIS の Pseudocolor + Hillshade + Contour で
目視判別する 色別標高図 と同じ DEM・同じアルゴリズムを使った
**自動抽出版** にあたる。
"""


def write_report(site_dir: Path,
                 info: dict,
                 sinks_gdf: gpd.GeoDataFrame,
                 sink_info: dict,
                 buildings: gpd.GeoDataFrame,
                 at_risk: gpd.GeoDataFrame,
                 n_sinks_total: int = None) -> Path:
    out = site_dir / "report.txt"
    ranked = sinks_gdf.sort_values("max_depth", ascending=False).reset_index(drop=True)
    ranked_wgs = ranked.to_crs(4326) if not ranked.empty else ranked

    # at-risk count per sink for the per-sink listing
    if not at_risk.empty and "sink_id" in at_risk.columns:
        bld_per_sink = at_risk.groupby("sink_id").size().to_dict()
    else:
        bld_per_sink = {}

    ref_lat = info["center_lat"]
    ref_lon = info["center_lon"]
    ref_url = gsi_search_url(ref_lat, ref_lon)

    lines = []
    lines.append(f"==============================================================")
    lines.append(f"  {info['slug']}  —  {info['label']}")
    lines.append(f"==============================================================")
    lines.append("")
    lines.append(f"■ 入力ユーザ座標")
    lines.append(f"  緯度経度 (WGS84): {ref_lat:.6f}°N, {ref_lon:.6f}°E")
    lines.append(f"  GSI 検索リンク : {ref_url}")
    lines.append(f"  解析範囲 (bbox): {tuple(round(v, 4) for v in info['bbox_wgs84'])}")
    lines.append(f"  DEM ソース     : GSI {info.get('dem_layer','?')} z{info.get('dem_zoom','?')} "
                 f"(画素 ≈ {sink_info['cell_size_m']:.2f} m)")
    lines.append(f"  CRS            : {sink_info['crs']}")
    lines.append("")
    lines.append(METHOD_BLOCK.format(min_depth=SINK_MIN_DEPTH,
                                     min_area=SINK_MIN_AREA))
    lines.append("")
    lines.append(f"■ 対象地形ごとの説明 (画像内 ID 番号 = 最大深さ降順)")
    lines.append("")
    if ranked.empty:
        lines.append("  (該当なし)")
    else:
        for i, row in ranked.iterrows():
            rank = i + 1
            geom_wgs = ranked_wgs.geometry.iloc[i]
            cwgs = geom_wgs.centroid
            slat, slon = cwgs.y, cwgs.x
            d = haversine_m(ref_lat, ref_lon, slat, slon)
            b = bearing_deg(ref_lat, ref_lon, slat, slon)
            blab = bearing_label(b)

            geom_dem = row.geometry
            mnx, mny, mxx, mxy = geom_dem.bounds
            extent_ew = mxx - mnx
            extent_ns = mxy - mny

            n_bld_in = bld_per_sink.get(int(row["sink_id"]), 0)
            base_elev_str = (f"   底面標高 {row['base_elev']:.1f} m"
                             if "base_elev" in row.index else "")
            lines.append(f"  ID {rank}")
            lines.append(f"    中心座標 (WGS84) : {slat:.6f}°N, {slon:.6f}°E")
            lines.append(f"    内部の住居数     : {n_bld_in} 棟")
            lines.append(f"    最大深さ         : {row['max_depth']:.2f} m"
                         f"   平均深さ {row['mean_depth']:.2f} m"
                         f"{base_elev_str}")
            lines.append(f"    面積             : {row['area_m2']:,.0f} m²"
                         f"   範囲 (東西×南北) {extent_ew:.0f} m × {extent_ns:.0f} m")
            lines.append(f"    入力点からの位置 : {blab} 方向 約 {d:.0f} m")
            lines.append(f"    GSI 検索リンク   : {gsi_search_url(slat, slon)}")
            lines.append("")
    lines.append("==============================================================")

    out.write_text("\n".join(lines))
    return out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> None:
    project_root = Path(__file__).resolve().parent
    root = project_root / "prefetch"
    summary = []
    for site_dir in sorted(root.iterdir()):
        if not site_dir.is_dir():
            continue
        if not (site_dir / "site.json").exists():
            continue
        try:
            summary.append(analyse_site(site_dir))
        except Exception as e:
            print(f"  ✗ FAILED for {site_dir.name}: {e}")
            summary.append({"slug": site_dir.name, "error": str(e)})
    (root / "analysis_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nSummary: {root / 'analysis_summary.json'}")


if __name__ == "__main__":
    main()
