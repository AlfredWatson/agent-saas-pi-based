# 离线镜像交付包

2026-09-30 从本测试机器的本地 Docker 导出，共 8 个 `linux/amd64` 镜像；不含容器数据和真实 `.env`。生产机无需连接镜像仓库。镜像清单见 `images.txt`，原始镜像 ID/摘要见 `image-manifest.txt`。

| 文件 | 用途 |
| --- | --- |
| `pi-saas-linux-amd64-20260930.tar.gz` | 7 个存储服务镜像和新版 Agent Runtime |
| `SHA256SUMS` | 镜像归档的 SHA-256 校验 |
| `images.txt` | 导出的 8 个镜像标签 |
| `image-manifest.txt` | 架构、镜像 ID、仓库摘要 |
| `archive-manifest.json` | 实际归档的 Docker 清单，用于核对 8 个标签 |
| `deployment.env` | 此归档匹配的镜像和 PostgreSQL 挂载选择，无密钥 |

归档文件被 Git 忽略。向生产机器交付时必须复制整个 `docker/images/`，不能仅靠 `git clone` 获得二进制镜像归档；同时交付仓库源码、Compose、Redis 入口脚本和受保护的应用配置。

## 导入

在生产机器仓库根目录执行，要求 Docker 支持目标架构 `linux/amd64`：

```bash
set -euo pipefail
(cd docker/images && sha256sum -c SHA256SUMS)
gzip -dc docker/images/pi-saas-linux-amd64-20260930.tar.gz | docker load
while IFS= read -r image; do
  docker image inspect "$image" \
    --format '{{json .RepoTags}} {{.Os}}/{{.Architecture}} {{.Id}} {{json .RepoDigests}}'
done < docker/images/images.txt
```

比对输出中架构和镜像 ID 与 `image-manifest.txt` 一致；以镜像 ID 为主要依据，Docker 导出/导入后仓库摘要显示可能不同。`docker load` 会从本地归档恢复镜像和标签，见 [Docker 官方说明](https://docs.docker.com/reference/cli/docker/image/load/)。

## PostgreSQL 17 对齐说明

此归档实际包含 PostgreSQL 18，归档及清单保持原样。当前目标部署改为 PostgreSQL 17，须增加 [Mac 拉取 PostgreSQL 17 补充包](../../docs/postgres17-offline.md)，使用新指南里的 PG17 配置。下面 PG18 配置仅适用于继续使用原归档的 PG18 部署，不能合并到本次 PG17 环境。

## 配置与启动（原 PostgreSQL 18 包）

**本机没有 `pgvector/pgvector:pg17`。此次交付的是现有数据库正在使用的 PostgreSQL 18 镜像 `gzdaniel/postgres-for-rag:pg18-age-pgvector`。** 主 Compose 仍默认 PostgreSQL 17，因此必须把 `deployment.env` 中三个变量合并到生产 `.env`（不要覆盖其他配置或密钥）：

```dotenv
POSTGRES_DOCKER_IMAGE=gzdaniel/postgres-for-rag:pg18-age-pgvector
POSTGRES_DATA_TARGET=/var/lib/postgresql
RUNTIME_DOCKER_IMAGE=pi-saas-agent-runtime:0.82.1-tools-20260930
```

PostgreSQL 18 镜像的默认 `PGDATA=/var/lib/postgresql/18/docker`，因此挂载父目录 `/var/lib/postgresql`。宿主机仍使用 `docker/volumes/postgres`。首次使用要求该目录为空，或已包含与此布局匹配的 PostgreSQL 18 数据；不能直接使用 PostgreSQL 17 的文件目录，需要逻辑备份/恢复。参考 [PostgreSQL 18 官方镜像定义](https://github.com/docker-library/postgres/blob/master/18/bookworm/Dockerfile)。

配置 `CHROMA_PORT=18000`；所有向量服务均部署时设置 `VECTOR_BASE=pmcq`。确认端口没有被旧容器占用、Milvus 目录权限就绪后：

```bash
docker compose --env-file .env -f docker/storage-compose.yml config --quiet
docker compose --env-file .env -f docker/storage-compose.yml \
  up -d --pull never --wait --wait-timeout 180
```

后续旧容器切换、数据保留、各后端连通性检查和 Gateway/worker 启动见 [存储后端部署指南](../../docs/storage-deployment.md)。Gateway 读取 `.env` 中的 Runtime 标签，按用户创建容器，不自动下载镜像。

此包只负责存储和 Agent Runtime 的容器镜像。Gateway/worker 在宿主机运行，其 Python 环境、前端构建产物和模型服务/权重需随应用交付准备；镜像导入不包含这些资源。生产无外网时，Agent 所需第三方包也应提前内置或通过内网源提供。
