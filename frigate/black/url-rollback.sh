#!/bin/bash
set -euo pipefail
state=/run/frigate-url-change
cp "$state/frigate-white-domain" /etc/nginx/sites-available/frigate-white-domain
rm -f /etc/nginx/sites-enabled/frigate-black-domain /etc/nginx/sites-available/frigate-black-domain
nginx -t
systemctl reload nginx
