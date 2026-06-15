# Current Production State

Last verified: `2026-06-15 09:33`, smoke-test `failed_count=0`.

## Host and VM

| Component | Value |
| --- | --- |
| Windows host | `ADLER-WHITE-1W`, `192.168.1.33` |
| Hyper-V VM | `frigate-ubuntu`, `192.168.1.138` |
| VM autostart | `AutomaticStartAction=Start`, delay `60` seconds |
| VM CPU/RAM | `8` vCPU, `4 GB` startup RAM |
| GPU | NVIDIA Tesla P40 via Hyper-V DDA, `PCIROOT(0)#PCI(0300)#PCI(0000)` |

## Frigate

| Component | Value |
| --- | --- |
| URL | `https://192.168.1.138:8971/` |
| Root | `/opt/frigate` |
| Image | `ghcr.io/blakeblackshear/frigate:stable-tensorrt` |
| Media | `/media/frigate`, ext4 VHDX-backed mount |
| Detector | `onnx`, `device=GPU` |
| Model | `/config/model_cache/yolov9-t-320.onnx` |
| Labelmap | `/config/model_cache/coco-yolo-80.txt` |
| ffmpeg | NVIDIA CUDA hwaccel, `scale_cuda` |

## Ollama

| Component | Value |
| --- | --- |
| Service | systemd `ollama`, enabled |
| HTTP | `0.0.0.0:11434` |
| HTTPS proxy | nginx, `https://192.168.1.138:11443` |
| Model | `qwen2.5vl:3b` |

## Validation Snapshot

```text
Frigate detector: type=onnx, device=GPU, inference_speed_ms=8.16
ONNX providers: TensorrtExecutionProvider, CUDAExecutionProvider, CPUExecutionProvider
Camera FPS: cam1 5.0/5.0 skipped 0.0; cam2 5.1/5.1 skipped 0.0
Ollama vision: qwen2.5vl:3b, 100% GPU
```

The full validation command is:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1
```
