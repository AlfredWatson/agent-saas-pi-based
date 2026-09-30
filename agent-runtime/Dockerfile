FROM node:24-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl wget findutils git openssh-client \
        python3 python-is-python3 python3-pip python3-venv python3-dev \
        ripgrep jq zip unzip poppler-utils make g++ \
    && rm -rf /var/lib/apt/lists/*

# Verify the Python toolchain during the build, including the offline pip
# bootstrap that was missing from the previous Runtime image.
RUN python --version \
    && python3 -m pip --version \
    && python3 -m venv /tmp/pi-build-venv \
    && /tmp/pi-build-venv/bin/python -m pip --version \
    && rm -rf /tmp/pi-build-venv

WORKDIR /opt/pi-runtime
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY src ./src
COPY scripts ./scripts
# Gateway starts a dedicated container with its host UID/GID so bind-mounted
# per-user data stays writable. Build-context files can be owner-only (0600),
# therefore make the image's runtime code readable by that non-root UID.
# Keep this repair in the standard build so no separate patch image is needed.
RUN chmod -R a+rX /opt/pi-runtime

ENV NODE_ENV=production \
    RUNTIME_HOST=0.0.0.0 \
    RUNTIME_PORT=3000 \
    RUNTIME_DATA_ROOT=/runtime-data \
    HOME=/runtime-data/home
EXPOSE 3000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
  CMD curl --fail --silent --show-error \
    -H "Authorization: Bearer $RUNTIME_SHARED_SECRET" \
    -H "X-Tenant-ID: $TENANT_ID" \
    "http://127.0.0.1:${RUNTIME_PORT:-3000}/internal/v1/health" || exit 1

CMD ["node", "--import", "tsx", "src/server.ts"]
