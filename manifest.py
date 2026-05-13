"""
Reproducibility manifest writer.

Writes one ``manifest.json`` per pipeline run, capturing every input/parameter/
output needed to reproduce the result bit-for-bit (modulo the algorithm itself).

Captured fields
---------------
    schema_version : int       — bump if the manifest layout changes
    timestamp_utc  : ISO8601    — when the run started
    git_commit     : str | null — current commit if the project is in a repo
    inputs         : dict       — DEM path + sha256, building source / cache
    parameters     : dict       — algorithm + thresholds + CRS
    environment    : dict       — python + key library versions
    outputs        : dict       — produced files, with sha256 for each
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Optional


MANIFEST_SCHEMA_VERSION = 1


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _safe_version(name: str) -> Optional[str]:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _git_commit(start: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=2,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def write_manifest(
    out_dir: str | Path,
    *,
    inputs: dict[str, Any],
    parameters: dict[str, Any],
    output_files: list[str | Path],
    extra: Optional[dict[str, Any]] = None,
) -> Path:
    """
    Serialize a manifest describing a pipeline run.

    Parameters
    ----------
    inputs       : dict of input paths/URIs and any other source descriptors.
                   Local file paths are auto-hashed.
    parameters   : dict of algorithm parameters (backend, thresholds, etc.).
    output_files : list of files produced by the run; each is hashed.
    extra        : additional free-form fields merged at the top level.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs_with_hashes: dict[str, Any] = {}
    for k, v in inputs.items():
        if isinstance(v, (str, Path)) and Path(v).is_file():
            p = Path(v)
            inputs_with_hashes[k] = {
                "path": str(p.resolve()),
                "size_bytes": p.stat().st_size,
                "sha256": _sha256(p),
            }
        else:
            inputs_with_hashes[k] = v

    outputs: dict[str, Any] = {}
    for f in output_files:
        p = Path(f)
        if p.is_file():
            outputs[p.name] = {
                "path": str(p.resolve()),
                "size_bytes": p.stat().st_size,
                "sha256": _sha256(p),
            }

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(Path(__file__).parent),
        "inputs": inputs_with_hashes,
        "parameters": parameters,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "executable": sys.executable,
            "cwd": os.getcwd(),
            "packages": {
                "numpy": _safe_version("numpy"),
                "scipy": _safe_version("scipy"),
                "rasterio": _safe_version("rasterio"),
                "geopandas": _safe_version("geopandas"),
                "shapely": _safe_version("shapely"),
                "osmnx": _safe_version("osmnx"),
                "richdem": _safe_version("richdem"),
                "matplotlib": _safe_version("matplotlib"),
            },
        },
        "outputs": outputs,
    }
    if extra:
        manifest.update(extra)

    out_path = out_dir / "manifest.json"
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return out_path
