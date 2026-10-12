# Hik-Connect shared cameras on Black

The adapter exposes four main and four substreams to the existing Frigate Docker
network. It uses the unchanged [experimental go2rtc implementation](https://github.com/pedropaulovc/go2rtc/pull/6)
at commit `b5c61c18c26710d93c47e8efec9dc7ec14205c54`, with its RTSP `exec` interface.
The Python worker implements the Russian Hik-Connect VTM TCP transport because the
fork's original UDP path does not establish these shared connections.

The supported recorder profile is HEVC: 1920x1080 main at 15 fps and 352x288 sub at
10 fps. Parameter sets and IRAP frames have selective AES-ECB encryption required
by the recorder protocol; P slices remain clear. Original RTP timestamps survive
local publication, fragmentation, clock wraps and reconnects. Audio is omitted.
There is no dependency on a phone or an open browser.

Frigate's existing go2rtc converts only the main streams to H.264 using the
already assigned Tesla P40 and CUDA/NVENC. Resolution and 15 fps are preserved;
the substreams stay HEVC. Encoder headers are available immediately and keyframes
occur every two seconds. This makes live video and recordings playable in Chrome
without adding GPU access to the cloud adapter. The existing Black compose GPU
assignment and FFmpeg 8 installation are required.

## Build and credentials

Run `build-on-red.sh <main-sha>` from an exact checkout of `origin/main` on Red as
UID 1000. Compilation and image assembly run offline; compilation containers are
unprivileged, read-only, resource limited, and receive no account configuration.
The trusted dependency fetchers only download modules and binary wheels. The
result is tagged and labelled with the exact main SHA and the pinned go2rtc SHA.
Transfer the exported image to Black and load it with Docker.

Create `/opt/hikconnect-relay/runtime.local.json` using the example; mode 0600,
owner UID 1000. Read credentials once from the owner's password manager during
setup. The service reads only this local file. Set `HIK_RELAY_IMAGE` in a local
`.env` to the verified SHA tag. Neither file belongs in Git. Start `compose.yml`
after the existing `frigate_default` network exists. No port is published; the
administration API binds to the container's loopback address.

Workers share a locked 0600 session cache in container tmpfs, authenticate again
after local expiry or server rejection, and consume short-lived VTM tokens once.
Disconnected streams retry with a 1–30 second backoff. `SIGUSR1` reconnects an
active worker. `relay.py --expire-session` invalidates the local cache for testing.
Logs contain only aliases, event names and exception classes. Debug and trace
logging should remain disabled.

## Frigate configuration and acceptance

Run `add_cameras.py <current-config> <candidate-config>` in the existing Frigate
Python environment. Validate the candidate with that installed Frigate version
before replacement. This operation does not import or run the Black migration.
It preserves the existing cameras and global settings, adds continuous main
recording and substream detection at 3 fps, retains recordings/events/snapshots
for three days, and disables face recognition on the new cameras. People are
alerts; the selected vehicles and animals are detections. `--main-only` is the
fallback when a recorder has no working substream.

`verify_runtime.py --seconds 1800 --record-main` checks all eight streams in
parallel. Run it in a disposable, unprivileged container using the installed
Frigate image and its FFmpeg 8 binaries on the same network. Mount an empty
temporary directory for bounded probe recordings/results, and limit CPU, memory
and PIDs. Each decoder is restricted to one thread. The report includes media
duration, decoded frames, error counts and a measured three-day storage estimate;
storage varies with activity. Verify forced session expiry and adapter restart
with `--allow-reconnect` before the uninterrupted acceptance run.
Repeat acceptance against the four H.264 main streams with `--main-only --host`
set to the restream's Docker service name; estimate archive capacity from those
converted main streams. Format probing allows ten seconds for the initial
encoder parameters; decoded duration and zero-error requirements still apply.
A disposable restream container running four CUDA encoders needs a PID limit of
at least 512; the cloud adapter's separate 128-PID limit is sufficient for its
eight lightweight workers. No production Frigate PID limit needs changing.

Before applying the validated candidate, retain a temporary local rollback copy
of the current config and arm an independent systemd timer to restore it and
restart `frigate-black.service`. Restart Frigate only after the timer is active.
Cancel it after checking all seven cameras, fresh recordings on the White NFS
archive, archive playback, the UI, events, and `krt-camera-watchdog.timer` from an
independent client. Remove the rollback copy after successful acceptance. A
rollback removes only the added streams/cameras and stops the adapter; it must
preserve the three existing cameras and watchdog. Deploy only a verified exact
SHA reachable from main after required CI; do not run `frigate/black/deploy.py`.
