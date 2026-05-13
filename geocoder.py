"""
Place name → WGS84 bbox via OSM Nominatim.

Usage policy (https://operations.osmfoundation.org/policies/nominatim/):
    * No heavy use; max 1 request/sec
    * Identify with a meaningful User-Agent
    * Cache results

This module submits a single query per pipeline run, well within the policy.
Results are cached in ``~/.cache/flood_risk_detector/geocode/`` keyed by query
to avoid re-hitting the service for repeated runs.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from hashlib import sha1
from pathlib import Path
from typing import Optional


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "flood-risk-detector/0.1 (research; +https://example.invalid)"
CACHE_DIR = Path.home() / ".cache" / "flood_risk_detector" / "geocode"

# Polite throttle; Nominatim asks for ≤ 1 req/sec.
_LAST_REQUEST_TS: list[float] = [0.0]


def _throttle(min_interval: float = 1.0) -> None:
    elapsed = time.time() - _LAST_REQUEST_TS[0]
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _LAST_REQUEST_TS[0] = time.time()


def _cache_path(query: str, country: Optional[str]) -> Path:
    key = sha1(f"{query}|{country or ''}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{key}.json"


def geocode(
    query: str,
    *,
    country: Optional[str] = None,
    use_cache: bool = True,
) -> dict:
    """
    Resolve a place name to a Nominatim hit.

    Parameters
    ----------
    query : str
        Place name in any language Nominatim recognizes (e.g., "箱根町",
        "Hakone", "Kanagawa, Japan").
    country : str | None
        Optional 2-letter ISO country code to constrain the search ("jp").

    Returns
    -------
    dict with keys: ``display_name``, ``lat``, ``lon``, ``bbox_wgs84``
    where ``bbox_wgs84`` is ``(lon_min, lat_min, lon_max, lat_max)``.
    """
    cache = _cache_path(query, country)
    if use_cache and cache.is_file():
        return json.loads(cache.read_text())

    params = {
        "q": query,
        "format": "json",
        "limit": "1",
        "addressdetails": "0",
        "polygon_geojson": "0",
    }
    if country:
        params["countrycodes"] = country

    url = NOMINATIM_URL + "?" + urllib.parse.urlencode(params)
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        results = json.loads(resp.read().decode("utf-8"))

    if not results:
        raise LookupError(f"Nominatim returned no result for {query!r}")

    hit = results[0]
    # Nominatim's boundingbox = [lat_min, lat_max, lon_min, lon_max] (strings)
    lat_min, lat_max, lon_min, lon_max = (float(v) for v in hit["boundingbox"])
    out = {
        "query": query,
        "display_name": hit["display_name"],
        "lat": float(hit["lat"]),
        "lon": float(hit["lon"]),
        "bbox_wgs84": (lon_min, lat_min, lon_max, lat_max),
    }

    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, ensure_ascii=False))

    return out


def shrink_bbox(
    bbox: tuple[float, float, float, float],
    max_side_deg: float,
) -> tuple[float, float, float, float]:
    """
    If a Nominatim bbox is much larger than ``max_side_deg``, crop it around
    its centre. Useful when the user types a prefecture name and we still
    want a tractable DEM download.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    span_lon = lon_max - lon_min
    span_lat = lat_max - lat_min
    if span_lon <= max_side_deg and span_lat <= max_side_deg:
        return bbox
    cx = (lon_min + lon_max) / 2
    cy = (lat_min + lat_max) / 2
    h = max_side_deg / 2
    return (cx - h, cy - h, cx + h, cy + h)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Geocode a place name to a WGS84 bbox.")
    p.add_argument("query")
    p.add_argument("--country", default=None, help="ISO country code (e.g., jp)")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--max-side", type=float, default=None,
                   help="Optional max bbox side in degrees (crops oversize bboxes).")
    args = p.parse_args()

    hit = geocode(args.query, country=args.country, use_cache=not args.no_cache)
    if args.max_side is not None:
        hit["bbox_wgs84"] = shrink_bbox(hit["bbox_wgs84"], args.max_side)
    print(f'display_name: {hit["display_name"]}')
    print(f'lat, lon    : {hit["lat"]}, {hit["lon"]}')
    bb = hit["bbox_wgs84"]
    print(f'bbox        : "{bb[0]},{bb[1]},{bb[2]},{bb[3]}"')
