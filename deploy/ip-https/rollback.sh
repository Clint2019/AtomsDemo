#!/bin/sh
# Restore the saved HTTP origin/proxy. Preserve all post-cutover database writes.
set -eu
umask 077

test "$(id -u)" -eq 0 || { echo 'Run as root' >&2; exit 2; }
test "$#" -eq 1 || { echo 'Usage: rollback.sh /root/atoms-ip-https-cutover-*' >&2; exit 2; }
BACKUP=$(realpath -e -- "$1")
case "$BACKUP" in
    /root/atoms-ip-https-cutover-*) ;;
    *) echo 'Backup path is outside the expected directory' >&2; exit 2 ;;
esac
test -f "$BACKUP/atoms.private.env"
test -f "$BACKUP/atoms-ip-direct-private.conf"
cd /opt/atoms-demo-docker
test -f compose.yaml && test -f compose.private.yaml
test -d /var/lib/atoms-demo-docker

python3 - <<'PY'
import sqlite3
with sqlite3.connect('file:/var/lib/atoms-demo-docker/platform.db?mode=ro', uri=True) as db:
    active = db.execute("SELECT count(*) FROM jobs WHERE state != 'done'").fetchone()[0]
if active:
    raise SystemExit(f'{active} active jobs prevent rollback')
PY

docker compose -f compose.yaml -f compose.private.yaml down
install -m 600 "$BACKUP/atoms.private.env" /opt/atoms-demo-docker/atoms.private.env
install -m 644 "$BACKUP/atoms-ip-direct-private.conf" \
    /etc/nginx/conf.d/atoms-ip-direct-private.conf
if test -f /etc/nginx/conf.d/atoms-ip-https.conf; then
    test ! -e "$BACKUP/atoms-ip-https.disabled.conf"
    mv /etc/nginx/conf.d/atoms-ip-https.conf "$BACKUP/atoms-ip-https.disabled.conf"
fi
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx

docker compose -f compose.yaml -f compose.private.yaml up -d app
attempt=0
while test "$attempt" -lt 30; do
    id=$(docker compose -f compose.yaml -f compose.private.yaml ps -q app)
    if test -n "$id" && test "$(docker inspect "$id" --format '{{.State.Health.Status}}')" = healthy; then
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
test "$attempt" -lt 30 || { echo 'HTTP app did not become healthy' >&2; exit 1; }
docker compose -f compose.yaml -f compose.private.yaml up -d --no-deps web

attempt=0
while test "$attempt" -lt 30; do
    if curl --noproxy '*' --silent --fail --max-time 2 \
            --header 'Host: 8.137.197.104' \
            http://127.0.0.1:18080/healthz >/dev/null; then
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
test "$attempt" -lt 30 || { echo 'HTTP proxy did not recover' >&2; exit 1; }
printf 'rollback=http_ready backup=%s data=preserved\n' "$BACKUP"
