#!/usr/bin/env bash
# Stage the Docker build context in build/: the two packages the pipeline needs,
# the RF-DETR weights and the image files. `cdk deploy` builds the image from this folder.
# Usage: ./prepare_build.sh   (reads SRC and WEIGHTS from config.env)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.env"

[ -d "$SRC/parking_occupancy/parking" ] || { echo "SRC is wrong: $SRC/parking_occupancy/parking not found" >&2; exit 1; }
[ -f "$SRC/rcs_geo/__init__.py" ]       || { echo "SRC is wrong: $SRC/rcs_geo not found" >&2; exit 1; }
[ -s "$WEIGHTS" ]                       || { echo "WEIGHTS not found or empty: $WEIGHTS" >&2; exit 1; }

BUILD="$HERE/build"
rm -rf "$BUILD"
mkdir -p "$BUILD/weights"
rsync -a --exclude tests --exclude train --exclude __pycache__ --exclude .pytest_cache --exclude '*.pth' \
  "$SRC/parking_occupancy" "$BUILD/"
rsync -a --exclude tests --exclude __pycache__ --exclude .pytest_cache "$SRC/rcs_geo" "$BUILD/"
cp "$WEIGHTS" "$BUILD/weights/rcs_weights.pth"
cp "$HERE/Dockerfile" "$HERE/requirements.txt" "$HERE/entry.py" "$BUILD/"

# Record exactly what went into the image (also printed for the run record).
{
  echo "built_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "src: $SRC"
  echo "src_commit: $(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "src_dirty_files: $(git -C "$SRC" status --porcelain -- parking_occupancy rcs_geo 2>/dev/null | wc -l)"
  echo "weights_sha256: $(sha256sum "$WEIGHTS" | cut -d' ' -f1)"
} | tee "$BUILD/BUILD_INFO.txt"

echo "Build context ready: $BUILD ($(du -sh "$BUILD" | cut -f1))"
