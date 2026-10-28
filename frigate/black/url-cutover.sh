#!/bin/bash
# Execute on the White nginx VM from a verified main checkout, as root.
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
state=/run/frigate-url-change
exec 9>/run/frigate-url-change.lock
flock -x 9
case "${1:-}" in
  arm)
    nginx -t
    test -L /etc/nginx/sites-enabled/frigate-black-domain
    test "$(readlink -f /etc/nginx/sites-enabled/frigate-black-domain)" = /etc/nginx/sites-available/frigate-black-domain
    if systemctl is-active --quiet frigate-url-rollback.timer || test -e "$state"; then
      echo 'An existing rollback is armed or its state remains' >&2
      exit 1
    fi
    install -d -m 700 "$state/available" "$state/enabled"
    for name in frigate-black-domain frigate-white-domain frigate-retired-names home-ai-proxies; do
      for dir in available enabled; do
        src="/etc/nginx/sites-$dir/$name"
        if test -e "$src" || test -L "$src"; then cp -a "$src" "$state/$dir/$name"; fi
      done
    done
    install -m 700 "$here/url-rollback.sh" "$state/rollback.sh"
    systemd-run --unit=frigate-url-rollback --on-active=1200s /bin/bash "$state/rollback.sh"
    systemctl is-active --quiet frigate-url-rollback.timer
    ;;
  apply)
    systemctl is-active --quiet frigate-url-rollback.timer
    install -m 644 "$here/frigate-black-domain.conf" /etc/nginx/sites-available/frigate-black-domain
    install -m 644 "$here/frigate-white-redirect.conf" /etc/nginx/sites-available/frigate-retired-names
    ln -sfn /etc/nginx/sites-available/frigate-retired-names /etc/nginx/sites-enabled/frigate-retired-names
    rm -f /etc/nginx/sites-enabled/frigate-white-domain /etc/nginx/sites-available/frigate-white-domain
    # Retain the IP endpoint, but remove names now handled by the rejection vhost.
    if test -f /etc/nginx/sites-available/home-ai-proxies; then
      sed -i -E 's/^([[:space:]]*listen 8971 ssl);/\1 default_server;/' /etc/nginx/sites-available/home-ai-proxies
      sed -i -E '/^[[:space:]]*server_name /{:again; s/ (adler-frigate\.lan|frigate\.adler-white-w1\.lan|frigate\.lan|adler-frigate\.local|frigate\.local|adler-asr\.lan|frigate-ubuntu)([ ;])/\2/g; t again;}' /etc/nginx/sites-available/home-ai-proxies
    fi
    nginx -t
    systemctl reload nginx
    ;;
  finish)
    # Caller must first prove independent TLS/auth/media/DNS acceptance.
    systemctl is-active --quiet frigate-url-rollback.timer
    ! systemctl is-active --quiet frigate-url-rollback.service
    cmp -s "$here/frigate-black-domain.conf" /etc/nginx/sites-available/frigate-black-domain
    cmp -s "$here/frigate-white-redirect.conf" /etc/nginx/sites-available/frigate-retired-names
    test ! -e /etc/nginx/sites-enabled/frigate-white-domain
    nginx -t
    systemctl stop frigate-url-rollback.timer
    if test "$(systemctl show -p LoadState --value frigate-url-rollback.service)" != not-found; then
      systemctl stop frigate-url-rollback.service
    fi
    ! systemctl is-active --quiet frigate-url-rollback.service
    rm -r -- "$state"
    ;;
  *) echo 'Usage: url-cutover.sh arm|apply|finish' >&2; exit 2 ;;
esac
