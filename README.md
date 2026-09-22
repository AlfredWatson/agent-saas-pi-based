# Pi SaaS Platform

First-phase multi-user control plane for `pi-coding-agent`.

```text
Client -> FastAPI Gateway (:8000) -> per-user Docker Runtime (:3000) -> Pi SDK
                                  -> PostgreSQL (platform + rag schemas)
                                  -> Redis (RAG intermediate cache)
                     RAG Worker -> PostgreSQL + Redis -> model providers
```

The Gateway owns JWT authentication, encrypted provider bindings, profiles,
workspaces, session/message projections and public SSE. The Runtime accepts only
Gateway-internal bearer calls, pins all tenant paths below `RUNTIME_DATA_ROOT`,
and owns active Pi SDK sessions and JSONL history.

## Local development

1. Build/import the Runtime image as described in [Runtime image delivery](docs/runtime-image.md).
2. Copy `.env.example` to `.env`, set `POSTGRES_*`, and replace both secrets and the encryption key.
3. Ensure the Gateway host can access the Docker daemon; Gateway creates the dedicated Runtime containers.
4. When `.env` points to an existing PostgreSQL with pgvector available, start only Redis with
   `docker compose --env-file .env -f infra/compose.dev.yml up -d redis`. For an isolated
   development database, the same Compose file also provides the optional `postgres` service.
5. Install Python dependencies with `uv sync`, then run
   `uv run python scripts/start_gateway.py --reload`. Without `--host` or
   `--port`, the Gateway listens on `GATEWAY_HOST` and `GATEWAY_PORT` from
   `.env`.
6. In a separate process run `uv run python scripts/start_rag_worker.py` for
   four RAG jobs: parsing, chunking, embedding and graph extraction.

To black-box test account registration, run `uv run python
test/user_registration.py`. It creates a fresh account, verifies its default
Workspace, login, and duplicate-registration rejection, then saves the account
to `.env` as both the Agent and RAG test credentials. The Gateway has no
user-deletion API, so the account is retained. After registration, run
`test/agent_user_flow.py` or `test/rag_user_flow.py` without repeating the
email and password configuration.

The RAG API is scoped below each Workspace. A Session may explicitly bind
Workspace knowledge bases and expose them to Pi as a constrained `rag_search`
tool; Gateway remains the only component allowed to access RAG storage and
models. See [Multi-tenant RAG](docs/rag.md) and [Runtime image delivery](docs/runtime-image.md)
for lifecycle, internal-network, and image-import requirements.

The default Runtime catalog is intentionally limited. A production deployment
must use a dedicated tenant container, a real secret store, an LLM gateway or
short-lived runtime keys, and container/network limits. MCP, arbitrary
extensions remain intentionally out of scope for this phase.

## Web workbench

The repository now includes a separate React/Vite workbench under
[`frontend/`](frontend/README.md). It provides Chinese-first Agent chat,
Workspace files, Provider/Profile settings, and guided four-stage RAG
management. Development uses Vite's `/api` proxy; production uses the
frontend Nginx image to serve the SPA and reverse-proxy `/api/` to Gateway on
the same origin. See the frontend README for build and deployment commands.

## Per-user Docker Runtime

Runtime is Docker-only: Gateway creates one loopback-only container per user on
demand. Runtime source is mounted read-only from `agent-runtime/src`; each
user's workspace and Pi JSONL trajectory are mounted under
`.runtime-data/tenants/<user-id>` at the container's fixed `/runtime-data`
root. Each Runtime uses Pi's default `read`, `write`, `edit`, and `bash` tools.

Each user starts with an active `default` Workspace at
`/runtime-data/workspaces/default`. Additional Workspace names are safe
lowercase directory names; Workspace count and the shared `workspaces/` soft
storage limit are configured in `.env`. The public Workspace API can recursively
list files, upload one multipart file to a relative path, and delete one file;
uploads have an independently configured hard per-file limit.
