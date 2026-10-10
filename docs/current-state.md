# Current Production State

Last live host and VM check: `2026-09-28`.

VM memory and Frigate health rechecked on `2026-10-07`: fixed RAM was reduced
from `12 GB` to `6 GB`. Hyper-V confirmed `6 GB` assigned; the guest reported
`5919 MiB` total RAM and zero swap use. Frigate returned version `0.18.0-77a66e7`
and Docker health `healthy` after the restart. VM autostart remains enabled.

## Host and VM

| Component | Value |
| --- | --- |
| Windows host | `ADLER-WHITE-W1`, `192.168.1.104` |
| Hyper-V VM | `frigate-ubuntu`, `Running`, `192.168.1.138` |
| VM autostart | `AutomaticStartAction=Start` |
| VM CPU/RAM | `6` vCPU, fixed `6 GB` RAM (reduced on `2026-10-07`) |
| GPU/DDA | Tesla absent and not planned to return; VM assignable-device count `0` |
| Media | `/media/frigate`, ext4 VHDX-backed mount, virtual size `2 TB` |
| Host storage guard | Keep at least `150 GB` free on `F:` |

## Frigate Recorder

| Component | Value |
| --- | --- |
| URL | `https://frigate.adler-white-w1.lan/` (since 2026-09-30; legacy `https://adler-frigate.lan:8971/`) |
| Image | `ghcr.io/blakeblackshear/frigate:stable` (`0.18.0`) |
| Runtime | Docker `runc`, no assigned GPU devices or device requests |
| Cameras | All three recording; fisheye `cam2_ds_i551` restored and checked on `2026-10-08` |
| Recording | Main stream, continuous, `-c copy`, audio copy |
| CPU snapshot | `47.2%` of the 2-vCPU guest; no object inference, main streams are not transcoded |
| Initial retention | `3` days; measure actual growth after 24 hours before increasing |
| Analytics | Detection, motion, review alerts/detections, snapshots, face/LPR and GenAI disabled |
| Live view | go2rtc substreams remain available through Frigate |

### Camera 2 fisheye restored on `2026-10-08`

`cam2_ds_i551` is the DS-I551 fisheye at `192.168.1.65`. After the owner restored
power, the old watchdog changed YAML to `enabled: true` without creating workers
that Frigate had skipped at startup. Enabling the YAML entry and restarting
Frigate once restored capture. The watchdog now uses the camera runtime ON/OFF
command; watched cameras remain enabled in startup YAML.

The main stream is H.265, `2560x1920`, `25` fps, VBR up to `8192` kbit/s,
with a two-second GOP. The browser substream is H.264, `640x480`, `25` fps,
up to `1024` kbit/s. Frigate capture dimensions now match `640x480` rather than
stretching the image to `1280x720`. Audio is AAC on both streams: the previous
G.711 codec could not be copied into MP4 and produced discarded empty segments.
New ten-second archive segments were checked with ffprobe: H.265 video at
`2560x1920` plus AAC audio. Runtime OFF/ON restored capture and recording.

Camera NTP already uses `192.168.1.1:123`, with a `60`-minute interval and
Moscow time (`UTC+03:00`); those working settings were retained. All three
cameras have healthy capture at the recorder profile's `1` fps, with continuous
main-stream recording and three-day retention.

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
Tesla P40 GPU0 next to `black-qwen`, Whisper `large-v3` in `int8`, and serves
`https://adler-black-u2.lan:19443` to the home LAN. The container uses host
networking, so the host ufw (default deny) governs the port; its single rule is

```bash
sudo ufw allow from 192.168.1.0/24 to 192.168.1.242 port 19443 proto tcp comment 'Black ASR LAN API'
```

The earlier `Red containment` deny for `192.168.1.185` stays ahead of it, and
the home-access VPN subnets routed to Black (`10.9x.35.0/24`) are not allowed.
Clients whose traffic the router itself proxies leave from `192.168.1.1` and are
allowed like any LAN host. The API has no authentication: the owner accepted
LAN-only exposure on 2026-09-30, so never publish the port beyond the LAN.
The TLS certificate is a `krt-local-lan-root-ca-2026-r3` leaf for
`adler-black-u2.lan`, `192.168.1.242` and `127.0.0.1` in `/opt/asr/certs`
(owner `10001`, mode `0600`). The read-only container needs `TMPDIR=/tmp/asr`,
otherwise multipart uploads fail with `There was an error parsing the body`.

One transcription runs at a time, so a long batch job from the Phone pipeline
delays VideoAgent's 15-second segments. A queued request whose client has
disconnected, for example after VideoAgent's 45-second timeout, is dropped
instead of being transcribed later.

In the normal two-GPU layout, GPU0 is shared with `black-qwen`. Whisper fits
beside it only because
`black-qwen` runs with `--n-cpu-moe 24` (`krotname/VpnOps#864`, 2026-09-30),
which leaves about 5 GiB free on GPU0. With `--n-cpu-moe 22` there was 2.7 GiB,
and anything longer than a short clip failed with `CUDA failed with error out of memory`.
Measured after the change: a 42-minute recording took 954 s.

The `language` form field defaults to `ru`. An empty value counts as a missing
field and also yields `ru`, so clients request detection with `language=auto`.
`krotname/VideoAgent` on White calls the LAN URL directly. The laptop's
`speech-whisper` and the `adler-media-transcribe` skill still reach the service
over `ssh adler-black-u2.lan` plus a `curl` to the loopback port.

## OCR on Black

Since `2026-09-30` text recognition from images, scans and PDFs runs on Black
next to ASR, from `/opt/ocr` with
[`ocr/docker-compose.black.yml`](../ocr/docker-compose.black.yml). The owner
chose the most accurate engine measured on the P40 rather than the fastest one:

| Engine on the P40 (5 synthetic Russian A4 pages, 200 dpi) | CER | s/page | VRAM |
| --- | ---: | ---: | ---: |
| PaddleOCR PP-OCRv5 mobile, Cyrillic recognizer | 1.35 % | 0.27 | 0.8 GiB |
| PaddleOCR-VL-1.5 on `llama.cpp` | 3.7 % | 3.4 | 2.2 GiB |
| **Qwen3-VL-2B Q8_0 on `llama.cpp`, plain text** | **0.07 %** | **10.5** | **4.1 GiB** |
| **Qwen3-VL-2B Q8_0, line boxes** | **0.09 %** | **16.6** | **4.5 GiB** |

PP-OCRv5 turned Latin inside Russian lines into Cyrillic look-alikes
(`info@example.ru` became `іпfо@ехатрӀе.ги`); PaddleOCR-VL invented words.

Two containers share the host network:

- `ocr-llm` is `llama-server` built by `ocr/Dockerfile.llama` for `sm_61` with
  CUDA 12.9 from the same `llama.cpp` commit as `black-qwen`. It serves
  Qwen3-VL-2B-Instruct Q8_0 and its projector on `127.0.0.1:18090` only, on
  Tesla P40 **GPU1** selected by UUID. `ocr/fetch-models.sh` downloads the two
  GGUF files at a pinned Hugging Face revision and checks their SHA256.
- Host RAM for `ocr-llm` is capped at 12 GiB, with another 6 GiB of swap as
  an emergency fallback (`memswap_limit: 18g`). On 2026-10-01 the former
  6 GiB RAM ceiling forced about 3 GiB into swap during sustained OCR while
  the host still had 83 GiB available, triggering `HostSwapThrashing`.
  Raising the live limit with `docker update --memory 12g --memory-swap 18g
  ocr-llm` preserves the running request; the compose file retains it after
  recreation. Roll back both the compose values and the live Docker limits
  to 6 GiB RAM / 12 GiB total if necessary.
- `ocr` is the FastAPI front end on `https://ocr.adler-black-u2.lan/` (port
  443). It starts as root with `NET_BIND_SERVICE`, `SETUID` and `SETGID` only to
  bind 443, then switches to uid `10001`, which clears those capabilities. It
  renders PDF pages at 200 dpi, applies EXIF rotation to photos, caps every page
  at 4096 image tokens (about 4.2 megapixels, an A4 page at 200 dpi fits), and
  asks the model one page at a time.

With `black-qwen` running in the normal two-GPU layout, GPU0 has insufficient
space for the OCR model: Whisper peaks about 3.5 GiB above its idle 2 GiB
while transcribing and leaves 1.8 GiB. This limit does not apply when
`black-qwen` is stopped: Whisper and OCR can share one healthy P40. GPU1 had
2.1 GiB free, so `black-qwen` moves the experts of its last three layers to the CPU
(`krotname/VpnOps#870`), which frees 3.9 GiB there at a 3 % generation cost.
Rolling that back requires stopping `ocr-llm` first.

Access mirrors ASR, by the owner's decision of `2026-09-30`: LAN only, no
authentication, one ufw rule. The name `ocr.adler-black-u2.lan` is a router
dnsmasq record managed by `krotname/VpnOps` (`ops/black-ocr`). The service has
its own `krt-local-lan-root-ca-2026-r3` leaf for `ocr.adler-black-u2.lan`,
`adler-black-u2.lan`, `192.168.1.242` and `127.0.0.1` in `/opt/ocr/certs`
(owner `10001`, mode `0600`). The first address, port 19444, was closed after
the clients moved:

```bash
sudo ufw allow from 192.168.1.0/24 to 192.168.1.242 port 443 proto tcp comment 'Black OCR LAN API'
```

API: `POST /v1/ocr`, multipart field `file` (PDF, PNG, JPEG, TIFF including
multi-page, WebP, BMP; up to 100 MiB and 200 pages), optional `format`,
`pages` (`1-3,5`), `dpi` (72–400, PDFs only) and `max_tokens` (256 through
the configured `OCR_MAX_TOKENS`, default ceiling 6144). A smaller per-request
token budget bounds generation for short image fragments; incomplete replies
still carry `truncated: true` so clients can split them further.

| `format` | Result |
| --- | --- |
| `text` (default) | `text/plain`, pages separated by a form feed |
| `text-json` | plain text in a JSON envelope with per-page `truncated` flags and empty `lines`; a length-limited response is retried once with a repetition penalty before reporting incomplete text |
| `json` | text plus line boxes per page: `bbox` is `[x0, y0, x1, y1]` in PDF points (`unit: pt`) or source pixels (`unit: px`), `truncated` flags a page that hit the token limit |
| `pdf` | a searchable PDF: the original pages with an invisible text layer; PDF pages that already carry text are left as they are |

```bash
curl --cacert krt-local-lan-root-ca-2026-r3.pem -F file=@scan.pdf -F format=pdf \
  -o scan.searchable.pdf https://ocr.adler-black-u2.lan/v1/ocr
```

One job runs at a time and a queued job whose client has disconnected is
dropped, as in ASR; a running multi-page job stops at the next page when its
client leaves. `GET /health` answers 503 until the model server is ready.

## Single GPU fallback on Black

The owner-approved fallback keeps ASR and OCR fully functional on the sole
healthy Tesla P40. The general-purpose Qwen service `black-qwen`, managed by
`krotname/VpnOps`, must be stopped before ASR and OCR share that card; running
it with only one healthy GPU is forbidden. The dedicated OCR Qwen3-VL-2B
`ocr-llm` remains running, bound only to `127.0.0.1:18090` and reached by the
OCR front end at `http://127.0.0.1:18090`.

The Black Compose files consume these GPU selectors:

| Compose service | Variable | Default when unset or empty |
| --- | --- | --- |
| ASR `asr` | `BLACK_ASR_GPU_UUID` | `0` (normal GPU0) |
| OCR `ocr-llm` | `BLACK_OCR_GPU_UUID` | `GPU-9b076900-4700-5e08-9abd-c67fb39c6026` (normal GPU1) |

The VpnOps controller selects the healthy card and writes
`/etc/black-gpu/runtime.env`. In fallback, **both variables contain the same
sole healthy GPU UUID**; the placeholder below must be replaced by that UUID:

```dotenv
BLACK_ASR_GPU_UUID=GPU-<sole-healthy-P40-uuid>
BLACK_OCR_GPU_UUID=GPU-<sole-healthy-P40-uuid>
```

The controller passes `--env-file /etc/black-gpu/runtime.env` to each Compose
invocation. GPU health detection, stopping the general-purpose Qwen service
and switching modes belong to VpnOps; these manifests only parameterize GPU
selection. The fallback preserves the ASR/OCR APIs, TLS certificates, ports,
LAN-only access without authentication, models and processing functionality.

Config-only validation on a checkout, without starting containers or contacting
a Docker daemon:

```bash
docker compose --env-file /etc/black-gpu/runtime.env -f asr/docker-compose.black.yml config --quiet
docker compose --env-file /etc/black-gpu/runtime.env -f ocr/docker-compose.black.yml config --quiet
```

## Validation

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test-recorder.ps1
```

The recorder smoke test treats camera availability separately from the
configuration count and verifies that at least one camera is actively producing
copy-mode recording segments.
