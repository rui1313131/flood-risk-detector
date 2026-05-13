"""
Optional: pull the prefetched DEM + building dataset from a remote host
(S3 / GCS / GitHub Releases / any HTTP server) into ./prefetch/.

Use this when you have *not* shipped prefetch/ directly with the source
tarball but instead host the dataset centrally so receivers don't have to
re-download from GSI per machine.

Environment / args:

    DATA_BASE_URL   base URL ending in the dataset version, e.g.
                    https://your-bucket.s3.amazonaws.com/flood_risk_v1

The remote layout must mirror the local prefetch/ tree, e.g.:

    {DATA_BASE_URL}/manifest.json                 — list of files + sha256
    {DATA_BASE_URL}/hitachi_kuji/dem_utm.tif
    {DATA_BASE_URL}/hitachi_kuji/buildings.geojson
    ...

Each file's sha256 is verified after download. To regenerate the manifest
from the local prefetch/ tree (publishing side):

    python3 download_data.py --build-manifest > prefetch/manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root: Path) -> dict:
    """Walk prefetch/ and emit a manifest of file → sha256 + size."""
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            rel = p.relative_to(root).as_posix()
            files.append({"path": rel, "size": p.stat().st_size,
                          "sha256": sha256(p)})
    return {"version": "1", "files": files}


def fetch(url: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "flood-risk-test-kit/1"})
    with urllib.request.urlopen(req, timeout=60) as r, dst.open("wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=os.environ.get("DATA_BASE_URL"),
                   help="Base URL hosting prefetch/ (or set DATA_BASE_URL).")
    p.add_argument("--out", default="prefetch", help="Local destination dir.")
    p.add_argument("--build-manifest", action="store_true",
                   help="Don't download — emit manifest.json from local prefetch/.")
    args = p.parse_args()

    project_root = Path(__file__).resolve().parent
    out_root = (project_root / args.out).resolve()

    if args.build_manifest:
        m = build_manifest(out_root)
        sys.stdout.write(json.dumps(m, indent=2))
        return

    if not args.base_url:
        sys.exit("--base-url or env DATA_BASE_URL is required for download.")

    base = args.base_url.rstrip("/")
    out_root.mkdir(parents=True, exist_ok=True)

    manifest_url = f"{base}/manifest.json"
    print(f"manifest: {manifest_url}")
    with urllib.request.urlopen(manifest_url, timeout=30) as r:
        manifest = json.loads(r.read().decode("utf-8"))

    for entry in manifest["files"]:
        rel = entry["path"]
        dst = out_root / rel
        if dst.exists() and sha256(dst) == entry["sha256"]:
            print(f"  ok (cached) {rel}")
            continue
        fetch(f"{base}/{rel}", dst)
        got = sha256(dst)
        if got != entry["sha256"]:
            sys.exit(f"sha256 mismatch for {rel}: got {got}, expected {entry['sha256']}")
        print(f"  ok {rel}  ({entry['size']:,} B)")

    print(f"\nDataset pulled to {out_root}")


if __name__ == "__main__":
    main()
