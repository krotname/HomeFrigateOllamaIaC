#!/usr/bin/env bash
# Keeps Frigate's camera list in step with the cameras that actually answer.
#
# A camera that is unplugged, powered off or otherwise gone makes Frigate
# restart ffmpeg every few seconds and fill the log with `Error opening input
# file` and `DESCRIBE failed: 404`. This watchdog disables such a camera through
# the Frigate API (applied live, no container restart) and enables it again as
# soon as its RTSP port answers, so switching a camera off and on needs no
# manual step.
set -Eeuo pipefail

readonly STATE_DIR=/run/krt-camera-watchdog
readonly CONTAINER="${CAMERA_WATCHDOG_CONTAINER:-frigate}"
readonly API_BASE="${CAMERA_WATCHDOG_API_BASE:-http://127.0.0.1:5000}"
readonly RTSP_PORT="${CAMERA_WATCHDOG_RTSP_PORT:-554}"
readonly PROBE_TIMEOUT_SECONDS="${CAMERA_WATCHDOG_PROBE_TIMEOUT_SECONDS:-3}"
readonly API_TIMEOUT_SECONDS="${CAMERA_WATCHDOG_API_TIMEOUT_SECONDS:-10}"
readonly OFFLINE_THRESHOLD="${CAMERA_WATCHDOG_OFFLINE_THRESHOLD:-10}"
readonly ONLINE_THRESHOLD="${CAMERA_WATCHDOG_ONLINE_THRESHOLD:-2}"
read -r -a TARGETS <<<"${CAMERA_WATCHDOG_TARGETS:-}"
readonly TARGETS

log() {
  printf '[camera-watchdog] %s\n' "$*"
}

if (( ${#TARGETS[@]} == 0 )); then
  log 'no camera targets configured; nothing to do'
  exit 0
fi

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/lock"
if ! flock -n 9; then
  log 'another instance holds the lock; skipping'
  exit 0
fi

container_state() {
  docker inspect "$CONTAINER" \
    --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null
}

state=$(container_state || true)
if [[ "$state" != 'running|healthy' ]]; then
  log "container '$CONTAINER' is '${state:-absent}'; leaving the camera list alone"
  exit 0
fi

api_get_config() {
  docker exec "$CONTAINER" \
    curl -sf --max-time "$API_TIMEOUT_SECONDS" "$API_BASE/api/config" 2>/dev/null
}

# Reads `cameras.<name>.enabled` out of the running config. Prints `true`,
# `false` or nothing at all when the camera is not configured.
config_enabled() {
  local config_json="$1" name="$2"
  printf '%s' "$config_json" | python3 -c '
import json
import sys

cameras = json.load(sys.stdin).get("cameras") or {}
camera = cameras.get(sys.argv[1])
if camera is None:
    sys.exit(0)
print("true" if camera.get("enabled", True) else "false")
' "$name"
}

count_enabled() {
  local config_json="$1"
  printf '%s' "$config_json" | python3 -c '
import json
import sys

cameras = json.load(sys.stdin).get("cameras") or {}
print(sum(1 for camera in cameras.values() if camera.get("enabled", True)))
'
}

set_enabled() {
  local name="$1" desired="$2"
  docker exec "$CONTAINER" curl -sf --max-time "$API_TIMEOUT_SECONDS" \
    -X PUT -H 'Content-Type: application/json' \
    -d "{\"requires_restart\":0,\"config_data\":{\"cameras\":{\"$name\":{\"enabled\":$desired}}}}" \
    "$API_BASE/api/config/set" >/dev/null 2>&1
}

probe() {
  local host="$1"
  timeout "$PROBE_TIMEOUT_SECONDS" \
    bash -c "exec 3<>/dev/tcp/$host/$RTSP_PORT" >/dev/null 2>&1
}

read_counter() {
  local path="$1"
  if [[ -r "$path" ]] && [[ $(<"$path") =~ ^[0-9]+$ ]]; then
    cat "$path"
  else
    printf '0\n'
  fi
}

config_json=$(api_get_config || true)
if [[ -z "$config_json" ]]; then
  log 'Frigate API did not answer; leaving the camera list alone'
  exit 0
fi

enabled_total=$(count_enabled "$config_json")
result=0

for target in "${TARGETS[@]}"; do
  name="${target%%=*}"
  host="${target#*=}"
  if [[ -z "$name" || -z "$host" || "$name" == "$target" ]]; then
    log "skipping malformed target '$target'"
    continue
  fi

  enabled=$(config_enabled "$config_json" "$name" || true)
  if [[ -z "$enabled" ]]; then
    log "camera '$name' is not in the running config; skipping"
    continue
  fi

  offline_file="$STATE_DIR/$name.offline"
  online_file="$STATE_DIR/$name.online"

  if probe "$host"; then
    online=$(( $(read_counter "$online_file") + 1 ))
    printf '%s\n' "$online" >"$online_file"
    printf '0\n' >"$offline_file"

    if [[ "$enabled" == 'false' ]] && (( online >= ONLINE_THRESHOLD )); then
      if set_enabled "$name" true; then
        log "camera '$name' answers on $host:$RTSP_PORT again; enabled it"
        printf '0\n' >"$online_file"
        enabled_total=$(( enabled_total + 1 ))
      else
        log "failed to enable camera '$name' through the Frigate API"
        result=1
      fi
    fi
    continue
  fi

  offline=$(( $(read_counter "$offline_file") + 1 ))
  printf '%s\n' "$offline" >"$offline_file"
  printf '0\n' >"$online_file"

  if [[ "$enabled" != 'true' ]] || (( offline < OFFLINE_THRESHOLD )); then
    continue
  fi

  if (( enabled_total <= 1 )); then
    log "camera '$name' is unreachable but it is the last enabled camera; keeping it"
    continue
  fi

  if set_enabled "$name" false; then
    log "camera '$name' has been unreachable for ${offline} checks; disabled it"
    printf '0\n' >"$offline_file"
    enabled_total=$(( enabled_total - 1 ))
  else
    log "failed to disable camera '$name' through the Frigate API"
    result=1
  fi
done

exit "$result"
