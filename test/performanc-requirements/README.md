# MTEB 检索评估

数据源为 `test/datasets/MTEB/` 下四个数据集的 `dev` Parquet：`queries` 是查询，
`corpus` 是候选文本，`data` 是相关性标注。脚本只索引 corpus，以 `score > 0` 定义正例；
它评估抽样后的检索效果，不是完整 MTEB 榜单成绩。每个数据集固定种子 42，选择
200 条查询，纳入其全部正例后补齐至最多 2,000 条候选语料。

开发依赖已写入项目的 `pyproject.toml` 与 `uv.lock`。准备环境：

```bash
uv sync --group dev
```

需要 Gateway、RAG worker、PostgreSQL、Redis、Milvus、embedding 服务，以及
`RAG_TEST_EMAIL` / `RAG_TEST_PASSWORD`。Gateway 的 `VECTOR_BASE` 必须启用 Milvus。
默认从模型服务 `/v1/models` 读取 `max_model_len`；若该字段缺失，传入实际部署值
`--max-input-tokens`。默认从 Hugging Face 获取 Qwen3-Embedding-8B 的
`tokenizer.json`，离线环境可用 `--tokenizer-json` 指定本地文件。

```bash
uv run python test/performanc-requirements/mteb_retrieval_eval.py
```

脚本从用户登录开始，创建一个临时 Workspace、四个 Milvus 知识库；逐数据集完成
文件上传、解析、固定切分、向量化、Top-20 向量检索。每条候选语料经 Markdown
解析与固定切分后必须恰好对应一个 chunk，否则准备阶段失败。指标使用查询宏平均：
`Recall@K = 命中正例数/该查询全部正例数`，`Precision@K = 命中正例数/K`，
`Hit Rate@K = 有命中的查询比例`，`MRR@K = 首个正例的倒数排名`。
未标注结果按不相关处理。

结果保留在 `./tmp/rag-retrieval-eval/<时间戳>/`，包含准备文件、每个数据集的
`metrics.csv`、`queries.jsonl`、`run.json`，以及四组均完成后的 `summary.csv`。
默认删除服务端资源；`--keep-resources` 会保留以供检查。失败时写 `failure.json`，
不会生成看似完整的汇总表。
