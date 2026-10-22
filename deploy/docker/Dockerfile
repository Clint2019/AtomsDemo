FROM node:22.16.0-alpine3.22

# The Node base is already cached on the Chengdu build host. The final image
# is exported with docker save, so the overseas host does not need Hub access.
RUN apk add --no-cache python3 py3-pip docker-cli caddy ca-certificates \
    && python3 -m venv /opt/venv

COPY requirements.lock /tmp/requirements.lock
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.lock

COPY release.tar.gz /tmp/release.tar.gz
RUN mkdir -p /opt/atoms-demo /etc/atoms-demo \
    && tar -xzf /tmp/release.tar.gz -C /opt/atoms-demo \
    && /opt/venv/bin/pip install --no-cache-dir --no-deps /opt/atoms-demo \
    && rm /tmp/release.tar.gz /tmp/requirements.lock

COPY entrypoint.py /opt/atoms-demo-entrypoint.py
COPY Caddyfile.private /etc/atoms-demo/Caddyfile.private
COPY Caddyfile.public /etc/atoms-demo/Caddyfile.public

ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /opt/atoms-demo
ENTRYPOINT ["/opt/venv/bin/python", "/opt/atoms-demo-entrypoint.py"]
