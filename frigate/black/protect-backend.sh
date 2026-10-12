#!/usr/bin/env bash
# Restrict the published backend without changing SSH, LAN or WAN access.
set -Eeuo pipefail
source_file=/opt/frigate/proxy-sources
[[ -s "$source_file" ]] || { echo 'Missing verified White proxy source'; exit 1; }
mapfile -t sources < "$source_file"
(( ${#sources[@]} >= 1 && ${#sources[@]} <= 2 )) || exit 1
for source in "${sources[@]}"; do
  [[ "$source" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || exit 1
done
iptables -w -S DOCKER-USER >/dev/null
iptables -w -N FRIGATE-PROXY 2>/dev/null || iptables -w -S FRIGATE-PROXY >/dev/null
iptables -w -F FRIGATE-PROXY
for source in "${sources[@]}"; do
  iptables -w -A FRIGATE-PROXY -s "$source" -j RETURN
done
iptables -w -A FRIGATE-PROXY -j REJECT --reject-with tcp-reset
while iptables -w -C DOCKER-USER -p tcp -m conntrack --ctorigdstport 18971 -j FRIGATE-PROXY 2>/dev/null; do
  iptables -w -D DOCKER-USER -p tcp -m conntrack --ctorigdstport 18971 -j FRIGATE-PROXY
done
iptables -w -C DOCKER-USER -p tcp -m conntrack --ctorigdstport 18971 --ctdir ORIGINAL -j FRIGATE-PROXY 2>/dev/null ||
  iptables -w -I DOCKER-USER 1 -p tcp -m conntrack --ctorigdstport 18971 --ctdir ORIGINAL -j FRIGATE-PROXY
