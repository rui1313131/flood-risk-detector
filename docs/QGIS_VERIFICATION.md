# QGIS で本システムの窪地検出結果を再現する手順

本システムが出力する黒塗り（対象窪地）が、本当に **QGIS の "Fill sinks (Wang & Liu)" アルゴリズム**を通った正しい窪地検出結果であることを、QGIS 上でゼロから再現して検証する手順。

---

## 0. 前提

| 項目 | 値 |
|---|---|
| QGIS バージョン | 3.22 LTR 以上（3.34 推奨） |
| 必要なプロバイダ | **SAGA** （Processing Toolbox に標準統合）|
| アルゴリズム | `saga:fillsinkswangliu`（= SAGA `ta_preprocessor 4`）|
| 入力 DEM | `prefetch/<slug>/dem_utm.tif`（システム出力をそのまま使う）|
| 比較対象 | `prefetch/<slug>/target_sinks.geojson` ＋ `prefetch/<slug>/depth.tif` |

QGIS 3.34 で SAGA が表示されない場合は、`Settings → Options → Processing → Providers` で **SAGA** を有効化（または `sudo apt install saga` で SAGA 本体を入れる）。

---

## 1. ファイルを QGIS にロード

```
レイヤ → レイヤを追加 → ラスタレイヤを追加 → dem_utm.tif
レイヤ → レイヤを追加 → ベクタレイヤを追加 → target_sinks.geojson
レイヤ → レイヤを追加 → ラスタレイヤを追加 → depth.tif    (本システムの結果、参照用)
```

例: kirishima なら
- `\\wsl.localhost\Ubuntu\home\msi\flood_risk_detector\prefetch\kirishima_hayato\dem_utm.tif`
- `\\wsl.localhost\Ubuntu\home\msi\flood_risk_detector\prefetch\kirishima_hayato\target_sinks.geojson`
- `\\wsl.localhost\Ubuntu\home\msi\flood_risk_detector\prefetch\kirishima_hayato\depth.tif`

---

## 2. QGIS で Wang & Liu 充填法を実行

`Processing Toolbox`（Ctrl+Alt+T）を開き、検索ボックスに「**fill sinks**」と入力。

候補: **SAGA → Terrain Analysis — Hydrology → Fill Sinks (Wang & Liu)**
（内部 ID: `saga:fillsinkswangliu`）

ダイアログで以下を設定:

| 入力 | 値 |
|---|---|
| **DEM** | `dem_utm` |
| **Minimum Slope [Degree]** | `0.1`  ← **必ずこの値**（システムと同一）|
| **Filled DEM** | 任意（例: `qgis_filled.tif`）|
| **Flow Directions** | 任意（不要）|
| **Watershed Basins** | 任意（不要）|

**Run** をクリック。数秒〜10 秒で完了。

---

## 3. 深さラスタを計算（充填DEM − 元DEM）

`Raster → Raster Calculator...` で:

```
"qgis_filled@1" - "dem_utm@1"
```

| 設定 | 値 |
|---|---|
| Output layer | `qgis_depth.tif` |
| Output extent | `dem_utm` の範囲を指定 |
| Output resolution | `dem_utm` と同じ |
| CRS | DEM と同じ UTM |

---

## 4. 本システムの depth.tif と差分を取って一致を確認

`Raster Calculator` で:

```
"qgis_depth@1" - "depth@1"
```

→ 全ピクセルが **0 ± 1e−4 m** ならアルゴリズムは bit-identical。
ヒストグラム（`Properties → Histogram`）を見て中心が 0 にあれば OK。

> 完全に 0 でなく数 mm の差が出る場合: SAGA のタイブレーカ（同標高セルの処理順）の違いで稀に起こる。本システムも内部で SAGA を呼んでいるので、同じ SAGA バージョンなら差は出ないはず。

---

## 5. 窪地ポリゴンを作成（QGIS 単独で再現）

### 5.1 深さ閾値で 2 値化

`Raster → Conversion → Reclassify by Table` または `Raster Calculator`:

```
("qgis_depth@1" >= 0.05) * 1
```

→ `qgis_mask.tif`（0 と 1 の 2 値ラスタ）

### 5.2 ベクタ化（ラスタ → ポリゴン）

`Raster → Conversion → Polygonize (Raster to Vector)`:

| 入力 | 値 |
|---|---|
| Input layer | `qgis_mask` |
| Band | `1` |
| Field name | `DN` |
| Output | `qgis_polys.shp` または `.geojson` |

→ 値 1 のピクセル群がポリゴンになる。値 0 のポリゴンは **属性で抽出**（`Vector → Geoprocessing Tools → Select by attribute → DN = 1`）して残す。

### 5.3 面積でフィルタ

`Vector → Geometry → Add geometry attributes` → `area` 列が追加される。

`Field Calculator` または `Select by Expression`:

```
$area >= 1000
```

→ 1,000 m² 未満のポリゴンを除外して保存。

### 5.4 最大深さでフィルタ（窪地単位）

`Processing Toolbox → Zonal statistics`:

| 入力 | 値 |
|---|---|
| Input vector layer | 5.3 で得たポリゴン |
| Raster layer | `qgis_depth` |
| Statistics to calculate | `Maximum`, `Mean` |
| Output column prefix | `depth_` |

→ 各ポリゴンに `depth_max`, `depth_mean` 列が追加される。

`Select by Expression`:

```
"depth_max" >= 0.5
```

→ 最大深さ 0.5 m 以上のポリゴンが残る。これが**本システムの "raw + filtered" sinks** に対応。

---

## 6. 「対象窪地」（黒塗り）まで絞る

本システムが対象とするのは以下を全て満たすもの:

1. ✅ 最大深さ ≥ 0.5 m（5.4 まででクリア）
2. ✅ 面積 ≥ 1,000 m²（5.3 まででクリア）
3. ⏭ **建物を含む**
4. ⏭ **底面標高 ≤ 10 m**

### 6.1 建物含有判定

`prefetch/<slug>/buildings.geojson` をロードし、`Vector → Research Tools → Select by Location`:

| 入力 | 値 |
|---|---|
| Select features from | 5.4 のポリゴン |
| Where the features (geometric predicate) | `intersects` |
| By comparing to features from | `buildings` |

→ 1 棟以上の建物と交差するポリゴンだけが選択される。

### 6.2 底面標高フィルタ

`Zonal statistics` をもう一度、今度は `dem_utm` をラスタに:

| 入力 | 値 |
|---|---|
| Input vector layer | 6.1 の選択結果 |
| Raster layer | `dem_utm` |
| Statistics | `Minimum` |
| Output column prefix | `elev_` |

→ `elev_min` 列に各窪地の底面標高（最低 DEM 標高）。

`Select by Expression`:

```
"elev_min" <= 10.0
```

→ これで残ったポリゴンが**本システムの `target_sinks.geojson`（黒塗り対象）**と完全一致するはず。

---

## 7. 一致を確認

QGIS で再現したポリゴン（6.2 の結果）と本システムの `target_sinks.geojson` を重ねる:

- 同色（透過 50%）で重ねて、両方が同じ位置・形状か目視
- `Vector → Geoprocessing Tools → Symmetric Difference` で**両者の差分**を取り、空であれば bit-identical

実数で比較したい場合: 両ポリゴン集合の `area` 合計、ポリゴン数、各 max_depth を比較。

| 期待件数 | hitachi_kuji | sakata_takasago | kirishima_hayato |
|---|---|---|---|
| 対象窪地 | 8 | 16 | 42 |

---

## 8. 検証で確認できること

| 検証項目 | 5.1〜5.4 で確認 | 6.1〜6.2 で確認 |
|---|---|---|
| 充填アルゴリズムが Wang & Liu か | ✅（QGIS 純正の Fill Sinks ツール）| — |
| 深さラスタが正しいか | ✅（Raster Calculator で再計算）| — |
| 0.05 m / 1,000 m² / 0.5 m フィルタが正しく適用されているか | ✅ | — |
| 建物含有判定が正しいか | — | ✅（QGIS Select by Location）|
| 標高 ≤ 10 m フィルタが正しいか | — | ✅（Zonal Statistics min）|

つまり**ステップ 7 まで全部 QGIS 単独で本システムを再現できる**ので、もし両者が一致すれば「黒塗り = QGIS の Fill sinks (Wang & Liu) + 透明な後処理」であることが客観的に確認できる。

---

## 9. 自動検証スクリプト（オプション）

QGIS の手作業を避けたい場合は、`tests/qgis_compare.py` のような形で自動化可能。本リポジトリには未同梱だが、`saga_cmd ta_preprocessor 4` を直接呼んで本システムの `depth.tif` と差分を取るのが最短:

```bash
saga_cmd ta_preprocessor 4 \
    -ELEV prefetch/kirishima_hayato/dem_utm.tif \
    -FILLED /tmp/saga_filled.sdat \
    -MINSLOPE 0.1

# Python で比較
python3 -c "
import rasterio, numpy as np
with rasterio.open('prefetch/kirishima_hayato/dem_utm.tif') as src: dem = src.read(1)
with rasterio.open('/tmp/saga_filled.sdat') as src: filled = src.read(1)
with rasterio.open('prefetch/kirishima_hayato/depth.tif') as src: ours = src.read(1)
qgis_depth = filled - dem
qgis_depth[dem == src.nodata] = 0
diff = np.abs(qgis_depth - ours)
print(f'max abs diff: {diff.max():.6f} m   median: {np.median(diff):.6f} m')
"
```

`max abs diff` が 0.001 m 未満なら本システムは QGIS と数値的に一致。

---

## 10. まとめ

- 本システムの黒塗り = **QGIS の "Fill sinks (Wang & Liu)" + 4 段の後処理フィルタ**
- 後処理は QGIS 標準ツール（Raster Calculator + Polygonize + Zonal Stats + Select by Location）だけで再現可能
- 充填アルゴリズム自体は QGIS が呼ぶのと同じ SAGA バイナリ → 同じ DEM・同じ MINSLOPE なら **bit-identical な結果**になる
