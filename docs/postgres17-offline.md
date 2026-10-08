# Mac 拉取 PostgreSQL 17 并对齐测试机 Compose

目标镜像为 `pgvector/pgvector:pg17`，与 [`docker/storage-compose.yml`](../docker/storage-compose.yml) 默认值一致。它包含 pgvector；当前应用只使用 PostgreSQL/pgvector，不依赖旧镜像里的 Apache AGE。不要拉取不含 pgvector 的普通 `postgres:17` 替代它，参见 [pgvector 官方 Docker 说明](https://github.com/pgvector/pgvector#docker)。

## 切换前实测情况（2026-09-30）

- 测试机架构：`x86_64`，镜像必须是 `linux/amd64`。
- 旧 PostgreSQL：容器 `ragtools-pg`，镜像 `gzdaniel/postgres-for-rag:pg18-age-pgvector`，宿主机端口 `15432`，具名卷 `ragtools-pgdata` 挂载到 `/var/lib/postgresql`。
- Redis：`infra-redis-1`；旧向量服务：`pi-saas-milvus`、`pi-saas-milvus-etcd`、`pi-saas-milvus-minio`、`pi-saas-chroma`、`pi-saas-qdrant`。
- 七个目标存储镜像中只缺少 `pgvector/pgvector:pg17`；原 `docker/images/pi-saas-linux-amd64-20260930.tar.gz` 内的 PostgreSQL 是 **18**，不能直接代替这次 PostgreSQL 17 交付。
- 以下操作已在本测试机执行：镜像已导入，旧测试容器和存储数据已删除，七个服务已由统一 Compose 重建。实际迁移及功能验证结果见 [部署验收记录](storage-validation-20260930.md)。命令保留用于同样的测试环境重建，不用于清理其他环境。

本次保留 PostgreSQL 对外端口 `15432`，由 `.env` 的 `POSTGRES_PORT` 控制；与 Compose 管理方式一致，无需强制使用模板端口 `5432`。Gateway/worker 改为连接新库，Chroma 的宿主机端口改为 `18000`。

## 1. Mac 拉取并打包

Mac 上打开终端，进入一个用于镜像交付的目录。Docker Desktop 需要能访问 Docker Hub；Apple Silicon Mac 仍拉取测试机所需的 `linux/amd64`。

```bash
set -euo pipefail
docker pull --platform linux/amd64 pgvector/pgvector:pg17
docker image inspect pgvector/pgvector:pg17 \
  --format '{{.Os}}/{{.Architecture}} {{.Id}} {{json .RepoDigests}}' \
  > postgres17-image-manifest.txt
cat postgres17-image-manifest.txt
# 应显示 linux/amd64。
docker image save --platform linux/amd64 pgvector/pgvector:pg17 \
  | gzip > postgres17-linux-amd64.tar.gz
shasum -a 256 postgres17-linux-amd64.tar.gz \
  > postgres17-linux-amd64.tar.gz.sha256
```

`docker image save --platform` 要求 Docker API 1.48+，旧版本应升级 Docker Desktop；指定平台可避免打包另一个架构，参见 [Docker 官方文档](https://docs.docker.com/reference/cli/docker/image/save/)。`pg17` 标签可能更新，因此保留归档及镜像 ID 清单，测试机核对同一个 ID。

传输三个文件，将 `USER@TEST_HOST` 换成自己的 SSH 目标：

```bash
scp postgres17-linux-amd64.tar.gz postgres17-linux-amd64.tar.gz.sha256 \
  postgres17-image-manifest.txt \
  USER@TEST_HOST:/home/haojifei/dev_projects/pi-saas-platform/docker/images/
```

## 2. 测试机校验、导入和前置检查

以下命令在测试机仓库根目录运行。若由 Codex 执行，可在文件传好后告知上述路径，由 Codex 继续执行切换。

```bash
cd /home/haojifei/dev_projects/pi-saas-platform
set -euo pipefail
(cd docker/images && sha256sum -c postgres17-linux-amd64.tar.gz.sha256)
gzip -dc docker/images/postgres17-linux-amd64.tar.gz | docker load
docker image inspect pgvector/pgvector:pg17 \
  --format '{{.Os}}/{{.Architecture}} {{.Id}}'
cat docker/images/postgres17-image-manifest.txt
# 核对 linux/amd64 和镜像 ID 一致，再继续。
```

此过程不访问镜像仓库。先确认全部目标镜像已存在，缺少任何一个都停止，补齐镜像后再删除旧容器：

```bash
set -euo pipefail
for image in \
  pgvector/pgvector:pg17 \
  redis:7.4-alpine \
  milvusdb/milvus:v3.0.1 \
  quay.io/coreos/etcd:v3.5.25 \
  quay.io/minio/minio:RELEASE.2024-12-18T13-15-44Z \
  chromadb/chroma:1.5.9 \
  qdrant/qdrant:v1.19.1; do
  docker image inspect "$image" --format '{{.RepoTags}} {{.Os}}/{{.Architecture}}'
done
```

## 3. 停止应用并修改连接配置

先停止当前仓库的 Gateway 和 RAG worker；若由 systemd/supervisor 或 `--reload` 管理，应停止管理器，避免它自动重启进程。停止期间不再接收 Agent 请求。

编辑仓库根目录 `.env`，保留真实 `POSTGRES_USER`、`POSTGRES_PASSWORD`、Redis 密码及 Gateway 密钥，仅合并下列配置：

```dotenv
POSTGRES_DOCKER_IMAGE=pgvector/pgvector:pg17
POSTGRES_DATA_TARGET=/var/lib/postgresql/data
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=15432
POSTGRES_DATABASE=pi_saas
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
CHROMA_HOST=127.0.0.1
CHROMA_PORT=18000
VECTOR_BASE=pmcq
FILE_BASE=p
GRAPH_BASE=p
```

不要把旧交付包 `deployment.env` 里的 PostgreSQL 18 镜像和挂载目标合并回来。PostgreSQL 17 的数据目录与 PostgreSQL 18 不同；此处使用全新的空目录，不将旧卷挂回新容器。

```bash
docker compose --env-file .env -f docker/storage-compose.yml config --quiet
# 确认结果包含 pgvector/pgvector:pg17，没有 PG18 覆盖。
docker compose --env-file .env -f docker/storage-compose.yml config --images
```

## 4. 删除本项目旧测试容器和存储数据

以下操作只适用于前文已核对的测试容器，删除范围限定到本项目。先列出它们及挂载，再执行；不要清理模型容器、其他项目数据库或整台机器的 Docker 资源。

```bash
docker inspect ragtools-pg infra-redis-1 pi-saas-milvus \
  pi-saas-milvus-etcd pi-saas-milvus-minio pi-saas-chroma pi-saas-qdrant \
  --format '{{.Name}} {{.Config.Image}} {{json .Mounts}}'
# 若之前已用新入口创建过存储容器，先停止新入口。
docker compose --env-file .env -f docker/storage-compose.yml down
# 移除旧存储容器；-v 一并清除容器附带的匿名卷，不删除具名卷。
docker rm -f -v ragtools-pg infra-redis-1 pi-saas-milvus \
  pi-saas-milvus-etcd pi-saas-milvus-minio pi-saas-chroma pi-saas-qdrant
# 只删除已确认属于旧 PostgreSQL 的具名测试卷。
docker volume rm ragtools-pgdata
```

旧数据库里的 RuntimeInstance 记录将消失，删除本项目受管 Runtime 容器，之后 Gateway 按新账号创建。宿主机 `.runtime-data` 不在此删除步骤内；其中旧文件不会自动绑定到新账号。

```bash
docker ps -aq --filter label=com.pi-saas.managed=true \
  | while IFS= read -r runtime_id; do
      docker rm -f "$runtime_id"
    done
```

清空本项目存储目录，避免新 PostgreSQL 记录与旧向量集合、缓存混用。先确认没有其他容器挂载这些目录；如果有，停止该步骤并核对用途。

```bash
cd /home/haojifei/dev_projects/pi-saas-platform
# 当前路径必须是上面的仓库根目录。仅删除指定的存储数据目录。
sudo rm -rf -- docker/volumes/postgres docker/volumes/redis \
  docker/volumes/milvus docker/volumes/chroma docker/volumes/qdrant
mkdir -p docker/volumes/milvus/standalone
sudo chown 999:999 docker/volumes/milvus/standalone
```

## 5. 用统一 Compose 重建并验收

```bash
docker compose --env-file .env -f docker/storage-compose.yml \
  up -d --pull never --wait --wait-timeout 180
docker compose --env-file .env -f docker/storage-compose.yml ps
# 确认镜像和绑定：pgvector/pgvector:pg17，127.0.0.1:15432 -> 5432。
docker compose --env-file .env -f docker/storage-compose.yml ps -q postgres \
  | xargs docker inspect --format '{{.Config.Image}} {{json .HostConfig.PortBindings}}'
# 实际 SQL 版本和 pgvector 扩展验证。
docker compose --env-file .env -f docker/storage-compose.yml exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SELECT version(); CREATE EXTENSION IF NOT EXISTS vector; SELECT extname, extversion FROM pg_extension WHERE extname = '\''vector'\'';"'
curl --noproxy '*' --fail http://127.0.0.1:19091/healthz
curl --noproxy '*' --fail http://127.0.0.1:18000/api/v2/heartbeat
curl --noproxy '*' --fail http://127.0.0.1:6333/readyz
```

确认 SQL 显示 PostgreSQL 17，且存在 `vector` 扩展；七个容器都由 `pi-saas-storage` 项目管理。`--wait` 对没有健康检查的 Chroma/Qdrant 只检查进程运行，所以仍需上述 HTTP 验证。

```bash
uv run python scripts/start_gateway.py
# Gateway 自动执行 Alembic；确认迁移和启动成功后，在另一个终端启动 worker。
uv run python scripts/start_rag_worker.py
```

最后重新注册测试账号，配置模型与知识库，再验证文档处理和 Agent 会话。数据库和向量服务检查通过只证明基础存储可用，不代表模型或 Agent 端到端测试已经完成。

## 6. 后续交付包

旧交付包及其 ID/校验清单保留原样，作为 PostgreSQL 18 包的历史记录。目标部署使用原包中的其余镜像，加本次 PostgreSQL 17 补充包；即使导入了旧 PG18 镜像，只要 `.env` 保持上述 PG17 配置，新 Compose 不会使用它。

后续重新制作完整包时，镜像清单应包含 `pgvector/pgvector:pg17` 并删除旧 PostgreSQL 18 条目，同时重新生成归档、SHA-256、镜像 ID 清单和对应的配置示例；不能只改旧包清单而不重新导出归档。
