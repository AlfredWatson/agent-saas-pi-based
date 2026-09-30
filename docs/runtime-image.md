# Runtime 镜像重建与离线交付

本文适用于：测试机无法通过代理拉取镜像，在自己的 Mac 上构建，再把镜像包传到 Linux 测试机。所有构建依赖下载都发生在 Mac；测试机只执行 `docker load` 和本地运行。

本次建议使用新标签 `pi-saas-agent-runtime:0.82.1-tools-20260930`。`0.82.1` 是 Pi SDK 版本，后缀区分这次工具环境更新；保留旧镜像便于回退。后续重建请更换后缀，避免混淆不同构建。

## 1. 本次内置环境

旧 Dockerfile 已包含 Node、Git 和 `python3`，但缺少 `python` 命令、pip 和完整 venv 支持。现有 Runtime 中实测 `python3 -m pip` 和 `python3 -m venv` 失败；已有 trace 也出现了安装 Python PDF 包、尝试补 pip 和安装 `poppler-utils` 的步骤。

更新后的 [Dockerfile](../agent-runtime/Dockerfile) 在构建时安装以下工具：


| 用途         | 内置工具                                       | 使用方式                                                          |
| ---------- | ------------------------------------------ | ------------------------------------------------------------- |
| JavaScript | Node 24、npm、npx                            | `node`、`npm`、`npx`                                            |
| Python     | Debian Bookworm Python 3.11、pip、venv、开发头文件 | `python` / `python3`、`python3 -m pip`、`python3 -m venv .venv` |
| Git 与 SSH  | Git、OpenSSH client                         | `git`、`ssh`；访问私有仓库的凭据仍需另行提供                                   |
| 原生构建       | make、GCC/G++                               | `make`、`gcc`、`g++`                                            |
| Shell 与检索  | Bash、findutils、ripgrep、jq                  | `bash`、`find`、`rg`、`jq`                                       |
| 网络与压缩      | CA certificates、curl、wget、zip、unzip        | `curl`、`wget`、`zip`、`unzip`                                   |
| PDF 文本提取   | poppler-utils                              | `pdftotext`、`pdfinfo`                                         |
| Agent 服务   | Pi SDK 0.82.1、Runtime npm 依赖               | 按仓库`package-lock.json` 安装                                     |


镜像构建会检查 Python、pip 和 venv；后面的断网冒烟验证还会验证 Node/npm/Git 的实际本地操作。

这是基础工具环境，不包含任意项目的所有第三方库。`pypdf`、pandas、Playwright、浏览器、uv、其他 Python/Node 版本等目前没有预装；`npx` 在调用未安装的软件包时仍可能下载。若 trace 中反复出现同一项目依赖，应把它作为下一次镜像的明确依赖，在 Mac 构建阶段预装。

## 2. 准备构建上下文

先在测试机确认目标 CPU 架构：

```bash
uname -m
```

`x86_64` 对应本文的 `linux/amd64`。如果目标机是 `aarch64`，下文构建/运行参数改为 `linux/arm64`，并更改镜像包文件名。Mac 是 Apple Silicon 也应按**测试机架构**构建。Docker Desktop 支持跨架构构建，参见 [Docker 官方说明](https://docs.docker.com/build/building/multi-platform/)。

Mac 需要安装并启动 Docker Desktop，且能够访问 Docker Hub、Debian 软件源和 npm registry。Mac 上终端的代理变量不保证 Docker Desktop 的镜像拉取已配置代理；基础镜像拉取失败时，在 Docker Desktop 的代理设置中配置可用代理。

推荐把测试机当前 `agent-runtime` 目录打包传给 Mac，保证 Dockerfile 和锁文件一致。在测试机仓库根目录执行：

```bash
tar --exclude='agent-runtime/node_modules' \
  --exclude='agent-runtime/.runtime-data' \
  --exclude='agent-runtime/.env' \
  --exclude='agent-runtime/npm-debug.log' \
  -czf /tmp/pi-saas-runtime-build-context-20260930.tar.gz agent-runtime
```

在 Mac 执行，替换 SSH 用户和地址：

```bash
mkdir -p ~/pi-saas-runtime-build
cd ~/pi-saas-runtime-build
scp TEST_USER@TEST_HOST:/tmp/pi-saas-runtime-build-context-20260930.tar.gz .
tar -xzf pi-saas-runtime-build-context-20260930.tar.gz
```

也可以在 Mac 使用同一版本的完整仓库，从仓库根目录执行下文。构建需要当前 `agent-runtime/Dockerfile`、`package.json`、`package-lock.json`、`src/` 和 `scripts/`；仓库中旧的 `pi-saas-agent-runtime-context.tar.gz` 不能替代本次上下文。无需把测试机 `.env` 或租户数据传给 Mac。

## 3. 在 Mac 构建新镜像

在包含 `agent-runtime/` 的目录执行：

```bash
set -euo pipefail

docker version
docker buildx version

docker buildx build \
  --platform linux/amd64 \
  --pull \
  --progress=plain \
  -f agent-runtime/Dockerfile \
  -t pi-saas-agent-runtime:0.82.1-tools-20260930 \
  --load agent-runtime

docker image inspect pi-saas-agent-runtime:0.82.1-tools-20260930 \
  --format '{{.Os}}/{{.Architecture}} {{.Id}}'
```

预期架构为 `linux/amd64`。`--load` 将结果加载到 Mac 本地镜像库，随后才能 `docker save`。`--pull` 只在 Mac 构建阶段检查基础镜像更新；测试机无需拉取基础镜像或 apt/npm 依赖。任何构建或架构检查失败都应先修复，暂不导出/切换镜像。

## 4. 在 Mac 断网验证内置工具

下面使用无网络、只读根文件系统、非 root UID 和可写临时目录验证，接近 Gateway 实际的工具运行约束。无需配置真实服务密钥或启动 Gateway。

```bash
docker run --rm --pull=never \
  --platform linux/amd64 \
  --network none \
  --read-only \
  --user 10001:10001 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777 \
  -e HOME=/tmp/pi-runtime-home \
  --entrypoint bash \
  pi-saas-agent-runtime:0.82.1-tools-20260930 \
  /opt/pi-runtime/scripts/check-environment.sh
```

预期打印工具路径/版本，创建带 pip 的 Python venv、运行 Node、生成 npm 项目并完成本地 Git commit，最后打印 `Runtime toolchain OK (no downloads required)`。失败则停止交付，回到构建步骤修复。此验证不调用真实模型，不证明 Agent 整体耗时已降低。

## 5. 从 Mac 导出并传输

继续在 Mac 上执行：

```bash
set -euo pipefail

docker save pi-saas-agent-runtime:0.82.1-tools-20260930 \
  | gzip > pi-saas-agent-runtime-0.82.1-tools-20260930-amd64.tar.gz

shasum -a 256 pi-saas-agent-runtime-0.82.1-tools-20260930-amd64.tar.gz \
  > pi-saas-agent-runtime-0.82.1-tools-20260930-amd64.tar.gz.sha256

scp pi-saas-agent-runtime-0.82.1-tools-20260930-amd64.tar.gz \
  pi-saas-agent-runtime-0.82.1-tools-20260930-amd64.tar.gz.sha256 \
  TEST_USER@TEST_HOST:/tmp/
```

使用 `docker save` 保存镜像层和标签，参见 [官方命令文档](https://docs.docker.com/reference/cli/docker/image/save/)。不要使用 `docker export`，它导出容器文件系统，不能保留本文依赖的镜像配置。

## 6. 测试机离线导入与验证

在测试机执行（Docker 需要权限时使用已授权的 Docker 用户或 `sudo docker`）：

```bash
set -euo pipefail
cd /tmp

sha256sum -c pi-saas-agent-runtime-0.82.1-tools-20260930-amd64.tar.gz.sha256

gzip -dc pi-saas-agent-runtime-0.82.1-tools-20260930-amd64.tar.gz | docker load

docker image inspect pi-saas-agent-runtime:0.82.1-tools-20260930 \
  --format '{{.Os}}/{{.Architecture}} {{.Id}}'
```

校验必须为 `OK`，镜像架构必须与测试机匹配，镜像 ID 应与 Mac 上一致。随后在测试机重复第 4 步的断网验证；这一步仍使用 `--pull=never`，不会触发 Docker Hub 拉取。导入或验证失败时，不删除旧 Runtime。

## 7. 替换测试 Runtime 容器

修改测试机仓库根目录 `.env` 中的这一行：

```dotenv
RUNTIME_DOCKER_IMAGE=pi-saas-agent-runtime:0.82.1-tools-20260930
```

保留其他配置。Gateway 默认仍是旧标签，必须显式设置新值并重启 Gateway；只改 `.env` 或导入同名镜像不会更换运行中的容器。

### 批量替换现有测试容器

当前测试 Runtime 可以删除。先结束/取消运行中的 Agent 任务，再停止 Gateway 进程（开发前台启动用 `Ctrl-C`；服务托管部署用原来的服务管理方式停止），防止清理期间创建新容器。

删除只针对带 `com.pi-saas.managed=true` 标签的 Pi SaaS Runtime，先列出核对：

```bash
docker ps -a \
  --filter label=com.pi-saas.managed=true \
  --format '{{.ID}} {{.Names}} {{.Image}} {{.Status}}'
```

确认列出的都是本次可以删除的测试 Runtime 后执行：

```bash
docker ps -aq --filter label=com.pi-saas.managed=true \
  | while IFS= read -r runtime_id; do
      if [ -n "$runtime_id" ]; then
        docker rm -f "$runtime_id" || exit 1
      fi
    done
```

不要执行全机容器清理。此操作保留 PostgreSQL、Redis、向量数据库和其他服务，也保留宿主机 `.runtime-data/tenants/` 中的 Workspace 与 JSONL；无需删除会话或修改数据库。Gateway 下次使用 Runtime 时会发现旧容器不存在，创建新容器并更新 `RuntimeInstance` 记录。

按原方式重新启动 Gateway。若一直使用仓库的前台启动脚本，在仓库根目录执行：

```bash
uv run python scripts/start_gateway.py --reload
```

这里的 `uv` 是测试机已有的 Gateway 启动工具，与 Agent 容器是否内置 uv 无关。若使用 systemd 等托管方式，重启原 Gateway 服务；独立的 RAG worker 不需要因为 Runtime 镜像变化而重启。

在工作台启动 Runtime 或发送一条新消息后检查：

```bash
docker ps --filter label=com.pi-saas.managed=true \
  --format '{{.Names}} {{.Image}} {{.Status}}'
```

所有新容器应使用 `pi-saas-agent-runtime:0.82.1-tools-20260930`，稍后达到 `healthy`。若需确认实际镜像 ID，对输出中的具体容器执行 `docker inspect CONTAINER_NAME --format '{{.Image}}'`，与第 6 步的 ID 比较。

### 仅替换当前用户的 Runtime

如不批量删除，在改 `.env` 并重启 Gateway 后，可用当前用户 JWT 调用公开 API：

```bash
export GATEWAY_BASE_URL=http://127.0.0.1:21995
# 在当前终端设置 JWT 为自己的访问令牌。
curl --noproxy '*' --fail-with-body -sS -X POST \
  -H "Authorization: Bearer ${JWT:?请先设置当前用户 JWT}" \
  "$GATEWAY_BASE_URL/api/v1/runtime:recreate"
```

端口应与 `.env` 的 `GATEWAY_PORT` 一致。预期 `{"state":"running"}`。此 API 只重建当前用户，运行中任务会返回 `409 runtime_busy`；结束/取消任务后再执行。修改源码后，现有 Node 进程也需要重建才会加载新代码。

## 8. Agent 使用与 trace 验收

Runtime 按宿主 UID/GID 运行，根文件系统只读，可写持久目录是 `/runtime-data`，`HOME=/runtime-data/home`；`/tmp` 是容量有限且不持久的 tmpfs。不要让 Agent 再尝试 `apt-get install`、sudo、系统级 Node/Python/Git 安装或写入 `/usr`、`/opt`。

Python 项目依赖应放在 Workspace 的虚拟环境中。Debian Bookworm 系统 Python 使用 externally-managed 约束，参见 [Debian 官方说明](https://www.debian.org/releases/bookworm/amd64/release-notes/ch-information.en.html)。示例：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python your_script.py
```

依赖包安装仍需网络，除非已经在镜像中预装或提供离线 wheel。Node 项目同理：在 Workspace 内 `npm ci`，无需安装 Node 本身。优先使用 `pdftotext`/`pdfinfo` 完成 PDF 文本提取，可以省去仅为抽取文本而安装 Python PDF 库的步骤。

镜像预装不会自动使模型知道环境。首次测试建议在任务或 Agent 指令中明确说明：

> 当前 Runtime 已预装 Node 24/npm/npx、Git、Python 3.11（python/python3、pip、venv）、make/GCC、curl/wget、rg/jq、zip/unzip 和 pdftotext/pdfinfo。请直接使用现有工具；需要检查时一次性运行 command -v 和版本命令。Python 项目依赖安装到 Workspace 的 .venv，Node 依赖安装到项目目录；仅在任务确实需要时下载第三方包或其他版本的环境。

新建测试会话，要求 Agent 在 Workspace 内验证 `python --version`、`python3 -m pip --version`、创建 venv、执行简单 Python/Node 脚本和 `git --version`。检查 trace 是否仍下载基础 Python/Node/Git，并与相同模型、相同任务的旧 trace 比较安装步骤及总耗时。基础工具的断网验证通过与真实模型 trace 改善应分别记录。

## 9. 回退与后续维护

回退时将 `.env` 的 `RUNTIME_DOCKER_IMAGE` 改回仍保留的 `pi-saas-agent-runtime:0.82.1-dev`，结束任务并重启 Gateway，再按第 7 步清理/重建 Runtime。测试数据继续使用原宿主挂载目录。

Gateway 不会自动构建或拉取 Runtime 镜像。运行时把测试机本仓库的 `agent-runtime/src` 只读挂载到 `/opt/pi-runtime/src`，覆盖镜像内同路径源码，因此需要保证测试机源码与 Mac 构建使用的 npm 锁文件兼容。源码变化需要重建容器；`package-lock.json`、系统工具、基础镜像或镜像中的验证脚本变化需要重建并重新导入镜像。

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
调用 `POST /api/v1/runtime:recreate`。仅修改 RAG 工具的 TypeScript 源码时，可以使用依赖兼容的已有镜像；
本次补齐基础工具环境涉及 Dockerfile 变更，必须按本文重新构建并导入镜像。

在 Gateway、RAG worker、Redis、向量/Embedding 服务与 Runtime 容器均就绪后，可运行完整
公开 HTTP 验收（默认清理 Workspace、知识库、Profile 和 Binding）：

```bash
uv run python test/agent_rag_user_flow.py --report /tmp/agent-rag-user-flow.json
```

它需要既有的 `AGENT_TEST_*` 和 `RAG_TEST_*` 配置；其中 Agent 与 RAG 测试账号应为同一
用户，确保新建 Session 与知识库位于同一 Workspace。可选的
`RAG_TEST_RERANKER_BASE_URL`、`RAG_TEST_RERANKER_MODEL` 和 `RAG_TEST_RERANKER_API_KEY` 会先在
知识库配置 vLLM reranker，再验证 Runtime `rag_search` 仍可完成检索。`--keep-resources` 仅用于失败诊断。

## 文件权限排查

Runtime 统一使用 `agent-runtime/Dockerfile` 构建。主 Dockerfile 已包含
`chmod -R a+rX /opt/pi-runtime`，确保从权限为 `0600` 的文件构建时，镜像内的代码与依赖也可被 Gateway 指定的非 root UID 读取；无需额外构建权限补丁镜像。

如果 Docker 日志出现 `EACCES: permission denied, open '/opt/pi-runtime/package.json'`，先核对容器实际镜像 ID 是否为新构建版本。若仍是旧镜像，按本文在 Mac 重新构建、传输并导入，再重建 Runtime 容器。

`/opt/pi-runtime/src` 会被测试机源码的只读挂载覆盖，镜像内的 `chmod` 不会改变宿主机文件权限。若错误指向该目录，需检查宿主机 `RUNTIME_SOURCE_DIR` 的文件读取权限和目录访问权限是否允许 Gateway 所使用的 UID/GID 访问。不要通过让用户 Runtime 以 root 身份运行来绕过该问题。