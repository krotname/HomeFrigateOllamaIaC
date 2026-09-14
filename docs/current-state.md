# Current Production State

Last live host, VM, LAN camera and Pi kiosk check verified: `2026-09-14`.

## Host and VM

| Component | Value |
| --- | --- |
| Windows host | `ADLER-WHITE-W1`, `192.168.1.104` |
| Windows host admin transport | Key-only OpenSSH through the managed `adler-white-w1.lan` host profile |
| Hyper-V VM | `frigate-ubuntu`, retained but `Off`; former address `192.168.1.138` is offline |
| Frigate / ASR LAN addresses | Offline with the VM |
| VM autostart | `AutomaticStartAction=Nothing` |
| VM CPU/RAM | `8` vCPU, `8 GB` startup RAM |
| GPU | No Tesla P40 is present in White; one stale DDA assignment remains on the stopped VM and must not be treated as hardware presence |
| Pi kiosk camera path | Direct camera RTSP substreams -> go2rtc sidecar on Red -> trusted HTTPS kiosk; no Frigate dependency |
| Azure guest agent | `walinuxagent.service` disabled and masked; this non-Azure VM must not probe WireServer through DHCP |
| Config backups | Scheduled task `WinHome Config Backup`, daily `03:20`, retained at `F:\Files\Backups\win-home-configs` |

## Frigate

`frigate-ubuntu` is intentionally offline. The values below describe its last
known retained configuration, not currently reachable production services.

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
Live check 2026-09-14:
White host: 192.168.1.104 reachable
Tesla P40 present devices: 0
frigate-ubuntu: Off, autostart=Nothing, stale DDA assignments=1
Camera 1: 192.168.1.12:554 reachable
Camera 3: 192.168.1.51:554 unreachable
Pi kiosk: trusted HTTPS on Red; camera source configuration is direct RTSP
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
