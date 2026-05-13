#!/usr/bin/env bash
# Flood Risk Detector — dependency installer
# Tested on Ubuntu 22.04+ with Python 3.10–3.12

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"

echo "[1/4] Installing system packages (GDAL / PROJ / SAGA / spatial libs)..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends \
        python3-venv python3-dev build-essential \
        gdal-bin libgdal-dev \
        libspatialindex-dev \
        libproj-dev proj-data proj-bin \
        libgeos-dev \
        saga
    echo "  SAGA installed → pipeline.py --backend auto will pick saga (QGIS-faithful Wang & Liu)."
else
    echo "  apt-get not found — install GDAL/PROJ/GEOS/SAGA manually for your distro." >&2
fi

echo "[2/4] Creating virtual environment at ${VENV_DIR}..."
python3 -m venv "${VENV_DIR}"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo "[3/4] Upgrading pip and installing wheels..."
pip install --upgrade pip wheel setuptools

echo "[4/4] Installing Python requirements..."
pip install -r "${PROJECT_DIR}/requirements.txt"

echo
echo "Done. Activate the environment with:"
echo "  source ${VENV_DIR}/bin/activate"
