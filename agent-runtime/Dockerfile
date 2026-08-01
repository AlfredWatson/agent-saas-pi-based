FROM node:24-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl findutils git python3 ripgrep make g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/pi-runtime
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY src ./src
# Gateway starts a dedicated container with its host UID/GID so bind-mounted
# per-user data stays writable. Build-context files can be owner-only (0600),
# therefore make the image's runtime code readable by that non-root UID.
RUN chmod -R a+rX /opt/pi-runtime

ENV NODE_ENV=production \
    RUNTIME_HOST=0.0.0.0 \
    RUNTIME_PORT=3000 \
    RUNTIME_DATA_ROOT=/runtime-data
EXPOSE 3000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
  CMD curl --fail --silent --show-error \
    -H "Authorization: Bearer $RUNTIME_SHARED_SECRET" \
    -H "X-Tenant-ID: $TENANT_ID" \
    "http://127.0.0.1:${RUNTIME_PORT:-3000}/internal/v1/health" || exit 1

CMD ["node", "--import", "tsx", "src/server.ts"]
