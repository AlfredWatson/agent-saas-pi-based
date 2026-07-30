# Pi SaaS Platform

First-phase multi-user control plane for `pi-coding-agent`.

```text
Client -> FastAPI Gateway (:8000) -> Node Agent Runtime (:3000) -> Pi SDK
                                  -> PostgreSQL
```

The Gateway owns JWT authentication, encrypted provider bindings, profiles,
workspaces, session/message projections and public SSE. The Runtime accepts only
Gateway-internal bearer calls, pins all tenant paths below `RUNTIME_DATA_ROOT`,
and owns active Pi SDK sessions and JSONL history.

## Local development

1. Copy `.env.example` to `.env`, set `POSTGRES_*`, and replace both secrets and the encryption key.
2. Start PostgreSQL with `docker compose -f infra/compose.dev.yml up -d`.
3. Install Python dependencies with `uv sync`, then run
   `uv run uvicorn gateway.app.main:app --reload --port 8000`.
4. Install runtime dependencies with `npm --prefix agent-runtime install --ignore-scripts`, then run
   `npm --prefix agent-runtime run dev`.

The default Runtime catalog is intentionally limited. A production deployment
must use a dedicated tenant container, a real secret store, an LLM gateway or
short-lived runtime keys, and container/network limits. MCP, arbitrary
extensions, dynamic Docker orchestration and a browser UI are intentionally out
of scope for this phase.
