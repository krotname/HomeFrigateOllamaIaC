#!/bin/bash
set -euo pipefail
state=/run/frigate-url-change
exec 9>/run/frigate-url-change.lock
flock -x 9
for name in frigate-black-domain frigate-white-domain frigate-retired-names home-ai-proxies; do
  for dir in available enabled; do
    dst="/etc/nginx/sites-$dir/$name"
    rm -f -- "$dst"
    if test -e "$state/$dir/$name" || test -L "$state/$dir/$name"; then
      cp -a "$state/$dir/$name" "$dst"
    fi
  done
done
nginx -t
systemctl reload nginx
