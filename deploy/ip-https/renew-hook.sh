#!/bin/sh
set -eu

# The host may have certificates for unrelated services.
test "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/8.137.197.104 || exit 0
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
