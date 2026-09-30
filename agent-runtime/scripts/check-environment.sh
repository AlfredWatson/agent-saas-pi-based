#!/usr/bin/env bash
# Run inside a throwaway container; creates probe data only below /tmp.
set -euo pipefail

for tool in bash curl wget find git ssh python python3 pip pip3 node npm npx rg jq zip unzip pdftotext pdfinfo make gcc g++; do
  command -v "$tool"
done
python --version
python3 --version
python3 -m pip --version
node --version
npm --version
git --version

probe_dir=$(mktemp -d /tmp/pi-runtime-tools.XXXXXX)
trap 'rm -rf "$probe_dir"' EXIT
mkdir -p "$HOME"
cd "$probe_dir"

# venv seeds pip from bundled wheels. This must pass with --network none.
python3 -m venv .venv
.venv/bin/python -m pip --version
.venv/bin/python -c 'import json, ssl, sqlite3, venv; print("Python stdlib OK")'

# Check real local operations under the same arbitrary UID/read-only rootfs
# constraints as Gateway-managed Runtime containers.
node -e 'require("node:fs").writeFileSync("node-ok.txt", "OK")'
npm init --yes >/dev/null
npm config get cache
git init --quiet
git config user.email runtime-smoke@example.invalid
git config user.name runtime-smoke
git add node-ok.txt package.json
git commit --quiet -m 'Verify preinstalled runtime tools'
printf 'Runtime toolchain OK (no downloads required)\n'
