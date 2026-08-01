# Pi SaaS Gateway 与 Agent Runtime 开发计划

## 1. 总体方案

第一阶段在宿主机运行：

```text
FastAPI Gateway :8000
    │ HTTP + NDJSON/SSE
    ▼
Node Agent Runtime :3000
    │ TypeScript SDK
    ▼
@earendil-works/pi-coding-agent 0.82.1
```

- Gateway：用户认证、PostgreSQL、Provider 密钥、Agent Profile、Skill、Session、Chat API。
- Agent Runtime：调用 Pi SDK，维护活动 `AgentSession`、Pi JSONL、工作区和流式事件。
- 本阶段不实现前端、Docker 动态编排和 MCP。
- 预留 `RuntimeLocator` 接口；当前固定指向 `http://127.0.0.1:3000`，后续替换为每用户容器定位。
- 自动化测试使用 Pi Faux Provider，不调用付费模型；真实 Provider 仅用于手动冒烟测试。

## 2. 项目结构

```text
pi-saas-platform/
├── gateway/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── app/
│   │   ├── main.py
│   │   ├── api/
│   │   │   ├── dependencies.py
│   │   │   └── v1/
│   │   │       ├── auth.py
│   │   │       ├── providers.py
│   │   │       ├── profiles.py
│   │   │       ├── skills.py
│   │   │       ├── workspaces.py
│   │   │       └── sessions.py
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── security.py
│   │   │   └── encryption.py
│   │   ├── db/
│   │   │   ├── bootstrap.py
│   │   │   ├── session.py
│   │   │   └── models.py
│   │   ├── repositories/
│   │   ├── services/
│   │   │   ├── auth.py
│   │   │   ├── provider.py
│   │   │   ├── skill.py
│   │   │   ├── runtime_locator.py
│   │   │   └── chat.py
│   │   └── clients/agent_runtime.py
│   ├── migrations/versions/
│   └── tests/{unit,integration,e2e}/
├── agent-runtime/
│   ├── package.json
│   ├── package-lock.json
│   ├── tsconfig.json
│   ├── src/
│   │   ├── server.ts
│   │   ├── config.ts
│   │   ├── api/
│   │   ├── auth/internal-auth.ts
│   │   ├── models/model-service.ts
│   │   ├── sessions/session-registry.ts
│   │   ├── sessions/session-factory.ts
│   │   ├── sessions/event-stream.ts
│   │   └── skills/skill-store.ts
│   └── test/
├── infra/
│   └── compose.dev.yml
├── scripts/
│   └── smoke_flow.py
├── .env.example
└── README.md
```

Gateway 使用 Python 3.10+、uv、FastAPI、SQLAlchemy 2 async、asyncpg、Alembic、Pydantic Settings、PyJWT、Argon2、cryptography 和 httpx。

Runtime 使用 Node 24、TypeScript、Fastify、TypeBox、Vitest、tsx，并将 `@earendil-works/pi-coding-agent` 固定为 `0.82.1`。全局安装的 `pi` 不作为 SDK 依赖来源。

## 3. 数据库与启动初始化

数据库名称使用 `pi_saas`，业务 schema 使用 `platform`。开发环境使用用户给出的 PostgreSQL 连接参数，但密码只写入未跟踪的 `.env`；`.env.example` 仅保留占位符。

宿主机开发使用 `POSTGRES_HOST=localhost`。Gateway 容器化后不能继续使用 `localhost`，Compose 中改为 PostgreSQL 服务名 `postgres`。

FastAPI lifespan 启动顺序：

1. 连接维护库 `postgres`。
2. 获取 PostgreSQL advisory lock。
3. 不存在时执行 `CREATE DATABASE pi_saas`。
4. 连接目标数据库并执行 `CREATE SCHEMA IF NOT EXISTS platform`。
5. 程序化执行 Alembic `upgrade head`。
6. 验证 Runtime 健康状态并启动聊天任务协调器。
7. 任一步失败则 Gateway 启动失败，不带着半初始化状态提供服务。

不使用 `Base.metadata.create_all()` 代替迁移。数据库角色必须具有本地开发环境的 `CREATEDB` 权限；测试使用独立的 `pi_saas_test`，不得清理或复用开发库。

### 表设计

- `users`
  - UUID 主键、规范化邮箱唯一索引、Argon2id 密码摘要、状态、创建/更新时间。
- `provider_bindings`
  - 用户、Provider ID、显示名、AES-256-GCM 密文、nonce、密钥版本、验证时间、状态。
  - 唯一约束：`user_id + provider_id + display_name`。
  - 加密 AAD 包含用户、binding 和 provider ID；API 永不返回密钥。
- `agent_profiles`
  - 用户、名称、Provider binding、model ID、thinking level、固定工具策略、创建/更新时间。
- `workspaces`
  - 用户、名称、随机 storage key、状态。
  - 不接受客户端传入宿主机绝对路径；Runtime 将 storage key 映射到安全根目录。
- `skills`
  - 用户、Skill 名称、描述、状态、当前版本。
- `skill_versions`
  - Skill、版本号、SHA-256、ZIP `BYTEA`、压缩及解压大小、文件数、解析后的 manifest、创建时间。
- `agent_profile_skills`
  - Profile 与具体 Skill version 的多对多绑定。
- `agent_sessions`
  - 用户、Profile、Workspace、Pi session ID、Pi session file key、状态、标题、错误和时间戳。
- `session_skill_versions`
  - 创建 Session 时冻结使用的 Skill 版本；Profile 后续修改不影响已有 Session。
- `agent_runs`
  - Session、用户消息、状态、idempotency key、开始/结束时间、错误和 token usage。
  - 部分唯一索引保证每个 Session 最多一个 `running` run。
- `chat_messages`
  - Session、run、角色、输入模式、顺序号、文本内容、状态和时间戳。
  - PostgreSQL 只保存用户消息和面向用户的助手最终文本投影，不保存逐个流式 delta、thinking 或完整工具结果。

所有 repository 查询必须同时包含 `user_id` 所有权条件。本阶段不启用 PostgreSQL RLS。

## 4. Gateway API 与行为

### 认证

- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `GET /api/v1/auth/me`

使用邮箱密码和 60 分钟 HS256 Bearer JWT，包含 `sub`、`iss`、`aud`、`iat`、`exp`、`jti`。第一阶段不实现 refresh token；过期后重新登录。注册时创建默认 Workspace。

### Provider 与模型

- `GET /api/v1/providers`：从 Runtime 的 Pi Provider catalog 获取 API-Key Provider。
- `POST /api/v1/provider-bindings`：接收 Provider、显示名、API Key；先让 Runtime 验证并返回模型，再加密入库。
- `GET /api/v1/provider-bindings`
- `GET /api/v1/provider-bindings/{id}/models`
- `DELETE /api/v1/provider-bindings/{id}`：有 Profile 引用时仅禁用，不物理删除。
- `POST /api/v1/agent-profiles`：校验 binding 和 model 后创建 Profile。
- `PUT /api/v1/agent-profiles/{id}`：修改只影响新 Session。

### Skill

- `POST /api/v1/skills`：multipart 上传 ZIP，单个压缩包最大 10 MiB。
- `GET /api/v1/skills`
- `GET /api/v1/skills/{id}`
- `DELETE /api/v1/skills/{id}`：软删除。
- `PUT /api/v1/agent-profiles/{id}/skills`：传入明确的 `skill_version_id` 列表。

ZIP 校验：

- 必须包含一个有效 `SKILL.md`，包含合法 `name` 和 `description`。
- 解压后最大 50 MiB、最多 500 个文件。
- 拒绝绝对路径、`..`、软链接、硬链接、设备文件、重复路径和 ZIP bomb。
- 保留脚本文件，但 Gateway 和 Runtime 不执行依赖安装或初始化脚本。
- Runtime 按 SHA-256 校验后解压，并通过 `DefaultResourceLoader.additionalSkillPaths` 加载。
- 本地无容器隔离阶段只使用可信测试 Skill；生产用户 Skill 必须运行在用户容器边界内。

### Session 和 Chat

- `POST /api/v1/sessions`
- `GET /api/v1/sessions`
- `GET /api/v1/sessions/{id}`
- `GET /api/v1/sessions/{id}/messages`
- `POST /api/v1/sessions/{id}/messages:stream`
- `POST /api/v1/sessions/{id}/abort`
- `POST /api/v1/sessions/{id}/steer`
- `POST /api/v1/sessions/{id}/follow-up`

Chat 规则：

- 同一 Session 普通 Chat 同时只能有一个；冲突返回 `409 session_busy`。
- `steer` 和 `follow-up` 只在已有活动 run 时接受，返回 `202`，并记录为该 run 的控制消息。
- 创建 run、用户消息和活动唯一约束在一个事务中完成。
- Gateway 启动后台消费任务，再将 SSE 订阅者接入该任务。
- 浏览器或 API 客户端断开 SSE 只移除订阅者，不中止 Agent；Gateway 继续消费 Runtime 事件并写入最终消息。
- `abort` 显式调用 Pi `session.abort()`。
- Gateway 进程崩溃不承诺继续事件投影；Pi JSONL 仍保留完整执行历史，重启后已完成 Session 可惰性恢复。启动时遗留 `running` run 标记为 `interrupted`。
- Session 首次访问或 Runtime 重启后，由 Gateway 使用数据库中的 Pi session reference 调用 Runtime ensure API，通过 `SessionManager.open()` 惰性恢复。

SSE 对外事件固定为：

- `message.accepted`
- `assistant.delta`
- `tool.started`
- `tool.completed`
- `message.completed`
- `message.failed`
- `done`

不向外暴露 thinking delta。收到 Pi `agent_settled` 后才发送 `done`，不能以 `agent_end` 作为终态。

## 5. Agent Runtime

Runtime 提供仅供 Gateway 调用的内部 API：

- `GET /internal/v1/health`
- `GET /internal/v1/providers`
- `POST /internal/v1/providers/validate`
- `PUT /internal/v1/sessions/{platform_session_id}`：幂等创建或恢复。
- `POST /internal/v1/sessions/{id}/chat`：返回 NDJSON Pi 事件流。
- `POST /internal/v1/sessions/{id}/abort`
- `POST /internal/v1/sessions/{id}/steer`
- `POST /internal/v1/sessions/{id}/follow-up`
- `GET /internal/v1/sessions/{id}/state`
- `PUT /internal/v1/skills/{skill_version_id}`：同步并校验 Skill ZIP。

所有接口校验 `Authorization: Bearer <RUNTIME_SHARED_SECRET>` 和 `X-Tenant-ID`。Runtime 只以每用户独立 Docker 容器运行，容器固定对应的租户并拒绝不匹配的请求；所有目录必须位于：

```text
RUNTIME_DATA_ROOT/
└── tenants/{tenant_uuid}/
    ├── agent/
    ├── workspaces/{workspace_key}/
    ├── sessions/
    └── skills/{skill_version_id}/
```

每个活动 Session 使用独立 `AgentSession`，由 `SessionRegistry` 管理：

- Provider Key 通过 Gateway 内部请求临时传入 `ModelRuntime.setRuntimeApiKey()`，只保存在内存。
- `SessionManager.create/open()` 持久化 Pi JSONL。
- Profile 的 Skill 版本在创建 Session 时同步并冻结。
- 同一 Session 的互斥在 Runtime 再次检查，避免绕过 Gateway。
- Runtime 输出原始但有版本号的 NDJSON Agent 事件；Gateway 负责映射成公开 SSE。
- Runtime 不接受任意文件系统路径、任意 Extension、MCP 配置或 npm 安装命令。

## 6. 测试与验收

### Gateway

使用 pytest、pytest-asyncio、httpx 和真实测试 PostgreSQL：

- 启动时自动创建测试数据库、schema，并执行全量 Alembic。
- 重复启动初始化幂等；两个初始化器并发时由 advisory lock 串行化。
- 注册、重复邮箱、密码校验、登录、JWT 过期与伪造。
- Provider Key 加密往返、AAD 不匹配失败、数据库中不存在明文。
- 用户 A 无法读取或修改用户 B 的 Provider、Profile、Skill、Workspace、Session。
- 无效 Provider、无效模型、失效 binding 和 Runtime 不可用的错误映射。
- Skill ZIP 路径穿越、软链接、超限、缺少 SKILL.md、重复版本及校验和。
- Session 活动唯一约束、普通 Chat 409、abort/steer/follow-up 状态。
- SSE 断开后后台任务继续，最终消息仍写入 PostgreSQL。
- Runtime 超时、流中断、错误事件和 `agent_settled` 终态。

### Agent Runtime

使用 Vitest 和 Pi Faux Provider：

- Provider catalog、API Key 注入和 model 解析。
- 创建 Session、文本 delta、工具事件、`agent_settled` 顺序。
- Session JSONL 持久化及进程内 registry 清空后的恢复。
- 每租户 Workspace、Session 和 Skill 路径隔离。
- 并发 Chat 拒绝、abort、steer、follow-up。
- Skill ZIP SHA-256、解压安全和 `additionalSkillPaths` 加载。
- 内部 token、tenant header 和 dedicated tenant 不匹配拒绝。

### 端到端

启动 `pi_saas_test`、Faux Runtime 和真实 Uvicorn，通过 `scripts/smoke_flow.py` 只调用 FastAPI：

1. 注册并取得 JWT。
2. 绑定测试 Provider。
3. 获取模型并创建 Agent Profile。
4. 上传测试 Skill 并绑定 Profile。
5. 创建 Session。
6. 发起 Chat，验证 SSE delta 和 `done`。
7. 查询 PostgreSQL 消息历史。
8. 模拟 SSE 断线，确认后台完成。
9. 重启 Runtime，确认 Session 可以恢复并继续 Chat。
10. 使用第二用户验证资源隔离。

可选手动冒烟测试使用真实 Provider API Key；默认测试和 CI 不读取真实密钥、不消费付费 token。

## 7. 默认约束

- 第一阶段单 Gateway 进程、单本地 Runtime 进程；不引入 Redis、任务队列或对象存储。
- Provider Key 使用环境变量中的 32 字节主密钥加密；密钥轮换通过 `key_version` 预留。
- Skill ZIP 存 PostgreSQL `BYTEA`，后续迁移到 S3/MinIO 时保持 API 和 SHA-256 不变。
- Profile 更新不热更新已有 Session；新配置只应用到新 Session。
- Pi JSONL 是 Agent 恢复与完整轨迹的权威来源，PostgreSQL 是 SaaS 用户、配置、运行状态和聊天展示投影的权威来源。
- 不实现 MCP、用户 Extension、任意 npm/git 安装、容器生命周期或前端。
