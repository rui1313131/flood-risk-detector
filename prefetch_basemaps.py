"""
One-shot helper: fetch GSI 標準地図 basemap tiles for every site that already
has prefetched DEM data. Idempotent — skips sites whose basemap_utm.tif
already exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from basemap_fetcher import fetch_gsi_basemap


BASEMAP_ZOOM = 16   # std tiles look good at z16; ~100 tiles / 0.04° bbox


def main() -> None:
    project_root = Path(__file__).resolve().parent
    root = project_root / "prefetch"
    for site_dir in sorted(root.iterdir()):
        if not site_dir.is_dir() or not (site_dir / "site.json").exists():
            continue
        utm_path = site_dir / "basemap_utm.tif"
        if utm_path.exists():
            print(f"=== {site_dir.name}  (basemap_utm.tif already exists, skip)")
            continue

        info = json.loads((site_dir / "site.json").read_text(encoding="utf-8"))
        print(f"\n=== {site_dir.name} ({info['label']}) ===")
        bbox = tuple(info["bbox_wgs84"])
        fetch_gsi_basemap(bbox, site_dir, zoom=BASEMAP_ZOOM)


if __name__ == "__main__":
    main()
