#!/usr/bin/env bash
# Run on each node before changing the writer or nginx.
set -Eeuo pipefail
readonly STATE=/run/frigate-black-cutover
node=${1:?Usage: arm-rollback.sh black|white seconds}
delay=${2:?Specify rollback delay in seconds}
[[ "$node" == black || "$node" == white ]] || exit 2
[[ "$delay" =~ ^[0-9]+$ ]] && ((delay >= 10 && delay <= 1800)) || exit 2
[[ -s "$STATE/token" && -x "$STATE/rollback-$node.sh" ]] || {
  echo 'Missing cutover token or executable rollback script'; exit 1;
}
mountpoint -q /media/frigate
if [[ "$node" == white ]]; then
  for file in home-ai-proxies frigate-white-domain nginx.conf; do
    [[ -s "$STATE/$file" ]] || { echo "Missing rollback file: $file"; exit 1; }
  done
fi
unit=frigate-$node-rollback
if systemctl is-active --quiet "$unit.timer"; then
  echo 'Existing rollback timer is already armed; inspect it before retrying'
  exit 1
fi
systemd-run --unit="$unit" --on-active="${delay}s" --timer-property=AccuracySec=1s \
  --property=Type=oneshot --property=TimeoutStartSec=240 "$STATE/rollback-$node.sh"
systemctl is-active --quiet "$unit.timer"
systemctl show "$unit.timer" --property=ActiveState --property=NextElapseUSecMonotonic
