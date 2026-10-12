#!/usr/bin/env bash
# A transient systemd timer on White executes this independently of SSH.
set -Eeuo pipefail
readonly STATE=/run/frigate-black-cutover
[[ -d "$STATE" ]] || { echo 'No cutover state'; exit 1; }

# The paired Black timer writes its token only after systemd stopped the writer
# and Docker confirmed it is stopped. The marker travels through the archive,
# so recovery does not need a private SSH key or the operator connection.
token=$(cat "$STATE/token")
for ((attempt=0; attempt<30; attempt++)); do
  if [[ -f /media/frigate/.black-cutover-stopped ]] &&
     [[ $(cat /media/frigate/.black-cutover-stopped) == "$token" ]]; then
    break
  fi
  sleep 5
done
[[ -f /media/frigate/.black-cutover-stopped ]] &&
  [[ $(cat /media/frigate/.black-cutover-stopped) == "$token" ]] || {
    echo 'Black has not confirmed writer shutdown; refusing a second writer'
    exit 1
  }
cp "$STATE/home-ai-proxies" /etc/nginx/sites-available/home-ai-proxies
cp "$STATE/frigate-white-domain" /etc/nginx/sites-available/frigate-white-domain
if [[ -f "$STATE/nginx.conf" ]]; then
  cp "$STATE/nginx.conf" /etc/nginx/nginx.conf
fi
rm -f /etc/nginx/frigate-black-streams.conf
nginx -t
systemctl reload nginx
docker compose -f /opt/frigate/docker-compose.yml start frigate
systemctl start krt-container-watchdog.timer krt-camera-watchdog.timer
echo 'White recorder restored; Black stopped'

