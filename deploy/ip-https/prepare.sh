#!/bin/sh
# Run on the verified Chengdu host, from a copy of this directory, as root.
# This phase leaves the current HTTP workbench and preview untouched.
set -eu
umask 077

IP=8.137.197.104
BIND=172.19.36.105
WEBROOT=/var/lib/atoms-demo-ip-acme
CERTBOT=/opt/atoms-demo-certbot/bin/certbot
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

test "$(id -u)" -eq 0 || { echo 'Run as root' >&2; exit 2; }
ip -4 -o addr show | grep -Fq " $BIND/" || {
    echo 'Expected private NIC address is absent' >&2; exit 2;
}
test -f /etc/nginx/conf.d/atoms-ip-direct-private.conf || {
    echo 'Existing HTTP proxy configuration is absent' >&2; exit 2;
}
test -x /usr/sbin/nginx
test -x /usr/bin/openssl
test -f "$HERE/nginx-acme.conf"
test -f "$HERE/nginx-https.conf"

install -d -m 755 "$WEBROOT/.well-known/acme-challenge"
if test -e /etc/nginx/conf.d/atoms-ip-acme.conf; then
    cmp -s "$HERE/nginx-acme.conf" /etc/nginx/conf.d/atoms-ip-acme.conf || {
        echo 'Existing ACME Nginx configuration differs' >&2; exit 2;
    }
else
    install -m 644 "$HERE/nginx-acme.conf" /etc/nginx/conf.d/atoms-ip-acme.conf
fi
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx

probe="$WEBROOT/.well-known/acme-challenge/atoms-probe-$$"
printf 'atoms-ip-acme-%s\n' "$$" > "$probe"
chmod 644 "$probe"
trap 'rm -f "$probe"' EXIT HUP INT TERM
expected=$(cat "$probe")
actual=$(curl --noproxy '*' --silent --show-error --fail --max-time 10 \
    --connect-to "$IP:80:$BIND:80" \
    "http://$IP/.well-known/acme-challenge/$(basename "$probe")")
test "$actual" = "$expected" || { echo 'ACME HTTP challenge probe failed' >&2; exit 2; }
rm -f "$probe"
trap - EXIT HUP INT TERM

if test ! -x "$CERTBOT"; then
    python3 -m venv /opt/atoms-demo-certbot
    /opt/atoms-demo-certbot/bin/pip install --disable-pip-version-check \
        'certbot>=5.4,<6'
fi
version=$($CERTBOT --version | awk '{print $2}')
test "$(printf '%s\n' '5.4' "$version" | sort -V | head -n 1)" = '5.4' || {
    echo 'Certbot 5.4 or newer is required for IP webroot validation' >&2; exit 2;
}

# Validate the HTTP-01 route against Let's Encrypt staging before production.
"$CERTBOT" certonly --staging --non-interactive --agree-tos \
    --register-unsafely-without-email --preferred-profile shortlived \
    --webroot --webroot-path "$WEBROOT" \
    --cert-name atoms-demo-ip-staging --ip-address "$IP"
"$CERTBOT" certonly --non-interactive --agree-tos \
    --register-unsafely-without-email --preferred-profile shortlived \
    --webroot --webroot-path "$WEBROOT" \
    --cert-name "$IP" --ip-address "$IP"

CERT="/etc/letsencrypt/live/$IP/fullchain.pem"
test -f "$CERT"
openssl x509 -in "$CERT" -noout -checkip "$IP"
openssl x509 -in "$CERT" -noout -checkend 259200

install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
install -m 755 "$HERE/renew-hook.sh" \
    /etc/letsencrypt/renewal-hooks/deploy/atoms-ip-nginx.sh
install -m 644 "$HERE/atoms-ip-certbot-renew.service" \
    /etc/systemd/system/atoms-ip-certbot-renew.service
install -m 644 "$HERE/atoms-ip-certbot-renew.timer" \
    /etc/systemd/system/atoms-ip-certbot-renew.timer
/usr/bin/systemctl daemon-reload
/usr/bin/systemctl enable --now atoms-ip-certbot-renew.timer

if test -e /etc/nginx/conf.d/atoms-ip-https.conf; then
    cmp -s "$HERE/nginx-https.conf" /etc/nginx/conf.d/atoms-ip-https.conf || {
        echo 'Existing HTTPS Nginx configuration differs' >&2; exit 2;
    }
else
    install -m 644 "$HERE/nginx-https.conf" /etc/nginx/conf.d/atoms-ip-https.conf
fi
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx

curl --noproxy '*' --silent --show-error --fail --max-time 10 \
    --connect-to "$IP:443:$BIND:443" "https://$IP/healthz" >/dev/null
code=$(curl --noproxy '*' --silent --show-error --max-time 10 --output /dev/null \
    --write-out '%{http_code}' --connect-to "$IP:18443:$BIND:18443" \
    "https://$IP:18443/")
test "$code" = 403 || { echo "Unexpected pre-cutover preview root HTTP $code" >&2; exit 2; }
/usr/bin/systemctl start atoms-ip-certbot-renew.service

printf 'tls_prepared=true certificate_name=%s https_preview_port=18443\n' "$IP"
