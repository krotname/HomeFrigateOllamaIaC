# Current Production State

Last live LAN/API check verified: `2026-07-06`.
Windows host WinRM HTTPS administration verified: `2026-06-15 20:54`.

## Host and VM

| Component | Value |
| --- | --- |
| Windows host | `ADLER-WHITE-1W`, `192.168.1.33` |
| Windows host admin transport | WinRM over HTTPS `5986`, endpoint `PowerShell.7`, credential `C:\Users\KRT\.codex\secrets\adler-winrm.credential.xml` |
| Hyper-V VM | `frigate-ubuntu`, `192.168.1.138` |
| Frigate LAN address | `192.168.1.138:8971` |
| ASR LAN address | `192.168.1.138:9443` |
| VM autostart | `AutomaticStartAction=Start`, delay `60` seconds |
| VM CPU/RAM | `8` vCPU, `8 GB` startup RAM |
| GPU | NVIDIA Tesla P40 via Hyper-V DDA, `PCIROOT(0)#PCI(0300)#PCI(0000)` |
| Config backups | Scheduled task `WinHome Config Backup`, daily `03:20`, retained at `F:\Files\Backups\win-home-configs` |

## Frigate

| Component | Value |
| --- | --- |
| URL | `https://192.168.1.138:8971/` |
| Auth | nginx basic auth on LAN `8971`; Frigate container listens on `127.0.0.1:18971` |
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
| Backend HTTP | Docker bridge gateway port `11435`, plus loopback proxy `127.0.0.1:11435` |
| LAN URL | nginx TLS/basic-auth proxy on `192.168.1.138:11443` |
| Frigate GenAI model | `qwen2.5:3b` |
| Installed larger model | `huihui_ai/gpt-oss-abliterated:20b` |
| Frigate GenAI | Review/object generation disabled; Frigate can still reach Ollama |

## ASR

| Component | Value |
| --- | --- |
| URL | `https://192.168.1.138:9443/` |
| Endpoint | `POST /v1/audio/transcriptions` |
| Root | `/opt/asr` |
| Engine | `faster-whisper` |
| Model | `Systran/faster-whisper-large-v3` |
| Device | CUDA, `int8` compute type |
| TLS/Auth | nginx terminates LAN TLS and basic auth on `9443`; the ASR container uses HTTP only on `127.0.0.1:19443` |

## Validation Snapshot

```text
Smoke-test after RAM upgrade: failed_count=0
Hyper-V VM memory: startup=8589934592, assigned=8589934592
Frigate API: 0.17.1-416a9b7, container healthy
Ollama API: 0.30.8, Frigate model=qwen2.5:3b
ASR API: Systran/faster-whisper-large-v3, cuda, int8
GPU: Tesla P40, detector inference about 8.34 ms
```

The full validation command is:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1
```

The smoke test reaches the Windows host through WinRM HTTPS. SSH is used only
for the Ubuntu VM checks.

Backup policy and registry:

- [Backup Policy](backup-policy.md)
- `registries/backup-registry.csv`
