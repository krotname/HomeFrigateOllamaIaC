# Smoke-Test Proof

This snapshot records the last production validation that the repository is
designed to reproduce.

## Verified Run

| Field | Value |
| --- | --- |
| Date/time | `2026-06-28 17:40` |
| Command | `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1 -SkipOllamaGenerate -AsrSamplePath $env:TEMP\asr-test-sample.m4a -TrustUnknownHostKeys` |
| Result | `failed_count=0` |
| Host | `ADLER-WHITE-1W` |
| VM | `frigate-ubuntu` |
| GPU | NVIDIA Tesla P40 through Hyper-V DDA |
| Frigate | CUDA ffmpeg + ONNX YOLOv9-t 320 detector |
| Ollama | `huihui_ai/gpt-oss-abliterated:20b` over authenticated LAN HTTPS |
| ASR | `Systran/faster-whisper-large-v3` over LAN HTTPS |

## Runtime Evidence

```text
Frigate detector: type=onnx, device=GPU, inference_speed_ms=11.53
ONNX providers: TensorrtExecutionProvider, CUDAExecutionProvider, CPUExecutionProvider
Camera FPS: cam1_ds_i202 5/5 skipped 0; cam2_ds_i551 5/5 skipped 0
Ollama: huihui_ai/gpt-oss-abliterated:20b present, service active
ASR health: Systran/faster-whisper-large-v3, cuda, int8, container up
ASR sample: 90s Russian audio, 1414 chars, language=ru
```

## Reproduction Gate

The repository considers the runtime healthy only when `scripts/smoke-test.ps1`
can verify:

- Windows host identity and Hyper-V VM state.
- Tesla P40 DDA assignment.
- trusted HTTPS endpoint for Frigate and LAN endpoints for Ollama and ASR.
- Docker and Frigate container health.
- ONNX GPU detector provider selection.
- CUDA ffmpeg decode/scale path.
- camera FPS and recent recordings.
- Ollama API/model availability.
- Frigate-to-Ollama network path.
- ASR HTTPS health and optional real audio transcription.

## Release Signal

The first public release is
[v0.1.0](https://github.com/krotname/HomeFrigateOllamaIaC/releases/tag/v0.1.0).
It publishes a source archive and `checksums.txt`; the release workflow also
creates GitHub build provenance attestations for tagged source archives.
