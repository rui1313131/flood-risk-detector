# Flood Risk Detector (DEM-based)

国土地理院（GSI）の標高タイルから DEM を自動取得し、**Wang & Liu (2006) Priority-Flood 充填法**で窪地（=内水氾濫リスク地形）を検出。OpenStreetMap の建物フットプリントと空間結合して「浸水リスクのある建物」を CSV / GeoJSON / PNG で出力します。

QGIS の "Fill sinks (Wang & Liu)" と同一バックエンド（SAGA `ta_preprocessor 4`）を採用しているため、QGIS で開いて同じ結果を再現できます。

## 対応エリア

**日本全国** で使用可能です。DEM は国土地理院の公開タイル（API キー不要、商用利用可、出典明示で OK）から取得します。

| 解像度 | レイヤ | カバレッジ |
|---|---|---|
| 10 m | `dem_png` | 日本全国（陸地） |
| 5 m  | `dem5a_png` | 都市部・平野部（写真測量） |
| 5 m  | `dem5b_png` | 山地（航空レーザ） |
| 5 m  | `dem5c_png` | 混合 |

海外では動作しません（GSI タイルは日本領域限定）。

## 必要環境

- **Linux**（Ubuntu 22.04+ で動作確認）/ **macOS** / **Windows + WSL2**
- Python 3.10 以上
- インターネット接続（DEM・建物データ・依存ライブラリの取得）

Windows ネイティブも Python 部分は動きますが、SAGA を別途インストールしないと `--backend pure`（純 Python 実装）にフォールバックします。QGIS と完全一致させたい場合は WSL2 を推奨。

## セットアップ

```bash
git clone https://github.com/rui1313131/flood-risk-detector.git
cd flood-risk-detector
bash setup.sh        # apt: SAGA + GDAL + PROJ + venv 作成 + pip install
source .venv/bin/activate
```

`sudo` が無い環境では手動で：

```bash
sudo apt-get install -y saga gdal-bin libgdal-dev libproj-dev libgeos-dev libspatialindex-dev fonts-noto-cjk
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### macOS

```bash
brew install saga-gis gdal proj geos spatialindex
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Windows

`setup.sh` / `install.sh` は apt-get 前提なので Windows では使いません。Python と pip を直接使います。

**事前準備:**
- Python 3.10 以上（python.org または `winget install Python.Python.3.11`）
- Git for Windows（推奨。`git clone` と Git Bash が入る）

シェル別に `venv` の有効化コマンドが異なります。**自分が使っているシェルに合った節を選んでください。**

---

#### コマンドプロンプト（cmd.exe）

```bat
git clone https://github.com/rui1313131/flood-risk-detector.git
cd flood-risk-detector
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip wheel
pip install -r requirements.txt

REM 動作確認
python run_site.py 36.496985 140.613220 --label "茨城県日立市久慈町三丁目"
```

有効化に成功するとプロンプト先頭に `(.venv)` が付きます。

---

#### PowerShell

```powershell
git clone https://github.com/rui1313131/flood-risk-detector.git
cd flood-risk-detector
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip wheel
pip install -r requirements.txt

# 動作確認
python run_site.py 36.496985 140.613220 --label "茨城県日立市久慈町三丁目"
```

`Activate.ps1` で「このシステムではスクリプトの実行が無効になっているため…」と出る場合は、初回だけ実行ポリシーを許可：

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

---

#### Git Bash（任意）

```bash
git clone https://github.com/rui1313131/flood-risk-detector.git
cd flood-risk-detector
python -m venv .venv
source .venv/Scripts/activate
python -m pip install --upgrade pip wheel
pip install -r requirements.txt

# 動作確認
python run_site.py 36.496985 140.613220 --label "茨城県日立市久慈町三丁目"
```

> **シェル取り違え注意:** PowerShell / cmd で `source ...` は **動きません**（`source` は bash 専用のコマンドです）。逆に Git Bash で `Activate.ps1` を直接叩いても動きません。

---

**richdem について**: Windows では C++ ビルドが MSVC で失敗するため `requirements.txt` 側で自動スキップしています（環境マーカー `sys_platform != 'win32'`）。コード側も `try/except` で吸収済みで、`--backend auto` は SAGA > pure の順にフォールバックします。気にせず先に進んで OK です。

**SAGA GIS（任意）**: QGIS と完全一致させたい場合のみ [SourceForge](https://sourceforge.net/projects/saga-gis/) からダウンロードして `saga_cmd.exe` を PATH に追加。入れない場合は `--backend pure`（純 Python 実装の Wang & Liu、同一アルゴリズム）に自動フォールバックします。

**日本語フォント**: Windows 標準の Yu Gothic を matplotlib が自動検出するので追加インストール不要です。

QGIS バイト一致が要件で SAGA セットアップが面倒な場合は **WSL2 + Ubuntu** を導入して `bash setup.sh` を使うのが最も簡単です。

## 使い方

### 1. 単一地点を解析（緯度経度を指定）

```bash
python3 run_site.py 36.496985 140.613220 --label "茨城県日立市久慈町三丁目"
```

これで以下が `prefetch/<ラベル>/` に出力されます：

| ファイル | 内容 |
|---|---|
| `elevation_gsi_classic.png` | 元の標高図（検出マーキングなし） |
| `map_overlay_with_sinks.png` | 検出後の地図（窪地=黒、リスク建物=赤、深さ順 ID 付き） |
| `report.txt` | 各窪地の中心座標・建物数・入力点からの方位距離・GSI 検索リンク |
| `sinks.geojson` | 窪地ベクタ（QGIS で開ける） |
| `depth.tif` | 浸水深ラスタ |
| `dem_utm.tif` | UTM 投影された DEM |
| `buildings.geojson` | OSM 建物フットプリント |
| `site.json` | 入力メタデータ |

主なオプション：
- `--half-side 0.02`（デフォルト ≈ 4.4 km 四方）— 解析範囲
- `--skip-basemap` — 背景地図なしで PNG を生成（オフライン）
- `--slug "..."` — 出力フォルダ名を明示指定

### 2. 任意の bbox / 地名を解析（汎用パイプライン）

```bash
# bbox 直接指定
python3 pipeline_1_flood_risk.py --bbox "140.60,36.48,140.63,36.51"

# 地名（Nominatim ジオコーディング）
python3 pipeline_1_flood_risk.py --place "箱根町" --country jp --max-side 0.05

# 既存の DEM GeoTIFF を流用
python3 pipeline_1_flood_risk.py path/to/dem.tif
```

主なオプション：
- `--min-depth 0.10` — このセル深さ未満は除外（DEM ノイズ対策）
- `--min-area 50.0` — このポリゴン面積（m²）未満は除外
- `--top 20` — リスクスコア上位 N 件のみ出力
- `--basemap` — GSI 標準地図を背景に重ねる
- `--backend saga|richdem|pure` — FillSink 実装の選択（既定: auto）
- `--source osm|gsi` — 建物データの取得元
- `--skip-buildings` — 建物データなしで窪地検出だけ実行

### 3. 複数地点の一括バッチ

`prefetch_test_sites.py` の `SITES` を編集してから：

```bash
python3 prefetch_test_sites.py    # DEM + 建物をダウンロード
python3 analyze_sites.py          # 全サイトを解析
```

## 出力フォルダ構成（pipeline_1）

```
results/<run_name>/
├── coordinates/   — at_risk_buildings.csv, sinks_ranked.csv, sinks_ranked.geojson
├── images/        — overview.png + per_sink/000_sink_0001.png ...
├── rasters/       — dem_utm.tif, depth.tif
└── meta/          — manifest.json（入力 sha256・パラメータ・出力 sha256 を記録）
```

`manifest.json` のおかげで、同じ bbox・同じパラメータでの再実行は幾何ステップがバイト一致します。

## 手法

**Wang, L. & Liu, H. (2006). An efficient method for identifying and filling surface depressions in digital elevation models for hydrologic analysis and modelling. International Journal of Geographical Information Science, 20(2), 193-213.**

Priority-Flood 法で DEM を充填し「充填後 − 元」差分から浸水深ラスタを得ます。詳細は `docs/MECHANISM.md` を、QGIS との数値一致検証は `docs/QGIS_VERIFICATION.md` を参照。

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `backend_used=pure` と表示される | SAGA 未インストール。`sudo apt install saga` で QGIS と完全一致 |
| PNG タイトルが文字化け | `sudo apt install fonts-noto-cjk` → `rm -rf ~/.cache/matplotlib` |
| `ModuleNotFoundError: rasterio` | venv 有効化忘れ。`source .venv/bin/activate` |
| タイル数が多すぎる警告 | `--max-side` を小さく、または `--dem-zoom 11` で粗くする |
| Nominatim が地名を引けない | `--country jp` を付ける、または `--bbox` で直接指定 |

## データソース・ライセンス

- DEM: 国土地理院「基盤地図情報 数値標高モデル」タイル形式  
  出典: `データ出典：国土地理院（https://maps.gsi.go.jp/）`
- 建物: OpenStreetMap contributors（ODbL）
- 背景地図: 国土地理院 標準地図タイル

本リポジトリのコードは MIT ライセンスです。
