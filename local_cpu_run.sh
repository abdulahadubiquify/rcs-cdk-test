#!/usr/bin/env bash
# Run the job image end to end on CPU with networking off, on the first N frames of
# FRAMES_DIR (default 20). No AWS, no GPU. Output: local_cpu_check/ (git-ignored).
# Usage: ./local_cpu_run.sh [N]
# Needs the image built first:  docker build --platform linux/amd64 -t rcs-batch-sim:check build/
# Docker Desktop only shares /home with its VM, so FRAMES_DIR and this repo must be under /home.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.env"
N="${1:-20}"
IMAGE="${IMAGE:-rcs-batch-sim:check}"
OUT="$HERE/local_cpu_check"

docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || { echo "image $IMAGE not found; build it: docker build --platform linux/amd64 -t $IMAGE build/" >&2; exit 1; }

MOUNTS=()
while read -r f; do MOUNTS+=(-v "$FRAMES_DIR/$f:/frames/$f:ro"); done \
  < <(ls "$FRAMES_DIR" | grep -iE '\.jpe?g$' | sort | head -n "$N")
[ "${#MOUNTS[@]}" -gt 0 ] || { echo "no frames found in $FRAMES_DIR" >&2; exit 1; }

CPUS=$(nproc); [ "$CPUS" -gt 8 ] && CPUS=8
rm -rf "$OUT" && mkdir -p "$OUT"
echo "Running $IMAGE on $((${#MOUNTS[@]} / 2)) frames, CPU only ($CPUS cores, 5 GB RAM cap), network off"
docker run --rm --network none --memory 5g --cpus "$CPUS" -e PARKING_PROFILE=1 \
  "${MOUNTS[@]}" -v "$OUT":/out "$IMAGE" \
  python /app/entry.py --local-dir /frames --out /out --allow-cpu 2>&1 | tee "$OUT/cpu-run.log"

echo
echo "Summary: $OUT/run_summary.json"
cat "$OUT/run_summary.json"
