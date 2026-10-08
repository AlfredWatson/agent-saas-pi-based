# PostgreSQL 17 / 统一 Compose 部署验收（2026-09-30）

本记录来自当前测试机器的实际容器、SQL、公共 HTTP API 和真实模型验收，不是静态部署计划。本次按已授权的测试数据重置执行，不做数据迁移。

## 实际部署

- 七个存储服务均由 `docker/storage-compose.yml` 的 `pi-saas-storage` 项目管理，使用本地镜像启动（`--pull never`）。旧 PostgreSQL 18、Redis、向量服务容器及本项目测试存储数据已清理。
- PostgreSQL 实测 **17.11**，镜像 `pgvector/pgvector:pg17`，镜像 ID `sha256:dca0d688bbb31d3f851502ffcb9c7791387b4fcc544ae434dab41761e5ece317`。
- 数据库 `pi_saas`，宿主机端口 `127.0.0.1:15432`，数据挂载 `docker/volumes/postgres` → `/var/lib/postgresql/data`；未使用旧 PG18 卷。宿主机上其他 PostgreSQL 服务未改动。
- Alembic 从空库升级到 **`0013_subagents`**，重复 `upgrade head` 成功。实测 `platform` 11 张表、`rag` 12 张表，pgvector 扩展 **0.8.6**。
- Redis `6379`，Milvus `19530` / 健康接口 `19091`，Chroma `18000`，Qdrant `6333/6334`，发布地址均为 loopback。MinIO/etcd 仅通过存储网络供 Milvus 使用。
- `.env` 已设置 `VECTOR_BASE=pmcq`；保留真实密码、模型密钥和 Gateway 密钥，文件权限为 `0600`。新测试账号的邮箱/密码已写入 `AGENT_TEST_*`、`RAG_TEST_*`。
- Runtime 镜像为 `pi-saas-agent-runtime:0.82.1-tools-20260930`。非 root、只读根目录、禁用网络下的工具链验证通过（Python/venv、Node/npm、Git 等），输出 `Runtime toolchain OK (no downloads required)`。

## 应用入口

Gateway 和 worker 使用仓库已有 `.venv/bin/python` 在宿主机后台运行，日志和 PID 文件在 `logs/storage-rebuild-20260930/`，未安装新宿主机依赖。

| 进程 | 入口 | 日志 / PID 文件 |
| --- | --- | --- |
| Gateway | `http://gpu-h20-4:21995`；OpenAPI `/openapi.json` | `gateway.log` / `gateway.pid` |
| RAG worker | 后台消费任务，无 HTTP 监听 | `worker.log` / `worker.pid` |
| 前端工作台 | `http://gpu-h20-4:21996` | `frontend.log` / `frontend.pid` |

前端通过 `VITE_GATEWAY_ORIGIN=http://127.0.0.1:21995` 代理 API。机器已有 `5173` 服务属于其他应用，未停止它。Gateway 的 Runtime RAG 地址继续使用 `http://host.docker.internal:21995`。

这些是本次测试的后台进程，没有新增 systemd 托管；重启主机后需按部署指南重新启动应用。前端重新启动命令：

```bash
cd frontend
VITE_GATEWAY_ORIGIN=http://127.0.0.1:21995 \
  npm run dev -- --host 0.0.0.0 --port 21996 --strictPort
```

## 功能结果

以下九项实测全部通过，测试资源清理均无错误；四个向量后端全流程均以退出码 0 结束。

| 验收 | 结果 | 本机证据（未提交 Git） |
| --- | --- | --- |
| `registration` | passed | `tmp/storage-rebuild-registration.json` |
| `agent` | passed | `tmp/storage-rebuild-agent.json` |
| `agent-rag` | passed | `tmp/storage-rebuild-agent-rag.json` |
| `subagents` | passed | `tmp/storage-rebuild-subagents.json` |
| `rag-postgresql` | passed | `tmp/storage-rebuild-rag-postgresql.json` |
| `rag-milvus` | passed | `tmp/storage-rebuild-rag-milvus.json` |
| `rag-chroma` | passed | `tmp/storage-rebuild-rag-chroma.json` |
| `rag-qdrant` | passed | `tmp/storage-rebuild-rag-qdrant.json` |
| `frontend-proxy` | passed | `tmp/storage-rebuild-frontend-proxy.json` |

- 注册流程验证注册、登录、默认 Workspace 和重复注册拒绝；测试账号保留，凭据在 `.env`，不写入本文。
- Agent 流程使用实际 DeepSeek 模型，验证 Runtime、XLSX 文件上传/标准库读取、bash 工具调用、SSE、历史及断开连接后的消息持久化，结束后清理测试资源。
- Agent RAG 流程验证真实 `rag_search` tool call/result、唯一知识库标记、文档证据、会话绑定、重命名及删除。
- 子代理流程验证 agent-config 更新、过期配置版本返回 409、真实 `call_subagents` 调用以及公开子会话历史中的标记。
- 各向量后端的 RAG 流程使用本机实际 `Qwen3-Embedding-8B` 和 `Qwen3.8-27B-FP8`，覆盖 PDF/Word/Excel/PPT/Markdown、四阶段处理、预期失败与缓存续跑、检索、复制和异步删除；不以数据库 ping 代替功能测试。纯图片 PPT 的 `document_contains_no_extractable_text` 是流程预期失败项。
- 前端检查验证页面 HTTP 200、代理登录和鉴权后的账号/Workspace/Provider/Runtime 接口。**没有浏览器交互 E2E**：本机缺少 Playwright Chromium，本次没有下载安装浏览器。
- 配套的 Gateway 单元及架构检查：**15 passed**（Agent RAG 合约、Workspace 存储、工具配置、RAG 依赖边界）。

## 验收发现并修复

1. 新空库的 `0001_initial` 使用当前 ORM metadata，提前创建依赖 `rag.knowledge_bases` 的会话绑定表，导致 `schema "rag" does not exist`。已让该表留到原有 `0009_agent_rag_tool` 创建；修复后空库全量迁移和重复升级均成功。已初始化数据库不重新执行 `0001`。
2. 原 `.env` 设置 `AGENT_TEST_THINKING_LEVEL=low`，当前 DeepSeek 测试模型不支持该选项。已清空这个可选测试偏好，再运行 Agent 验收成功。
3. Workspace 删除会级联删除 Profile/Provider Binding；旧验收脚本随后再次删除这些资源，将正常 404 误报成清理失败。已修正两个 Agent 验收脚本的清理期望，实际资源清理通过。

原 `docker/images/pi-saas-linux-amd64-20260930.tar.gz` 及其清单仍如实记录 PG18，不是新 PG17 完整包。本次由用户额外导入 PG17 镜像后部署；下一次完整交付需重新导出包含 PG17 的归档。操作指南见 [PG17 离线重建](postgres17-offline.md) 和 [统一存储部署](storage-deployment.md)。
