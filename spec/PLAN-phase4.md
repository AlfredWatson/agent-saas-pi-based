# Milvus、Chroma、Qdrant 向量后端实施计划

## 总体方案

- 保留知识库级、创建后不可修改的 `vector_backend`，扩展取值为 `postgresql | milvus | chroma | qdrant`；该字段继续显式必填。
- Milvus、Chroma、Qdrant 均采用“每个知识库一个集合”，集合名固定为 `<prefix>_<knowledge_base_uuid_hex>`，使用 COSINE 相似度。
- PostgreSQL 继续保存知识库、文档、chunk、任务状态和租户关系；外部向量库只保存向量及 `chunk_id`、`document_id`、generation、模型指纹，检索结果回 PostgreSQL 装载权威文本和 metadata。
- 不修改现有 URL、检索请求/响应、任务类型、知识库复制/删除 API 或数据库结构；无需 Alembic migration。
- 已确认当前三个容器健康：Milvus `19530`、Chroma `8000`、Qdrant `6333/6334`。

## 配置和依赖

- 使用 `uv` 安装并锁定与服务器匹配的官方 SDK：
  - `pymilvus==3.0.1`
  - `chromadb==1.5.9`
  - `qdrant-client==1.19.1`
- 同步根目录 `pyproject.toml`、`uv.lock` 和 `gateway/pyproject.toml`。PyMilvus 按官方建议与 Milvus 服务端版本一致；Chroma/Qdrant 客户端与服务端同版本。[PyMilvus 安装说明](https://milvus.io/docs/install-pymilvus.md)、[Chroma Python 客户端](https://docs.trychroma.com/reference/python/client)、[Qdrant 异步客户端](https://qdrant.tech/documentation/database-tutorials/async-api/)
- 扩展现有 `VECTOR_BASE`：`p/m/c/q` 分别表示 PostgreSQL、Milvus、Chroma、Qdrant；要求包含 `p`、字符不重复。默认仍为 `p`，当前本机 `.env` 设置为 `pmcq`。
- 在 `.env` 和 `.env.example` 同步增加并注释：

```dotenv
VECTOR_BASE=pmcq
RAG_VECTOR_COLLECTION_PREFIX=pi_saas_rag
RAG_VECTOR_STORE_TIMEOUT_SECONDS=10
RAG_VECTOR_STORE_BATCH_SIZE=256

MILVUS_URI=http://127.0.0.1:19530
MILVUS_TOKEN=
MILVUS_DATABASE=default

CHROMA_HOST=127.0.0.1
CHROMA_PORT=8000
CHROMA_SSL=false
CHROMA_TENANT=default_tenant
CHROMA_DATABASE=default_database

QDRANT_URL=http://127.0.0.1:6333
QDRANT_API_KEY=
QDRANT_GRPC_PORT=6334
QDRANT_PREFER_GRPC=false
```

- `.env.example` 的默认 `VECTOR_BASE` 保持 `p`，但完整解释 `pmcq`；真实 token/API key 只放 `.env`，启动日志不得输出。
- Gateway 和 Worker 共用配置校验及客户端生命周期。Gateway 启动时严格探活所有已启用后端：
  - Milvus：列出集合；
  - Chroma：heartbeat；
  - Qdrant：读取集合列表；
  - 任一失败则以 `rag_vector_backend_unavailable:<backend>` 中止启动。
- 启动日志输出启用后端、脱敏 endpoint、数据库/tenant、探活结果和集合前缀，不输出 token、API key 或自定义认证头。

## 存储适配与业务流程

- 在 `gateway/app/integrations/rag/vector_store.py` 建立统一的具名 Protocol 和 registry，能力包括：
  - 建立并验证集合；
  - 文档向量幂等替换；
  - COSINE 检索及文档过滤；
  - 判断知识库是否已有向量；
  - 删除文档向量；
  - 复制知识库向量；
  - 删除知识库集合；
  - 健康检查和客户端关闭。
- 保留 `PostgresVectorStore` 的 LangChain `VectorStore` 兼容性；新增三个直接使用官方 SDK 的适配器，不引入 `langchain-community` 或额外 LangChain 向量库包。
- 客户端选择：
  - Milvus：`AsyncMilvusClient` 执行数据操作，缺少异步接口的 schema 操作通过 `asyncio.to_thread` 调用 `MilvusClient`；
  - Chroma：`AsyncHttpClient`；
  - Qdrant：`AsyncQdrantClient`。
- 集合首次向量化时按已验证 embedding dimension 懒创建；已有非空集合若维度、COSINE metric 或 schema 版本不匹配，则报 `vector_collection_schema_mismatch`。空集合允许重建。
- point ID 固定使用 chunk UUID。检索强制限定当前知识库及允许的 `document_ids`，随后按命中顺序从 PostgreSQL加载 chunk；不存在、generation 过期或文档未成功发布的命中直接丢弃。
- 统一 relevance score：
  - PostgreSQL、Milvus、Qdrant 直接使用 COSINE similarity；
  - Chroma 使用 `1 - cosine_distance`；
  - 浮点误差裁剪到 `[-1, 1]`，之后再应用现有 `min_score`。
- Worker 根据 `kb.vector_backend` 从 registry 创建适配器，不再直接构造 PostgreSQL 实现。Redis embedding 缓存和模型调用流程保持不变。
- 外部写入采用确定性 chunk ID、generation 和模型指纹：
  1. 清除该文档旧 points；
  2. 分批 upsert 全部新 points；
  3. 成功后才提交 PostgreSQL 文档阶段状态；
  4. 失败时尽力清除本次 points，任务进入现有失败/重试流程；
  5. 检索只接受 PostgreSQL 标记为成功且 generation 一致的文档，因此中途写入不可见。
- 将“embedding 配置是否被向量锁定”改为调用当前 backend 的 `has_vectors()`，避免外部后端绕过 `embedding_model_locked_by_vectors`。
- 所有清理操作先调用向量后端，再提交 PostgreSQL 状态；外部成功但数据库提交失败时可安全重试。
- 知识库复制保持现有深复制语义：导出源集合向量，按照新旧 document/chunk 映射写入目标集合，全部成功后才把目标标记为 active。最终失败时恢复源知识库并尽力删除目标集合。
- 知识库删除 operation 先幂等删除外部集合，再删除 PostgreSQL 记录；外部库不可用时沿用现有 operation 重试，不提前报告成功。

## API、文档和兼容性

- `KnowledgeBaseCreate.vector_backend` 的 OpenAPI enum 扩展为四种值，但继续必填；复制仍继承源后端，更新接口仍禁止修改。
- `/api/v1/rag/capabilities.vector_backends` 按 `VECTOR_BASE` 返回已启用后端，固定顺序为 PostgreSQL、Milvus、Chroma、Qdrant。
- 请求一个受支持但未启用的后端返回 `422 vector_backend_not_enabled`。
- PostgreSQL 已有知识库和向量无需迁移，行为保持不变。
- 更新 `docs/rag.md`、API reference 和架构文档，覆盖配置、集合命名、启动失败诊断、后端选择、数据权威边界以及备份时必须同时备份外部向量卷。

## 测试与验收

- 单元测试覆盖 registry、配置解析、集合命名、schema/dimension 校验、score 转换、文档过滤、结果 hydration、批量 upsert、幂等删除和敏感日志脱敏。
- 服务测试使用 fake adapters 覆盖：
  - upsert 中途失败不可检索；
  - retry 不产生重复 points；
  - embedding 配置锁定；
  - 删除 vectors/chunks/blocks/document 的级联清理；
  - 知识库复制成功、重试及最终失败补偿；
  - 跨知识库和跨文档命中被拒绝。
- API 契约测试确认新增 enum/capabilities、显式必填、未启用后端 422，以及现有路径、响应结构和错误码不变。
- 将 `test/rag_user_flow.py` 增加 `--vector-backend`，默认保持 `postgresql`；依次对四个后端运行完整公开 HTTP 流程，包括向量化、vector/hybrid retrieval、复制、重新向量化、级联删除和默认资源清理。
- 负向启动测试：把一个已启用 endpoint 指向不可达地址，Gateway 必须启动失败且日志只显示脱敏地址；从 `VECTOR_BASE` 移除后应正常启动并从 capabilities 隐藏。
- 最终门禁：
  - `uv lock --check` 和三个 SDK import；
  - RAG unit/API/architecture tests；
  - Ruff、format check、compileall、`git diff --check`；
  - 当前 Compose 五个容器健康；
  - Gateway/Worker 启动探活日志通过；
  - 四个后端各一份 `status=passed` 的黑盒报告，且 cleanup errors 为空。
