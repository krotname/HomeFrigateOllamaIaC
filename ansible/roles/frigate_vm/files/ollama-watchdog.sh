#!/usr/bin/env bash
set -Eeuo pipefail

readonly OLLAMA_URL="${OLLAMA_WATCHDOG_URL:-http://127.0.0.1:11435}"
readonly PROBE_MODEL="${OLLAMA_WATCHDOG_PROBE_MODEL:-bge-m3}"
readonly PROBE_TIMEOUT="${OLLAMA_WATCHDOG_PROBE_TIMEOUT:-150}"
readonly LIGHT_TIMEOUT="${OLLAMA_WATCHDOG_LIGHT_TIMEOUT:-10}"
readonly VRAM_SLACK_BYTES=$((512 * 1024 * 1024))
readonly STATE_DIR="${OLLAMA_WATCHDOG_STATE_DIR:-/run/krt-ollama-watchdog}"
readonly FAILURE_THRESHOLD="${OLLAMA_WATCHDOG_FAILURE_THRESHOLD:-2}"
readonly COOLDOWN_SECONDS="${OLLAMA_WATCHDOG_COOLDOWN_SECONDS:-900}"
readonly DRY_RUN="${OLLAMA_WATCHDOG_DRY_RUN:-0}"

log() {
  printf '[ollama-watchdog] %s\n' "$*"
}

read_number() {
  local path="$1"
  if [[ -r "$path" ]] && [[ $(<"$path") =~ ^[0-9]+$ ]]; then
    cat "$path"
  else
    printf '0\n'
  fi
}

reset_failure_streak() {
  printf '0\n' >"$STATE_DIR/failures"
}

restart_ollama() {
  local reason="$1"
  log "restart triggered: $reason"
  if [[ "$DRY_RUN" == 1 ]]; then
    log 'dry-run: restart suppressed'
    exit 0
  fi
  if ! systemctl restart ollama; then
    log 'ERROR: systemctl restart ollama failed'
    exit 1
  fi
  for _ in {1..12}; do
    if systemctl is-active --quiet ollama && curl -fsS -m "$LIGHT_TIMEOUT" "$OLLAMA_URL/api/version" >/dev/null; then
      log 'restart succeeded and API is responding'
      exit 0
    fi
    sleep 5
  done
  log 'ERROR: Ollama stayed unavailable after restart'
  exit 1
}

handle_failure() {
  local reason="$1"
  local failure_file="$STATE_DIR/failures"
  local recovery_file="$STATE_DIR/last-recovery"
  local failures=$(( $(read_number "$failure_file") + 1 ))
  local now
  local last_recovery

  printf '%s\n' "$failures" >"$failure_file"
  log "probe failed: $reason; failure streak=$failures/$FAILURE_THRESHOLD"
  if (( failures < FAILURE_THRESHOLD )); then
    log 'restart deferred until the failure is confirmed by another watchdog run'
    exit 1
  fi

  now=$(date +%s)
  last_recovery=$(read_number "$recovery_file")
  if (( now - last_recovery < COOLDOWN_SECONDS )); then
    log "restart suppressed by ${COOLDOWN_SECONDS}s cooldown"
    exit 1
  fi

  printf '%s\n' "$now" >"$recovery_file"
  reset_failure_streak
  restart_ollama "$reason"
}

if ! [[ "$FAILURE_THRESHOLD" =~ ^[1-9][0-9]*$ && "$COOLDOWN_SECONDS" =~ ^[0-9]+$ ]]; then
  log 'ERROR: invalid failure threshold or cooldown'
  exit 2
fi

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/lock"
if ! flock -n 9; then
  log 'another watchdog instance holds the lock; skipping'
  exit 0
fi

if ! systemctl is-active --quiet ollama; then
  handle_failure 'systemd unit is not active'
fi

if ! curl -fsS -m "$LIGHT_TIMEOUT" "$OLLAMA_URL/api/version" >/dev/null; then
  handle_failure 'API version probe failed'
fi

probe_start=$(date +%s)
if ! probe_body=$(curl -fsS -m "$PROBE_TIMEOUT" -X POST "$OLLAMA_URL/api/embeddings" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$PROBE_MODEL\",\"prompt\":\"watchdog probe\"}"); then
  handle_failure "generation probe for '$PROBE_MODEL' failed"
fi
probe_elapsed=$(( $(date +%s) - probe_start ))
if [[ "$probe_body" != *'"embedding"'* ]]; then
  handle_failure "generation probe returned an unexpected response after ${probe_elapsed}s"
fi
reset_failure_streak
log "liveness OK; generation probe answered in ${probe_elapsed}s"

if ! ps_json=$(curl -fsS -m "$LIGHT_TIMEOUT" "$OLLAMA_URL/api/ps"); then
  log 'ERROR: /api/ps request failed'
  exit 1
fi

if ! offenders=$(python3 -c '
import json
import sys

slack = int(sys.argv[1])
try:
    data = json.load(sys.stdin)
except Exception as exc:
    print(f"invalid /api/ps JSON: {exc}", file=sys.stderr)
    raise SystemExit(2)
if not isinstance(data, dict) or not isinstance(data.get("models", []), list):
    print("invalid /api/ps schema", file=sys.stderr)
    raise SystemExit(2)
for model in data.get("models", []):
    name = str(model.get("name", "?"))
    size = int(model.get("size", 0) or 0)
    vram = int(model.get("size_vram", 0) or 0)
    if size > 0 and size - vram > slack:
        percent = 100 * vram / size
        print(f"{name}\t{vram}\t{size}\t{percent:.1f}")
' "$VRAM_SLACK_BYTES" <<<"$ps_json"); then
  log 'ERROR: /api/ps returned invalid JSON or schema'
  exit 1
fi

unload_failed=0
if [[ -n "$offenders" ]]; then
  while IFS=$'\t' read -r name vram size percent; do
    log "CPU fallback detected for '$name': ${percent}% in VRAM ($vram/$size); unloading"
    if curl -fsS -m "$LIGHT_TIMEOUT" -X POST "$OLLAMA_URL/api/generate" \
      -H 'Content-Type: application/json' \
      -d "{\"model\":\"$name\",\"keep_alive\":0}" >/dev/null; then
      log "unloaded '$name'"
    else
      log "ERROR: failed to unload '$name'"
      unload_failed=1
    fi
  done <<<"$offenders"
else
  log 'GPU check OK; every loaded model is VRAM-resident'
fi

exit "$unload_failed"
