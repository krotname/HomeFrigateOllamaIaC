# Smoke-Test Proof

This snapshot records the last production validation that the repository is
designed to reproduce.

## Verified Run

| Field | Value |
| --- | --- |
| Date/time | `2026-06-15 09:33` |
| Command | `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1` |
| Result | `failed_count=0` |
| Host | `ADLER-WHITE-1W` |
| VM | `frigate-ubuntu` |
| GPU | NVIDIA Tesla P40 through Hyper-V DDA |
| Frigate | CUDA ffmpeg + ONNX YOLOv9-t 320 detector |
| Ollama | `qwen2.5vl:3b` behind HTTPS proxy |

## Runtime Evidence

```text
Frigate detector: type=onnx, device=GPU, inference_speed_ms=8.16
ONNX providers: TensorrtExecutionProvider, CUDAExecutionProvider, CPUExecutionProvider
Camera FPS: cam1 5.0/5.0 skipped 0.0; cam2 5.1/5.1 skipped 0.0
Ollama vision: qwen2.5vl:3b, 100% GPU
```

## Reproduction Gate

The repository considers the runtime healthy only when `scripts/smoke-test.ps1`
can verify:

- Windows host identity and Hyper-V VM state.
- Tesla P40 DDA assignment.
- trusted HTTPS endpoints for Frigate and Ollama.
- Docker and Frigate container health.
- ONNX GPU detector provider selection.
- CUDA ffmpeg decode/scale path.
- camera FPS and recent recordings.
- Ollama API/model availability.
- Frigate-to-Ollama network path.
- one live-frame vision request answered by Ollama on GPU.

## Release Signal

The first public release is
[v0.1.0](https://github.com/krotname/HomeFrigateOllamaIaC/releases/tag/v0.1.0).
It publishes a source archive and `checksums.txt`; the release workflow also
creates GitHub build provenance attestations for tagged source archives.
