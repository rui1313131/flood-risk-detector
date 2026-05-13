# Flood-Risk Detector 説明書

国土地理院 5 m DEM から **「住宅を含む内水氾濫リスク窪地」** を自動検出し、地図上に黒塗り＋ID で示すツール。日本国内なら任意の緯度経度を 1 コマンドで解析できる。

---

## 1. 何ができるか

緯度・経度を 1 組与えると、その地点を中心に約 **4 km × 3.6 km** を切り出し、以下を自動生成する。

| 出力 | 内容 |
|---|---|
| `elevation_gsi_classic.png` | **元画像** — 標準地図 + 1 m 刻みの色別標高図（検出マーキング無し） |
| `map_overlay_with_sinks.png` | **検出後** — 上記に対象窪地（黒塗り）と ID 番号を重畳 |
| `target_sinks.geojson` | 対象窪地ポリゴン（QGIS で開ける） |
| `at_risk_buildings.csv` / `.geojson` | 窪地内に位置する建物の一覧 |
| `depth.tif` | 各セルの想定湛水深（m） |
| `report.txt` | ID 別の中心座標・深さ・面積・GSI 検索リンク（日本語） |
| `manifest.json` | 入出力 sha256 + ライブラリバージョン（再現性検証用） |

---

## 2. 実行手順（最短）

### 初回セットアップ（1 回のみ）

```bash
bash setup.sh   # apt: SAGA + GDAL + venv + pip install を一括
```

`sudo` が無い環境では `QUICKSTART.md` の手動手順を参照。

### 任意座標を解析

```bash
source .venv/bin/activate
python3 run_site.py <lat> <lon> [--label "表示名"]
```

例:
```bash
python3 run_site.py 36.496985 140.613220 --label "茨城県日立市久慈町三丁目"
```

- 出力先: `prefetch/<slug>/`（slug は `--label` から自動生成）
- 同じ slug で再実行すると **DEM は再取得されない**（しきい値だけ変えて再解析が可能）
- オプション
  - `--half-side 0.02`: bbox 半辺（°）。既定 0.02 ≈ 4.4 km × 3.6 km
  - `--slug NAME`: 出力フォルダ名を明示
  - `--skip-basemap`: 背景地図ダウンロードを省略

---

## 3. 仕組み（4 段階）

```
入力 (lat, lon)
   │
   ├─ ① DEM + 建物データの取得
   │     ・GSI dem5a_png z15 (5 m 写真測量) → UTM に再投影 (≈ 2.4 m/px)
   │     ・OpenStreetMap から建物フットプリント取得
   │
   ├─ ② 背景地図の取得
   │     ・GSI 標準地図 z17 を RGB GeoTIFF 化
   │
   ├─ ③ 窪地検出 (Wang & Liu 2006 Priority-Flood)
   │     ・SAGA `ta_preprocessor 4` (= QGIS "Fill sinks" と同一バイナリ)
   │     ・湛水深 = 充填後DEM − 元DEM
   │     ・湛水セルをポリゴン化、各窪地に sink_id / 最大深さ / 面積 付与
   │
   └─ ④ フィルタ + 可視化
         ・5 段の閾値で「住宅を含む実害ある窪地」だけ抽出
         ・PNG 2 枚 + GeoJSON + CSV + report.txt を出力
```

DEM は **国土地理院 dem5a_png（z15、5 m 写真測量）決め打ち**。日本国外の座標はエラーで停止する。

---

## 4. 閾値（フィルタ条件）

`analyze_sites.py` 冒頭で定数として定義。窪地は **5 段すべてを通過**したものだけ「対象」となり黒塗り表示される。

| 閾値 | 既定値 | 何を切るか | 設定の根拠 |
|---|---|---|---|
| `SINK_MIN_DEPTH` | **0.05 m** | 浅すぎるセルをポリゴンから除外 | DEM ノイズ底（≈3 cm）のすぐ上 |
| `SINK_MIN_MAX_DEPTH` | **0.50 m** | 最深点が浅い窪地を丸ごと棄却 | 5 m DEM 写真測量精度 (±30 cm) の倍を確保 |
| `SINK_MIN_AREA` | **1,000 m²** | 小さい窪地を棄却 | 約 32 m × 32 m。GSI 1/25,000 で視認可能なサイズ |
| **建物含有判定** | OSM 建物 1 棟以上 | 田畑・空き地の窪地を除外 | 「住居がある = 実害がある」場所に限定 |
| `SINK_MAX_BASE_ELEV_M` | **10.0 m** | 高地の窪地を棄却 | 標高パレット上限 (0–10 m) と一致。山間部の谷・採掘穴を除外 |

しきい値だけ変えたいときは `analyze_sites.py` の冒頭定数を編集して、同じ slug で `run_site.py` を再実行すれば DEM 再ダウンロードなしで再解析される。

---

## 5. 出力画像の読み方

凡例（右側カラーバー）は以下の 12 階層 + 上下外れ値:

| 色 | 標高 |
|---|---|
| 濃紺 | 0 m 以下（海・河川） |
| 水色 → 緑 → 黄 | 0 m → 5 m（低地、浸水想定エリア） |
| 橙 → 赤 | 5 m → 10 m（高台縁） |
| 鮮紅 | 10 m 以上（高台） |

**画像内の黒色**:
- **対象窪地ポリゴン**（標準仕様）。輪郭線なし、純黒塗り。中心に深さ降順の **ID 番号**（白文字+黒ハロー）。
- 画像 4 隅の細い黒帯は **NoData**（UTM 再投影で生じる空き）。窪地ではない。

**画像に描かないもの**（明示的に除外している）:
- 入力座標を示す赤枠・十字・「+」マーカー
- 4 分割線、デバッグ用の格子
- 各窪地の数値ラベル（深さ・面積） — ID 番号のみ
- 浸水リスク建物の赤フィル — 建物が長方形に集合して「赤い四角」に見えるため

集計値（合計件数など）は画像タイトルに **載せない**。集計は `report.txt` で確認する。

---

## 6. レポート (`report.txt`) の構造

```
■ 入力ユーザ座標         緯度経度・bbox・DEM 解像度・GSI 検索リンク
■ 手法                   Wang & Liu / SAGA / 閾値の説明
■ 対象地形ごとの説明     画像内 ID と 1:1 対応（深さ降順）
    ID 1
      中心座標 (WGS84)   : 36.502918°N, 140.612752°E
      内部の住居数       : 1 棟
      最大深さ           : 4.74 m   平均深さ 3.31 m   底面標高 2.3 m
      面積               : 6,025 m²   範囲 92 m × 123 m
      入力点からの位置   : 北 方向 約 661 m
      GSI 検索リンク     : https://maps.gsi.go.jp/...
    ID 2 ...
```

画像 ID と report ID は完全一致するため、**画像で気になる窪地を ID で確認** → **report で詳細**という導線で読める。

---

## 7. 必要環境

| 区分 | 要件 |
|---|---|
| OS | Linux (Ubuntu 22.04+ 動作確認) / macOS / WSL2 |
| Python | 3.10 以上 |
| 必須コマンド | `saga`（Wang & Liu 本体）、`gdal-bin`、`fonts-noto-cjk`（日本語表示用） |
| Python 依存 | `requirements.txt` 参照（numpy, scipy, rasterio, geopandas, shapely, osmnx, matplotlib 等） |
| ネットワーク | 初回 `pip install` 時のみ。新規座標を解析する時のみ GSI / OSM へ通信 |
| 処理時間 | 1 サイトあたり **2〜3 分**（4 km × 3.6 km、4 コア / 8 GB ノート PC で十分） |
| GPU | 不要 |

---

## 8. 再現性

`prefetch/<slug>/manifest.json` に以下を記録する:

- 入力 DEM の sha256
- 出力ファイル全部の sha256
- 主要ライブラリバージョン（numpy, scipy, rasterio, geopandas, shapely, osmnx, richdem, matplotlib）
- Python / OS / SAGA バージョン
- パラメータ（閾値、CRS、backend）

**同じ DEM × 同じ閾値 × 同じバージョン → 別 PC でも `at_risk_buildings.csv` と `sinks.geojson` がビット完全一致**。実測（2026-05-06）で Linux 送信側 ↔ WSL2 Ubuntu 受信側で 3 サイト全件一致を確認済み。

---

## 9. 制約と既知の挙動

- **日本国内のみ**: DEM が GSI 決め打ち。海外座標は `RuntimeError` で停止する。
- **bbox 縁の窪地は要注意**: 川や海への流出口が bbox の外にあると、Wang & Liu 充填法は流路を「巨大な閉じた窪地」と認識して、低地全体が黒塗りされることがある。`--half-side` を広げるか、画像 ID と航空写真を照合して判断する。
- **OSM 建物の鮮度**: 建物データは取得時点のスナップショット。新築・解体は反映されない。
- **OSM サーバ依存**: `prefetch_site` 実行時に Overpass API が落ちていると失敗する。リトライで通ることが多い。

---

## 10. 参考: 数学的な仕組み

**Wang & Liu (2006) Priority-Flood**: DEM を「水を流す」モデルとして、地形の最低点に低い順から「水を満たし」、流出口を持たない窪地は満杯になる。満杯後の DEM から元 DEM を引いた差分が **湛水深 (depth)**。SAGA `ta_preprocessor 4` がこの実装で、QGIS の「Fill sinks (Wang & Liu)」が呼ぶ同一バイナリ。最小勾配 0.1° を保持することで、平坦部でも一意な流向が生成される。

参照:
- Wang, L. and Liu, H. (2006) "An efficient method for identifying and filling surface depressions in digital elevation models for hydrologic analysis and modelling," *International Journal of Geographical Information Science*, 20(2), 193–213.
- アルゴリズム詳細は `docs/MECHANISM.md`（開発者向け）を参照。
