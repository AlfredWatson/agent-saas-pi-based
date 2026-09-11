# Pi SaaS Platform API 文档

> 版本：基于当前仓库源码整理，日期：2026-09-11。
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

可使用仓库交互脚本验证此流程：

```bash
uv run python scripts/api_test_register_login_provider.py
uv run python scripts/api_test_login_session_chat.py
```

脚本默认使用 `.env` 的 `GATEWAY_HOST` 和 `GATEWAY_PORT`。可设置完整的
`GATEWAY_URL` 临时覆盖该地址。

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

RAG 资源位于 `/workspaces/{workspace_id}/knowledge-bases`，完整契约和调用顺序见
[`docs/rag.md`](rag.md)。主要接口包括知识库 CRUD/复制、模型验证配置、批量文档上传、
三类处理任务、向量/混合/图谱检索、图谱合并及维护 operation 查询。

所有 RAG 子资源都会同时验证当前用户、Workspace 与知识库。文档阶段状态可通过
文档列表/详情读取，队列进度可通过知识库的 `/jobs` 读取。模型 API key 只写不读。

## 9. 通用错误与兼容性

FastAPI 字段校验失败为 `422`，响应含 `detail` 数组；Gateway 自己显式产生的业务错误通常为 `{"detail":"<machine_code>"}`。当前 Gateway 未统一转换所有 Runtime HTTP 异常，因此上游目录/内部校验失败也可能表现为 `5xx`；客户端不应依赖该类失败的精确状态码，应记录机器码并以退避策略重试。

客户端应只依赖本文列出的 Gateway 路径、字段和 SSE 事件。Runtime 内部 NDJSON、动态 Docker 端口、`container_id`、Pi JSONL 文件位置与 Pi SDK 事件均是实现细节，可能随 Runtime 镜像升级而变化。
