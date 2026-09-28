# Current Production State

Last live host and VM check: `2026-09-28`.

## Host and VM

| Component | Value |
| --- | --- |
| Windows host | `ADLER-WHITE-W1`, `192.168.1.104` |
| Hyper-V VM | `frigate-ubuntu`, `Running`, `192.168.1.138` |
| VM autostart | `AutomaticStartAction=Start` |
| VM CPU/RAM | `6` vCPU, fixed `12 GB` RAM (raised on `2026-09-27`) |
| GPU/DDA | Tesla absent and not planned to return; VM assignable-device count `0` |
| Media | `/media/frigate`, ext4 VHDX-backed mount, virtual size `2 TB` |
| Host storage guard | Keep at least `150 GB` free on `F:` |

## Frigate Recorder

| Component | Value |
| --- | --- |
| URL | `https://192.168.1.138:8971/` |
| Image | `ghcr.io/blakeblackshear/frigate:stable` (`0.18.0`) |
| Runtime | Docker `runc`, no assigned GPU devices or device requests |
| Cameras | `cam1_ds_i202`, `cam2_ds_i551`, `cam3_ds_i200` |
| Recording | Main stream, continuous, `-c copy`, audio copy |
| CPU snapshot | `47.2%` of the 2-vCPU guest; no object inference, main streams are not transcoded |
| Initial retention | `3` days; measure actual growth after 24 hours before increasing |
| Analytics | Detection, motion, review alerts/detections, snapshots, face/LPR and GenAI disabled |
| Live view | go2rtc substreams remain available through Frigate |

`cam2_ds_i551` (`192.168.1.65`) was unreachable during the deployment check.
Frigate continued recording cameras 1 and 3; an unavailable camera does not
block the available streams.

## Disabled Services

| Service | Production state |
| --- | --- |
| Ollama | systemd service disabled and inactive; watchdog timer disabled |
| ASR | compose container absent; HTTPS listener removed; image and model cache deleted `2026-09-28` |
| Legacy nginx sites | `adler-frigate`, `ollama-https`, `asr-https` removed from enabled sites |

## ASR for the Phone pipeline

Since `2026-09-28` speech recognition for `krotname/Phone` runs on Black
(`adler-black-u2`, `192.168.1.242`), not on this VM: the same `asr/` image is
built there as `home-asr:local` and started from `/opt/asr` with
[`asr/docker-compose.black.yml`](../asr/docker-compose.black.yml). It uses
Tesla P40 GPU0 next to `black-qwen`, Whisper `large-v3` in `int8`, and listens
only on `127.0.0.1:19443`. The read-only container needs `TMPDIR=/tmp/asr`,
otherwise multipart uploads fail with `There was an error parsing the body`.

## Validation

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test-recorder.ps1
```

The recorder smoke test treats camera availability separately from the
configuration count and verifies that at least one camera is actively producing
copy-mode recording segments.
