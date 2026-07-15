# FDAgent Evaluation Contract

> 定义本仓库 eval case 的稳定元数据、suite 标签和本地运行命令。

## PR 前该跑什么

- 默认全量：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json`
- PR 阻塞核心集：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --suite core`
- RAG / embedding 质量烟测：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --suite rag`
- RAG retrieval benchmark：`.venv/bin/python scripts/run_eval.py --golden evals/rag/v1.json --suite rag`
- 精确 case 子集：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --case numeric-class-i-count --case taxonomy-reason-breakdown`
- 查看选择结果：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --suite core --list-cases`

`--suite` 和 `--case` 都支持重复传入或逗号分隔；同时传入时取交集。运行本地
`ask` case 需要本地 Postgres `fda` 数据库和 chat provider；`rag` case 还可能需要
embedding provider。每个 case 用 `requires_llm` / `requires_embedding` /
`requires_db` 显式声明这些前置条件。

## RAG retrieval benchmark

`evals/rag/v1.json` 是固定查询 + 固定 `recall_number` 证据集的检索基准。每个
`retrieval_recall` case 输出 provider/model/dimension、`retrieval_mode`、fallback
reason、vector/FTS/fused hit counts、`recall@k`、MRR、nDCG、matched recall numbers
和 returned recall numbers。

- `retrieval_mode=hybrid` case 需要可用 embedding provider；provider 不可用时必须
  `SKIP` 并打印 provider/model/fallback metadata，不能当作 zero-recall 通过。
- `retrieval_mode=fts_only` fallback case 使用 `simulate_embedding_fallback`，不需要
  外部 embedding credentials，用来固定 degraded retrieval 行为和 fallback reason；
  需要区分具体错误类型时可用 `simulate_embedding_error`（例如
  `ProviderMissingKeyError`）。
- 当前基准使用 per-case metric floors（如 `min_recall_at_k` / `min_mrr_at_k` /
  `min_ndcg_at_k`）来捕捉召回率下降；跨分支 baseline snapshot/compare-to-main 报告由
  后续 baseline issue 实现，避免在这里重复。

## Case 元数据契约

每个 `evals/golden/*.json` case 必须包含：

- `id`：稳定唯一 ID；修 bug 后不要重命名既有 regression case。
- `kind`：runner 分发类型，例如 `ask`、`deterministic_helper`、`retrieval_recall`。
- `suite`：非空 suite 标签字符串或字符串数组；标签必须在文件顶层 `suites` 中定义。
- `risk`：该 case 防守的风险面，例如 `agent_control`、`filter_preservation`、`retrieval_recall_at_k`。
- `requires_llm` / `requires_embedding` / `requires_db`：布尔前置条件。
- `assert`：可执行断言对象；输入字段（如 `question`、`query`、`k`）留在 case 顶层。

Runner 会在执行前校验这些字段，防止新 case 漏掉 suite 或前置条件说明。

## Suite 标签

- `core`：稳定、可作为 PR 阻塞的核心 `/ask`、deterministic helper 与 provider 配置回归。
- `rag`：检索、embedding、recall@k 或语义路由质量；通常不应混入快速 PR gate。
- `answer_quality`：最终答案诚实性、证据边界、措辞约束；由后续 answer-quality 工作扩展。
- `firm_profile`：未来 Recall Profile / 公司画像行为；不得提前实现业务路由。
- `regression`：所有已修 bug 的永久回归标签；可与 `core`、`rag` 等主 suite 同时存在。

## 如何添加 regression case

1. 在修 bug 的同一 PR 中添加最小可复现 case。
2. 保留完整断言，不要只断言“没有报错”。
3. 设置合适主 suite，并加上 `regression` 标签。
4. `risk` 写清楚防守面；如果关联 issue/PR，可在 `notes` 中记录。
5. 用 `--case <id>` 先跑新增 case，再跑相关 `--suite`。

本合同只定义 suite / metadata / selection 规则。下方数据集指纹只负责 stable fixture
preflight；baseline 对比、PR gate、RAG benchmark 和 answer-quality 细分实现分别由后续
issue 承担。

## 数据集指纹 preflight

`scripts/run_eval.py` 在执行任何 `requires_db: true` 的 case 前，会先检查本地
`drug_enforcement` fixture 是否等于已审查的稳定数据集指纹：

- baseline：`evals/baselines/drug_enforcement_fingerprint.json`
- 算法：`scripts/dataset_fingerprint.py` 读取稳定字段 `id`、`source`、`report_date`
  和完整 `raw` JSONB，排除易变的 `fetched_at`，将日期固定为 `YYYY-MM-DD`，
  按 `id` 排序后计算 SHA-256；
  同时记录 row count、`report_date` 范围、taxonomy label 覆盖、embedding 覆盖、
  以及当前 `drug_enforcement` schema/index 签名。
- 默认检查：`.venv/bin/python scripts/dataset_fingerprint.py --check`
- DateStyle 回归检查：`PGDATESTYLE='SQL, MDY' .venv/bin/python scripts/dataset_fingerprint.py --check`
- 查看当前指纹：`.venv/bin/python scripts/dataset_fingerprint.py`

如果 preflight 失败（`run_eval.py` 返回 3 / `DATASET DRIFT`），先确认本地 fixture 是否
误跑了 ingest 或连接到错误 DB。确实要接受新 fixture 时，必须在单独可审查的变更中显式运行
`.venv/bin/python scripts/dataset_fingerprint.py --write-baseline` 并说明原因；
`run_eval.py` 不会静默刷新或 bless 新 baseline。临时调试可用
`--skip-dataset-fingerprint`，但不能把它当作 PR 验证结果。
