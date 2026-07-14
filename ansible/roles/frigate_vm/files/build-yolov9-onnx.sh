#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
YOLOV9_REV="5b1ea9a8b3f0ffe4fe0e203ec6232d788bb3fcff"
YOLOV9_REQUIREMENTS_LOCK="${SCRIPT_DIR}/yolov9-export-requirements.lock"
MODEL_SIZE="${MODEL_SIZE:-t}"
IMG_SIZE="${IMG_SIZE:-320}"
FRIGATE_ROOT="${FRIGATE_ROOT:-/opt/frigate}"
if [[ ! "${MODEL_SIZE}" =~ ^(t|s|m|c|e)$ ]]; then
  echo "Unsupported MODEL_SIZE: ${MODEL_SIZE}" >&2
  exit 2
fi
if [[ ! "${IMG_SIZE}" =~ ^[0-9]+$ ]] || (( IMG_SIZE < 160 || IMG_SIZE > 1280 || IMG_SIZE % 32 != 0 )); then
  echo "IMG_SIZE must be a multiple of 32 in range 160..1280" >&2
  exit 2
fi
if [[ "${FRIGATE_ROOT}" != /* || "${FRIGATE_ROOT}" == "/" ]]; then
  echo "FRIGATE_ROOT must be an absolute non-root path" >&2
  exit 2
fi
FRIGATE_ROOT="$(realpath -m -- "${FRIGATE_ROOT}")"
MODEL_CACHE="${FRIGATE_ROOT}/config/model_cache"
MODEL_PATH="${MODEL_CACHE}/yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx"
MODEL_HASH_PATH="${MODEL_PATH}.sha256"
WORKDIR="${MODEL_CACHE}/build-yolov9-${MODEL_SIZE}-${IMG_SIZE}"
IMAGE="local/frigate-yolov9-${MODEL_SIZE}-${IMG_SIZE}-builder"

mkdir -p "${MODEL_CACHE}"
exec 9>"${MODEL_CACHE}/.yolov9-build.lock"
if ! flock -n 9; then
  echo "Another YOLOv9 build is already running" >&2
  exit 3
fi

if [[ -s "${MODEL_PATH}" ]]; then
  if [[ -s "${MODEL_HASH_PATH}" ]]; then
    (cd "${MODEL_CACHE}" && sha256sum --check --status "$(basename -- "${MODEL_HASH_PATH}")") || {
      echo "Existing model checksum is invalid: ${MODEL_PATH}" >&2
      exit 4
    }
  elif (( $(stat -c '%s' -- "${MODEL_PATH}") < 1048576 )); then
    echo "Existing model is unexpectedly small: ${MODEL_PATH}" >&2
    exit 4
  else
    echo "WARNING: legacy model has no checksum sidecar: ${MODEL_PATH}" >&2
  fi
  echo "MODEL_PRESENT=${MODEL_PATH}"
  exit 0
fi

case "${WORKDIR}" in
  "${MODEL_CACHE}"/build-yolov9-*) ;;
  *) echo "Refusing unsafe work directory: ${WORKDIR}" >&2; exit 2 ;;
esac
rm -rf -- "${WORKDIR}"
mkdir -p "${WORKDIR}"
cd "${WORKDIR}"
cp "${YOLOV9_REQUIREMENTS_LOCK}" "${WORKDIR}/requirements.lock"

cid=""
build_complete=0
cleanup() {
  if [[ -n "${cid}" ]]; then
    docker rm -f "${cid}" >/dev/null 2>&1 || true
  fi
  docker image rm "${IMAGE}" >/dev/null 2>&1 || true
  if [[ "${build_complete}" -ne 1 ]]; then
    rm -f -- "${MODEL_PATH}" "${MODEL_HASH_PATH}"
  fi
  rm -rf -- "${WORKDIR}"
}
trap cleanup EXIT

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
RUN python3 -c "import onnx; model=onnx.load('yolov9-${MODEL_SIZE}.onnx'); onnx.checker.check_model(model)"
DOCKERFILE

cid="$(docker create "${IMAGE}")"
docker cp "${cid}:/yolov9/yolov9-${MODEL_SIZE}.onnx" "${MODEL_PATH}"
if (( $(stat -c '%s' -- "${MODEL_PATH}") < 1048576 )); then
  echo "Exported ONNX model is unexpectedly small" >&2
  exit 5
fi
chmod 0644 "${MODEL_PATH}"
(cd "${MODEL_CACHE}" && sha256sum "$(basename -- "${MODEL_PATH}")" > "$(basename -- "${MODEL_HASH_PATH}")")
chmod 0644 "${MODEL_HASH_PATH}"
build_complete=1

echo "MODEL_BUILT=${MODEL_PATH}"
ls -lh "${MODEL_PATH}" "${MODEL_HASH_PATH}"
