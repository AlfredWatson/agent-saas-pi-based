# 多租户 RAG 后端开发计划

## 1. 总体方案

- 本期只建设后端 API、独立 RAG worker、数据库、Redis、测试与文档，不建设前端。
- RAG 代码放在 Gateway 内的独立 `rag` 领域包，复用现有 JWT、用户和 workspace 鉴权；不修改或依赖 `agent-runtime` 的智能体执行逻辑。
- 每个知识库严格属于一个 workspace；当前系统中“租户”即用户，所有数据库查询同时校验 `user_id + workspace_id + knowledge_base_id`。
- 使用同一个 PostgreSQL 实例，新增独立 `rag` schema；文档原始二进制、解析结果、chunks、向量、图谱、队列和操作记录全部持久化其中。
- 三类文档任务使用 PostgreSQL 持久任务表，由独立 worker 通过 `FOR UPDATE SKIP LOCKED` 消费；Redis只缓存向量化和图谱提取的中间结果。
- LangChain 1.x 负责统一 `Document`、文本切分、embedding/LLM 调用、retriever 与流水线编排。OpenAI-compatible 模型通过自定义 `base_url` 接入，Anthropic 使用独立集成。[LangChain OpenAI-compatible 模型文档](https://docs.langchain.com/oss/python/concepts/providers-and-models)、[OpenAI Embeddings 文档](https://docs.langchain.com/oss/python/integrations/embeddings/openai)、[Anthropic 集成文档](https://docs.langchain.com/oss/python/integrations/chat/anthropic)

## 2. 数据与领域模型

在 `rag` schema 中建立以下核心实体，并通过 Alembic 在 Gateway 启动时自动迁移：

- `knowledge_bases`
  - workspace、名称、状态、版本号。
  - 不可变后端配置：文档处理后端、向量存储后端、图谱存储后端。
  - 当前 chunking 策略及参数。
  - chunking、embedding、graph 三项独立并发上限。
- `rag_model_configs`
  - 知识库级 embedding 和 LLM 配置。
  - 保存协议类型、base URL、模型名、thinking effort、验证时间、embedding 维度和配置指纹。
  - API key 使用现有 AES-GCM 机制加密，查询接口永不返回明文。
- `documents`
  - 文件名、MIME、扩展名、SHA-256、大小、PostgreSQL `bytea` 原始内容。
  - 独立保存 chunking、vectorization、graph 三组状态、进度、提示、错误和更新时间。
  - 状态统一为 `not_started/queued/running/succeeded/failed`。
- `document_blocks`、`chunks`
  - blocks 是解析后带页码、sheet、slide、标题层级等来源信息的“大 chunks”。
  - chunks 保存稳定顺序、文本、token 数、内容哈希、切分策略快照和处理 generation。
- `chunk_vectors`
  - chunk、知识库、embedding 配置指纹、维度和 pgvector 向量。
  - 向量列允许知识库间维度不同；同一知识库因模型锁定只能存在一种维度。
- `graph_artifacts`、`graph_nodes`、`graph_edges`、`graph_evidence`
  - 文档图谱和合并图谱都作为独立 artifact。
  - node/edge 始终保留原始 chunk、文档和模型配置指纹作为证据。
- `processing_jobs`
  - `chunking/vectorization/graph_extraction` 三种逻辑队列。
  - 保存任务快照、attempt、进度、错误、lease、heartbeat 和幂等键。
  - 每个文档每个阶段最多一个 active job。
- `rag_operations`
  - 承载知识库复制、删除等维护操作，不计入三类文档处理队列。

外键默认级联删除。删除 workspace 时先由 RAG 生命周期服务取消任务并清理知识库，防止现有 workspace 删除流程遗留 RAG 数据。

## 3. API 与业务约束

统一增加 `/api/v1/workspaces/{workspace_id}/knowledge-bases` 资源：

- 知识库
  - 创建、列表、详情、修改名称/chunking 配置/并发、复制、删除。
  - 后端选择来自 `GET /api/v1/rag/capabilities`；本期只返回 `default/postgresql/postgresql`。
  - 后端类型创建后不可修改，复制时完全继承。
- 模型配置
  - `PUT .../embedding-model`：OpenAI-compatible。
  - `PUT .../llm-model`：OpenAI-compatible 或 Anthropic-compatible。
  - 设置请求必须实际调用模型验证；embedding 验证向量非空、数值有效并记录维度，LLM 验证基本调用及图谱结构化输出能力。验证失败不保存配置。
  - embedding 只有在知识库不存在任何持久向量时才能修改；存在 queued/running 向量任务时也拒绝，避免竞态。
  - LLM 可在已有图谱时修改，但存在 queued/running 图谱任务时拒绝；旧图谱保留原模型指纹。
- 文档
  - 批量 multipart 上传，支持 PDF、DOC/DOCX、XLS/XLSX、PPT/PPTX、Markdown。
  - 每个文件生成独立 document ID；允许同名文档，批次在校验通过后整体提交。
  - 提供列表、详情、删除，以及分别删除 chunks、vectors、文档图谱的接口。
- 任务
  - 三个批量提交接口接受选中的 `document_ids`，返回 `202 + job_ids`。
  - 文档已完成对应阶段时拒绝再次提交。
  - 向量化必须满足 embedding 已验证且 chunking 成功。
  - 图谱提取必须满足 LLM 已验证且 chunking 成功。
  - 语义切分额外要求 embedding 已验证。
  - 删除 chunks 同时删除向量、文档图谱及依赖它们的合并图谱；只删除 vectors 或 graph 时不影响 chunks。
- 图谱
  - 可选择多个文档图谱生成新的合并图谱；源图谱保持不变。
  - 合并图谱作为不可变快照，可单独查询和删除。
- 检索
  - `POST .../retrieve` 只返回排序后的 chunks、node/edge、分数及来源，不生成答案。
  - 支持：
    - `vector`：`top_k/candidate_k/min_score/document_ids`。
    - `hybrid`：向量候选与 BM25 候选通过带权 RRF 融合，参数包括两路候选数、权重、`rrf_k` 和最终 `top_k`。
    - `graph`：实体候选数、最大跳数、最终证据数和文档过滤。
  - 所有模式固定限制在一个知识库内，服务端不接受跨库 ID 列表。

## 4. 处理流水线

### 文档解析与 chunking

- 文档处理后端定义统一 `DocumentProcessor` 接口，输出 LangChain `Document` 大块及标准元数据。
- 默认实现：
  - PDF 按页提取文本。
  - Word 按标题/段落提取文本。
  - Excel 按 sheet 和行组输出单元格文本。
  - PPT 按 slide 输出文本框内容。
  - Markdown 按标题层级输出文本。
  - 忽略图片、扫描 OCR 和纯图片表格。
- 解析作为 chunking job 的第一阶段；已成功保存的 blocks 和 partial chunks 在重试时复用。
- 三种知识库级策略：
  - 固定字符：`MAX_TOKEN=512`、`OVERLAP=64`、分隔符默认 `\n\n`。
  - 正则：`MAX_TOKEN=512`、默认按段落及中英文句末切分。
  - 语义：复用知识库 embedding，默认 percentile breakpoint `95`，超长语义块再按 token 上限切分。
- 修改策略只影响之后提交的任务；每个 job 保存完整策略快照，不改变既有 chunks。

### 向量化

- 按 chunk 内容哈希、embedding 配置指纹和处理 generation 生成 Redis key。
- 每完成一个 chunk 就写入向量缓存，并维护文档级 key 索引；失败时文档状态变为 `failed`，已完成缓存保留。
- 重试时复用已有缓存并刷新 TTL，只调用缺失 chunk。
- 全部 chunk 成功后，在单一 PostgreSQL 事务中批量写入 `chunk_vectors`；提交成功后文档状态才变为 `succeeded`。
- PostgreSQL 存储适配器实现 LangChain `VectorStore` 接口；Milvus、Chroma、Qdrant 只预留接口，不添加依赖和不可用选项。

### 知识图谱

- 使用知识库 LLM 对每个 chunk 生成结构化节点、边及证据：
  - node：规范名称、类型、描述、属性。
  - edge：source、relation、target、描述、属性。
- Redis 缓存键包含 chunk 哈希、LLM 配置指纹和 prompt/schema 版本；重试与最终提交规则和向量化一致。
- 合并算法固定为：
  1. 名称执行 Unicode NFKC、case-fold、空白与标点规范化。
  2. 以 `(entity_type, normalized_name)` 对齐实体。
  3. 属性冲突保留多值及全部来源，不静默覆盖。
  4. 边按 `(source, relation, target)` 去重并合并证据。
- 删除源文档时删除其文档图谱，以及所有引用该文档的合并图谱，避免保留失效证据。

### 队列、并发与生命周期

- 每个知识库分别配置三类并发，默认 `2/2/1`，并受系统级 env 上限约束。
- worker 使用 lease 和 heartbeat；进程异常后 lease 到期的任务重新进入 queued。
- 每处理一个 block/chunk 都更新任务进度、文档阶段状态和用户可见提示。
- 单次失败标记 `failed`，由用户重新提交；attempt 增加但复用已完成缓存。
- 知识库复制：
  - 源存在 running job 时返回 `409`。
  - 无 running job 时冻结源调度，异步复制文档、blocks、chunks、向量、全部图谱、任务记录、配置和仍有效的 Redis 中间缓存。
  - queued 状态保持 queued；目标完成前状态为 `copying`，完成后源和副本同时恢复 `active`。
- 删除知识库或文档时先锁定对象、取消 queued/running job，再清理数据库和 Redis key 集；失败由维护 operation 重试，对象在完成前保持 `deleting`。

## 5. 部署、配置与安全

- PostgreSQL 镜像调整为带 pgvector 的 PostgreSQL 17 镜像，沿用现有数据库卷并在迁移中执行 `CREATE EXTENSION IF NOT EXISTS vector`。LangChain 官方 PostgreSQL 集成同样要求 pgvector 扩展。[PGVector 集成文档](https://docs.langchain.com/oss/python/integrations/vectorstores/pgvector)
- `infra/compose.dev.yml` 增加 `redis:7.4-alpine`：
  - ACL 用户名从 `REDIS_USERNAME` 读取，默认 `admin`。
  - 必须设置 `REDIS_PASSWORD`，关闭 default 用户。
  - 数据绑定到 `docker/volumes/redis/`，启用 AOF。
- `.env` 与 `.env.example` 同步增加：
  - `DOCUMENT_PROCESSING_SERVICE=default`
  - `VECTOR_BASE=p`
  - `GRAPH_BASE=p`
  - Redis 地址、admin 用户、密码、DB、连接池配置。
  - `RAG_CACHE_TTL_SECONDS=604800`。
  - 三类默认并发与系统最大并发。
  - chunking 默认参数、文件大小和单批文件数上限。
  - 可选 LangSmith tracing 配置。
- 本期仅接受 `VECTOR_BASE=p`、`GRAPH_BASE=p`；配置成 `pmc/pn` 时启动失败并明确报告尚未安装相应适配器。
- 用户提供的模型 base URL 默认只允许 HTTPS；访问 loopback、私网或 link-local 地址需要显式开发环境开关，防止多租户 SSRF。
- Gateway 启动执行 schema/table/extension 迁移并检查 Redis；RAG worker 独立通过启动脚本运行，启动前验证数据库版本和 Redis ACL。

## 6. 测试与验收

- 单元测试：
  - 五类文档解析、三类 chunking、token 上限和来源元数据。
  - 图谱实体规范化、属性冲突、边去重和证据保留。
  - RRF、BM25 分词、向量/图谱过滤和租户条件。
  - 模型配置加密、base URL 安全校验及配置指纹。
- PostgreSQL/Redis 集成测试：
  - 三队列抢占、每知识库并发上限、lease 恢复、幂等提交。
  - 向量/图谱中途失败后只处理缺失 chunk，并刷新缓存 TTL。
  - 完成前不产生部分向量或部分图谱可见数据。
  - embedding 模型锁定、删除派生数据后的解锁。
  - 知识库复制、文档/知识库级联删除和缓存清理。
- API 隔离测试：
  - 用户不能通过猜测 workspace、知识库、文档、任务或图谱 ID 访问其他租户。
  - 不允许跨知识库检索或合并图谱。
- 完整 HTTP 验收：
  1. 注册并创建 workspace、知识库。
  2. 批量上传五类样例文档。
  3. 配置并验证 embedding/LLM。
  4. 提交三类任务并轮询状态。
  5. 人为制造模型调用中断，验证缓存续跑。
  6. 执行 vector/hybrid/graph 检索。
  7. 合并图谱并验证源图谱仍存在。
  8. 复制知识库并比较文档、chunks、向量、图谱和队列。
  9. 删除 vectors 后重新向量化；删除文档后验证全部关联数据和缓存消失。
- 更新 README、API reference、部署说明和 env 说明；保留当前未提交的 `.gitignore` 与 `skills-lock.json` 改动，不混入实现提交。

## 7. 已锁定的假设

- 不提供回答生成接口，检索结果由后续智能体或应用消费。
- 不接入 cross-encoder 或 LLM reranker；“重排”在本期指 BM25 与向量结果的带权 RRF 融合排序。
- 语义切分必须先配置并验证知识库 embedding。
- 文档、向量和图谱后端创建后不可修改。
- Milvus、Chroma、Qdrant、Neo4j、OCR、图片解析、前端和跨库迁移均不在本期范围。
