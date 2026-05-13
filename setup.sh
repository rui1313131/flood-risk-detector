#!/usr/bin/env bash
# One-shot installer for the flood-risk test kit.
#   - Installs system packages required by rasterio / geopandas / SAGA
#   - Creates a venv at ./.venv
#   - Installs Python dependencies
#
# Designed for Ubuntu 22.04+/24.04. Other Linux distros: replicate the
# `apt-get install` block with your package manager and re-run from the
# `python3 -m venv` step.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"

echo "[1/4] System packages (GDAL / PROJ / GEOS / SAGA / fonts) ..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends \
        python3-venv python3-dev build-essential \
        gdal-bin libgdal-dev \
        libspatialindex-dev \
        libproj-dev proj-data proj-bin \
        libgeos-dev \
        saga \
        fonts-noto-cjk
    echo "    SAGA installed → analyze_sites.py uses QGIS-faithful FillSink."
else
    echo "    apt-get not found. Install GDAL, PROJ, GEOS, SAGA, and a CJK font manually." >&2
fi

echo "[2/4] Python venv at ${VENV_DIR} ..."
python3 -m venv "${VENV_DIR}"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo "[3/4] pip / wheel ..."
pip install --upgrade pip wheel setuptools

echo "[4/4] Project dependencies ..."
pip install -r "${PROJECT_DIR}/requirements.txt"

cat <<EOF

  Setup done.

  Activate the venv next time with:
      source ${VENV_DIR}/bin/activate

  Run the analysis (uses bundled prefetch/ data, no network needed):
      python3 analyze_sites.py
EOF
