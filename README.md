# Pi SaaS Platform

First-phase multi-user control plane for `pi-coding-agent`.

```text
Client -> FastAPI Gateway (:8000) -> per-user Docker Runtime (:3000) -> Pi SDK
                                  -> PostgreSQL
```

The Gateway owns JWT authentication, encrypted provider bindings, profiles,
workspaces, session/message projections and public SSE. The Runtime accepts only
Gateway-internal bearer calls, pins all tenant paths below `RUNTIME_DATA_ROOT`,
and owns active Pi SDK sessions and JSONL history.

## Local development

1. Build/import the Runtime image as described in [Runtime image delivery](docs/runtime-image.md).
2. Copy `.env.example` to `.env`, set `POSTGRES_*`, and replace both secrets and the encryption key.
3. Ensure the Gateway host can access the Docker daemon; Gateway creates the dedicated Runtime containers.
4. Start PostgreSQL with `docker compose -f infra/compose.dev.yml up -d`.
5. Install Python dependencies with `uv sync`, then run
   `uv run uvicorn gateway.app.main:app --reload --port 8000`.

The default Runtime catalog is intentionally limited. A production deployment
must use a dedicated tenant container, a real secret store, an LLM gateway or
short-lived runtime keys, and container/network limits. MCP, arbitrary
extensions and a browser UI are intentionally out of scope for this phase.

## Per-user Docker Runtime

Runtime is Docker-only: Gateway creates one loopback-only container per user on
demand. Runtime source is mounted read-only from `agent-runtime/src`; each
user's workspace and Pi JSONL trajectory are mounted under
`.runtime-data/tenants/<user-id>` at the container's fixed `/runtime-data`
root. Each Runtime uses Pi's default `read`, `write`, `edit`, and `bash` tools.

Each user starts with an active `default` Workspace at
`/runtime-data/workspaces/default`. Additional Workspace names are safe
lowercase directory names; Workspace count and the shared `workspaces/` soft
storage limit are configured in `.env`.
