# 多租户 RAG 后端

RAG 是 Gateway 内独立的 Workspace 资源，不进入 `agent-runtime`。每个知识库固定绑定文件、blocks、chunks、向量和图谱后端；本版本五项均为 PostgreSQL。文件原文、blocks、chunks、向量、图谱和任务均位于同一 PostgreSQL 实例的 `rag` schema。Redis 只缓存可恢复的向量和图谱中间结果。

## 启动

现有 PostgreSQL 由 `.env` 的 `POSTGRES_*` 连接信息提供。不要启动或拉取 PostgreSQL Docker 镜像；开发 Compose 只启动 Redis：

```bash
docker compose --env-file .env -f infra/compose.dev.yml up -d redis
uv run python scripts/start_gateway.py
# 另一个终端
uv run python scripts/start_rag_worker.py
```

首次升级到四阶段版本前，停止 Gateway 和 worker。迁移会删除并重建 **仅** `rag` schema；其中的历史测试数据不可恢复，`platform` schema 不受影响。现有 PostgreSQL 必须允许 `CREATE EXTENSION vector`。

当前能力：

- `FILE_BASE=p`、`VECTOR_BASE=p`、`GRAPH_BASE=p`
- `DOCUMENT_PROCESSING_SERVICE=default`
- PDF、Word、Excel、PPT、Markdown 的纯文本解析
- fixed、regex、semantic 三种 chunking 策略

`FILE_BASE=pm` 会在启动时明确失败；MinIO 尚未实现，也不会出现在 capabilities 中。

## 四阶段流程

```text
创建知识库 → 上传文件 → （设置文档级 chunking 配置）→ parsing → chunking
                                                    └─→ vectorization
                                                    └─→ graph extraction
                                                           → retrieve / merge graphs
```

上传只写入文件和文档元数据；四个阶段的初始状态都是 `not_started`。文档级
chunking 配置可在上传后任何时候设置，但必须早于 chunking 任务进入 `queued` 状态。
实际提交 chunking 仍以 parsing 成功且已产生 blocks 为前置条件。

创建知识库必须显式指定五项后端：

```json
{
  "name":"manuals",
  "file_backend":"postgresql",
  "block_backend":"postgresql",
  "chunk_backend":"postgresql",
  "vector_backend":"postgresql",
  "graph_backend":"postgresql"
}
```

后端不可修改，复制知识库时完整继承。每个知识库可独立调整 parsing、chunking、embedding、graph 的并发额度，不能超过 env 系统上限。

上传接口只验证和持久化文件，不创建 Redis key 或处理任务。每个文件返回单独结果，所以同一批次可部分成功。持久化文件名为 `<user_uuid>_<YYMMDD>_<sanitized_stem><extension>`；同名文件使用不同 `storage_key` 保存，因此允许重复上传。

提交 parsing 时可为每个文档选择处理后端：

```json
{"items":[{"document_id":"<uuid>","processor_backend":"default"}]}
```

使用 `PUT .../documents/{document_id}/chunking-config` 设置当前文档的策略和参数；未设置时使用上传时从 env 复制的 fixed 默认参数。语义切分需要已验证 embedding；vectorization 和 graph extraction 分别需要 chunking 成功加 embedding/LLM 配置。

| 阶段 | 输入 | 失败与重试 | 最终数据可见性 |
| --- | --- | --- | --- |
| parsing | PostgreSQL 文件对象、文档指定的处理后端 | 不使用缓存；失败不保留 partial blocks。删除 `/blocks` 后可重新解析。 | 全部解析成功且至少一个有效 block 时一次性发布。 |
| chunking | 已持久化 blocks、文档策略快照 | 不使用缓存；失败不保留 partial chunks。删除 `/chunks` 后可重新切分。 | 全部切分成功时一次性发布。 |
| vectorization | chunks、已验证 embedding | Redis 按 chunk 保存已完成向量；重试只补缺失项并刷新 TTL。 | 所有 chunk 完成后单事务写入 vectors。 |
| graph extraction | chunks、已验证 LLM | Redis 按 chunk 保存已抽取的节点和边；重试只补缺失项并刷新 TTL。 | 所有 chunk 完成后单事务写入图谱及证据。 |

因此 parsing/chunking 的失败不会留下可查询的中间数据；向量和图谱任务失败时缓存仅用于恢复，检索也看不到部分最终数据。

## 清理与复制

- 删除 `/blocks` 会删除所有下游 chunks、vectors、图谱、相关合并图谱与缓存，并允许重新解析。
- 删除 `/chunks` 保留 blocks，但删除所有下游资源，并允许以新的文档级配置重新切分。
- 删除文档会同时清理文件适配器内容、派生数据、合并图谱依赖和 Redis key。
- 复制知识库会复制文件、四阶段状态、文档配置、blocks、chunks、vectors、图谱、任务和有效的向量/图谱缓存；运行中任务仍会阻止复制。

## HTTP 调用要点

所有 RAG 路由在 `/api/v1` 下，并要求登录后的 `Authorization: Bearer <token>`。推荐由
`GET /api/v1/rag/capabilities` 取得当前支持的后端、文件扩展名和默认切分参数，再创建知识库。
处理提交返回 `202 {"job_ids":[...]}`；轮询 `GET .../jobs` 或文档详情中的 `stages`，不要把
提交成功视为处理完成。

典型清理边界：删除 `/vectors` 只允许重新向量化；删除 `/graph` 只允许重新抽取图谱；删除
`/chunks` 会同时删除向量和图谱；删除 `/blocks` 会继续删除 chunks 及全部下游数据。任一相关
任务处于 `queued` 或 `running` 时，派生数据删除会返回 `409 document_processing`。

## 最小验证流程

这个流程只验证不依赖外部模型的基础闭环：认证、Workspace 隔离、知识库创建、文件持久化、
parsing 和 fixed chunking。先按“启动”一节运行 Redis、Gateway 与 RAG worker；Gateway 启动成功
本身已完成 PostgreSQL、pgvector 和 Redis 的连通性检查。下面示例需要 `curl` 和 `jq`，并使用
当前 `.env` 的 `21995` 端口；如端口不同，覆盖 `RAG_BASE` 即可。

```bash
set -euo pipefail

RAG_BASE="${RAG_BASE:-http://127.0.0.1:21995/api/v1}"
EMAIL="rag-min-$(date +%s)-$RANDOM@example.test"
PASSWORD='rag-minimum-password-2026'
INPUT_FILE="$(mktemp --suffix=.md)"
trap 'rm -f "$INPUT_FILE"' EXIT
printf '# Minimal RAG\nThis document validates parsing and chunking.\n' >"$INPUT_FILE"

# 1. 注册会创建 default workspace；随后确认当前部署能力。
TOKEN="$(curl -fsS -X POST "$RAG_BASE/auth/register" \
  -H 'Content-Type: application/json' \
  --data "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | jq -r '.access_token')"
AUTH=(-H "Authorization: Bearer $TOKEN")
curl -fsS "${AUTH[@]}" "$RAG_BASE/rag/capabilities" | jq .
WORKSPACE_ID="$(curl -fsS "${AUTH[@]}" "$RAG_BASE/workspaces" | jq -r '.items[] | select(.is_current).id')"

# 2. 创建后端均固定为 PostgreSQL 的知识库。
KB_ID="$(curl -fsS -X POST "${AUTH[@]}" \
  -H 'Content-Type: application/json' \
  --data '{"name":"minimal-rag","file_backend":"postgresql","block_backend":"postgresql","chunk_backend":"postgresql","vector_backend":"postgresql","graph_backend":"postgresql"}' \
  "$RAG_BASE/workspaces/$WORKSPACE_ID/knowledge-bases" | jq -r '.id')"

# 3. 上传不创建任务；响应中四个 stages 都应为 not_started。
DOCUMENT_ID="$(curl -fsS -X POST "${AUTH[@]}" \
  -F "files=@${INPUT_FILE};filename=minimal.md;type=text/markdown" \
  "$RAG_BASE/workspaces/$WORKSPACE_ID/knowledge-bases/$KB_ID/documents" \
  | tee /dev/stderr | jq -r '.items[0].document.id')"

# 4. 提交 parsing，等待文档阶段变为 succeeded。
curl -fsS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  --data "{\"items\":[{\"document_id\":\"$DOCUMENT_ID\"}]}" \
  "$RAG_BASE/workspaces/$WORKSPACE_ID/knowledge-bases/$KB_ID/jobs/parsing" | jq .

wait_stage() {
  local stage="$1" state deadline
  deadline=$((SECONDS + 60))
  while (( SECONDS < deadline )); do
    state="$(curl -fsS "${AUTH[@]}" \
      "$RAG_BASE/workspaces/$WORKSPACE_ID/knowledge-bases/$KB_ID/documents/$DOCUMENT_ID" \
      | jq -r --arg stage "$stage" '.stages[$stage].status')"
    case "$state" in
      succeeded) return 0 ;;
      failed) echo "$stage failed; inspect document error and jobs" >&2; return 1 ;;
    esac
    sleep 1
  done
  echo "timed out waiting for $stage" >&2
  return 1
}
wait_stage parsing

# 5. 未设置配置时，文档使用上传时从 env 复制的 fixed 默认参数。
curl -fsS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  --data "{\"document_ids\":[\"$DOCUMENT_ID\"]}" \
  "$RAG_BASE/workspaces/$WORKSPACE_ID/knowledge-bases/$KB_ID/jobs/chunking" | jq .
wait_stage chunking

# 6. 最终状态：parsing/chunking=succeeded，vectorization/graph=not_started。
curl -fsS "${AUTH[@]}" \
  "$RAG_BASE/workspaces/$WORKSPACE_ID/knowledge-bases/$KB_ID/documents/$DOCUMENT_ID" | jq .
```

若 parsing 或 chunking 失败，先读取上述文档对象中的 `stages.<stage>.error`，再查询
`GET .../knowledge-bases/{knowledge_base_id}/jobs` 的 `error` 和 `message`。最小流程完成后可删除
知识库：`DELETE .../knowledge-bases/{knowledge_base_id}` 返回异步 operation；轮询
`GET /api/v1/rag/operations/{operation_id}` 至 `succeeded` 后，其文件、blocks、chunks 与任务会被清理。

## Smoke 验收

先在三个终端分别启动 Redis、模型 mock、Gateway 和 worker。Gateway 必须由启动脚本读取 `.env`
中的 `GATEWAY_HOST` 与 `GATEWAY_PORT`；例如当前配置端口为 `21995` 时：

```bash
# terminal 1
docker compose --env-file .env -f infra/compose.dev.yml up -d redis

# terminal 2
uv run python scripts/rag_mock_model.py

# terminal 3 and 4
uv run python scripts/start_gateway.py
uv run python scripts/start_rag_worker.py

# terminal 5: use the value configured in .env, rather than the script's fallback URL
RAG_SMOKE_GATEWAY=http://127.0.0.1:21995/api/v1 \
  uv run python scripts/rag_smoke_flow.py
```

该 smoke 会创建隔离测试用户和知识库，覆盖五种文件、四阶段、向量/图谱缓存续跑、三种检索、
图谱合并、知识库复制和级联删除；脚本结束时会清理其创建的测试数据。生产或共享环境应使用
独立测试数据库和 Redis DB。
