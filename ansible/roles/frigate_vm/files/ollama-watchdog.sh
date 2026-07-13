#!/usr/bin/env bash
set -Eeuo pipefail

readonly OLLAMA_URL='http://127.0.0.1:11435'
readonly PROBE_MODEL='bge-m3'
readonly PROBE_TIMEOUT=150
readonly LIGHT_TIMEOUT=10
readonly VRAM_SLACK_BYTES=$((512 * 1024 * 1024))

log() {
  printf '[ollama-watchdog] %s\n' "$*"
}

restart_ollama() {
  local reason="$1"
  log "restart triggered: $reason"
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

if ! systemctl is-active --quiet ollama; then
  restart_ollama 'systemd unit is not active'
fi

if ! curl -fsS -m "$LIGHT_TIMEOUT" "$OLLAMA_URL/api/version" >/dev/null; then
  restart_ollama 'API version probe failed'
fi

probe_start=$(date +%s)
if ! probe_body=$(curl -fsS -m "$PROBE_TIMEOUT" -X POST "$OLLAMA_URL/api/embeddings" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$PROBE_MODEL\",\"prompt\":\"watchdog probe\"}"); then
  restart_ollama "generation probe for '$PROBE_MODEL' failed"
fi
probe_elapsed=$(( $(date +%s) - probe_start ))
if [[ "$probe_body" != *'"embedding"'* ]]; then
  restart_ollama "generation probe returned an unexpected response after ${probe_elapsed}s"
fi
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
