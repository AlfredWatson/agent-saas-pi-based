# Pi SaaS Platform 部署报告

> 报告日期：2026-08-03
>
> 依据：当前仓库的 Gateway、Runtime、Docker、Alembic、配置与测试脚本源码。
>
> 验证等级：**静态核对**。本次未启动 PostgreSQL、Uvicorn、Docker Runtime 或真实 Provider；本文不将部署写成已在当前主机完成的事实。

## 1. 结论

当前仓库实现的是“宿主机 Gateway + 宿主机 PostgreSQL Compose 服务 + 按用户懒创建 Docker Runtime”，不是把 Gateway 放入 Compose，也不是一个共享 Runtime 服务承载所有用户。

已具备的源码部署要素：

- FastAPI Gateway 启动时执行 Alembic、确保 `platform` schema、把残留 `running` Run 标记为 `interrupted`，并在接受请求前检查 Docker Runtime 前置条件。
- 开发 Compose 使用 `postgres:17-alpine`，数据通过具名卷 `postgres-data` 持久化，端口映射为宿主机 `5432`。
- Gateway 使用 Docker API 为每位用户创建只绑定 `127.0.0.1` 的 Runtime；租户数据在 `.runtime-data/tenants/<user-id>`。
- Runtime 采用只读根文件系统、tmpfs、全 capability drop、`no-new-privileges` 与内存/CPU/PID 限制；源码只读挂载。

上线前仍须在现场完成：Docker socket 权限、目标架构镜像导入、数据库连接与迁移、Runtime 健康检查、Faux 端到端 SSE、重建后的会话恢复，以及 TLS/备份/监控。

## 2. 当前拓扑和责任边界

```text
互联网客户端
     │ HTTPS（建议反向代理终结 TLS）
     ▼
宿主机 FastAPI Gateway :8000
     ├──── localhost:5432 ──── PostgreSQL 17（Compose）
     │        └── platform schema / postgres-data volume
     │
     └──── Docker API ──── 每用户 Runtime 容器
                              ├── 127.0.0.1:<动态端口> → :3000
                              ├── agent-runtime/src → /opt/pi-runtime/src:ro
                              └── .runtime-data/tenants/<user>
                                     → /runtime-data:rw
                                            │
                                            └── 外部 LLM Provider API
```

| 组件          | 启动方式                       | 责任                                                     | 持久化               |
| ------------- | ------------------------------ | -------------------------------------------------------- | -------------------- |
| Gateway       | 宿主机`uvicorn`              | JWT、租户权限、密钥加密、API/SSE、公共投影、Runtime 编排 | PostgreSQL           |
| PostgreSQL    | `infra/compose.dev.yml`      | `platform` schema 业务数据与 Alembic 状态              | `postgres-data` 卷 |
| Agent Runtime | Gateway 按用户 Docker API 创建 | Pi SDK、活动会话、Pi JSONL、租户工作区、工具执行         | 租户目录挂载         |
| 外部 Provider | 外部服务                       | 模型推理                                                 | 不在系统内           |

Provider 密钥仅在 Chat 时由 Gateway 解密，经内部 loopback 调用交给 Runtime，通过 `ModelRuntime.setRuntimeApiKey()` 置于 Runtime 内存凭据存储。Runtime 内部接口同时要求 `Authorization: Bearer <RUNTIME_SHARED_SECRET>` 与 `X-Tenant-ID`，不能作为客户端 API 使用。

## 3. 配置基线

从模板创建受保护的配置文件：

```bash
cp .env.example .env
```

生产环境设定 `ENVIRONMENT=production`，并替换全部占位值：

| 变量                                 | 用途                            | 生产要求                                          |
| ------------------------------------ | ------------------------------- | ------------------------------------------------- |
| `JWT_SECRET`                       | JWT HS256 签名                  | 独立的高熵随机值                                  |
| `ENCRYPTION_KEY`                   | Provider 密钥 AEAD 加密         | 32 字节 URL-safe Base64 密钥                      |
| `RUNTIME_SHARED_SECRET`            | Gateway ↔ Runtime 内部 Bearer  | 与 JWT/Provider 密钥均不同的高熵值                |
| `POSTGRES_PASSWORD`                | PostgreSQL 密码                 | 非空且由密钥系统托管                              |
| `WORKSPACE_MAX_PER_USER`           | 每用户 Workspace 数量上限       | `unlimited` 或不小于 1 的整数；包含 `default` |
| `WORKSPACE_STORAGE_LIMIT_MB`       | 每用户所有 workspace 文件软上限 | 默认 1024 MB；达到上限后拒绝后续创建和 Chat       |
| `WORKSPACE_FILE_MAX_MB`            | 单个 Workspace 文件上传硬上限   | 默认 100 MB；超出时上传返回`413 file_too_large` |
| `POSTGRES_HOST/PORT/USER/DATABASE` | Gateway 数据库连接              | 默认是`localhost:5432`，须按目标环境调整        |
| `RUNTIME_DOCKER_IMAGE`             | Runtime 镜像                    | 必须已导入目标 Docker daemon                      |
| `RUNTIME_DOCKER_NETWORK`           | Runtime bridge 网络             | Gateway 有权限创建和使用                          |
| `RUNTIME_SOURCE_DIR`               | Runtime 源码只读挂载源          | 必须存在的实际路径                                |
| `RUNTIME_DATA_HOST_ROOT`           | 租户数据根目录                  | 持久化磁盘，纳入备份和权限控制                    |
| `RUNTIME_*_LIMIT`                  | 启停和资源限制                  | 按节点容量和配额设置                              |

生产模式下，Gateway 会拒绝占位 JWT/Runtime secret/加密密钥及空数据库密码。开发模式允许模板占位值，仅限本地开发，不能对外暴露。

修改 `WORKSPACE_STORAGE_LIMIT_MB` 或 `WORKSPACE_FILE_MAX_MB` 后，重启 Gateway，并对已存在的用户 Runtime 调用 `POST /api/v1/runtime:recreate`，使容器获得新的环境变量；本功能只改 Runtime 源码挂载内容和环境变量，不需要重建镜像。

## 4. 推荐部署步骤

### 4.1 准备宿主机

目标主机需要 Docker daemon、Docker CLI/Compose、Python/`uv` 和可运行 Node 24 Runtime 镜像的 Docker 环境。运行 Gateway 的服务账号必须能连接 Docker socket；此权限接近宿主机高权限，应使用专用服务账号、主机隔离、审计与最小权限控制。

Gateway 用户应能读源码和受保护的 `.env`、写租户数据根、访问 Docker daemon。**不要**把 Docker socket 传入用户 Runtime 容器。

### 4.2 交付 Runtime 镜像

Gateway 不会自动 build 或 pull Runtime 镜像。可联网的 `linux/amd64` 构建机：

```bash
docker buildx build --platform linux/amd64 \
  -f agent-runtime/Dockerfile \
  -t pi-saas-agent-runtime:0.82.1-dev \
  --load agent-runtime
docker save pi-saas-agent-runtime:0.82.1-dev | gzip > pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz
sha256sum pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz > pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz.sha256
```

将归档和校验文件带到 Gateway 主机后：

```bash
sha256sum -c pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz.sha256
gzip -dc pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz | docker load
docker image inspect pi-saas-agent-runtime:0.82.1-dev --format '{{.Os}}/{{.Architecture}}'
```

确认架构与目标节点匹配。只修改 `agent-runtime/src` 后，调用 `POST /api/v1/runtime:recreate` 可加载新挂载源码；只有锁文件、系统工具或基础镜像变更才需要重新构建/导入镜像。

### 4.3 启动 PostgreSQL

先在 `.env` 中设置 `POSTGRES_PASSWORD`：

```bash
docker compose -f infra/compose.dev.yml up -d
docker compose -f infra/compose.dev.yml ps
```

该 Compose 只启动 PostgreSQL；Gateway 在宿主机运行。当前开发配置将 5432 映射到宿主机所有接口，生产建议移除此映射或用私网/防火墙限制到仅 Gateway 可达。

### 4.4 启动 Gateway

```bash
uv sync
uv run uvicorn gateway.app.main:app --host 127.0.0.1 --port 8000
```

启动顺序为：记录脱敏后的连接信息 → Alembic 升级到 `head` → 确保 `platform` schema → 终止崩溃遗留的 Run → 检查源码目录、数据根、Docker daemon、镜像及网络。任一步失败都应令启动失败，避免提供半可用 API。

生产应通过 systemd 或等价 supervisor 维持 Gateway，并只允许反向代理访问它。反向代理应处理 TLS、速率/请求体限制、访问日志脱敏和必要的 IP 控制；不要直接暴露 Uvicorn 或 PostgreSQL。

### 4.5 验收

先检查 Runtime 基础条件：

```bash
docker image inspect "$RUNTIME_DOCKER_IMAGE"
docker network inspect "$RUNTIME_DOCKER_NETWORK"
curl --noproxy '*' http://127.0.0.1:8000/openapi.json >/dev/null
```

再运行不产生真实费用的 Faux 全流程：

```bash
SMOKE_PROVIDER=faux uv run python scripts/smoke_flow.py
```

脚本覆盖注册、登录、Binding、模型目录、Profile、Session、SSE 文本、消息历史字段/顺序和客户端断开后后台持续写入。只有显式设置 `SMOKE_PROVIDER_API_KEY` 才允许真实 Provider；可通过 `SMOKE_RUNTIME_RESTART_COMMAND` 将 Runtime 重启恢复加入 smoke。

## 5. Runtime 隔离与数据流

首次需要 Runtime 时，Gateway 创建名为 `pi-saas-runtime-<无连字符用户UUID>` 的容器，约束如下：

- `3000/tcp` 仅发布为 `127.0.0.1:<随机端口>`，不对局域网或互联网公开。
- `agent-runtime/src` 以只读方式挂到 `/opt/pi-runtime/src`。
- `.runtime-data/tenants/<user-id>` 以读写方式挂到容器固定的 `/runtime-data`，存放 workspace、Pi agent 数据与 JSONL。
- 根文件系统只读；`/tmp` 是 256 MiB、`nosuid,nodev` 的 tmpfs。
- 丢弃所有 Linux capabilities、设置 `no-new-privileges`、使用 Gateway 宿主 UID/GID；默认限制为 1 GiB 内存、1 CPU、256 PIDs。
- 添加 `com.pi-saas.managed=true` 与租户 ID Docker 标签，同一用户只存在一个 `runtime_instances` 记录。

这些是容器级限制，不等于完整多租户安全边界。Runtime 默认保留 Pi 的 `read`、`write`、`edit`、`bash` 工具；生产仍应补足独立节点/主机、网络出口策略、镜像供应链、cgroup 配额、日志与密钥管理。不能把 `cwd` 限制当作安全沙箱。

## 6. 数据、迁移、备份与恢复

业务表位于 PostgreSQL 的 `platform` schema：用户、Provider bindings、Profiles、Workspaces、Sessions、Runs、Chat messages 与 Runtime instances。迁移由 `gateway/migrations/` 的 Alembic 版本控制；不要用 `Base.metadata.create_all()` 替代迁移。

备份必须覆盖：

1. PostgreSQL 数据库和 `postgres-data` 卷；密文 Binding 必须有对应 `ENCRYPTION_KEY` 才能解密。
2. `.runtime-data/tenants/`；其中有用户 workspace、Pi session 文件和 JSONL 轨迹。
3. 由密钥管理系统保护的 `ENCRYPTION_KEY`、JWT secret、Runtime shared secret。
4. Runtime 镜像归档、SHA-256 校验文件及对应源代码/锁文件。

恢复顺序：恢复数据库和密钥材料 → 校验/导入 Runtime 镜像 → 恢复租户目录 → 启动 PostgreSQL 和 Gateway。Gateway 会把未完成 Run 标为 `interrupted`；这不等于自动恢复未完成推理。容器重建后内存中的活动 Pi 会话也不可视作已恢复，应作为现场验收项目。

## 7. 运维、观察与故障处理

| 情况                    | 观察方式                                      | 处理方向                                                |
| ----------------------- | --------------------------------------------- | ------------------------------------------------------- |
| Gateway 无法启动        | Uvicorn 日志、`platform` 迁移状态           | 检查数据库、迁移、Docker socket、源目录、镜像和网络。   |
| `runtime_unavailable` | `GET /api/v1/runtime`、Docker daemon 日志   | 检查镜像、Docker 权限/daemon、Runtime 网络和健康检查。  |
| `runtime_busy`        | Session/Run 记录、SSE                         | 等 Run 完成或先`abort`，再 stop/recreate。            |
| 模型调用失败            | SSE`message.failed`、Gateway/Runtime 日志   | 核对 Binding、Provider 凭据、出网/DNS、模型 ID、资源。  |
| 历史缺内容              | `GET /sessions/{id}/messages`               | 对照`sequence`、`message_end` 和 Runtime JSONL。    |
| 容器异常                | `docker ps -a`、`docker logs <container>` | 排障后用受控 Runtime recreate，避免不可追溯的手工修改。 |

日志不得记录 Provider API key、JWT、`RUNTIME_SHARED_SECRET`、解密后的凭据或完整未处理工具载荷。公开 SSE 与数据库投影实施了秘密值脱敏与载荷截断；反向代理、APM 和集中日志仍要独立配置脱敏。

## 8. 当前验证证据与上线门禁

| 项目                                     | 当前证据                                              | 结论           |
| ---------------------------------------- | ----------------------------------------------------- | -------------- |
| API 路由、请求体、SSE 事件               | 核对`gateway/app/api/v1/` 与 Gateway Runtime 客户端 | 静态已核对     |
| 启动/迁移顺序                            | 核对`gateway/app/main.py`、Alembic/模型             | 静态已核对     |
| Docker 限制、网络、挂载                  | 核对`runtime_locator.py`、Dockerfile、环境模板      | 静态已核对     |
| 镜像构建/离线导入                        | 核对`docs/runtime-image.md`                         | 静态已核对     |
| PostgreSQL + Uvicorn + Docker + Faux SSE | 本次未启动服务                                        | 待现场验收     |
| 真实 Provider 与`verified_at`          | 本次未使用真实凭据                                    | 待受控环境验收 |
| Runtime 重建后的会话继续                 | 脚本支持可选检查，本次未执行                          | 待现场验收     |
| TLS、反向代理、备份恢复、告警            | 仓库未提供生产编排/监控配置                           | 目标环境补齐   |

建议将以下全部作为正式上线门禁：Faux smoke 通过、镜像架构匹配、迁移成功、Runtime 生命周期、SSE 断线持续执行、备份恢复演练、TLS/反向代理和密钥轮换。完成后，应将本报告的“待现场验收”替换为带日期、环境和证据的实测结论。
