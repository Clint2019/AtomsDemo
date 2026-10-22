FROM node:22.16.0-alpine3.22
WORKDIR /opt/runner
COPY build-runtime/package.json build-runtime/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force && mkdir -p /opt/runner/site /output && chown -R 10001:10001 /opt/runner /output
COPY build-runtime/run.mjs ./run.mjs
USER 10001:10001
CMD ["node", "/opt/runner/run.mjs"]
