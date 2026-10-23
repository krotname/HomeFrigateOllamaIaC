"""Optional CUDA two-wheeler proposals, preserving the standard detector output."""
import gc
import ctypes
import json
import logging
import subprocess
import time
import uuid
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from pydantic import Field

from frigate.detectors.detection_api import DetectionApi
from frigate.detectors.detector_config import BaseDetectorConfig, ModelTypeEnum
from frigate.detectors.plugins.onnx import ONNXDetector

logger = logging.getLogger(__name__)
GPU_UUID = "GPU-d9edf332-2d18-bd6d-efdd-c4617743a0e0"
BUDGET_MIB = 1536
HEADROOM_MIB = 2048


class KugooDetectorConfig(BaseDetectorConfig):
    type: Literal["kugoo_onnx"]
    device: str = "AUTO"
    supplement_model: str
    supplement_gpu: Literal[GPU_UUID] = GPU_UUID
    supplement_interval: float = Field(default=0.55, ge=0.5)
    supplement_threshold: float = Field(default=0.7, ge=0.7, le=1)
    supplement_pyramid: bool = False
    supplement_warmup_seconds: float = Field(default=30, ge=0, le=60)


def gpu_state():
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=uuid,memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=2, check=True,
    )
    rows = [r.split(",") for r in result.stdout.splitlines() if r.split(",")[0].strip() == GPU_UUID]
    if len(rows) != 1:
        raise RuntimeError("Exact supplemental GPU is unavailable")
    return {"free_mib": int(rows[0][1]), "util_percent": int(rows[0][2])}


def verify_cuda_device():
    # Frigate changes NVIDIA_VISIBLE_DEVICES after container initialization.
    # Query the CUDA device actually visible to this detector process.
    driver = ctypes.CDLL('libcuda.so.1')
    count = ctypes.c_int()
    device = ctypes.c_int()
    identifier = (ctypes.c_ubyte * 16)()
    for status in (driver.cuInit(0), driver.cuDeviceGetCount(ctypes.byref(count)),
                   driver.cuDeviceGet(ctypes.byref(device), 0)):
        if status != 0:
            raise RuntimeError('Cannot verify the CUDA device')
    if count.value != 1 or driver.cuDeviceGetUuid(ctypes.byref(identifier), device) != 0:
        raise RuntimeError('Expected one restricted CUDA device')
    actual = 'GPU-' + str(uuid.UUID(bytes=bytes(identifier)))
    if actual != GPU_UUID:
        raise RuntimeError('CUDA device does not match the approved GPU')
    return actual


def overlap(first, second):
    # Native normalized ymin, xmin, ymax, xmax.
    top, left = max(first[0], second[0]), max(first[1], second[1])
    bottom, right = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0, bottom-top)*max(0, right-left)
    areas = [(b[2]-b[0])*(b[3]-b[1]) for b in (first, second)]
    return intersection / max(1e-12, sum(areas)-intersection)


def append_proposals(base, output, threshold, size=320):
    """Keep every base row in place; NMS applies only to supplemental proposals."""
    predictions = output[0].T
    if predictions.shape[1] == 84:
        predictions = predictions[:, [0, 1, 2, 3, 5]]
    elif predictions.shape[1] != 5:
        raise ValueError("Expected a single-class or COCO YOLO model")
    predictions = predictions[np.isfinite(predictions).all(axis=1) & (predictions[:, 4] >= threshold)]
    if not len(predictions):
        return base
    boxes = predictions[:, :4].copy()
    boxes[:, :2] -= boxes[:, 2:] / 2
    indices = cv2.dnn.NMSBoxes(boxes.tolist(), predictions[:, 4].tolist(), threshold, .5)
    result = base.copy()
    empty = iter(np.flatnonzero(result[:, 1] == 0))
    for index in np.asarray(indices).reshape(-1):
        x, y, width, height = boxes[index]
        box = np.clip(np.array([y, x, y+height, x+width])/size, 0, 1)
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        if any(row[1] > 0 and int(row[0]) in (1, 3) and overlap(box, row[2:]) > .5 for row in result):
            continue
        slot = next(empty, None)
        if slot is None:
            break
        result[slot] = [1, float(predictions[index, 4]), *box]
    return result


class KugooDetector(DetectionApi):
    type_key = "kugoo_onnx"
    supported_models = [ModelTypeEnum.yologeneric]

    def __init__(self, detector_config):
        super().__init__(detector_config)
        if self.width not in (320, 640, 960) or self.height != self.width:
            raise ValueError("Supplement requires square 320, 640 or 960 input")
        base_config = detector_config.model_copy(deep=True)
        base_config.model.width = 320
        base_config.model.height = 320
        self.base = ONNXDetector(base_config)
        self.config = detector_config
        self.session = None
        self.last_attempt = 0.0
        self.retry_after = 0.0
        self.last_status = 0.0
        self.warmup_until = time.monotonic() + detector_config.supplement_warmup_seconds
        self.counts = {"inferences": 0, "proposals": 0, "busy": 0, "errors": 0}

    def status(self, reason, gpu=None):
        now = time.monotonic()
        if now-self.last_status < 10:
            return
        self.last_status = now
        state = dict(time=time.time(), reason=reason, gpu=gpu, counts=self.counts,
                     gpu_uuid=GPU_UUID, cuda_only=True, loaded=self.session is not None,
                     budget_mib=BUDGET_MIB, headroom_mib=HEADROOM_MIB,
                     interval=self.config.supplement_interval)
        temporary = Path("/tmp/kugoo-detector-status.tmp")
        try:
            temporary.write_text(json.dumps(state))
            temporary.replace("/tmp/kugoo-detector-status.json")
        except OSError:
            logger.debug("Supplement status file unavailable")

    def load(self):
        import onnxruntime as ort
        self.cuda_device = verify_cuda_device()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.enable_profiling = True
        options.profile_file_prefix = '/tmp/kugoo-cuda'
        options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
        session = ort.InferenceSession(self.config.supplement_model, options, providers=[
            ("CUDAExecutionProvider", {"device_id": 0, "gpu_mem_limit": BUDGET_MIB*1024**2,
                "arena_extend_strategy": "kSameAsRequested", "cudnn_conv_algo_search": "DEFAULT"})])
        if session.get_providers()[0] != "CUDAExecutionProvider":
            raise RuntimeError("Supplement CUDA execution provider unavailable")
        shape = session.get_inputs()[0].shape
        if shape not in ([1, 3, 320, 320], [1, 3, 640, 640], [2, 3, 640, 640]):
            raise ValueError("Unexpected supplemental input")
        self.supplement_size = shape[2]
        self.supplement_batch = shape[0]
        self.input_name = session.get_inputs()[0].name
        self.session = session
        self.cuda_audited = False

    def detect_raw(self, tensor_input):
        base_input = tensor_input
        if tensor_input.shape[2:] != (320, 320):
            pixels = tensor_input[0].transpose(1, 2, 0)
            base_input = cv2.resize(pixels, (320, 320), interpolation=cv2.INTER_LINEAR).transpose(2, 0, 1)[None]
        base = self.base.detect_raw(np.ascontiguousarray(base_input))
        now = time.monotonic()
        if now < self.retry_after:
            return base
        delay = self.config.supplement_interval - (now-self.last_attempt)
        if delay > 0:
            if now >= getattr(self, "warmup_until", 0):
                return base
            time.sleep(delay)
        self.last_attempt = time.monotonic()
        try:
            state = gpu_state()
            required = HEADROOM_MIB + (BUDGET_MIB if self.session is None else 0)
            if state["free_mib"] < required or state["util_percent"] >= 25:
                self.counts["busy"] += 1
                self.status("waiting_for_gpu", state)
                return base
            if self.session is None:
                self.load()
                state = gpu_state()
                if state["free_mib"] < HEADROOM_MIB:
                    raise RuntimeError("Supplement loaded below required GPU headroom")
            supplement_input = tensor_input
            if self.supplement_size != tensor_input.shape[2]:
                pixels = tensor_input[0].transpose(1, 2, 0)
                supplement_input = cv2.resize(pixels, (self.supplement_size, self.supplement_size), interpolation=cv2.INTER_LINEAR).transpose(2, 0, 1)[None]
            if self.supplement_batch == 2:
                from PIL import Image
                pixels=np.clip(supplement_input[0].transpose(1,2,0)*255,0,255).astype(np.uint8)
                low=Image.fromarray(pixels).resize((320,320),Image.Resampling.BILINEAR).resize((640,640),Image.Resampling.BILINEAR)
                low_input=np.asarray(low,dtype=np.float32).transpose(2,0,1)[None]/255
                supplement_input=np.concatenate((supplement_input,low_input),axis=0)
            raw = self.session.run(None, {self.input_name: np.ascontiguousarray(supplement_input)})[0]
            output = np.concatenate([raw[i:i+1] for i in range(self.supplement_batch)],axis=2)
            if not self.cuda_audited:
                profile = Path(self.session.end_profiling())
                providers = {entry['args']['provider'] for entry in json.loads(profile.read_text())
                             if entry.get('args',{}).get('provider')}
                if providers != {'CUDAExecutionProvider'}:
                    raise RuntimeError('Supplement contains non-CUDA execution kernels')
                self.cuda_audited = True
                profile.unlink()
            self.counts["inferences"] += 1
            if getattr(self.config, "supplement_pyramid", False) and self.supplement_batch == 1:
                # Two measured scales of the same current region; no cached boxes.
                time.sleep(self.config.supplement_interval)
                second_state = gpu_state()
                if second_state["free_mib"] >= HEADROOM_MIB and second_state["util_percent"] < 25:
                    from PIL import Image
                    pixels = np.clip(supplement_input[0].transpose(1, 2, 0)*255, 0, 255).astype(np.uint8)
                    low = Image.fromarray(pixels).resize((320,320),Image.Resampling.BILINEAR).resize((self.supplement_size,self.supplement_size),Image.Resampling.BILINEAR)
                    low_input = np.asarray(low,dtype=np.float32).transpose(2,0,1)[None]/255
                    other = self.session.run(None, {self.input_name: np.ascontiguousarray(low_input)})[0]
                    output = np.concatenate((output,other),axis=2)
                    self.counts["inferences"] += 1
                self.last_attempt = time.monotonic()
            result = append_proposals(base, output, self.config.supplement_threshold, self.supplement_size)
            self.counts["proposals"] += int(np.count_nonzero(result[:, 1])-np.count_nonzero(base[:, 1]))
            if gpu_state()["free_mib"] < HEADROOM_MIB:
                raise RuntimeError("Supplement exhausted GPU headroom")
            self.status("watching", state)
            return result
        except Exception as error:
            self.counts["errors"] += 1
            self.session = None
            gc.collect()
            self.retry_after = time.monotonic()+60
            logger.warning("Supplement paused; base detector retained: %s", error)
            self.status("paused")
            return base
