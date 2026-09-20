# 多租户 RAG 四阶段 Pipeline 更新计划

## 1. 数据模型与存储后端

- 将知识库创建参数调整为五个必填且创建后不可修改的后端字段：
  - `file_backend`
  - `block_backend`
  - `chunk_backend`
  - `vector_backend`
  - `graph_backend`
- 本期五个字段都只接受 `postgresql`；知识库复制原样继承，复制 API 不提供后端参数。
- 删除知识库级 `document_backend`、`chunking_strategy`、`chunking_config`；解析后端和切分策略改为文档级。
- 文档增加：
  - `original_filename`
  - `stored_filename`
  - `storage_backend/storage_key`
  - parsing、chunking、vectorization、graph 四组状态字段
  - 文档级 `parsing_backend`
  - 文档级 `chunking_strategy/chunking_config`
- 增加文件存储接口，提供 `put/read/delete/copy` 操作；默认 PostgreSQL 实现使用 `bytea`，业务代码不再直接读取 `RagDocument.content`。预留 MinIO 适配入口，但不安装依赖、不返回 MinIO 能力。
- blocks、chunks 继续直接持久化 PostgreSQL；虽在知识库中分别保存后端字段，但本期无需抽象其他实现。
- 新增破坏性 `rag` schema 重建迁移：只删除并重建 `rag` schema，不影响 `platform` schema。现有测试数据不迁移；部署迁移前先停止 Gateway 和 worker。

## 2. API 契约调整

### 能力与知识库

- `GET /api/v1/rag/capabilities` 返回：
  - `file_backends: ["postgresql"]`
  - `block_backends: ["postgresql"]`
  - `chunk_backends: ["postgresql"]`
  - `vector_backends: ["postgresql"]`
  - `graph_backends: ["postgresql"]`
  - `document_processing_backends: ["default"]`
  - 三种切分策略及 env 默认参数
- 创建知识库请求必须显式提交五个存储后端。
- 知识库更新仅保留名称及 parsing、chunking、embedding、graph 四项并发配置，不再修改知识库级切分策略。

### 文档上传

- 上传只负责验证和持久化文件，不创建任务、不解析、不访问 Redis。
- 持久化名称固定为：
  - `<user_uuid>_<YYMMDD>_<sanitized_stem><normalized_extension>`
  - 日期使用 UTC 上传日期。
  - 仅取客户端文件名的 basename；控制字符、路径字符及不安全字符替换为 `_`，连续 `_` 合并，扩展名只追加一次。
- 同日同用户上传同名文件仍然允许；`stored_filename` 可以相同，真正唯一的 `storage_key` 使用 document UUID。
- 批量上传采用逐文件部分成功。语法合法的请求统一返回 `200`：
  ```json
  {
    "uploaded": 1,
    "failed": 1,
    "items": [
      {"original_filename":"a.pdf","status":"uploaded","document":{}},
      {
        "original_filename":"b.pdf",
        "status":"failed",
        "error_code":"document_too_large",
        "message":"...",
        "retryable":true
      }
    ]
  }
  ```
- 文件数量超过批次上限仍为请求级 `422`；鉴权、知识库状态或数据库整体故障仍使用对应非成功状态码。

### 解析、切分与派生数据

- 新增 `POST .../jobs/parsing`，请求按文档指定后端：
  ```json
  {
    "items": [
      {"document_id":"<uuid>","processor_backend":"default"}
    ]
  }
  ```
  未传 `processor_backend` 时使用 `DOCUMENT_PROCESSING_SERVICE`。
- 新增 `PUT .../documents/{document_id}/chunking-config`：
  ```json
  {
    "strategy":"fixed",
    "config":{
      "max_token_size":512,
      "overlap_token_size":64,
      "split_by_character":"\n\n"
    }
  }
  ```
- 文档上传时复制 env 中的 fixed 默认配置；配置只作用于当前文档。chunking queued/running/succeeded 时拒绝修改；失败后可修改，成功后需先删除 chunks。
- `POST .../jobs/chunking` 继续接收 `document_ids`，但从每个文档读取并快照其独立策略。
- 新增 `DELETE .../documents/{document_id}/blocks`：删除 blocks、chunks、vectors、文档图谱、依赖的合并图谱及向量/图谱缓存，并重置四个阶段。
- 现有 `/chunks`、`/vectors`、`/graph` 删除接口继续分别重置对应阶段及下游资源。

## 3. Worker 与四阶段处理

- `processing_jobs.kind` 扩展为：
  - `parsing`
  - `chunking`
  - `vectorization`
  - `graph_extraction`
- parsing 使用独立知识库并发和系统上限：
  - `RAG_DEFAULT_PARSING_CONCURRENCY=2`
  - `RAG_MAX_PARSING_CONCURRENCY=8`
- 调度器同步扩展四类并发映射，并确保只有存在可执行 task 容量时才 claim job，避免任务停留在无执行协程的 running 状态。
- parsing job：
  - 从文件存储适配器读取原文。
  - 使用 job 中的处理后端快照解析。
  - 全部解析成功且至少产生一个有效 block 后，在单一事务中发布 blocks。
  - 失败时不保留任何 partial blocks，也不写 Redis。
- chunking job：
  - 只读取已持久化 blocks，不再负责解析文件。
  - 使用文档级策略快照；语义切分使用已验证的知识库 embedding。
  - 全部切分成功后一次性发布 chunks；失败不保留 partial chunks，也不写 Redis。
- 前置条件固定为：
  - parsing：文件已上传，尚无成功解析结果。
  - chunking：parsing succeeded 且 blocks 非空。
  - vectorization：chunking succeeded、chunks 非空且 embedding 已验证。
  - graph extraction：chunking succeeded、chunks 非空且 LLM 已验证。
- parsing/chunking 已成功时禁止重复提交；失败可以重新提交。重新解析必须先删除 blocks，重新切分必须先删除 chunks。
- 向量化、图谱提取的 Redis 中间缓存、失败续跑和最终原子入库机制保持不变。
- 知识库复制增加 parsing 状态、文档级处理配置、四类任务、五个后端字段和 PostgreSQL 文件对象的深复制；Redis 只复制向量/图谱缓存。
- 文档删除通过文件存储接口清理原文件，并删除全部派生数据、依赖合并图谱和缓存。

## 4. 配置、部署与文档

- `.env` 和 `.env.example` 同步增加：
  - `FILE_BASE=p`
  - parsing 默认并发与最大并发
- `FILE_BASE=p` 启用 PostgreSQL；配置为 `pm` 时明确启动失败，提示 MinIO 适配器尚未实现。
- 保持 `DOCUMENT_PROCESSING_SERVICE=default`、`VECTOR_BASE=p`、`GRAPH_BASE=p`。
- 不修改现有 `.env` 中的 PostgreSQL连接信息和密码。
- 从开发 Compose 中移除 PostgreSQL 服务及其数据卷，避免启动或拉取新 PostgreSQL 镜像；Compose 只负责现有 Redis。
- Gateway 和 worker 直接连接已经部署的 `localhost:5432` PostgreSQL；启动检查验证数据库连接、Alembic head 和 pgvector extension，不执行镜像拉取。
- 更新 README、RAG 文档、API reference、部署报告及 HTTP smoke 脚本，将推荐流程改为：
  `创建知识库 → 上传 → parsing → 配置文档切分 → chunking → embedding/graphing → retrieval`。

## 5. 测试与验收

- 单元测试覆盖文件名清洗、用户 UUID 前缀、UTC 日期、扩展名处理、路径穿越字符及同名文件不同 storage key。
- API 测试覆盖五个必填后端、不可修改和复制继承，以及 `FILE_BASE=pm` 启动失败。
- 上传测试覆盖全成功、部分失败、全部文件失败、空文件、超限文件、不支持扩展名，并验证无 Redis key 和无解析任务。
- parsing 测试覆盖逐文档后端、失败不产生 blocks、成功后禁止重复提交、删除 blocks 后允许重跑。
- chunking 测试覆盖同知识库不同文档使用不同策略、未解析拒绝切分、失败无 partial chunks、语义切分缺少 embedding 时拒绝。
- 队列集成测试覆盖四类并发、知识库级限制、系统级限制、lease 恢复和 active job 唯一约束。
- 生命周期测试覆盖 blocks/chunks 级联删除、文件适配器清理、合并图谱清理，以及知识库完整复制。
- 在同一已部署 PostgreSQL 实例上使用独立测试数据库执行集成测试；不启动 PostgreSQL 容器。Redis 使用独立测试 DB。
- 完整 HTTP 验收必须验证四阶段状态变化、部分上传结果、两份文档使用不同切分策略、向量/图谱缓存续跑、检索、复制和级联删除。

## 已锁定假设

- blocks 和 chunks 使用两个独立知识库字段，当前均只能选择 PostgreSQL。
- 文件名中的 UID 是当前用户 UUID，不是 document UUID。
- 上传部分成功使用逐文件结果；数据库级整体故障不会返回虚假的成功项。
- parsing 使用独立的知识库级及系统级并发限制。
- MinIO、其他 blocks/chunks 后端、OCR、前端和 agent-runtime 改动不在本期范围。
- 现有 RAG 数据全部视为可删除测试数据，不提供兼容迁移。
