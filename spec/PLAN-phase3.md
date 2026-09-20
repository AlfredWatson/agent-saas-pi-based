# Gateway 全局分层的 RAG 等价重构计划

## 总结

- 保留 RAG 与 `agent-runtime` 的业务隔离，但不再把所有实现平铺在 `app/rag`；改为 Gateway 全局分层。
- 本轮只做结构重构与测试补强，不改变公开 API、数据库结构、任务语义、环境变量或检索结果。
- 当前基线为 RAG 单测 `17 passed`；重构必须保持现有 HTTP 用户流程通过。
- 重点拆除两个上帝模块：1,609 行的路由模块和 981 行的 worker 模块。

## 目标结构与依赖规则

- `app/api/v1/rag/`
  - 按知识库、模型、文档、任务、检索、图谱、操作拆分 Router。
  - 保存请求/响应 Pydantic 模型、鉴权依赖和响应映射。
  - 路由只做参数接收、依赖注入、调用服务和错误映射；禁止直接查询数据库、操作 Redis 或提交事务。
- `app/services/rag/`
  - 承载上传、模型配置、任务提交、检索、图谱合并、复制和删除等用例。
  - 集中处理租户所有权、状态前置条件及事务边界；API 和 repository 均不自行 `commit`。
  - 定义不依赖 FastAPI 的 `RagError` 机器码，由 API 层统一映射现有 HTTP 状态。
- `app/domain/rag/`
  - 保存 chunking、图谱规范化/RRF、状态转换、任务快照和纯数据类型。
  - 禁止依赖 FastAPI、SQLAlchemy、Redis和具体模型客户端。
- `app/db/rag/`
  - 保存 ORM 模型和按知识库、文档、任务、图谱划分的 repository。
  - 所有租户查询必须显式携带 `user_id + workspace_id + knowledge_base_id`；repository 不提交事务。
- `app/integrations/rag/`
  - 保存 Redis 缓存、文件/向量/图谱存储、文档解析器及 embedding/LLM 客户端。
  - 用具名 Protocol 和 backend registry 替代分散的后端判断；首期仍只注册 PostgreSQL/default，不引入新向量库。
  - 索引和查询继续使用同一已验证 embedding 指纹与维度。
- `app/workers/rag/`
  - 将 runner、任务抢占/租约、状态收尾、四阶段 handler、复制/删除 operation 分开。
  - 统一 `StageHandler.execute(context)` 接口和 kind→handler 注册表；增加新阶段时无需修改主循环。
- 历史 Alembic revision 不修改；保留最小 `app/rag/models.py` 兼容导出，使全新数据库仍能执行旧迁移。其他旧 `app.rag.*` 导入全部移除，并用架构测试禁止回流。

依赖方向固定为：

`API / Worker → Services → Domain + Repositories + Integration Ports`

Domain、Services 不得反向依赖 API；Worker 不得导入 Router。

## 实施步骤

1. **锁定行为契约**
   - 固化当前 OpenAPI 的路径、方法、状态码、请求字段、响应字段、机器错误码和 operation ID。
   - 为五个不可变 backend 字段、上传仅持久化、批次部分成功、四阶段独立提交、任务快照、租户 404、检索不生成答案等行为增加特征测试。
   - 记录当前单测和黑盒用户流程结果，作为后续每阶段门禁。

2. **提取领域逻辑和基础设施**
   - 先移动纯 chunking、图谱算法和 DTO，再移动 ORM、存储、缓存、解析器、模型客户端。
   - 引入 backend/processor registry，但保持当前配置值和工厂行为不变。
   - 保证模型密钥加密、SSRF 校验、缓存 key、prompt/schema 版本和文件命名算法不变。

3. **建立服务和 repository 层**
   - 按知识库/模型、文档、任务、检索、图谱、维护操作逐组迁移。
   - API 不再返回 ORM 对象；服务在事务内完成必要的 `flush/refresh`，返回稳定 DTO，消除延迟加载和 `MissingGreenlet` 风险。
   - 删除、复制、图谱合并和派生数据清理保持现有调用顺序；不在本轮引入 outbox 或修改原子性语义。

4. **拆分 API 与 Worker**
   - 将原 Router 替换为聚合 `router` 包，`app.main` 的注册方式及公开 URL 不变。
   - Worker 主循环仅负责调度；租约组件负责 claim/heartbeat，handler 负责单阶段处理，executor 统一处理成功、失败和 lease 丢失后的状态。
   - `scripts/start_rag_worker.py` 只更新内部导入路径，启动命令保持不变。

5. **清理与文档**
   - 删除已迁移的旧实现，只保留 Alembic 兼容 shim。
   - 增加 RAG 架构文档，写明目录职责、依赖方向、事务所有权、扩展一个 backend/processor/stage 的步骤。
   - 更新 `docs/rag.md` 的内部架构说明；公开 API 文档不改契约内容。

## 接口与兼容性

- 所有 `/api/v1` 路径、HTTP 状态、字段名和机器错误码保持不变。
- `file_backend`、`block_backend`、`chunk_backend`、`vector_backend`、`graph_backend` 继续创建后不可修改。
- 上传仍为 HTTP 200 的逐文件结果，不创建任务、不解析、不访问 Redis。
- 数据库表、Alembic head、Redis key 规则、环境变量和 worker 启动方式不变，不新增数据迁移。
- Gateway 和 Worker 使用同一版本发布；回滚只需回退代码，不需要回滚数据库。

## 测试与验收门禁

- 单元测试：领域算法、配置校验、状态转换、错误映射、backend registry 和各阶段 handler。
- 服务测试：使用 fake repository/adapters 覆盖上传部分成功、任务快照与重试、模型锁定、删除状态重置、图谱合并和复制编排。
- API 测试：通过依赖替换验证鉴权、输入输出 DTO、错误码以及 Router 不泄漏 ORM。
- 架构测试：用 AST 检查禁止依赖，包括 API→SQLAlchemy/Redis、Domain→框架、Services→FastAPI、Worker→API。
- PostgreSQL/Redis 集成测试使用独立测试实例，覆盖租户过滤、active job 唯一性、claim/lease 状态、事务回滚和级联清理；禁止连接开发或生产数据库。
- 每个迁移阶段必须通过：
  - 现有 `gateway/tests/unit/rag`。
  - 新增的领域、服务、API、架构测试。
  - Ruff、format check、compileall、`git diff --check`。
  - OpenAPI 归一化前后零差异。
  - 从空数据库执行 Alembic 到 head，确认历史迁移兼容。
  - 使用真实 Gateway、Worker、Redis 和模型服务运行 `test/rag_user_flow.py`，覆盖四阶段、三种检索、缓存重试、复制、级联删除和租户隔离。
- 如果集成测试暴露租约竞争、DNS 重绑定、原子复制等既有缺陷，停止对应阶段并单独登记修复；不把行为修复静默混入本次等价重构。
