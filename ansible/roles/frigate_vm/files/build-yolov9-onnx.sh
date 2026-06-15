#!/usr/bin/env bash
set -euo pipefail

export DOCKER_BUILDKIT=0

MODEL_SIZE="${MODEL_SIZE:-t}"
IMG_SIZE="${IMG_SIZE:-320}"
FRIGATE_ROOT="${FRIGATE_ROOT:-/opt/frigate}"
MODEL_CACHE="${FRIGATE_ROOT}/config/model_cache"
MODEL_PATH="${MODEL_CACHE}/yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx"
WORKDIR="${MODEL_CACHE}/build-yolov9-${MODEL_SIZE}-${IMG_SIZE}"
IMAGE="local/frigate-yolov9-${MODEL_SIZE}-${IMG_SIZE}-builder"

if [[ -s "${MODEL_PATH}" ]]; then
  echo "${MODEL_PATH} already exists"
  exit 0
fi

mkdir -p "${MODEL_CACHE}"
rm -rf "${WORKDIR}"
mkdir -p "${WORKDIR}"
cd "${WORKDIR}"

docker build --build-arg MODEL_SIZE="${MODEL_SIZE}" --build-arg IMG_SIZE="${IMG_SIZE}" -t "${IMAGE}" -f- . <<'DOCKERFILE'
FROM python:3.11
RUN apt-get update && apt-get install --no-install-recommends -y git wget cmake libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /yolov9
RUN git clone --depth 1 https://github.com/WongKinYiu/yolov9.git .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir onnx==1.18.0 onnxruntime "onnx-simplifier==0.4.*" onnxscript
ARG MODEL_SIZE
ARG IMG_SIZE
RUN wget -O yolov9-${MODEL_SIZE}.pt https://github.com/WongKinYiu/yolov9/releases/download/v0.1/yolov9-${MODEL_SIZE}-converted.pt
RUN sed -i "s/ckpt = torch.load(attempt_download(w), map_location='cpu')/ckpt = torch.load(attempt_download(w), map_location='cpu', weights_only=False)/g" models/experimental.py
RUN python3 export.py --weights ./yolov9-${MODEL_SIZE}.pt --imgsz ${IMG_SIZE} --simplify --include onnx
DOCKERFILE

cid="$(docker create "${IMAGE}")"
trap 'docker rm -f "${cid}" >/dev/null 2>&1 || true' EXIT
docker cp "${cid}:/yolov9/yolov9-${MODEL_SIZE}.onnx" "${MODEL_PATH}"
chmod 0644 "${MODEL_PATH}"

docker image rm "${IMAGE}" >/dev/null 2>&1 || true
docker image prune -f >/dev/null 2>&1 || true
rm -rf "${WORKDIR}"

ls -lh "${MODEL_PATH}"
