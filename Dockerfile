# Parking-occupancy image for AWS Batch: same torch/CUDA as production (torch 2.6.0 + cu124).
# Built by `cdk deploy` (DockerImageAsset, --platform linux/amd64) from the context that
# prepare_build.sh stages in build/.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# OpenCV needs libgl1/libglib2.0-0. exiftool is what production reads DJI telemetry with
# (rcs_geo falls back to a built-in reader without it; installed for parity).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Client deliverable: no copyleft. The build fails if Ultralytics (AGPL) slipped in.
RUN ! pip show ultralytics

# The build fails if pip replaced the base image's torch or installed a second OpenCV.
RUN python -c "import torch, torchvision, cv2; \
assert torch.__version__.startswith('2.6.0'), torch.__version__; \
assert torchvision.__version__.startswith('0.21.0'), torchvision.__version__; \
assert cv2.__version__.startswith('4.10.'), cv2.__version__" \
    && test "$(pip list 2>/dev/null | grep -ci '^opencv')" = "1" \
    && pip freeze > /app/pip-freeze.txt

COPY parking_occupancy/ /app/parking_occupancy/
COPY rcs_geo/ /app/rcs_geo/
COPY weights/rcs_weights.pth /app/weights/rcs_weights.pth
COPY entry.py /app/entry.py

# The build fails if the pipeline cannot be imported or exiftool is missing (no GPU needed).
RUN python -c "import shutil, rfdetr, supervision, exifread, rcs_geo, parking_occupancy.parking.pipeline; \
assert shutil.which('exiftool')" \
    && python /app/entry.py --help > /dev/null

ENTRYPOINT []
CMD ["python", "/app/entry.py", "--help"]
