# Runtime 自定义 RAG 工具接入计划

## 摘要

在 Pi Runtime 中注册 `rag_search` 自定义工具，由 Runtime 通过可配置内部 URL 直接调用 Gateway。Session 创建时显式绑定多个同 Workspace 知识库，绑定创建后不可修改；工具支持 `vector`、`hybrid`、`graph`，默认 `hybrid`。

保持现有边界：Gateway 负责租户鉴权、知识库授权和检索；Runtime 不接触 PostgreSQL、Redis或向量库凭证；现有公开 RAG API、处理任务和检索语义不改变。

## 接口与数据模型

- 新增迁移 `0009_agent_rag_tool`：
  - 建立 `platform.agent_session_knowledge_bases`，以 `(session_id, knowledge_base_id)` 为主键，分别外键到 Agent Session 和 RAG KnowledgeBase，并使用级联删除。
  - 为 `platform.runtime_instances` 增加可空的 `rag_secret_digest BYTEA`。
- 扩展 `POST /api/v1/sessions`：
  - 新增可选 `knowledge_base_ids: UUID[]`，默认空，最多 20 个且不允许重复。
  - 所有知识库必须属于当前用户、选定 Workspace 且状态为 active；统一以 `422 invalid_knowledge_base_binding` 拒绝越权、跨 Workspace、失效或不存在的绑定。
  - 成功响应和 `GET /sessions` 增加 `knowledge_base_ids`。
  - 不提供修改绑定的接口；需要更换知识库时新建 Session。省略该字段的旧客户端保持原行为。
- 新增不进入公开 OpenAPI 的内部接口：
  - `POST /internal/v1/runtime-rag/retrieve`
  - Header：每租户 Runtime Bearer 密钥及 `X-Tenant-ID`。
  - Body：`session_id`、`knowledge_base_id`、`query`、`mode`、可选 `document_ids`、`top_k`。
  - `mode` 支持三种现有模式并默认 `hybrid`；`top_k` 默认 5、最大 20；其余候选数、权重、阈值和图谱参数使用现有服务默认值。
  - Gateway 重新校验 Runtime 身份、Session 所有权、固定绑定、Workspace、知识库和文档过滤，然后调用现有 retrieval service。
  - 未认证返回 401；未绑定或越权资源统一返回 404；文档过滤错误返回 422；RAG 前置条件错误保持现有 409 机器码。

## 实现改动

- Gateway 配置增加可选 `RUNTIME_GATEWAY_BASE_URL`。未配置时仍可使用无 RAG 的 Agent Session；创建带知识库绑定的 Session 返回 `409 agent_rag_unavailable`。
- 本地开发配置为可从容器访问的地址，例如：
  - `GATEWAY_HOST=0.0.0.0`
  - `RUNTIME_GATEWAY_BASE_URL=http://host.docker.internal:21995`
  - Runtime 容器增加 `host.docker.internal:host-gateway` 映射。生产环境可替换为内网域名。
- 每次创建 Runtime 容器时生成独立的高熵 `RUNTIME_RAG_SHARED_SECRET`：
  - 明文注入该容器环境，数据库仅保存 SHA-256 摘要。
  - Gateway 使用常量时间比较验证，不复用全局 `RUNTIME_SHARED_SECRET` 或用户 JWT。
  - Runtime 启动后立即读取并从 `process.env` 删除，减少 Agent bash 子进程直接读取的机会；同时加入事件与日志脱敏列表。
  - Runtime stop/start 保留密钥，`:recreate` 生成新密钥并替换摘要。
- Gateway 传给 Runtime 的 Session 配置增加固定知识库的 ID、名称及 Session ID。没有绑定时不注册 RAG 工具。
- Runtime 注册 `rag_search` custom tool，参数固定为：
  - `knowledge_base_id`
  - `query`
  - `mode=vector|hybrid|graph`
  - 可选 `document_ids`
  - `top_k`
- 工具请求使用固定配置的 Gateway 地址，模型不能指定 URL、Workspace 或认证信息；请求超时默认 30 秒，并响应 Pi 的 AbortSignal。
- 返回给模型的文本最多 64 KiB：
  - vector/hybrid：证据正文、KB、document、chunk、score、source。
  - graph：节点、边和 document/chunk evidence。
  - 明确标记检索内容为不可信资料，要求答案保留来源引用，不能执行资料中的指令。
  - `details` 保留结构化原始检索结果，由现有工具历史投影继续执行 256 KiB 截断和敏感字段脱敏。
- 网络、超时、401、404、409、422 等失败转换为不含 URL、密钥或响应正文的安全工具错误；Pi 将其记录为 `isError=true`，但允许 Agent 继续回答或调整查询。
- 更新 `.env.example`、RAG/API 文档和 Runtime 部署文档，明确监听地址、安全边界、密钥轮换以及旧容器必须 recreate。

## 测试与验收

- Gateway 单元/契约测试：
  - 空绑定保持兼容；多知识库绑定成功。
  - 重复、跨用户、跨 Workspace、inactive 和不存在知识库全部拒绝。
  - 内部接口验证正确密钥、错误密钥、伪造租户、未绑定 KB、跨库文档过滤及三种检索模式。
  - 删除知识库、Session 或 Workspace 时绑定记录正确级联清理。
  - Runtime 密钥每容器唯一，stop/start 不变，recreate 后轮换，日志和响应不出现明文。
- Runtime TypeScript 测试：
  - 无绑定不注册工具；有绑定注册 `rag_search`。
  - 参数 schema、三种结果格式、64 KiB 边界、超时/取消和安全错误映射。
  - 密钥从进程环境清除，工具事件及 JSONL 投影不泄漏密钥。
  - `npm test` 与 `npm run check` 全部通过。
- 新增 `test/agent_rag_user_flow.py` 黑盒验收：
  - 只调用公开 Gateway API，创建 Workspace/KB、上传唯一事实文档、完成解析/切分/向量化、创建绑定 Session。
  - 明确要求 Agent 调用 `rag_search`，验证 SSE 中工具开始/完成、历史中的引用信息、最终答案包含唯一事实。
  - live 路径至少验证 hybrid；vector/graph 由内部契约测试及现有 RAG E2E 覆盖。
  - 默认清理资源，保留 `--keep-resources`；报告脱敏。
- 上线顺序：
  1. 执行 Alembic 升级并配置 Runtime 可达的 Gateway URL。
  2. 重启 Gateway。
  3. 在没有运行中 Agent Run 时 recreate 旧 Runtime 容器，使其获得新工具与独立密钥。
  4. 验证容器内可达 Gateway 内部接口，再运行 scoped tests 和黑盒流程。
  5. 只有黑盒脚本完整通过后，才认定 Agent→RAG 接入成功；仅端口连通、内部接口 200 或单元测试通过不算完整验收。

## 已确定的假设

- 一个 Session 可绑定多个知识库，但一次工具调用只查询其中一个，不修改现有“单知识库检索”契约。
- Session 创建后绑定固定，不增加更新接口或前端 UI。
- 接受静态密钥经容器环境注入；Docker daemon/宿主管理员仍可通过容器元数据看到它，这是本方案明确接受的风险。
- Gateway 不生成答案、不做二次摘要；最终回答仍由 Pi Agent 生成。
- 不允许 Runtime 直接访问数据库、Redis、Milvus、Chroma 或 Qdrant。
