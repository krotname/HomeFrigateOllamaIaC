# Current Production State

Last live LAN/API check verified: `2026-06-28`.
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
| VM CPU/RAM | `8` vCPU, `4 GB` startup RAM |
| GPU | NVIDIA Tesla P40 via Hyper-V DDA, `PCIROOT(0)#PCI(0300)#PCI(0000)` |
| Config backups | Scheduled task `WinHome Config Backup`, daily `03:20`, retained at `F:\Files\Backups\win-home-configs` |

## Frigate

| Component | Value |
| --- | --- |
| URL | `https://192.168.1.138:8971/` |
| Auth | Frigate auth disabled; use `curl.exe -k` for the local certificate |
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
| LAN URL | `http://192.168.1.138:11434` |
| Model | `huihui_ai/gpt-oss-abliterated:20b` |
| Frigate GenAI | Disabled; current gpt-oss model is text-only |

## ASR

| Component | Value |
| --- | --- |
| URL | `https://192.168.1.138:9443/` |
| Endpoint | `POST /v1/audio/transcriptions` |
| Root | `/opt/asr` |
| Engine | `faster-whisper` |
| Model | `Systran/faster-whisper-large-v3` |
| Device | CUDA, `int8` compute type |
| TLS | Reuses `/opt/frigate/certs/fullchain.pem` and `privkey.pem` |

## Validation Snapshot

```text
Frigate LAN API: https://192.168.1.138:8971/api/version -> 0.17.1-416a9b7
Ollama LAN API: http://192.168.1.138:11434/api/version -> 0.30.8
Ollama model: huihui_ai/gpt-oss-abliterated:20b, 20.9B, cold start ~301s, 100% GPU, about 12 GB VRAM loaded
ASR LAN API: https://192.168.1.138:9443/health -> Systran/faster-whisper-large-v3, cuda, int8
ASR sample: 90s Russian audio -> 1414 characters, language=ru, probability=1
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
