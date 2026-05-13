"""
Building footprint loader.

Two sources are supported:

    * OpenStreetMap, via OSMnx (``load_buildings_osm``).
    * Local GSI (国土地理院 基盤地図情報) GML or Shapefile (``load_buildings_gsi``).

Both functions return a GeoDataFrame with at least ``building_id`` and
``geometry`` columns, reprojected into a caller-supplied target CRS so that
downstream area / overlay operations are metric.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import geopandas as gpd
from shapely.geometry import box


# --------------------------------------------------------------------------- #
# OpenStreetMap
# --------------------------------------------------------------------------- #

def load_buildings_osm(
    bounds_wgs84: tuple[float, float, float, float],
    target_crs: str | int,
) -> gpd.GeoDataFrame:
    """
    Download OSM building footprints inside a WGS84 bounding box.

    Parameters
    ----------
    bounds_wgs84 : (minx, miny, maxx, maxy)  in EPSG:4326 (lon/lat).
    target_crs   : CRS to reproject into (must match the DEM CRS).
    """
    import osmnx as ox  # imported lazily so GSI-only flows don't need it

    minx, miny, maxx, maxy = bounds_wgs84
    polygon = box(minx, miny, maxx, maxy)

    # OSMnx ≥ 1.9: features_from_polygon. Older versions: geometries_from_polygon.
    if hasattr(ox, "features_from_polygon"):
        gdf = ox.features_from_polygon(polygon, tags={"building": True})
    else:
        gdf = ox.geometries_from_polygon(polygon, tags={"building": True})  # type: ignore[attr-defined]

    if gdf.empty:
        return gpd.GeoDataFrame(
            columns=["building_id", "source", "geometry"],
            geometry="geometry",
            crs=target_crs,
        )

    # Keep polygonal features only (drops nodes tagged as buildings, etc.)
    gdf = gdf[gdf.geometry.type.isin(("Polygon", "MultiPolygon"))].copy()
    gdf = gdf.to_crs(target_crs)
    gdf["building_id"] = [f"osm_{i}" for i in range(len(gdf))]
    gdf["source"] = "osm"
    return gdf[["building_id", "source", "geometry"]].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 国土地理院 (GSI) 基盤地図情報
# --------------------------------------------------------------------------- #

def load_buildings_gsi(
    path: str | Path,
    target_crs: str | int,
    *,
    layer: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """
    Load buildings from a GSI base-map file (GML / Shapefile / GeoPackage).

    GSI 基盤地図情報 ships building polygons as ``BldA`` / ``建築物`` features.
    Shapefile / GeoJSON / GPKG are autodetected by extension; for multi-layer
    files (GPKG, FileGDB), pass ``layer``.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    read_kwargs = {"layer": layer} if layer else {}
    gdf = gpd.read_file(path, **read_kwargs)

    gdf = gdf[gdf.geometry.type.isin(("Polygon", "MultiPolygon"))].copy()
    if gdf.crs is None:
        # GSI GML often comes without a CRS in the file; assume JGD2011 (EPSG:6668).
        gdf.set_crs(epsg=6668, inplace=True)
    gdf = gdf.to_crs(target_crs)
    if "building_id" not in gdf.columns:
        gdf["building_id"] = [f"gsi_{i}" for i in range(len(gdf))]
    if "source" not in gdf.columns:
        gdf["source"] = "gsi"
    return gdf[["building_id", "source", "geometry"]].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Convenience: pull a bbox in DEM CRS, transform to WGS84, fetch OSM
# --------------------------------------------------------------------------- #

def load_buildings_for_dem(
    dem_bounds: tuple[float, float, float, float],
    dem_crs,
    *,
    source: str = "osm",
    gsi_path: Optional[str | Path] = None,
) -> gpd.GeoDataFrame:
    """
    Convenience wrapper: given the DEM's bounding box in its native CRS,
    fetch building footprints in the same CRS.

    For ``source='osm'``, the bbox is reprojected to WGS84 for OSMnx.
    For ``source='gsi'``, ``gsi_path`` must point at a local file.
    """
    if source == "osm":
        bbox_geom = gpd.GeoSeries([box(*dem_bounds)], crs=dem_crs).to_crs(4326).iloc[0]
        return load_buildings_osm(bbox_geom.bounds, target_crs=dem_crs)
    if source == "gsi":
        if gsi_path is None:
            raise ValueError("gsi_path is required when source='gsi'.")
        return load_buildings_gsi(gsi_path, target_crs=dem_crs)
    raise ValueError(f"Unknown source: {source!r} (expected 'osm' or 'gsi').")
