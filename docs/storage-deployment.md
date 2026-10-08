# 存储后端统一部署

统一入口是 [`docker/storage-compose.yml`](../docker/storage-compose.yml)。以下命令除 Mac 镜像准备部分外，均在测试机的仓库根目录执行。默认启动全部 7 个存储容器；只需 PostgreSQL 向量时可以只启动 `postgres redis`。

Gateway 和 RAG worker 继续在宿主机运行，Agent Runtime 由 Gateway 按用户动态创建。Runtime 镜像的构建与导入见 [Runtime 镜像指南](runtime-image.md)。本测试机已导入 PostgreSQL 17 并完成统一 Compose 存储重建，Gateway/worker 已启动。实际数据库迁移、功能验收及运行端口见 [部署验收记录](storage-validation-20260930.md)。原 `docker/images/` 完整归档仍是 PostgreSQL 18 包，不能误当成 PostgreSQL 17 包。

## 服务与数据位置

| 服务 | 镜像 | 宿主机端口（仅 127.0.0.1） | 持久化目录（相对仓库根） | 用途 |
| --- | --- | --- | --- | --- |
| postgres | `pgvector/pgvector:pg17` | 5432 | `docker/volumes/postgres` | 账号、Workspace、会话投影、RAG 文件/blocks/chunks/图谱/任务及 PostgreSQL 向量 |
| redis | `redis:7.4-alpine` | 6379 | `docker/volumes/redis` | 带 ACL 和 AOF 的可恢复 RAG 中间缓存 |
| milvus | `milvusdb/milvus:v3.0.1` | 19530、19091（健康接口） | `docker/volumes/milvus/standalone` | Milvus 向量 |
| milvus-etcd | `quay.io/coreos/etcd:v3.5.25` | 无 | `docker/volumes/milvus/etcd` | Milvus 元数据 |
| milvus-minio | `quay.io/minio/minio:RELEASE.2024-12-18T13-15-44Z` | 无 | `docker/volumes/milvus/minio` | Milvus 对象数据 |
| chroma | `chromadb/chroma:1.5.9` | 18000 | `docker/volumes/chroma` | Chroma 向量 |
| qdrant | `qdrant/qdrant:v1.19.1` | 6333、6334 | `docker/volumes/qdrant` | Qdrant 向量 |

MinIO 仅供 Milvus 使用；当前应用的 `FILE_BASE` 和 `GRAPH_BASE` 都只支持 `p`，没有独立的应用文件 MinIO 或图数据库。全部数据目录被 Git 忽略。向量服务沿用旧 Compose 的目录，PostgreSQL 使用新目录；不能把旧 PostgreSQL 其他大版本的数据目录直接挂载到 PostgreSQL 17。

这些镜像版本沿用仓库既有向量服务配置。`pg17` 和 `7.4-alpine` 标签可能更新，交付时保留镜像 ID/摘要清单；两台机器使用同一份镜像归档。PostgreSQL 镜像内包含 pgvector，数据库扩展和应用表由 Gateway 的 Alembic 迁移创建，参见 [pgvector 官方说明](https://github.com/pgvector/pgvector#docker)。

## 本机 PostgreSQL 18 与目标 PostgreSQL 17

实测旧 PostgreSQL 容器是 `ragtools-pg`，使用 `gzdaniel/postgres-for-rag:pg18-age-pgvector`，不是本表默认的 PostgreSQL 17。本次测试数据可删除，按 [Mac 拉取 PostgreSQL 17 与测试容器重建指南](postgres17-offline.md) 补传 PG17 镜像后重建，无需数据迁移。该指南包含明确的旧容器/卷清理范围及新 Compose 验收命令。本机现已完成上述切换；该指南保留切换前容器信息及可复用的操作步骤。

## 已打包的本机镜像（生产无外网）

当前交付包位于 [`docker/images/`](../docker/images/README.md)，包含全部 7 个存储服务及 `pi-saas-agent-runtime:0.82.1-tools-20260930`，附校验文件、镜像 ID 清单和导入说明。原包只包含 PostgreSQL 18；对齐 PostgreSQL 17 时，还需按 [补充指南](postgres17-offline.md) 下载 PostgreSQL 17。其余存储镜像可复用原包。

注意：本机实际部署的是 PostgreSQL 18 镜像 `gzdaniel/postgres-for-rag:pg18-age-pgvector`；归档未包含上表默认 PostgreSQL 17。保留 PostgreSQL 18 部署时可按原交付包说明配置；本次目标改为 PostgreSQL 17，应设置 `POSTGRES_DOCKER_IMAGE=pgvector/pgvector:pg17`、`POSTGRES_DATA_TARGET=/var/lib/postgresql/data`，不要合并原包的 PG18 覆盖。保留数据的跨大版本部署需要逻辑备份/恢复；本次测试环境删除旧数据后初始化新库。

## 1. 配置

没有 `.env` 时再复制模板，已有文件直接编辑，保留密钥：

```bash
# 仅首次部署执行
cp -n .env.example .env
chmod 600 .env
```

设置真实的 `POSTGRES_USER`、`POSTGRES_PASSWORD`、`REDIS_PASSWORD` 和 Gateway 安全配置；连接项如下：

```dotenv
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DATABASE=pi_saas
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_USERNAME=admin
REDIS_DATABASE=0
FILE_BASE=p
GRAPH_BASE=p
VECTOR_BASE=pmcq
MILVUS_PORT=19530
MILVUS_HEALTH_PORT=19091
MILVUS_URI=http://127.0.0.1:19530
MILVUS_DATABASE=default
CHROMA_HOST=127.0.0.1
CHROMA_PORT=18000
CHROMA_SSL=false
QDRANT_HTTP_PORT=6333
QDRANT_URL=http://127.0.0.1:6333
QDRANT_GRPC_PORT=6334
```

`VECTOR_BASE=pmcq` 启用四个向量后端；只运行 PostgreSQL/Redis 时设为 `p`。Gateway/worker 会检查所有启用的向量后端。已有 `.env` 的 `CHROMA_PORT=8000` 应改为 `18000`，避免与 Gateway 冲突；若现有知识库在 Chroma 上，切换期间同时更新连接端口。

Compose 从 `.env` 读取密码和宿主机端口；`POSTGRES_HOST`、`MILVUS_URI`、`QDRANT_URL` 是应用连接地址，不会修改 Compose 网络。修改 `MILVUS_PORT`、`QDRANT_HTTP_PORT` 时同步更新对应 URL。密码包含 `$` 时在 `.env` 中用单引号包围，避免 Compose 插值。

## 2. 在 Mac 上准备离线镜像

存储服务使用上游镜像，无需本地构建。把当前仓库中的 `docker/storage-compose.yml`、`docker/redis/entrypoint.sh` 和 `.env.example` 带到 Mac，保持相对目录；不必复制测试机真实 `.env` 或数据。先在测试机确认 `uname -m`：`x86_64` 对应 `linux/amd64`，`aarch64` 对应 `linux/arm64`。下面针对当前交付方案使用 `linux/amd64`；Apple Silicon Mac 也按测试机架构拉取。

Mac 在仓库目录运行，需要 Docker Desktop 能访问镜像仓库：

```bash
set -euo pipefail
TARGET_PLATFORM=linux/amd64
docker compose --env-file .env.example -f docker/storage-compose.yml config --images \
  | sort -u > storage-images.txt
while IFS= read -r image; do
  docker pull --platform "$TARGET_PLATFORM" "$image"
done < storage-images.txt

# 逐个确认镜像架构并记录镜像 ID/摘要，随归档交付。
while IFS= read -r image; do
  docker image inspect "$image" \
    --format '{{.RepoTags}} {{.Os}}/{{.Architecture}} {{.Id}} {{json .RepoDigests}}'
done < storage-images.txt > storage-image-manifest.txt

# 当前镜像名都不含空格；读为数组，避免混入其他镜像。
image_args=()
while IFS= read -r image; do
  image_args+=("$image")
done < storage-images.txt
docker image save --platform "$TARGET_PLATFORM" "${image_args[@]}" \
  | gzip > pi-saas-storage-linux-amd64.tar.gz
shasum -a 256 pi-saas-storage-linux-amd64.tar.gz \
  > pi-saas-storage-linux-amd64.tar.gz.sha256
scp pi-saas-storage-linux-amd64.tar.gz \
  pi-saas-storage-linux-amd64.tar.gz.sha256 storage-images.txt storage-image-manifest.txt \
  USER@TEST_HOST:/tmp/
```

此段在 **bash** 中执行（Mac 默认交互 shell 可能是 zsh，可先运行 `bash`）。`docker image save --platform` 需要 Docker API 1.48+；旧版本请升级 Docker Desktop。该选项会只保存指定平台，见 [Docker 官方说明](https://docs.docker.com/reference/cli/docker/image/save/)。拉取或导出失败时停止，不要传输不完整归档。若更换为 ARM 测试机，先确认每个镜像支持 ARM，再同步修改平台和归档名称。

测试机导入，不执行 pull：

```bash
set -euo pipefail
(cd /tmp && sha256sum -c pi-saas-storage-linux-amd64.tar.gz.sha256)
gzip -dc /tmp/pi-saas-storage-linux-amd64.tar.gz | docker load
while IFS= read -r image; do
  docker image inspect "$image" \
    --format '{{.RepoTags}} {{.Os}}/{{.Architecture}} {{.Id}} {{json .RepoDigests}}'
done < /tmp/storage-images.txt
```

核对架构和镜像 ID 与 Mac 清单一致。保留此归档用于重新部署。

## 3. 从现有测试容器切换

先停止 Gateway 和 worker，防止迁移期间写入。检查现有容器及其数据挂载，不要只凭容器名判断：

```bash
docker ps -a --format 'table {{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Ports}}'
# 替换为上一步确认的数据库容器名，逐个检查。
docker inspect EXISTING_CONTAINER --format '{{json .Mounts}}'
```

旧向量 Compose 与新入口挂载同一组数据目录，必须先停止旧栈，不能让两个数据库进程同时打开同一目录：

```bash
docker compose -p pi-saas-vector-db -f docker/vector-db-compose.yml down
# Redis 若由旧开发入口启动，停止并移除该入口的 Redis。
docker compose --env-file .env -f infra/compose.dev.yml stop redis
docker compose --env-file .env -f infra/compose.dev.yml rm -f redis
```

以上旧栈命令仅用于确实由这些 Compose 项目管理的容器；若旧项目名不同，用实际项目名。其他现有 PostgreSQL/Redis 容器需要通过检查结果确认，停掉占用新栈端口的具体测试容器。这里不提供全局 prune 或删除所有容器命令。容器删除不会替你迁移数据。

PostgreSQL 可选择：

- 使用全新测试数据库：确认 `docker/volumes/postgres` 为空，新 PostgreSQL 自动初始化。旧账号、KB 和会话投影不会出现在新库中，需重新注册和配置。
- 保留现有数据库：先用 `pg_dump`/`pg_dumpall` 备份，在新库启动后恢复，再启动 Gateway；向量服务目录和 Redis 数据也需一起保留，才能保持 KB 引用一致。仅复用向量目录不能恢复 PostgreSQL 里的知识库记录。
- 继续使用现有 PostgreSQL：保留原 `POSTGRES_*` 连接，不启动新 Compose 的 `postgres`，用下面指定服务的命令。

已有 Redis、Milvus、Chroma、Qdrant 目录默认继续使用；若要完全清空测试数据，另行确认并处理这些目录。删除容器或 `compose down` 不会清空 bind mount。

## 4. 启动与检查

首次创建 Milvus standalone 目录时，需要可由容器 UID/GID 999 写入。仅对该服务目录设置权限；已有数据应先备份再调整：

```bash
mkdir -p docker/volumes/milvus/standalone
sudo chown -R 999:999 docker/volumes/milvus/standalone
# 只解析配置，不输出含密码的渲染配置。
docker compose --env-file .env -f docker/storage-compose.yml config --quiet
# 默认全部 7 个服务。缺少本地镜像时直接失败，不尝试访问镜像仓库。
docker compose --env-file .env -f docker/storage-compose.yml \
  up -d --pull never --wait --wait-timeout 180
docker compose --env-file .env -f docker/storage-compose.yml ps
```

只使用 PostgreSQL 向量（`VECTOR_BASE=p`）：

```bash
docker compose --env-file .env -f docker/storage-compose.yml \
  up -d --pull never --wait --wait-timeout 180 postgres redis
```

复用已有 PostgreSQL 并启用全部外部向量库：

```bash
docker compose --env-file .env -f docker/storage-compose.yml \
  up -d --pull never --wait --wait-timeout 180 redis milvus chroma qdrant
```

`milvus` 自动带起 etcd 和 MinIO。PostgreSQL、Redis、etcd、MinIO、Milvus 有容器健康检查；Qdrant/Chroma 的 `--wait` 只保证进程在运行，需再验证 HTTP 接口。`--pull never`、`--wait` 的行为见 [Compose 官方文档](https://docs.docker.com/reference/cli/docker/compose/up/)。

```bash
# 默认端口；自定义时替换为 .env 中对应值。绕过继承的宿主机代理。
curl --noproxy '*' --fail http://127.0.0.1:19091/healthz
curl --noproxy '*' --fail http://127.0.0.1:18000/api/v2/heartbeat
curl --noproxy '*' --fail http://127.0.0.1:6333/readyz
# 新 Compose PostgreSQL 的实际 SQL 连接与扩展文件检查。
docker compose --env-file .env -f docker/storage-compose.yml exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SELECT name, default_version FROM pg_available_extensions WHERE name = '\''vector'\'';"'
```

确认 SQL 查询返回 `vector` 行。任何服务退出或接口失败都应先看对应日志，再启动应用：

```bash
docker compose --env-file .env -f docker/storage-compose.yml logs --tail=100 milvus
uv sync
uv run python scripts/start_gateway.py
# 另一个终端，在 Gateway 迁移/前置检查成功后运行
uv run python scripts/start_rag_worker.py
```

Gateway 自动运行 Alembic；`pg_isready` 不能代替应用迁移或后端连通性检查。Gateway 与 worker 成功启动后，再验证注册、知识库处理和 Agent 的 `rag_search`。导入 Runtime 镜像、配置模型和 Runtime 到 Gateway 的可达地址仍是独立前置条件。

## 5. 重启、备份与故障排查

统一管理命令：

```bash
docker compose --env-file .env -f docker/storage-compose.yml restart
docker compose --env-file .env -f docker/storage-compose.yml logs --tail=100
docker compose --env-file .env -f docker/storage-compose.yml down
```

`down` 保留全部数据目录；重新 `up --pull never` 使用同一数据。PostgreSQL 的初始化用户/密码/数据库只对空目录生效，修改 `.env` 不会修改已有数据库密码；应在数据库中修改角色密码并同步配置。Redis ACL 在每次启动时重新生成，更换密码后需重启 Gateway/worker。

备份应覆盖 PostgreSQL、全部启用的向量后端、Redis 和 `.runtime-data`，并保存 `.env` 中的加密密钥及镜像清单。在线备份应使用各后端支持的导出/快照方式；直接复制数据库文件目录前应停止应用及全部存储容器，不要复制正在写入的数据文件。

端口冲突时先检查旧容器；Chroma 的宿主机 `18000` 与容器 `8000` 不同。Milvus 退出时检查 standalone 目录权限以及 etcd/MinIO 日志。PostgreSQL 提示数据库文件版本不兼容时，通过逻辑备份/恢复迁移，不直接覆盖数据目录。所有发布端口只绑定 loopback；这份 Compose 面向当前单机测试部署，向量库认证配置沿用现有测试环境，不应将其端口直接改为公网监听。
