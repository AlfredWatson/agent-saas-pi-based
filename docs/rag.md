# 多租户 RAG 后端

RAG 是 Gateway 内独立的 Workspace 资源，不进入 `agent-runtime`。每个知识库固定绑定文件、blocks、chunks、向量和图谱后端；本版本五项均为 PostgreSQL。文件原文、blocks、chunks、向量、图谱和任务均位于同一 PostgreSQL 实例的 `rag` schema。Redis 只缓存可恢复的向量和图谱中间结果。

## 启动

现有 PostgreSQL 由 `.env` 的 `POSTGRES_*` 连接信息提供。不要启动或拉取 PostgreSQL Docker 镜像；开发 Compose 只启动 Redis：

```bash
docker compose --env-file .env -f infra/compose.dev.yml up -d redis
uv run python scripts/start_gateway.py
# 另一个终端
uv run python scripts/start_rag_worker.py
```

首次升级到四阶段版本前，停止 Gateway 和 worker。迁移会删除并重建 **仅** `rag` schema；其中的历史测试数据不可恢复，`platform` schema 不受影响。现有 PostgreSQL 必须允许 `CREATE EXTENSION vector`。

当前能力：

- `FILE_BASE=p`、`VECTOR_BASE=p`、`GRAPH_BASE=p`
- `DOCUMENT_PROCESSING_SERVICE=default`
- PDF、Word、Excel、PPT、Markdown 的纯文本解析
- fixed、regex、semantic 三种 chunking 策略

`FILE_BASE=pm` 会在启动时明确失败；MinIO 尚未实现，也不会出现在 capabilities 中。

## 四阶段流程

```text
创建知识库 → 上传文件 → parsing → 设置文档级 chunking 配置 → chunking
            → vectorization 和/或 graph extraction → retrieve
```

创建知识库必须显式指定五项后端：

```json
{
  "name":"manuals",
  "file_backend":"postgresql",
  "block_backend":"postgresql",
  "chunk_backend":"postgresql",
  "vector_backend":"postgresql",
  "graph_backend":"postgresql"
}
```

后端不可修改，复制知识库时完整继承。每个知识库可独立调整 parsing、chunking、embedding、graph 的并发额度，不能超过 env 系统上限。

上传接口只验证和持久化文件，不创建 Redis key 或处理任务。每个文件返回单独结果，所以同一批次可部分成功。持久化文件名为 `<user_uuid>_<YYMMDD>_<sanitized_stem><extension>`；同名文件使用不同 `storage_key` 保存，因此允许重复上传。

提交 parsing 时可为每个文档选择处理后端：

```json
{"items":[{"document_id":"<uuid>","processor_backend":"default"}]}
```

完成 parsing 后，使用 `PUT .../documents/{document_id}/chunking-config` 设置当前文档的策略和参数，再提交 chunking。语义切分需要已验证 embedding；vectorization 和 graph extraction 分别需要 chunking 成功加 embedding/LLM 配置。

parsing 和 chunking 都不会缓存，也不会暴露部分 blocks/chunks：只有整个阶段成功后才在数据库中发布。向量化和图谱提取继续使用 Redis 完成失败续跑，并在全部 chunk 完成后原子写入最终存储。

## 清理与复制

- 删除 `/blocks` 会删除所有下游 chunks、vectors、图谱、相关合并图谱与缓存，并允许重新解析。
- 删除 `/chunks` 保留 blocks，但删除所有下游资源，并允许以新的文档级配置重新切分。
- 删除文档会同时清理文件适配器内容、派生数据、合并图谱依赖和 Redis key。
- 复制知识库会复制文件、四阶段状态、文档配置、blocks、chunks、vectors、图谱、任务和有效的向量/图谱缓存；运行中任务仍会阻止复制。

完整 HTTP 验收：

```bash
uv run python scripts/rag_mock_model.py
uv run python scripts/rag_smoke_flow.py
```
