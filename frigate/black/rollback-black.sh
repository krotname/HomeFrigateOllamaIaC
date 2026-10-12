#!/usr/bin/env bash
set -Eeuo pipefail
readonly STATE=/run/frigate-black-cutover
systemctl stop frigate-black.service
running=$(docker inspect frigate --format '{{.State.Running}}' 2>/dev/null || true)
[[ "$running" != true ]] || { echo 'Black writer still running'; exit 1; }
mountpoint -q /media/frigate
install -m 600 "$STATE/token" /media/frigate/.black-cutover-stopped.tmp
mv /media/frigate/.black-cutover-stopped.tmp /media/frigate/.black-cutover-stopped
echo 'Black writer stopped and confirmed to White'
