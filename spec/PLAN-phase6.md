# 补齐 RAG 重排流程

## 总结

- 在 Gateway 内新增稳定的 `Reranker` 协议与 `RerankResult(index, score)`；首个适配器调用 vLLM 的 rerank API。vLLM 当前支持 `/rerank`、`/v1/rerank` 和 `/v2/rerank`，请求包含 `model/query/documents/top_n`。[vLLM Scoring Usages](https://docs.vllm.ai/en/latest/models/pooling_models/scoring/)
- 重排只作用于 `vector`、`hybrid`。`graph` 在检索入口优先返回，完全不读取 reranker 配置、不创建 client、不发请求；Agent Runtime 因复用同一 Gateway retrieval service，自动获得相同行为。
- 无配置时保持现有排序；运行时重排失败时按用户选择自动降级，并在响应中显式标记。

## 接口与配置

- 新增 `PUT .../{knowledge_base_id}/reranker-model`，请求为：
  ```json
  {
    "protocol": "vllm",
    "base_url": "http://reranker:8000",
    "api_key": "<secret>",
    "model_name": "BAAI/bge-reranker-v2-m3"
  }
  ```
  `base_url` 后追加 `/rerank`，因此根地址和以 `/v1` 结尾的地址分别调用 `/rerank`、`/v1/rerank`。
- 设置时先执行真实探测：固定 query、两个 documents、`top_n=1`；验证 HTTP 成功、结果数量、唯一且合法的 index、有限数值 score。失败返回 `422 reranker_model_verification_failed` 或 `422 invalid_reranker_response`，不覆盖旧配置。
- 复用现有模型配置表、加密、fingerprint、`verified_at` 和知识库复制逻辑，以 `kind="reranker"`、`protocol="vllm"` 保存；无需数据库迁移。
- 新增幂等的 `DELETE .../reranker-model`，成功或本来未配置均返回 204；删除后 vector/hybrid 恢复原排序。
- `GET .../models` 增加 reranker 项且继续保证不返回 API key。重构通用 `set_model()` 的任务锁和缓存清理分支，避免 reranker 被误当作 LLM、阻塞 graph job 或清理 graph cache。
- vector/hybrid 顶层响应增加：
  ```json
  "rerank": {
    "configured": true,
    "applied": true,
    "error": null
  }
  ```
  无配置为 `false/false/null`；无候选为 `true/false/null`；运行时降级为 `true/false/reranker_unavailable` 或 `invalid_reranker_response`。graph 响应结构保持不变。
- 重排成功时 `item.score` 为 rerank score，并新增 `item.retrieval_score` 保存原向量/RRF 分数；无配置或降级时保持当前 item 结构、分数和排序。

## 检索实现

- 定义 `Reranker.rerank(query, documents, top_n)`，输出按相关性降序排列的 `RerankResult`。vLLM 适配器使用现有模型 URL 安全校验、超时、本地代理绕过和重试配置；仅对网络错误、408、429、5xx 重试，不记录 query、document 或密钥。
- `vector`：
  1. embedding 和 vector backend 获取 `max(top_k, candidate_k)` 个候选。
  2. 保持 PostgreSQL 回填、范围过滤和 `min_score` 过滤。
  3. 无 reranker 直接取前 `top_k`；有配置则传全部候选，使用 `top_n=min(top_k, 候选数)`。
- `hybrid`：
  1. 分别获取 `vector_k`、`bm25_k` 候选并完成 RRF。
  2. 无 reranker 时仍立即截取 `top_k`。
  3. 有配置时截取 `max(top_k, candidate_k)` 个 RRF 候选，加载对应 chunk 文本后重排到 `top_k`。
- 运行时超时、非成功响应或非法结果统一告警并降级为候选阶段前 `top_k`；日志只包含知识库 ID、模型 fingerprint、安全错误码和脱敏端点。
- `retrieve()` 首个分支处理 `mode == "graph"`；只有 vector/hybrid 才查询可选 reranker 配置和调用 factory。Agent Runtime 与 TypeScript `rag_search` 参数无需变更，现有 score 展示自然显示最终 rerank score。

## 测试与验收

- 单元测试覆盖 vLLM 请求 URL、Bearer header、请求体、响应映射、排序、非法/重复/越界 index、非有限 score、超时和重试。
- 检索测试覆盖：
  - vector 将 `max(top_k, candidate_k)` 候选交给 reranker；
  - hybrid 无配置保持当前 `top_k` 行为，有配置保留扩大的 RRF 候选；
  - 成功时双分数正确映射；
  - 失败时顺序、score、数量恢复为原结果并返回降级状态；
  - 空候选不发送请求。
- 增加强约束 graph 测试：把 reranker 配置查询和 client factory 替换为“一旦调用即失败”的桩，证明公开检索和 Runtime 内部检索的 graph 模式均只调用 `graph_retrieve()`。
- API 契约测试覆盖 PUT、DELETE、models 脱敏、仅允许 `protocol=vllm`、验证失败不落库，以及复制知识库保留 reranker 配置。
- 扩展 `test/rag_user_flow.py` 的可选 reranker 参数和 `.env.example` 示例：预检真实 vLLM、配置后验证 vector/hybrid 的 `applied=true` 与双分数、验证 graph 不受影响、删除后恢复原流程；`test/agent_rag_user_flow.py` 在配置存在时继续证明 `rag_search` 能完成检索。
- 验证门槛：RAG unit/architecture tests、Agent Runtime Vitest、compileall、变更文件 Ruff、`git diff --check`；有真实 rerank vLLM 时再运行公开 HTTP user flow，静态或 mock 测试不宣称为真实模型验收。

## 假设与边界

- 本期只实现 vLLM 文本 reranker，不加入 Cohere/Jina SDK、前端配置页或 graph 重排。
- `candidate_k < top_k` 时始终按 `max(top_k, candidate_k)` 保证最多返回 `top_k`。
- 复制知识库沿用现有模型复制语义，不重复探测；后续实际检索失败仍按降级策略处理。
- 不新增依赖或运行时环境变量；HTTP 使用已有 `httpx`，模型超时与重试复用 `RAG_MODEL_REQUEST_TIMEOUT_SECONDS`、`RAG_MODEL_MAX_RETRIES`。
