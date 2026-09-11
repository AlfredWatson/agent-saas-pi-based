# 多租户 RAG 后端

RAG 是 Gateway 内的独立领域模块，复用用户 JWT 和 Workspace 权限，但不进入
`agent-runtime`。原始文档、解析块、chunks、向量、图谱及任务状态存放在同一个
PostgreSQL 的 `rag` schema；Redis 只保存可过期、可重建的向量和图谱中间结果。

## 启动

```bash
# 复用 .env 已配置的 PostgreSQL，只启动 Redis
docker compose --env-file .env -f infra/compose.dev.yml up -d redis
uv sync
uv run python scripts/start_gateway.py
# 另一个终端
uv run python scripts/start_rag_worker.py
```

如需隔离的开发数据库，再显式启动 Compose 的 `postgres` 服务；已经部署 PostgreSQL
时不要启动该服务。数据库必须允许创建或已经安装 pgvector extension。

Gateway 启动时执行 Alembic，创建 `rag` schema、pgvector extension 和全部表。
Worker 使用 PostgreSQL lease 消费 `chunking`、`vectorization`、
`graph_extraction` 三类队列。Redis 使用 ACL 用户 `admin`，关闭 default 用户，
并通过 `docker/volumes/redis/` 持久化 AOF。

本版本只接受：

- `DOCUMENT_PROCESSING_SERVICE=default`
- `VECTOR_BASE=p`（PostgreSQL/pgvector）
- `GRAPH_BASE=p`（PostgreSQL）

Milvus、Chroma、Qdrant、Neo4j、OCR 和图片解析没有伪装成可用能力。

本地完整验收可使用仓库内的 OpenAI-compatible stub（仅测试用途）：

```bash
# 在 Gateway、worker 已启动后，另开一个终端
uv run python scripts/rag_mock_model.py
# 默认请求 http://127.0.0.1:21996/api/v1，可用 RAG_SMOKE_GATEWAY 覆盖
uv run python scripts/rag_smoke_flow.py
```

验收脚本会创建隔离测试数据，覆盖五类文档、故障续跑、三种检索、图谱合并、
完整复制、租户隔离与级联删除，并在结束时删除测试知识库和测试用户。

## 最小 API 流程

以下路径均以 `/api/v1` 为前缀，并要求 `Authorization: Bearer <JWT>`。

```text
POST /workspaces/{workspace}/knowledge-bases
POST /workspaces/{workspace}/knowledge-bases/{kb}/documents
PUT  /workspaces/{workspace}/knowledge-bases/{kb}/embedding-model
PUT  /workspaces/{workspace}/knowledge-bases/{kb}/llm-model
POST /workspaces/{workspace}/knowledge-bases/{kb}/jobs/chunking
POST /workspaces/{workspace}/knowledge-bases/{kb}/jobs/vectorization
POST /workspaces/{workspace}/knowledge-bases/{kb}/jobs/graph-extraction
GET  /workspaces/{workspace}/knowledge-bases/{kb}/jobs
POST /workspaces/{workspace}/knowledge-bases/{kb}/retrieve
```

创建知识库：

```json
{
  "name": "manuals",
  "document_backend": "default",
  "vector_backend": "postgresql",
  "graph_backend": "postgresql"
}
```

文档上传使用 multipart 的重复 `files` 字段。支持 `.pdf`、`.doc/.docx`、
`.xls/.xlsx`、`.ppt/.pptx`、`.md` 和 `.markdown`；同名文件会产生不同 document
ID。旧版 OLE Office 格式使用尽力而为的纯文本提取，生产资料优先使用 OOXML
格式。一个批次中任何文件不合规都会回滚整个批次。

模型配置：

```json
{
  "protocol": "openai",
  "base_url": "https://model.example/v1",
  "api_key": "secret",
  "model_name": "embedding-model",
  "thinking_effort": null
}
```

Embedding 只接受 OpenAI-compatible 协议；LLM 接受 OpenAI-compatible 或
Anthropic-compatible。Gateway 会实际请求模型并验证响应，只有成功后才加密保存
API key。知识库中存在持久向量时不能更换 embedding。

批量提交任务：

```json
{"document_ids":["<document-uuid>"]}
```

已完成的阶段不能重复提交。删除文档的 `/vectors`、`/graph` 或 `/chunks` 派生资源
后可以重新处理；删除 chunks 会同时删除向量和依赖图谱。

检索请求示例：

```json
{
  "query": "部署要求",
  "mode": "hybrid",
  "top_k": 5,
  "vector_k": 20,
  "bm25_k": 20,
  "vector_weight": 1,
  "bm25_weight": 1,
  "rrf_k": 60
}
```

`vector` 返回余弦相似度结果；`hybrid` 使用中文分词 BM25 与向量候选的带权 RRF；
`graph` 返回匹配实体、指定跳数内的边和来源 chunk。接口只返回证据，不生成答案。

## 状态、重试与缓存

文档分别暴露 chunking、vectorization 和 graph 的状态、百分比、提示、错误和更新时间。
状态取值为 `not_started`、`queued`、`running`、`succeeded`、`failed`。

向量化和图谱提取按 chunk 写 Redis。失败后重新提交时，命中的中间结果会刷新
`RAG_CACHE_TTL_SECONDS`，只重新调用缺失 chunk。全部 chunk 完成后才在单一数据库
事务中发布向量或图谱，所以检索不会看到半成品。

Worker 异常时，超过 `RAG_JOB_LEASE_SECONDS` 的 running job 自动恢复为 queued。
每个知识库的三类并发可以独立调整，但不能超过系统环境变量规定的上限。
复制和删除维护 operation 失败时最多按 `RAG_OPERATION_MAX_ATTEMPTS` 自动重试。

## 复制、合并与删除

- 知识库有 running job 时拒绝复制。复制是异步 operation，包含模型配置、文档、
  blocks、chunks、向量、图谱、任务记录和仍有效的 Redis 缓存。
- 图谱合并按 Unicode NFKC 后的 `(entity_type, normalized_name)` 对齐节点，边按
  `(source, relation, target)` 去重，属性冲突保留为多值并保留全部证据。
- 删除文档会删除所有派生数据和 Redis 缓存；引用该文档图谱的合并图谱也会删除。
- 删除知识库是异步 operation。可通过 `GET /rag/operations/{id}` 查询结果。

## 安全边界

所有 RAG 查询必须同时匹配当前用户、Workspace 和知识库。API key 不会由读取接口
返回。生产默认只接受 HTTPS 模型地址，并拒绝解析到 loopback、私网、link-local 或
保留地址的 URL；仅可信开发网络可设置 `RAG_MODEL_BASE_URL_ALLOW_PRIVATE=true`。
