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
    supplement_cameras: list[str] = Field(default_factory=list)


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
    """Preserve base detections and sort for Frigate's first-low-score cutoff."""
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
        if any(row[1] >= predictions[index, 4] and int(row[0]) in (1, 3)
               and overlap(box, row[2:]) > .5 for row in result):
            continue
        slot = next(empty, None)
        if slot is None:
            break
        result[slot] = [1, float(predictions[index, 4]), *box]
    return result[np.argsort(-result[:, 1], kind="stable")]


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
        self.last_reason = None
        self.camera = None
        self.region = None
        self.frame = None
        self.plans = {}
        self.last_attempts = {}
        self.last_seen = {}
        self.counts = {"inferences": 0, "proposals": 0, "busy": 0, "errors": 0}
        self.camera_counts = {}

    def status(self, reason, gpu=None):
        now = time.monotonic()
        if now-self.last_status < 10 and reason == getattr(self, "last_reason", None):
            return
        self.last_status = now
        self.last_reason = reason
        state = dict(time=time.time(), reason=reason, gpu=gpu, counts=self.counts,
                     camera=self.camera, camera_counts=self.camera_counts,
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
        if self.config.supplement_pyramid and shape[0] != 2:
            raise ValueError("Pyramid inference requires a single batch-two model")
        self.supplement_size = shape[2]
        self.supplement_batch = shape[0]
        self.input_name = session.get_inputs()[0].name
        self.session = session
        self.cuda_audited = False

    def set_camera(self, camera, region=None, frame=None):
        self.camera = camera
        self.region = None if region is None else tuple(region)
        self.frame = frame

    def supplement_turn(self, now):
        if self.camera not in self.config.supplement_cameras:
            return False
        if self.frame is None or self.region is None:
            return False
        frame_time, regions, tracked_boxes = self.frame
        regions = [tuple(region) for region in regions]
        if not regions:
            return False
        self.last_seen[self.camera] = now
        active = [camera for camera in self.config.supplement_cameras
                  if now - self.last_seen.get(camera, float('-inf')) < 3]
        # Give a stream consecutive opportunities without sleeping in the shared
        # detector. A missing stream ages out instead of holding another one.
        if len(active) > 1 and active[int(now // 8) % len(active)] != self.camera:
            return False
        plan = self.plans.get(self.camera)
        if plan is None or plan['time'] != frame_time:
            plan = {'time':frame_time, 'region':regions[-1], 'done':False}
            self.plans[self.camera] = plan
        if plan['done'] or self.region != plan['region']:
            return False
        if now-self.last_attempts.get(self.camera, float('-inf')) < self.config.supplement_interval:
            return False
        plan['done'] = True
        self.last_attempts[self.camera] = now
        return True

    def detect_raw(self, tensor_input):
        base_input = tensor_input
        if tensor_input.shape[2:] != (320, 320):
            pixels = tensor_input[0].transpose(1, 2, 0)
            base_input = cv2.resize(pixels, (320, 320), interpolation=cv2.INTER_LINEAR).transpose(2, 0, 1)[None]
        base = self.base.detect_raw(np.ascontiguousarray(base_input))
        now = time.monotonic()
        if now < self.retry_after or not self.supplement_turn(now):
            return base
        self.last_attempt = now
        camera_counts = self.camera_counts.setdefault(self.camera, {"inferences": 0, "proposals": 0, "busy": 0})
        try:
            state = gpu_state()
            required = HEADROOM_MIB + (BUDGET_MIB if self.session is None else 0)
            if state["free_mib"] < required or state["util_percent"] >= 25:
                if self.session is not None and state["free_mib"] < HEADROOM_MIB:
                    self.session = None
                    gc.collect()
                self.counts["busy"] += 1
                camera_counts["busy"] += 1
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
                try:
                    providers = {entry['args']['provider'] for entry in json.loads(profile.read_text())
                                 if entry.get('args',{}).get('provider')}
                    if providers != {'CUDAExecutionProvider'}:
                        raise RuntimeError('Supplement contains non-CUDA execution kernels')
                    self.cuda_audited = True
                finally:
                    profile.unlink(missing_ok=True)
            self.counts["inferences"] += 1
            camera_counts["inferences"] += 1
            result = append_proposals(base, output, self.config.supplement_threshold, self.supplement_size)
            proposals = int(np.count_nonzero(result[:, 1])-np.count_nonzero(base[:, 1]))
            self.counts["proposals"] += proposals
            camera_counts["proposals"] += proposals
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
