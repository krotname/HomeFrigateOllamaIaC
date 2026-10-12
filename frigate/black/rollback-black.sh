#!/usr/bin/env bash
set -Eeuo pipefail
readonly STATE=/run/frigate-black-cutover
systemctl disable --now krt-camera-watchdog.timer
systemctl stop krt-camera-watchdog.service
systemctl disable --now frigate-black.service
running=$(docker inspect frigate --format '{{.State.Running}}' 2>/dev/null || true)
[[ "$running" != true ]] || { echo 'Black writer still running'; exit 1; }
mountpoint -q /media/frigate
token=$(cat "$STATE/token")
[[ "$token" =~ ^[0-9a-f-]{36}$ ]] || exit 1
if [[ -f /opt/frigate/config/frigate.db ]]; then
  db_files=(frigate.db)
  for file in frigate.db-wal frigate.db-shm; do
    [[ ! -f /opt/frigate/config/$file ]] || db_files+=("$file")
  done
  tar -C /opt/frigate/config -cf "/media/frigate/.black-cutover-db-$token.tar.tmp" "${db_files[@]}"
  mv "/media/frigate/.black-cutover-db-$token.tar.tmp" "/media/frigate/.black-cutover-db-$token.tar"
fi
install -m 600 "$STATE/token" /media/frigate/.black-cutover-stopped.tmp
mv /media/frigate/.black-cutover-stopped.tmp /media/frigate/.black-cutover-stopped
echo 'Black writer stopped and confirmed to White'
