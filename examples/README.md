# Examples — Windows CLI 動作確認

Windows 11 + Python 3.10 で `run_site.py` をシェル別に CLI から実行し、生成された地図を収録。

## 倉敷市真備町（PowerShell から実行）

2018 年 7 月の西日本豪雨で甚大な被害が出た小田川流域。

```powershell
chcp 65001 | Out-Null
cd D:\Jikken\flood_risk_detector_dem
.\.venv\Scripts\Activate.ps1
$env:PYTHONIOENCODING="utf-8"
python run_site.py 34.629 133.689 --label "岡山県倉敷市真備町"
```

| 項目 | 値 |
|---|---|
| 中心座標 | 34.629°N, 133.689°E |
| 検出窪地 | 99 件（最深点 ≥ 0.5 m を満たすもの） |
| 対象窪地（住宅含む & 標高 ≤ 10 m） | 36 件 |
| 建物総数 | 11,137 棟 |
| 浸水リスク建物 | 1,207 棟 |
| FillSink バックエンド | pure（Wang & Liu 2006 純 Python 実装） |
| DEM レイヤ | GSI dem5a_png z15（5 m 写真測量） |

成果物:
- [`elevation_gsi_classic.png`](kurashiki_mabi/elevation_gsi_classic.png) — 元の標高図（GSI 標準地図 + 1 m bin 色別標高 overlay）
- [`map_overlay_with_sinks.png`](kurashiki_mabi/map_overlay_with_sinks.png) — 検出窪地（黒）+ ID 番号 + リスク建物
- [`report.txt`](kurashiki_mabi/report.txt) — 各窪地の中心座標・建物数・GSI 検索リンク
- [`at_risk_buildings.csv`](kurashiki_mabi/at_risk_buildings.csv) — リスク建物の座標一覧

## 神戸市東灘区魚崎南町（CMD から実行）

阪神工業地帯の海岸低地。河口部の埋立地・海岸線沿いで窪地を検出。

```bat
chcp 65001 > nul
cd /d D:\Jikken\flood_risk_detector_dem
.venv\Scripts\activate.bat
set PYTHONIOENCODING=utf-8
python run_site.py 34.715 135.265 --label "兵庫県神戸市東灘区魚崎南町"
```

| 項目 | 値 |
|---|---|
| 中心座標 | 34.715°N, 135.265°E |
| 検出窪地 | 49 件 |
| 対象窪地（住宅含む & 標高 ≤ 10 m） | 32 件 |
| 建物総数 | 28,395 棟 |
| 浸水リスク建物 | 2,934 棟 |
| FillSink バックエンド | pure |
| DEM レイヤ | GSI dem5a_png z15 |

成果物:
- [`elevation_gsi_classic.png`](kobe_higashinada/elevation_gsi_classic.png)
- [`map_overlay_with_sinks.png`](kobe_higashinada/map_overlay_with_sinks.png)
- [`report.txt`](kobe_higashinada/report.txt)
- [`at_risk_buildings.csv`](kobe_higashinada/at_risk_buildings.csv)

## 検証ポイント

両ケースとも以下を確認済み:

1. **PowerShell / CMD のどちらからでも CLI 一発で完走**（venv 有効化 → `chcp 65001` → `python run_site.py ...`）
2. **PNG タイトル・凡例が日本語で正しく描画**される（Yu Gothic / Meiryo 自動検出）
3. **窪地検出位置が地形と整合**:
   - 倉敷真備: 小田川（2018 年決壊した一級河川）沿いの氾濫原に窪地が集中
   - 神戸東灘: 住吉川・天上川河口の埋立地と海岸線沿いの低地に窪地
4. **report.txt と CSV が UTF-8 で出力**され、文字化けなく読める

実行時間は各サイトで概ね 1〜3 分（DEM タイル取得 + OSM 建物取得 + FillSink + 描画の合計）。
