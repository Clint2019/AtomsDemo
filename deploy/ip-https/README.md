# Public-IP HTTPS test entry

Target host: Chengdu instance with private address `172.19.36.105` and public address `8.137.197.104`. This package keeps the existing Docker app on loopback ports `18080` and `18001`. Nginx adds HTTPS workbench `443` and HTTPS preview `18443`. The existing HTTP entry points `18080` and `18001` continue serving until the cutover passes verification, then redirect to their HTTPS equivalents.

Let's Encrypt issues IP address certificates only with its six-day `shortlived` profile. Certbot 5.4 or newer is required for IP address validation through `webroot`; this package expects `/opt/atoms-demo-certbot/bin/certbot`. The certificate is renewed by a systemd timer four times a day, and the deploy hook validates and reloads Nginx after renewal. See [Let's Encrypt's Certbot instructions](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/).

## Preconditions

1. Confirm this is the expected instance and its NIC still owns `172.19.36.105`; verify the public address maps to it. Open inbound TCP `80` for HTTP-01 and `443` and `18443` for HTTPS in the cloud security group and host firewall. Keep `80` available for future renewals. Check whether local policy permits a public-IP HTTPS test entry.
2. Existing Nginx file `/etc/nginx/conf.d/atoms-ip-direct-private.conf` must still proxy HTTP `18080` and `18001`; Docker stack lives at `/opt/atoms-demo-docker`, and data at `/var/lib/atoms-demo-docker`.
3. Copy this directory to the server, for example `/root/atoms-ip-https`. Keep the files together. No private key or `.env` file belongs in source control.

## Prepare without changing the HTTP entries

On the server, run `sh /root/atoms-ip-https/prepare.sh` as root. It checks the local challenge route, requests a Let's Encrypt staging certificate, then a production IP certificate, installs TLS listeners, and enables renewal. If Certbot is missing from the expected venv, it creates that venv and installs Certbot 5.4–5.x.

From an **external network**, verify all three ports. These checks must succeed without `-k` or a certificate warning:

```sh
curl -fsS https://8.137.197.104/healthz
curl -sS -o /dev/null -w '%{http_code}\n' https://8.137.197.104:18443/
curl -sS -o /dev/null -w '%{http_code}\n' http://8.137.197.104/.well-known/acme-challenge/no-such-file
```

The expected statuses before cutover are `200`, `403`, and `404`. The preview returns `403` because the running app still expects the old HTTP Host. After cutover, its root returns `404`. The last check verifies public reachability of the renewal listener; its 404 is intentional. Also inspect `systemctl status atoms-ip-certbot-renew.timer`, `systemctl status atoms-ip-certbot-renew.service`, and `openssl x509 -in /etc/letsencrypt/live/8.137.197.104/fullchain.pem -noout -dates -ext subjectAltName`.

## Switch origins and old HTTP links

Run `sh /root/atoms-ip-https/cutover.sh` as root. It requires no queued or leased jobs, backs up the old environment and Nginx file plus the stopped SQLite data, switches the public/preview origins to `https://8.137.197.104` and `https://8.137.197.104:18443`, starts the app and web service, and checks the HTTPS entry. Its output includes an absolute `/root/atoms-ip-https-cutover-*` backup directory. An error after shutdown triggers automatic rollback.

From an external network, check the workbench in a browser, auth configuration, preview ticket URL, and an SSE response. A preview ticket is a bearer credential: do not paste the full URL into logs or reports. Confirm the browser trusts the IP certificate and the old HTTP workbench/preview URLs redirect. The Nginx preview listeners disable access logs and critical error logging to avoid recording ticket paths.

## Roll back

If HTTPS application behavior fails, run `sh /root/atoms-ip-https/rollback.sh /root/atoms-ip-https-cutover-XXXXXXXX` using the exact directory printed by `cutover.sh`. This restores the previous HTTP origins and Nginx proxy, disables the new TLS Nginx file, and restarts the stack. It preserves database writes made after cutover. The ACME listener, certificate and renewal timer remain installed for later reuse. Rollback refuses to interrupt queued or leased jobs.

## Ongoing renewal

The timer is `atoms-ip-certbot-renew.timer`; the service renews only the production IP certificate. Certbot's deployment hook reloads Nginx only when that certificate actually changes. To inspect a scheduled run, use `systemctl list-timers atoms-ip-certbot-renew.timer` and `journalctl -u atoms-ip-certbot-renew.service --since today`. A certificate with less than one day left makes the service fail its post-check. Keep TCP `80` open for HTTP-01 validation.
