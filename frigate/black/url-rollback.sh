#!/bin/bash
set -euo pipefail
state=/run/frigate-url-change
exec 9>/run/frigate-url-change.lock
flock -x 9
cp "$state/frigate-white-domain" /etc/nginx/sites-available/frigate-white-domain
rm -f /etc/nginx/sites-enabled/frigate-black-domain /etc/nginx/sites-available/frigate-black-domain
nginx -t
systemctl reload nginx
