# RAG Gateway architecture

RAG remains a Gateway-owned backend capability and has no dependency on
`agent-runtime`.  Code is grouped by the Gateway concern that owns it:

- `app/api/v1/rag`: public route composition, request binding, and response mapping.
- `app/services/rag`: application use cases such as lifecycle and retrieval.
- `app/domain/rag`: pure chunking, graph rules, and transport-neutral contracts.
- `app/db/rag`: PostgreSQL ORM models and startup validation.
- `app/integrations/rag`: Redis, PostgreSQL storage adapters, parsers, and model clients.
- `app/workers/rag`: durable-job runner and registered stage handlers.

Dependencies flow from API and workers toward services, domain, persistence, and
adapters.  Domain code must not import HTTP, SQLAlchemy, Redis, or integration
modules; workers must not import public routes.  The compatibility-only
`app/rag/models.py` exists for already-recorded Alembic revisions and must not
be used by application code.

To add a storage backend, implement its existing Protocol and register its
factory in the appropriate integration module.  To add a document processor,
implement `DocumentProcessor` and register it in `get_document_processor`.  To
add a durable stage, implement the stage operation and register it in
`workers/rag/handlers.py`; retain its job snapshot and cache-key versioning.

Transaction ownership remains explicit: request handlers delegate business
work, repositories never commit, and application/worker operations commit only
after their complete visible result is ready.  This refactor deliberately does
not change the existing public API, database schema, or Redis key format.
