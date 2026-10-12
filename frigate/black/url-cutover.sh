#!/bin/bash
# Execute on the White nginx VM from a verified main checkout, as root.
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
state=/run/frigate-url-change
exec 9>/run/frigate-url-change.lock
flock -x 9
case "${1:-}" in
  arm)
    test ! -e /etc/nginx/sites-available/frigate-black-domain
    test ! -L /etc/nginx/sites-available/frigate-black-domain
    test ! -e /etc/nginx/sites-enabled/frigate-black-domain
    test ! -L /etc/nginx/sites-enabled/frigate-black-domain
    test -L /etc/nginx/sites-enabled/frigate-white-domain
    test "$(readlink -f /etc/nginx/sites-enabled/frigate-white-domain)" = /etc/nginx/sites-available/frigate-white-domain
    if systemctl is-active --quiet frigate-url-rollback.timer; then
      echo 'An existing rollback is armed' >&2
      exit 1
    fi
    install -d -m 700 "$state"
    cp /etc/nginx/sites-available/frigate-white-domain "$state/frigate-white-domain"
    install -m 700 "$here/url-rollback.sh" "$state/rollback.sh"
    systemd-run --unit=frigate-url-rollback --on-active=1200s /bin/bash "$state/rollback.sh"
    systemctl is-active --quiet frigate-url-rollback.timer
    ;;
  apply)
    systemctl is-active --quiet frigate-url-rollback.timer
    install -m 644 "$here/frigate-black-domain.conf" /etc/nginx/sites-available/frigate-black-domain
    ln -s /etc/nginx/sites-available/frigate-black-domain /etc/nginx/sites-enabled/frigate-black-domain
    sed 's/return 308 /return 307 /' "$here/frigate-white-redirect.conf" > /etc/nginx/sites-available/frigate-white-domain
    nginx -t
    systemctl reload nginx
    ;;
  finish)
    # Caller must first prove independent TLS/auth/media/DNS acceptance.
    systemctl is-active --quiet frigate-url-rollback.timer
    ! systemctl is-active --quiet frigate-url-rollback.service
    grep -q 'return 307 https://frigate.adler-black-u2.lan$request_uri;' /etc/nginx/sites-available/frigate-white-domain
    nginx -t
    systemctl stop frigate-url-rollback.timer frigate-url-rollback.service
    ! systemctl is-active --quiet frigate-url-rollback.service
    test "$(readlink -f /etc/nginx/sites-enabled/frigate-black-domain)" = /etc/nginx/sites-available/frigate-black-domain
    cmp -s "$here/frigate-black-domain.conf" /etc/nginx/sites-available/frigate-black-domain
    grep -q 'return 307 https://frigate.adler-black-u2.lan$request_uri;' /etc/nginx/sites-available/frigate-white-domain
    install -m 644 "$here/frigate-white-redirect.conf" /etc/nginx/sites-available/frigate-white-domain
    nginx -t
    systemctl reload nginx
    ;;
  *) echo 'Usage: url-cutover.sh arm|apply|finish' >&2; exit 2 ;;
esac
