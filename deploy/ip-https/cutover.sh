#!/bin/sh
# Switch only the existing Docker demo origins and old HTTP listeners.
# The certificate and additive TLS listeners must have passed prepare.sh first.
set -eu
umask 077

IP=8.137.197.104
BIND=172.19.36.105
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
test "$(id -u)" -eq 0 || { echo 'Run as root' >&2; exit 2; }
test "$#" -eq 0 || { echo 'cutover.sh takes no arguments' >&2; exit 2; }
test -f "$HERE/nginx-http-redirect.conf"
test -f "$HERE/rollback.sh"
test -f /etc/nginx/conf.d/atoms-ip-https.conf
cmp -s "$HERE/nginx-https.conf" /etc/nginx/conf.d/atoms-ip-https.conf || {
    echo 'HTTPS Nginx configuration differs from the verified file' >&2; exit 2;
}
test -f /etc/nginx/conf.d/atoms-ip-direct-private.conf
openssl x509 -in "/etc/letsencrypt/live/$IP/fullchain.pem" -noout -checkip "$IP"
openssl x509 -in "/etc/letsencrypt/live/$IP/fullchain.pem" -noout -checkend 259200
curl --noproxy '*' --silent --show-error --fail --max-time 10 \
    --connect-to "$IP:443:$BIND:443" "https://$IP/healthz" >/dev/null
code=$(curl --noproxy '*' --silent --show-error --max-time 10 \
    --output /dev/null --write-out '%{http_code}' \
    --connect-to "$IP:18443:$BIND:18443" "https://$IP:18443/")
test "$code" = 404 || { echo 'Preview TLS listener is not ready' >&2; exit 2; }

cd /opt/atoms-demo-docker
test -f .env && test -f atoms.private.env
test "$(stat -c %a .env)" = 600
test "$(stat -c %a atoms.private.env)" = 600
test -d /var/lib/atoms-demo-docker
docker compose -f compose.yaml -f compose.private.yaml config --quiet
python3 - <<'PY'
import sqlite3
from pathlib import Path

settings = dict(line.split('=', 1) for line in Path('atoms.private.env').read_text().splitlines()
                if line and not line.startswith('#') and '=' in line)
expected = {
    'ATOMS_PUBLIC_ORIGIN': 'http://8.137.197.104:18080',
    'ATOMS_PREVIEW_ORIGIN': 'http://8.137.197.104:18001',
    'ATOMS_PRIVATE_TEST_MODE': 'true',
    'ATOMS_PUBLIC_IP_TEST_MODE': 'true',
    'ATOMS_HOST': '127.0.0.1',
    'ATOMS_PREVIEW_BIND_HOST': '127.0.0.1',
    'ATOMS_PREVIEW_BIND_PORT': '8132',
}
for key, value in expected.items():
    if settings.get(key) != value:
        raise SystemExit(f'Unexpected pre-cutover {key}')
with sqlite3.connect('file:/var/lib/atoms-demo-docker/platform.db?mode=ro', uri=True) as db:
    active = db.execute("SELECT count(*) FROM jobs WHERE state != 'done'").fetchone()[0]
    integrity = db.execute('PRAGMA quick_check').fetchone()[0]
if active or integrity != 'ok':
    raise SystemExit(f'Pre-cutover database: jobs={active}, quick_check={integrity}')
PY

BACKUP=$(mktemp -d /root/atoms-ip-https-cutover-XXXXXXXX)
cp -a .env atoms.private.env "$BACKUP/"
cp -a /etc/nginx/conf.d/atoms-ip-direct-private.conf \
    "$BACKUP/atoms-ip-direct-private.conf"
printf 'cutover_backup=%s\n' "$BACKUP"
changed=0
on_exit() {
    status=$?
    trap - EXIT
    if test "$status" -ne 0 && test "$changed" -eq 1; then
        if sh "$HERE/rollback.sh" "$BACKUP"; then
            echo 'Cutover failed; HTTP configuration restored and data preserved' >&2
        else
            printf 'Cutover failed; manual recovery required from %s\n' "$BACKUP" >&2
        fi
    fi
    exit "$status"
}
trap on_exit EXIT

changed=1
docker compose -f compose.yaml -f compose.private.yaml down
test -z "$(docker compose -f compose.yaml -f compose.private.yaml ps -q)"
cp -a /var/lib/atoms-demo-docker "$BACKUP/data"
python3 - "$BACKUP/data/platform.db" <<'PY'
import sqlite3, sys
with sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True) as db:
    result = db.execute('PRAGMA integrity_check').fetchone()[0]
    active = db.execute("SELECT count(*) FROM jobs WHERE state != 'done'").fetchone()[0]
if result != 'ok':
    raise SystemExit('Stopped-stack backup failed integrity_check')
if active:
    raise SystemExit(f'{active} active jobs appeared before stack shutdown')
print('backup_integrity=ok')
PY

python3 - <<'PY'
import os
from pathlib import Path

path = Path('atoms.private.env')
lines = path.read_text().splitlines()
replacements = {
    'ATOMS_PUBLIC_ORIGIN': 'https://8.137.197.104',
    'ATOMS_PREVIEW_ORIGIN': 'https://8.137.197.104:18443',
}
for key in replacements:
    if sum(line.startswith(key + '=') for line in lines) != 1:
        raise SystemExit(f'Expected exactly one {key} entry')
next_lines = [line.split('=', 1)[0] + '=' + replacements[line.split('=', 1)[0]]
              if line.split('=', 1)[0] in replacements else line for line in lines]
temporary = path.with_suffix('.https-next')
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write('\n'.join(next_lines) + '\n')
os.replace(temporary, path)
PY
install -m 644 "$HERE/nginx-http-redirect.conf" \
    /etc/nginx/conf.d/atoms-ip-direct-private.conf
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
test "$attempt" -lt 30 || { echo 'HTTPS app did not become healthy' >&2; exit 1; }
docker compose -f compose.yaml -f compose.private.yaml up -d --no-deps web

curl --noproxy '*' --silent --show-error --fail --max-time 10 \
    --connect-to "$IP:443:$BIND:443" "https://$IP/healthz" >/dev/null
status=$(curl --noproxy '*' --silent --show-error --max-time 10 \
    --output /dev/null --write-out '%{http_code}' \
    --header "Origin: https://$IP" --connect-to "$IP:443:$BIND:443" \
    "https://$IP/api/v1/identity/auth-config")
test "$status" = 200 || { echo "HTTPS auth config returned $status" >&2; exit 1; }
status=$(curl --noproxy '*' --silent --show-error --max-time 10 \
    --output /dev/null --write-out '%{http_code}' \
    --connect-to "$IP:18080:$BIND:18080" "http://$IP:18080/healthz")
test "$status" = 308 || { echo "Old HTTP workbench returned $status" >&2; exit 1; }
printf 'cutover=https_ready backup=%s\n' "$BACKUP"
