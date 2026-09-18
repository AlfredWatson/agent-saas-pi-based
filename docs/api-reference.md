# Pi SaaS Platform API 文档

> 版本：基于当前仓库源码整理，日期：2026-09-18。
>
> 公共 API 根路径：`http(s)://<gateway-host>/api/v1`。本文描述 FastAPI Gateway 的对外契约；`/internal/v1/*` 是 Gateway 与 Runtime 的内部协议，不能经公网或客户端直接调用。

## 1. 概览

```text
Client
  │ HTTPS + Bearer JWT
  ▼
FastAPI Gateway (/api/v1)
  ├── PostgreSQL（platform schema：用户、配置、会话和消息投影）
  └── 127.0.0.1:<动态端口> + 内部 Bearer
        └── Docker Agent Runtime（Pi SDK、租户工作区、Pi JSONL）
```

Gateway 负责身份认证、租户授权、Provider 密钥加密存储、运行记录、消息投影及对外 SSE；Runtime 仅负责 Pi SDK 会话、工具调用与 NDJSON 事件。Provider 密钥不会由公开读取接口返回。

除注册和登录外，所有接口均要求 Bearer JWT：

```http
Authorization: Bearer <access_token>
```

带 JSON 请求体的接口使用 `Content-Type: application/json`；Workspace 文件上传是
`multipart/form-data`，无请求体的 `GET` 与 `DELETE` 不需要 `Content-Type`。

ID 均为 UUID。时间字段为带时区的 ISO 8601 时间。所有资源按当前登录用户隔离；访问他人资源通常返回 `404`，而不是泄露其是否存在。

令牌为 HS256 JWT，含 `iss=pi-saas`、`aud=pi-saas-api`，有效期 60 分钟。未带令牌通常返回 `403`（HTTP Bearer 机制），无效、过期或所属用户非 `active` 时返回 `401 {"detail":"invalid_token"}`。

## 2. 首次调用顺序

注册会同时创建并选中名为 `default` 的 Workspace。创建 Provider Binding 不会请求模型；首次成功 Chat 才写入该 Binding 的 `verified_at`。

```text
register/login → providers → provider-bindings → models
       → agent-profiles → workspaces → sessions → messages:stream
       → sessions/{id}/messages
```

可使用统一的真实模型黑盒验收流程验证此链路。该流程使用既有专用测试账号，创建并清理
独立的 Workspace、Binding、Profile 和 Session，并证明 Agent 通过工具读取上传的 XLSX：

```bash
export AGENT_TEST_EMAIL='agent-test@example.com'
export AGENT_TEST_PASSWORD='replace-with-test-password'
export AGENT_TEST_PROVIDER_ID='your-provider'
export AGENT_TEST_PROVIDER_API_KEY='replace-with-provider-key'
export AGENT_TEST_MODEL_ID='your-model'
uv run python test/agent_user_flow.py --report /tmp/agent-user-flow.json
```

Gateway 默认使用 `.env` 的 `GATEWAY_HOST` 和 `GATEWAY_PORT`；可用
`AGENT_TEST_GATEWAY` 提供完整 API 根路径覆盖。测试凭据只应放在环境变量中，报告会脱敏。

## 3. 认证

### `POST /auth/register`

注册用户并创建默认工作区。

```json
{"email":"alice@example.com","password":"at-least-12-characters"}
```

`email` 必须是有效邮箱，`password` 长度为 12–256。成功返回 `201`：

```json
{"access_token":"<jwt>","token_type":"bearer"}
```

邮箱已注册：`409 {"detail":"email_exists"}`。请求体不合规：`422`。

### `POST /auth/login`

请求体同注册。成功返回 `200` 和令牌对象；失败返回 `401 {"detail":"invalid_credentials"}`。

### `GET /auth/me`

返回当前用户：

```json
{"id":"<uuid>","email":"alice@example.com","status":"active"}
```

## 4. Provider 与模型

### `GET /providers`

返回当前 Runtime 支持的 Provider 目录。此操作会按需确保当前用户的 Runtime 已运行。

```json
{"providers":[{"id":"faux","name":"Faux"}]}
```

真实目录受当前 Pi SDK 注册表影响。Docker、镜像或 Runtime 无法启动时可返回 `503 {"detail":"runtime_unavailable"}`。

### `POST /provider-bindings`

创建 Provider 凭据绑定。密钥经服务端 AEAD 加密后写入 PostgreSQL；读取接口不返回密钥、密文或 nonce。

```json
{
  "provider_id":"faux",
  "display_name":"开发测试",
  "api_key":"faux-key"
}
```

成功返回 `201`：

```json
{"id":"<binding-uuid>","provider_id":"faux","status":"active"}
```

这一步只验证 Provider 目录和非空密钥，**不**访问上游 Provider。当前 Gateway 对 Runtime 的 HTTP 校验异常没有统一的公开错误转换；调用方不应把 Provider 校验失败的具体 HTTP 状态当作稳定契约。

### `GET /provider-bindings`

```json
{"items":[{"id":"<uuid>","provider_id":"faux","display_name":"开发测试","status":"active"}]}
```

### `GET /provider-bindings/{binding_id}/models`

Binding 必须属于当前用户且是 `active`：

```json
{
  "models":[
    {"id":"faux-1","provider_id":"faux","name":"Faux 1",
     "thinking_levels":["minimal","low","medium","high"]}
  ]
}
```

无效、禁用或非本人 Binding 返回 `404 {"detail":"binding_not_found"}`。

### `DELETE /provider-bindings/{binding_id}`

软禁用 Binding，成功返回 `204`（无响应体）。不存在或不属于当前用户返回 `404`。禁用后不可创建/更新引用该 Binding 的 Profile，也不能运行引用它的 Session。

## 5. Agent Profile 与工作区

### `POST /agent-profiles`

创建模型配置。模型和推理等级会与 Runtime 返回的模型目录实时校验。

```json
{
  "name":"faux-high",
  "provider_binding_id":"<binding-uuid>",
  "model_id":"faux-1",
  "thinking_level":"high"
}
```

成功：`201 {"id":"<profile-uuid>","name":"faux-high","model_id":"faux-1"}`。

失败包括：`422 invalid_binding`、`422 invalid_model`、`422 invalid_thinking_level`。`thinking_level` 可以省略或为 `null`，只能取模型提供的 `thinking_levels`。

### `GET /agent-profiles`

```json
{"items":[{"id":"<uuid>","name":"faux-high","model_id":"faux-1","thinking_level":"high"}]}
```

按创建时间倒序返回。为减少密钥/配置关联暴露，该响应不返回 `provider_binding_id`。

### `PUT /agent-profiles/{profile_id}`

请求体与创建相同。成功：`200 {"id":"<uuid>","name":"faux-high"}`；不存在/非本人：`404 profile_not_found`；Binding、模型或推理等级无效：`422`。

### `DELETE /agent-profiles/{profile_id}`

删除当前用户不再被任何 Session 引用的 Profile，成功返回 `204`。不存在或不属于当前用户
返回 `404 profile_not_found`；仍被任一 Session 引用时返回 `409 profile_in_use`。调用方应先
删除关联 Workspace（其会级联删除 Session），再删除 Profile。

### `GET /workspaces`

```json
{"items":[{"id":"<workspace-uuid>","name":"default","status":"active","is_current":true}]}
```

注册时自动创建并选中 `default`。Workspace 名称同时是容器内
`/runtime-data/workspaces/<name>/` 的目录名，必须匹配
`^[a-z0-9][a-z0-9_-]{0,63}$`；同一用户内不得重名。

### `POST /workspaces`

```json
{"name":"project-a"}
```

成功：`201 {"id":"<workspace-uuid>","name":"project-a","status":"active","is_current":false}`。
首次实际 Chat 或文件上传时才创建目录。名称非法为 `422 invalid_workspace_name`；重名、数量上限或存储上限分别为 `409 workspace_exists`、`workspace_limit_reached`、`workspace_storage_limit_reached`。

### `POST /workspaces/{workspace_id}:switch`

将该 Workspace 设为当前 Workspace。后续 `POST /sessions` 省略 `workspace_id` 时使用它。不可用 Workspace 返回 `409 workspace_unavailable`。

### `GET /workspaces/{workspace_id}/sessions`

```json
{"session_ids":["<session-uuid>"]}
```

### `GET /workspaces/{workspace_id}/files`

递归列出该 Workspace 中的普通文件，按 `path` 排序；没有实际文件目录时返回空列表。响应不包含目录、符号链接或特殊文件：

```json
{"items":[{"path":"src/app.py","size_bytes":1234,"modified_at":"2026-08-03T00:00:00.000Z"}]}
```

列表允许在 Agent Run 执行期间调用。

三个文件接口都会按需启动当前用户的 Runtime；因此 Docker、镜像或 Runtime
健康检查失败时可返回 `503 {"detail":"runtime_unavailable"}`。Workspace 不存在
或不属于当前用户时返回 `404 workspace_not_found`；已不可用时返回
`409 workspace_unavailable`。

### `POST /workspaces/{workspace_id}/files`

以 `multipart/form-data` 上传单个文件：`path` 和 `file` 必填，`overwrite` 可选且默认 `false`。`path` 是 POSIX 相对路径，例如 `src/app.py`；禁止绝对路径、空路径、`.`/`..`、反斜杠、NUL、超长路径及符号链接路径。客户端文件名不作为目标路径。

首次写入成功返回 `201`，显式 `overwrite=true` 的原子替换返回 `200`，响应均为：

```json
{"path":"src/app.py","size_bytes":1234,"modified_at":"2026-08-03T00:00:00.000Z"}
```

同路径且未启用覆盖为 `409 file_exists`；单文件超过 `WORKSPACE_FILE_MAX_MB` 为 `413 file_too_large`；超出用户总配额为 `409 workspace_storage_limit_reached`。执行中的 Workspace 拒绝上传，返回 `409 workspace_busy`。

### `DELETE /workspaces/{workspace_id}/files?path=...`

删除指定普通文件，成功返回 `204` 并清理空父目录（不删除 Workspace 根目录）。不存在返回 `404 file_not_found`；路径或文件类型不合法返回 `422 invalid_file_path` 或 `422 unsupported_file_type`；执行中的 Workspace 返回 `409 workspace_busy`。

### `DELETE /workspaces/{workspace_id}`

删除该 Workspace 的目录、关联 Pi JSONL、Session、Run 和消息记录，成功返回 `204`。`default` 不能删除；若任一关联 Session 正在运行，返回 `409 workspace_busy` 且不删除任何内容。

## 6. Runtime 生命周期

这些接口控制当前用户独占的 Docker Runtime。Provider/模型目录读取和实际执行也会触发懒启动。

### `GET /runtime`

```json
{
  "state":"running",
  "image":"pi-saas-agent-runtime:0.82.1-dev",
  "container_id":"<container-id-or-null>",
  "last_error":null,
  "last_seen_at":"2026-08-03T00:00:00+00:00"
}
```

从未创建容器时 `state` 为 `absent`，其余诊断字段为 `null`。`container_id` 不是稳定业务 ID。

### `POST /runtime:start`

确保容器存在、启动并通过内部健康检查。成功：`200 {"state":"running"}`；不可用：`503 runtime_unavailable`。

### `POST /runtime:stop`

停止容器但保留实例记录和租户数据。成功时返回与 `GET /runtime` 相同的对象；没有容器时返回 `state: "absent"`。若用户有运行中的 Agent Run：`409 runtime_busy`；无法停止：`409 runtime_not_stoppable`。

### `POST /runtime:recreate`

删除旧容器并使用同一租户数据目录重新创建、启动和健康检查。成功：`200 {"state":"running"}`；任务运行中：`409 runtime_busy`；Docker/镜像故障：`503 runtime_unavailable`。

## 7. Session、流式消息与历史

### `POST /sessions`

创建逻辑 Session，不会立刻将 Provider 密钥交给 Runtime，也不会强制创建容器。

```json
{"profile_id":"<profile-uuid>","workspace_id":"<workspace-uuid>"}
```

`workspace_id` 可以省略，此时使用当前 Workspace（新用户默认为 `default`）。成功：`201 {"id":"<session-uuid>","status":"ready"}`。任一 ID 不属于当前用户时为 `422 invalid_profile_or_workspace`。

### `GET /sessions`

轻量会话列表：

```json
{"items":[{"id":"<uuid>","status":"ready","title":null}]}
```

完整历史不包含在这里，应使用下一接口。

### `GET /sessions/{session_id}/messages`

按 `sequence` 升序返回完整公开投影：

```json
{
  "items":[{
    "id":"<uuid>","run_id":"<uuid-or-null>","role":"user",
    "content":"请说明当前目录","sequence":1,"status":"completed",
    "created_at":"2026-08-03T00:00:00+00:00",
    "tool_call_id":null,"tool_name":null,"arguments":null,"result":null,
    "is_error":null,"payload_truncated":false
  }]
}
```

`role` 可为 `user`、`assistant`、`tool_call` 或 `tool_result`。工具调用和结果以 `tool_call_id` 关联；`arguments`/`result` 是 JSON 值，`is_error` 对工具结果有意义。过大工具载荷会截断并设 `payload_truncated=true`。秘密值经 Runtime 和 Gateway 两层脱敏后才进入 SSE 和数据库。

### `POST /sessions/{session_id}/messages:stream`

提交用户消息并建立 SSE。请求：

```json
{"content":"请完成这个任务"}
```

`content` 长度为 1–100,000。响应为 `200 text/event-stream`，带 `Cache-Control: no-cache`。

| 事件 | `data` | 说明 |
| --- | --- | --- |
| `message.accepted` | `{}` | Gateway 已创建运行记录并持久化用户消息。 |
| `assistant.delta` | `{"delta":"..."}` | 助手文本增量。 |
| `tool.started` | `{"tool":"...","toolCallId":"...","toolName":"...","args":{},"payload_truncated":false}` | 工具开始。 |
| `tool.completed` | `{"tool":"...","toolCallId":"...","toolName":"...","result":{},"isError":false,"payload_truncated":false}` | 工具结束。 |
| `message.completed` | `{}` | Runtime 返回 `agent_settled`，Run 成功完成。 |
| `message.failed` | `{"error":"runtime_stream_failed"}` | Runtime 流异常或未正常 settled。 |
| `done` | `{}` | 本次 SSE 终止事件。 |

客户端断开 SSE **不会**中止后台任务；Gateway 会继续消费 Runtime 流并持久化历史。历史顺序以 Pi `message_end` 为准，每到一条安全投影即提交，因此已完成工具调用/结果不会因后续失败丢失。一个 Session 同时只能有一个 `running` Run；冲突返回 `409 {"detail":"session_busy"}`。

```bash
curl -N -X POST "http://127.0.0.1:8000/api/v1/sessions/$SESSION_ID/messages:stream" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data '{"content":"Say OK."}'
```

### `POST /sessions/{session_id}/abort`

中止 Runtime 中当前任务。成功：`202 {"status":"accepted"}`。Session 不存在：`404 session_not_found`。

### `POST /sessions/{session_id}/steer`

向运行中的任务追加引导。请求为 `{"content":"改为只输出摘要"}`。成功 `202`；未运行时 Runtime 返回 `409 session_not_running`。

### `POST /sessions/{session_id}/follow-up`

与 `steer` 相同的路径格式和请求体，但调用 Pi 的 follow-up 语义。成功 `202`；未运行时为 `409 session_not_running`。

## 8. 多租户 RAG

> **四阶段 pipeline 版本。** 本节中旧的知识库级 `document_backend`、
> `chunking_strategy` 与 `chunking_config` 已废弃。当前可执行的 RAG 请求契约以
> [`rag.md`](rag.md) 为准：创建知识库需提交五个 PostgreSQL 后端；上传返回逐文件
> 结果；parsing 和 chunking 是两个独立任务，文档级 chunking 配置必须在 chunking
> 入队前确定。完整 OpenAPI 可由运行中的 `/openapi.json` 获取。

RAG 是独立于 Agent Runtime 的知识库服务。所有知识库均属于一个 Workspace，所有下列
资源查询都会同时校验当前用户、`workspace_id` 与 `knowledge_base_id`；不能跨知识库检索
或合并图谱。完整数据处理与部署说明见 [`rag.md`](rag.md)。

RAG 文档上传使用 `multipart/form-data`；其余写接口使用 JSON。除另有说明外，业务错误为
`{"detail":"<machine_code>"}`，找不到或不属于当前用户的资源返回 `404`。

建议调用顺序：创建知识库 → 上传文档 → 设置文档级 chunking 配置（可选）→ 提交 parsing →
提交 chunking → 设置并验证模型 → 提交向量化和/或图谱提取 → 轮询文档或任务 → 检索/合并图谱。
RAG 不提供回答生成接口，检索结果供后续应用消费。

### 8.1 能力发现

### `GET /rag/capabilities`

返回当前部署实际启用的处理和存储后端、可上传扩展名及各切分策略的默认配置。应先调用此接口，
再将后端选择展示给用户。本期仅返回内置文档处理与 PostgreSQL 向量/图谱存储。

```json
{
  "file_backends":["postgresql"],
  "block_backends":["postgresql"],
  "chunk_backends":["postgresql"],
  "document_processing_backends":["default"],
  "vector_backends":["postgresql"],
  "graph_backends":["postgresql"],
  "document_extensions":[".doc",".docx",".md",".markdown",".pdf",".ppt",".pptx",".xls",".xlsx"],
  "chunking_strategies":{
    "fixed":{"max_token_size":512,"overlap_token_size":64,"split_by_character":"\\n\\n"},
    "regex":{"max_token_size":512,"re_expression":"\\n{2,}|(?<=[。！？.!?])\\s+"},
    "semantic":{"max_token_size":512,"breakpoint_threshold":95}
  }
}
```

### 8.2 知识库

以下响应中的知识库对象形如：

```json
{
  "id":"<knowledge-base-uuid>","workspace_id":"<workspace-uuid>",
  "name":"product-docs","status":"active","version":1,
  "file_backend":"postgresql","block_backend":"postgresql","chunk_backend":"postgresql",
  "vector_backend":"postgresql","graph_backend":"postgresql",
  "concurrency":{"parsing":2,"chunking":2,"embedding":2,"graph":1}
}
```

### `POST /workspaces/{workspace_id}/knowledge-bases`

创建知识库，成功返回 `201` 和知识库对象。

```json
{
  "name":"product-docs",
  "file_backend":"postgresql",
  "block_backend":"postgresql",
  "chunk_backend":"postgresql",
  "vector_backend":"postgresql",
  "graph_backend":"postgresql"
}
```

五个后端字段必须显式提供；只能取 capabilities 中的值，创建后不可更改。同一
Workspace 名称重复返回 `409 knowledge_base_exists`。

### `GET /workspaces/{workspace_id}/knowledge-bases`

返回 `{"items":[<知识库对象>, ...]}`。

### `GET /workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}`

返回一个知识库对象。

### `PATCH /workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}`

可修改名称和四类并发上限；后端类型、解析后端和切分策略不在知识库修改范围内。

```json
{
  "name":"product-docs-v2",
  "parsing_concurrency":2,
  "chunking_concurrency":2,
  "embedding_concurrency":2,
  "graph_concurrency":1
}
```

字段均可选。文档级 chunking 配置使用 `PUT .../documents/{document_id}/chunking-config`：

| 策略 | 参数 | 默认值 |
| --- | --- | --- |
| `fixed` | `max_token_size`、`overlap_token_size`、`split_by_character` | `512`、`64`、`"\n\n"` |
| `regex` | `max_token_size`、`re_expression` | `512`、段落/中英文句末正则 |
| `semantic` | `max_token_size`、`breakpoint_threshold` | `512`、`95` |

`max_token_size` 最小为 32；fixed 的 overlap 必须小于 token 上限；semantic 的阈值必须在
`(0, 100)` 之间。该配置只作用于当前文档，任务会保存快照。语义切分在提交任务前必须已有
已验证的 embedding 模型。无效参数返回 `422 invalid_chunking_config`。

### `POST /workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/copy`

异步复制完整知识库，成功返回 `202`：

```json
{"name":"product-docs-copy"}
```

```json
{"operation_id":"<operation-uuid>","target_knowledge_base_id":"<uuid>","status":"queued"}
```

副本继承文档、blocks、chunks、向量、图谱、任务记录、配置和仍有效的中间缓存，且不能选择
不同后端。源知识库有 running 文档任务时返回 `409 knowledge_base_processing`。

### `DELETE /workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}`

异步删除知识库，立即返回 `202 {"operation_id":"<uuid>","status":"queued"}`。服务会先取消
排队/执行中的任务，再删除数据库数据及 Redis 缓存；完成前知识库状态为 `deleting`。

### 8.3 模型配置

模型配置仅作用于当前知识库，API key 只写入、AES-GCM 加密保存，任何读取接口都不返回明文。
所有设置请求均会实际调用模型验证，验证失败不会保存配置。生产环境仅接受 HTTPS base URL；
本地/私网 URL 仅能在显式开发开关开启时使用。

本节至 8.7 中的 `.../{knowledge_base_id}/...` 均以前缀
`/workspaces/{workspace_id}/knowledge-bases` 开始。

### `PUT .../{knowledge_base_id}/embedding-model`

仅支持 OpenAI-compatible embedding 服务，服务端会验证向量非空且为有限数值，并记录维度和
配置指纹。

```json
{
  "protocol":"openai",
  "base_url":"https://embedding.example.com/v1",
  "api_key":"<secret>",
  "model_name":"text-embedding-3-small",
  "thinking_effort":null
}
```

成功返回脱敏后的配置对象（含 `embedding_dimension`、`fingerprint`、`verified_at`，不含
`api_key`）。已有持久向量时返回 `409 embedding_model_locked_by_vectors`；存在 queued/running
向量任务时返回 `409 embedding_jobs_active`，存在语义切分任务时返回
`409 semantic_chunking_jobs_active`。先删除该库所有文档的 vectors 才能重新设置。

### `PUT .../{knowledge_base_id}/llm-model`

请求结构同 embedding 配置，但 `protocol` 可为 `openai` 或 `anthropic`。服务端将验证基础调用及
图谱结构化输出能力。已有历史图谱不阻止改模型；queued/running 图谱任务会返回
`409 llm_jobs_active`。旧图谱保留创建时模型的配置指纹。

### `GET .../{knowledge_base_id}/models`

返回当前知识库的 embedding/LLM 配置列表，响应绝不包含 API key。

### 8.4 文档与处理状态

### `POST .../{knowledge_base_id}/documents`

批量上传。请求为 `multipart/form-data`，用同名字段重复传入文件：

```bash
curl -X POST "$BASE/workspaces/$WORKSPACE_ID/knowledge-bases/$KB_ID/documents" \
  -H "Authorization: Bearer $TOKEN" \
  -F 'files=@guide.pdf' -F 'files=@notes.md'
```

支持 PDF、Word、Excel、PPT 与 Markdown（扩展名见 capabilities）。每个文件独立校验；
语法合法的批量请求返回 `200`，并按文件报告 `uploaded` 或 `failed`。文件失败不会回滚其他
合法文件；只有文件数量非法才返回 `422 invalid_upload_file_count`。

上传结果包含 `uploaded`、`failed` 和逐文件 `items`；成功项含文档对象。上传不会创建任务、
解析数据或 Redis 缓存，因此四个阶段在成功上传后均为 `not_started`。文档对象包含原始名、
安全持久化名、唯一 storage key、当前文档的 parser/chunking 配置和四段独立处理状态：

```json
{
  "id":"<document-uuid>","original_filename":"guide.pdf","stored_filename":"<user>_<date>_guide.pdf",
  "storage_backend":"postgresql","storage_key":"<unique-key>","content_type":"application/pdf",
  "extension":".pdf","sha256":"<hex>","size_bytes":1234,"status":"active",
  "parsing_backend":null,
  "chunking_strategy":"fixed",
  "chunking_config":{"max_token_size":512,"overlap_token_size":64,"split_by_character":"\n\n"},
  "stages":{
    "parsing":{"status":"not_started","progress":0,"message":null,"error":null,"updated_at":"..."},
    "chunking":{"status":"not_started","progress":0,"message":null,"error":null,"updated_at":"..."},
    "vectorization":{"status":"not_started","progress":0,"message":null,"error":null,"updated_at":"..."},
    "graph":{"status":"not_started","progress":0,"message":null,"error":null,"updated_at":"..."}
  },"created_at":"...","updated_at":"..."
}
```

阶段 `status` 为 `not_started`、`queued`、`running`、`succeeded` 或 `failed`。`progress` 与
`message` 会随 worker 更新，失败原因写入 `error`。

失败项不含 `document`，而是包含 `error_code`、`message` 和 `retryable`，例如
`document_too_large`、`empty_document` 或 `unsupported_document_type:<extension>`。前两类可修正后
重传；不支持的扩展名不可由重试解决。

### `GET .../{knowledge_base_id}/documents`

返回 `{"items":[<文档对象>, ...]}`。

### `GET .../{knowledge_base_id}/documents/{document_id}`

返回一个文档对象。

### `PUT .../{knowledge_base_id}/documents/{document_id}/chunking-config`

设置该文档后续 chunking 使用的策略和参数。可以在上传后、parsing 前或 parsing 成功后调用；
chunking 已 queued、running 或 succeeded 时返回 `409 document_chunking_config_locked`；删除 chunks
后可重新设置。省略 `config` 时使用当前 env 默认值，提供的 `config` 与该策略的默认值合并；未知字段
或无效正则表达式返回 `422 invalid_chunking_config`。

```json
{
  "strategy":"fixed",
  "config":{"max_token_size":512,"overlap_token_size":64,"split_by_character":"\n\n"}
}
```

### `DELETE .../{knowledge_base_id}/documents/{document_id}`

删除原文档及其 blocks、chunks、向量、文档图谱、关联合并图谱和缓存，成功 `204`。

### `DELETE .../{knowledge_base_id}/documents/{document_id}/chunks`

删除 chunks 及全部派生 vectors/graphs/cache，并重置相关阶段，成功 `204`。这也是允许重新
chunking 的方式。

### `DELETE .../{knowledge_base_id}/documents/{document_id}/blocks`

删除 blocks 和所有下游 chunks、vectors、图谱及缓存，并将 parsing/chunking 状态重置；之后可重新提交 parsing。

### `DELETE .../{knowledge_base_id}/documents/{document_id}/vectors`

仅删除向量和向量缓存、重置向量阶段，成功 `204`；可随后重新向量化。

### `DELETE .../{knowledge_base_id}/documents/{document_id}/graph`

仅删除文档图谱、依赖它的合并图谱和图谱缓存、重置图谱阶段，成功 `204`；可随后重新提取。

任一对应阶段有运行中任务时，以上派生数据删除接口返回 `409 document_processing`。

### 8.5 文档处理队列

parsing 使用按文档指定处理后端的请求体：

```json
{"items":[{"document_id":"<uuid>","processor_backend":"default"}]}
```

其他三个提交接口使用 `document_ids` 为 1–100 个不重复 UUID：

```json
{"document_ids":["<document-uuid>","<document-uuid>"]}
```

| 接口 | 前置条件 | 成功响应 |
| --- | --- | --- |
| `POST .../{knowledge_base_id}/jobs/parsing` | 已上传且无 blocks；每项可选处理后端 | `202 {"job_ids":["<uuid>"]}` |
| `POST .../{knowledge_base_id}/jobs/chunking` | parsing 成功、blocks 非空；语义策略还需已验证 embedding | 同上 |
| `POST .../{knowledge_base_id}/jobs/vectorization` | chunking 成功、已验证 embedding | 同上 |
| `POST .../{knowledge_base_id}/jobs/graph-extraction` | chunking 成功、已验证 LLM | 同上 |

已 `queued`、`running` 或 `succeeded` 的同阶段文档不能重复提交；对应处理失败后可重新提交。
parsing/chunking 不使用缓存，失败时不发布 partial blocks/chunks。向量化和图谱提取会复用 Redis
中已完成 chunk 的中间结果并刷新 TTL，且只在全部 chunk 成功后才以单一事务写入最终数据；检索
不会看到部分完成的向量或图谱。

缺少前置条件时，常见错误为 `409 document_not_chunked`、`409 embedding_model_required`、
`409 llm_model_required` 或 `409 document_<stage>_already_processed`。

### `GET .../{knowledge_base_id}/jobs`

列出该知识库处理任务：

```json
{"items":[{
  "id":"<job-uuid>","document_id":"<uuid>","kind":"vectorization","status":"running",
  "attempt":2,"progress":{"current":3,"total":10},"message":"embedding chunk 3/10","error":null
}]}
```

`kind` 为 `parsing`、`chunking`、`vectorization` 或 `graph_extraction`。客户端可同时轮询该接口和文档
详情，以获取队列状态与用户可见的阶段状态。

### 8.6 检索

### `POST .../{knowledge_base_id}/retrieve`

只在当前知识库内检索；`document_ids` 可进一步过滤，但包含其他知识库文档会返回
`422 document_filter_outside_knowledge_base`。不会生成自然语言答案。

```json
{
  "query":"如何配置服务？",
  "mode":"hybrid",
  "document_ids":["<optional-document-uuid>"],
  "top_k":5,
  "vector_k":20,
  "bm25_k":20,
  "vector_weight":1.0,
  "bm25_weight":1.0,
  "rrf_k":60,
  "min_score":null
}
```

| `mode` | 参数 | 返回 |
| --- | --- | --- |
| `vector` | `top_k`、`candidate_k`、`min_score`、`document_ids` | 排序后的 chunks：`chunk_id`、`document_id`、`text`、`metadata`、`score`、`source:"vector"` |
| `hybrid` | `vector_k`、`bm25_k`、两路 `*_weight`、`rrf_k`、`top_k` | 向量/BM25 候选经带权 RRF 融合后的 chunks，`source:"hybrid"` |
| `graph` | `top_entities`、`max_hops`、`top_k`、`document_ids` | `nodes`、`edges` 与带 document/chunk 来源的 `evidence` |

`top_k` 为 1–100，候选数为 1–500，`max_hops` 为 0–3。vector/hybrid 需有 embedding
配置，否则返回 `409 embedding_model_required`。

### 8.7 图谱

### `GET .../{knowledge_base_id}/graphs`

返回图谱 artifact 列表：

```json
{"items":[{"id":"<graph-uuid>","name":"guide.pdf","kind":"document",
"source_document_id":"<uuid>","source_graph_ids":[],"status":"ready"}]}
```

`kind` 为 `document` 或 `merged`。文档图谱由 `graph-extraction` 任务生成。

### `GET .../{knowledge_base_id}/graphs/{graph_id}`

返回图谱详情：

```json
{"id":"<uuid>","name":"merged-guide","kind":"merged",
"nodes":[{"id":"<uuid>","name":"服务","entity_type":"component","description":"...","properties":{}}],
"edges":[{"id":"<uuid>","source_node_id":"<uuid>","relation":"depends_on","target_node_id":"<uuid>","description":"...","properties":{}}]}
```

### `POST .../{knowledge_base_id}/graphs:merge`

合并 2–100 个已就绪、同一知识库的图谱，源图谱保留。成功 `201`：

```json
{"name":"merged-guide","graph_ids":["<graph-uuid>","<graph-uuid>"]}
```

```json
{"id":"<merged-graph-uuid>","name":"merged-guide","kind":"merged","source_graph_ids":["<uuid>","<uuid>"]}
```

合并以 Unicode NFKC、case-fold、空白/标点规范化后的 `(entity_type, name)` 对齐实体；属性冲突
保留多值和所有来源，边按 `(source, relation, target)` 去重并合并证据。非本知识库或未就绪的
图谱返回 `422 graph_outside_knowledge_base`。

### `DELETE .../{knowledge_base_id}/graphs/{graph_id}`

删除图谱 artifact，成功 `204`。删除文档图谱会重置源文档图谱阶段，并删除所有依赖它的合并图谱。

### 8.8 维护 operation

### `GET /rag/operations/{operation_id}`

查询知识库复制或删除的异步操作；只允许创建该 operation 的当前用户读取。

```json
{
  "id":"<operation-uuid>","kind":"copy","status":"running","attempt":1,
  "message":"copying chunks","error":null,"payload":{}
}
```

`kind` 为 `copy` 或 `delete`；`status` 会随 worker 推进。找不到或非本人操作返回
`404 operation_not_found`。

## 9. 通用错误与兼容性

FastAPI 字段校验失败为 `422`，响应含 `detail` 数组；Gateway 自己显式产生的业务错误通常为 `{"detail":"<machine_code>"}`。当前 Gateway 未统一转换所有 Runtime HTTP 异常，因此上游目录/内部校验失败也可能表现为 `5xx`；客户端不应依赖该类失败的精确状态码，应记录机器码并以退避策略重试。

客户端应只依赖本文列出的 Gateway 路径、字段和 SSE 事件。Runtime 内部 NDJSON、动态 Docker 端口、`container_id`、Pi JSONL 文件位置与 Pi SDK 事件均是实现细节，可能随 Runtime 镜像升级而变化。
