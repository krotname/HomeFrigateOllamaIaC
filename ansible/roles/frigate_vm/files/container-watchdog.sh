#!/usr/bin/env bash
set -Eeuo pipefail

readonly STATE_DIR=/run/krt-container-watchdog
readonly FAILURE_THRESHOLD=3
readonly COOLDOWN_SECONDS=900
readonly RECOVERY_TIMEOUT_SECONDS=180
readonly CONTAINERS=(frigate asr)

log() {
  printf '[container-watchdog] %s\n' "$*"
}

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/lock"
if ! flock -n 9; then
  log 'another instance holds the lock; skipping'
  exit 0
fi

read_number() {
  local path="$1"
  if [[ -r "$path" ]] && [[ $(<"$path") =~ ^[0-9]+$ ]]; then
    cat "$path"
  else
    printf '0\n'
  fi
}

container_state() {
  docker inspect "$1" --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null
}

wait_healthy() {
  local name="$1"
  local deadline=$((SECONDS + RECOVERY_TIMEOUT_SECONDS))
  local state
  while (( SECONDS < deadline )); do
    state=$(container_state "$name" || true)
    if [[ "$state" == 'running|healthy' ]]; then
      return 0
    fi
    sleep 5
  done
  return 1
}

result=0
now=$(date +%s)
for name in "${CONTAINERS[@]}"; do
  if ! docker inspect "$name" >/dev/null 2>&1; then
    log "container '$name' is not deployed; skipping"
    continue
  fi

  state=$(container_state "$name" || true)
  failure_file="$STATE_DIR/$name.failures"
  recovery_file="$STATE_DIR/$name.last-recovery"
  if [[ "$state" == 'running|healthy' || "$state" == 'running|starting' ]]; then
    printf '0\n' >"$failure_file"
    continue
  fi

  failures=$(( $(read_number "$failure_file") + 1 ))
  printf '%s\n' "$failures" >"$failure_file"
  log "container '$name' is '$state'; failure streak=$failures/$FAILURE_THRESHOLD"
  if (( failures < FAILURE_THRESHOLD )); then
    continue
  fi

  last_recovery=$(read_number "$recovery_file")
  if (( now - last_recovery < COOLDOWN_SECONDS )); then
    log "container '$name' recovery is in cooldown"
    result=1
    continue
  fi

  printf '%s\n' "$now" >"$recovery_file"
  printf '0\n' >"$failure_file"
  if [[ "$state" == running\|* ]]; then
    log "restarting unhealthy container '$name'"
    docker restart "$name" >/dev/null
  else
    log "starting stopped container '$name'"
    docker start "$name" >/dev/null
  fi

  if wait_healthy "$name"; then
    log "container '$name' recovered and is healthy"
  else
    log "ERROR: container '$name' did not become healthy"
    result=1
  fi
done

exit "$result"
