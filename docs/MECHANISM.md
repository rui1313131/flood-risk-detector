# MECHANISM — flood_risk_detector

このドキュメントは「**どの入力**が**どこのデータ**を取りに行き、**どの順番で何の処理**を経て、**どこに何が書かれる**か」を記述する。最新版コード (`analyze_sites.py`, `run_site.py`) と整合。

---

## 1. 入力エントリの 3 通り

| エントリ | 入力 | 想定ユース |
|---|---|---|
| `run_site.py <lat> <lon> [--label ...]` | 座標 1 組 | 任意地点を 1 コマンドで解析（推奨） |
| `prefetch_test_sites.py` | コード上の `SITES = [...]` | 固定 3 サイト一括 prefetch |
| `analyze_sites.py` | 引数なし | `prefetch/` 配下の prefetch 済み全サイトを解析 |

`run_site.py` は `prefetch_site() → fetch_gsi_basemap() → analyse_site()` を順に呼ぶ薄いラッパで、ユーザ入力は `(lat, lon)` の 1 回のみ。同じ slug で再実行すれば DEM は再ダウンロードされず、しきい値変更だけで再解析できる。

---

## 2. データソースと地理的範囲

| データ | ソース | URL / API | 範囲 | 解像度 |
|---|---|---|---|---|
| DEM | GSI `dem5a_png` z15 | `cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png` | **日本のみ** | 5 m（写真測量）|
| 建物 | OpenStreetMap Overpass | `overpass-api.de` | 世界中 | ベクタ |
| 背景地図 | GSI 標準地図 z17 | `cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png` | **日本のみ** | ラスタ |

DEM ソースは `dem_fetcher.py` で GSI 決め打ち。海外座標を入れると空タイル（NaN）が返り、`prefetch_site()` 内で `RuntimeError("DEM has no valid pixels")` で停止する。

---

## 3. データフロー（`run_site.py` の場合）

```
ユーザ入力: python3 run_site.py 36.496985 140.613220 --label "..."
   │
   ├─ slug 自動生成: site_36p497N_140p613E
   ├─ bbox 計算   : (lon-0.02, lat-0.02, lon+0.02, lat+0.02)
   │                ≈ 4.4 km × 3.6 km @ 35°N
   │
   ├─ 1. prefetch_site() → prefetch/<slug>/
   │     ・dem_mercator.tif  (GSI z15 タイルモザイク, 中間生成物)
   │     ・dem_utm.tif       (UTM 再投影 ≈ 2.4 m px, 解析の主入力)
   │     ・buildings.geojson (OSM 建物フットプリント, DEM CRS)
   │     ・site.json         (中心座標, bbox, dem_layer, zoom などメタデータ)
   │
   ├─ 2. fetch_gsi_basemap() → prefetch/<slug>/basemap_utm.tif
   │     (GSI 標準地図 z17 を UTM 再投影した RGB GeoTIFF)
   │
   └─ 3. analyse_site() → prefetch/<slug>/
         ・depth.tif                     (Wang & Liu 充填DEM − 元DEM)
         ・sinks.geojson                 (検出された全窪地ポリゴン)
         ・target_sinks.geojson          (住宅含む & 底面標高 ≤10 m のもの)
         ・at_risk_buildings.csv/.geojson (上記 target 内に位置する建物)
         ・elevation_gsi_classic.png     (元画像: 標準地図+色別標高図, 検出なし)
         ・map_overlay_with_sinks.png    (検出後: 標準地図+標高+黒塗り対象+ID)
         ・report.txt                    (日本語; ID ごとの説明)
```

---

## 4. 解析アルゴリズム（4 段）

### 4.1 Fill (Wang & Liu 2006 Priority-Flood)
SAGA `ta_preprocessor 4` バックエンド = QGIS の "Fill sinks (Wang & Liu)" が呼ぶ同一バイナリ。最小勾配 0.1° を保持。`sink_detection.py` で `backend="saga"` を強制指定。

### 4.2 Depth = Filled − Original
0 m 超の連続領域を `rasterio.features.shapes` でポリゴン化。各ポリゴンに `sink_id, max_depth, mean_depth, area_m2` を付与。

### 4.3 ノイズ・スコープフィルタ（3 段重ね）

| フィルタ | 値 | 目的 |
|---|---|---|
| `min_depth ≥ 0.20 m` | 浅い水たまりを除外 | DEM ノイズ |
| `min_area ≥ 100 m²` | 小さい斑点を除外 | 5 m DEM 5 セル分相当 |
| **建物含有判定** | OSM 建物フットプリントが 1 棟以上ポリゴン内に交差 | 「対象=住居がある窪地」に限定 |
| **底面標高 ≤ 10 m** | 窪地内の最低 DEM 標高 ≤ `SINK_MAX_BASE_ELEV_M` (= 10 m) | パレット範囲 (0–10 m) 外の高地・山間部窪地を除外 |

最終的に残ったポリゴンが `target_sinks.geojson`、その中の建物が `at_risk_buildings.csv/.geojson`。

### 4.4 可視化（PNG 2 枚）

| ファイル | 基盤地図 | 検出マーキング | 用途 |
|---|---|---|---|
| `elevation_gsi_classic.png` | あり | なし | 元画像（標準地図 + 標高で地形参照）|
| `map_overlay_with_sinks.png` | あり | あり | 検出後（黒塗り対象 + ID 番号）|

**検出マーキング**（後者 2 枚に共通）：
- **対象窪地: 純黒塗り、輪郭線無し**
- **対象窪地内に ID 番号** (深さ降順、1 = 最深)
  - 配置: `polygon.representative_point()` で内部に置く（不正多角形でも内側）
  - スタイル: 白文字 + 黒ハロー（path_effects.Stroke）でどんな背景でも読める

**標高オーバーレイの透過度**:
- 基盤地図あり: α = 0.62 (`OVERLAY_ALPHA`)
- 基盤地図なし: α = 1.0 (背景は白)

**描画しないもの**（明示的に除外）：
- 入力座標を囲む赤い四角・クロスヘア・「+」マーカー
- 画像を 4 分割するような線
- 各窪地の数値ラベル（深さ・面積など）。ID 番号のみ。
- **浸水リスク建物の赤フィル**（2026-05-06 削除）。建物が長方形フットプリントとして「赤い四角」状に集合するため。建物の存在は `at_risk_buildings.csv` と `report.txt` の per-ID ブロックで参照。

タイトルに「○件」型の集計**は載せない**。集計は `report.txt` でのみ。

---

## 5. レポート (`report.txt`) の構造

```
■ 入力ユーザ座標         — 緯度経度・GSI 検索リンク・bbox・DEM ソース・CRS
■ 手法                   — Wang & Liu / SAGA / 閾値の説明
■ 対象地形ごとの説明     — 画像 ID 番号と一致 (深さ降順)
    ID 1
      中心座標 (WGS84)   : 36.502918°N, 140.612752°E
      内部の住居数       : 1 棟
      最大深さ           : 4.74 m   平均深さ 3.31 m   底面標高 2.3 m
      面積               : 6,025 m²   範囲 (東西×南北) 92 m × 123 m
      入力点からの位置   : 北 方向 約 661 m
      GSI 検索リンク     : https://maps.gsi.go.jp/...
    ID 2
      ...
```

画像内 ID = レポート内 ID = depth-rank (max_depth 降順) で 1 対 1 対応。

---

## 6. 主要パラメータ（`analyze_sites.py` の頭で定数）

| 定数 | 既定値 | 効果 |
|---|---|---|
| `SINK_MIN_DEPTH` | 0.20 m | 浅い窪地を切る |
| `SINK_MIN_AREA`  | 100 m² | 小さい窪地を切る |
| `SINK_MAX_BASE_ELEV_M` | 10.0 m | 高地の窪地を切る（パレット上限と一致）|
| `SINK_BACKEND` | `"saga"` | Wang & Liu 実装。`auto` で saga > richdem > pure |
| `OVERLAY_ALPHA` | 0.62 | 標高 overlay の透過度 |
| `ID_LABEL_FONTSIZE` | 9 pt | 画像内 ID 番号の文字サイズ |
| `USER_PALETTE_BINS` | 12 セル, 1 m bins, dark-navy → red | 色別標高図のパレット |

しきい値だけ変えて再実行する場合は同じ slug で `run_site.py` を呼べば DEM は再ダウンロードされない。

---

## 7. 再現性

`prefetch/<slug>/manifest.json`（`manifest.py` が書く）に：
- 入力ファイル sha256
- 主要ライブラリバージョン (rasterio, geopandas, shapely, numpy, scipy)
- 出力ファイル sha256
- SAGA バージョン
- プロンプト/設定のハッシュ

同じ DEM・同じパラメータ・同じバージョンなら、別 PC でも `at_risk_buildings.csv` と `sinks.geojson` の内容が **bit-identical** になる。Hitachi/Kirishima/Sakata の検出件数（295/508/448 sinks, 129/5988/4489 buildings）は送信側 PC（Linux）と受信側 PC（WSL2 Ubuntu）で完全一致を確認済み (2026-05-06)。

---

## 8. ドキュメントを書く場所

| 何を書くか | どこに書くか |
|---|---|
| 使い方（実行コマンド、配布物の中身）| `QUICKSTART.md` |
| **アルゴリズム / データフロー / フィルタ / 出力定義** | **`docs/MECHANISM.md` (このファイル)** |
| 実験ログ（いつ何を試して何が出たか）| `logs/experiments.md` |
| 開発進捗 / 構成変更 | `PROGRESS.md` |

新しい機能・閾値変更を追加したら、まず `docs/MECHANISM.md` を更新してから、`logs/experiments.md` に走らせた結果を追記する。
