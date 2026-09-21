# Runtime 镜像交付（Docker-only）

Gateway 不会自动构建或拉取 Runtime 镜像。请在可访问 npm registry 的 `linux/amd64`
机器，或安装 Docker Desktop 的 macOS 机器上，从仓库根目录执行：

```bash
docker buildx build --platform linux/amd64 \
  -f agent-runtime/Dockerfile \
  -t pi-saas-agent-runtime:0.82.1-dev \
  --load agent-runtime
docker save pi-saas-agent-runtime:0.82.1-dev | gzip > pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz
sha256sum pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz > pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz.sha256
```

将两个文件复制到 Gateway 宿主机后，先校验再导入：

```bash
sha256sum -c pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz.sha256
gzip -dc pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz | docker load
docker image inspect pi-saas-agent-runtime:0.82.1-dev --format '{{.Os}}/{{.Architecture}}'
```

Gateway 仅以 Docker Runtime 方式运行 Agent：它为每个用户创建一个仅绑定到宿主机 loopback 的容器。镜像固定内部监听地址、端口与数据根；Gateway 创建容器时只注入该用户的租户和内部鉴权信息。这些容器内部变量不应写入 `.env`；配置文件只提供 Gateway、PostgreSQL 与 Docker 编排参数。

镜像包含 Node、Pi SDK 和工具依赖；运行时将本仓库的 `agent-runtime/src` 只读挂载到 `/opt/pi-runtime/src`。因此修改 TypeScript 源码后只需调用 `POST /api/v1/runtime:recreate`，不需要重新构建镜像。只有锁文件、系统工具或基础镜像变更才需要重新制作和导入镜像。`createAgentSession()` 不覆盖 Pi 的工具列表，因此容器使用 Pi 默认的 `read`、`write`、`edit` 与 `bash`。

## Runtime RAG 工具

当 `.env` 设置 `RUNTIME_GATEWAY_BASE_URL` 后，Gateway 会为每个新建 Runtime 容器生成
独立的 `RUNTIME_RAG_SHARED_SECRET`，仅在容器环境中注入并在 Gateway 保存摘要。Runtime
用它访问 `/internal/v1/runtime-rag/retrieve`，该接口不属于公共 API。不要手工设置、复用或
记录这个密钥；Docker daemon/宿主管理员可以通过容器元数据读取它，这是该部署模式的信任边界。

本地 Linux Docker 的最小配置示例：

```dotenv
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=21995
RUNTIME_GATEWAY_BASE_URL=http://host.docker.internal:21995
```

Gateway 会为 Runtime 增加 `host.docker.internal:host-gateway` 映射。生产环境应将
`RUNTIME_GATEWAY_BASE_URL` 指向受防火墙限制的内网 Gateway 地址；不要把该内部路径暴露给
不受信任网络。更新到包含 RAG 工具的版本后，旧容器没有新的独立密钥，必须在无运行中任务时
调用 `POST /api/v1/runtime:recreate`。本次功能只修改 `agent-runtime/src`，使用现有
`0.82.1-dev` 镜像时无需重新构建；若以后变更 `package-lock.json`、Dockerfile 或基础镜像，
可在 Mac 上按本文 `buildx --platform linux/amd64` 命令构建、`docker save` 后传输并导入。

在 Gateway、RAG worker、Redis、向量/Embedding 服务与 Runtime 容器均就绪后，可运行完整
公开 HTTP 验收（默认清理 Workspace、知识库、Profile 和 Binding）：

```bash
uv run python test/agent_rag_user_flow.py --report /tmp/agent-rag-user-flow.json
```

它需要既有的 `AGENT_TEST_*` 和 `RAG_TEST_*` 配置；其中 Agent 与 RAG 测试账号应为同一
用户，确保新建 Session 与知识库位于同一 Workspace。`--keep-resources` 仅用于失败诊断。

如果 Docker 日志出现 `EACCES: permission denied, open '/opt/pi-runtime/package.json'`，说明镜像是在源文件为 `0600` 时构建的旧版本。更新 Dockerfile 后重新构建并导入镜像，再调用 `POST /api/v1/runtime:recreate`；不要通过让用户 Runtime 以 root 身份运行来绕过该问题。

已导入旧镜像、但当前机器无法联网时，可使用 `Dockerfile.permission-fix` 在本机生成一个只增加权限修复层的替代镜像：

```bash
docker build --network none \
  -f agent-runtime/Dockerfile.permission-fix \
  -t pi-saas-agent-runtime:0.82.1-dev \
  agent-runtime
```
