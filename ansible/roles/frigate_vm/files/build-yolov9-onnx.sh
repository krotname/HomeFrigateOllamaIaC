#!/usr/bin/env bash
set -euo pipefail

export DOCKER_BUILDKIT=0

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
YOLOV9_REV="5b1ea9a8b3f0ffe4fe0e203ec6232d788bb3fcff"
YOLOV9_REQUIREMENTS_LOCK="${SCRIPT_DIR}/yolov9-export-requirements.lock"
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
cp "${YOLOV9_REQUIREMENTS_LOCK}" "${WORKDIR}/requirements.lock"

docker build --build-arg MODEL_SIZE="${MODEL_SIZE}" --build-arg IMG_SIZE="${IMG_SIZE}" --build-arg YOLOV9_REV="${YOLOV9_REV}" -t "${IMAGE}" -f- . <<'DOCKERFILE'
FROM python:3.11@sha256:a30c4ff1a6a474019f9b1f0d921e81a254cf420d408c09e8a8b79fd803b62ebf
RUN apt-get update && apt-get install --no-install-recommends -y git wget cmake libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /yolov9
ARG YOLOV9_REV
RUN git init . && git remote add origin https://github.com/WongKinYiu/yolov9.git && git fetch --depth 1 origin "${YOLOV9_REV}" && git checkout --detach FETCH_HEAD
COPY requirements.lock /tmp/yolov9-export-requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/yolov9-export-requirements.lock
RUN sed -i "s/onnx-simplifier>=0.4.1/onnxsim>=0.4.1/g" export.py
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
