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
| Cameras | `cam1_ds_i202`, `cam3_ds_i200` recording; `cam2_ds_i551` configured with `enabled: false` while its hardware is switched off |
| Recording | Main stream, continuous, `-c copy`, audio copy |
| CPU snapshot | `47.2%` of the 2-vCPU guest; no object inference, main streams are not transcoded |
| Initial retention | `3` days; measure actual growth after 24 hours before increasing |
| Analytics | Detection, motion, review alerts/detections, snapshots, face/LPR and GenAI disabled |
| Live view | go2rtc substreams remain available through Frigate |

### Camera 2 is switched off at the wall since `2026-09-11`

`cam2_ds_i551` (`192.168.1.65`, MAC `04:EE:CD:5B:0C:4D`) stopped recording at
`2026-09-11 11:35`. It is absent from the LAN at layer 2 - no ARP entry on the
router or on a client, no DHCP lease, no Wi-Fi association, no frames at all in
a 60-second `tcpdump` filtered on its MAC, and an ARP sweep of the whole
`192.168.1.0/24` does not find it on any other address. The owner powered it
down on purpose; nothing is broken.

While it was listed as enabled, Frigate restarted `ffmpeg` for it every few
seconds and wrote about `99 744` log lines per day (`Error opening input file
rtsp://127.0.0.1:8554/cam2_main`, `DESCRIBE failed: 404`,
`Ffmpeg process crashed unexpectedly`). Since `2026-09-30` the camera carries
`enabled: false` in the live config: it keeps its `go2rtc` streams and its
`cameras` entry, Frigate starts no process for it, and the log is silent about it
- `0` lines in the two minutes after the change, against roughly `69` per minute
before. Cameras 1 and 3 kept recording throughout.

Nothing has to be done when the camera is switched back on:
`krt-camera-watchdog.timer` probes TCP `554` on every camera once a minute and
enables the camera again after two consecutive answers, live and without a
container restart (see [operations](operations.md#camera-presence-watchdog)).
The same watchdog disables a camera that stops answering for ten minutes, so the
error loop cannot come back.

An unavailable camera never blocked the other streams - cameras 1 and 3 kept
recording throughout.

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
