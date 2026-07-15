# FDAgent Evaluation Contract

> 定义本仓库 eval case 的稳定元数据、suite 标签和本地运行命令。

## PR 前该跑什么

- 默认全量：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json`
- PR 阻塞核心集：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --suite core`
- RAG / embedding 质量烟测：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --suite rag`
- RAG retrieval benchmark：`.venv/bin/python scripts/run_eval.py --golden evals/rag/v1.json --suite rag`
- Answer-quality / honesty regression：`.venv/bin/python scripts/run_eval.py --golden evals/answer_quality/v1.json --suite answer_quality`
- Future Recall Profile boundary checks：`.venv/bin/python scripts/run_eval.py --golden evals/answer_quality/v1.json --suite firm_profile`
- 精确 case 子集：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --case numeric-class-i-count --case taxonomy-reason-breakdown`
- 查看选择结果：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --suite core --list-cases`
- 生成可持久化 baseline/report：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --suite core --report-json evals/baselines/core-local-report.json`
- 对比 feature 分支与已生成 baseline：`.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --suite core --compare-baseline evals/baselines/core-local-report.json`

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

## Answer-quality / honesty suite

`evals/answer_quality/v1.json` 固定最终答案边界：证据链接、raw FDA 与 parent-group
caveat、degraded retrieval 的「不能当作事实零结果」提示、meta/out-of-domain 不进入 SQL/RAG，
以及公司安全问题不得输出 safe/unsafe verdict 或 safety score。

可执行的数据路径 case 优先用 `kind: ask_spec`：直接给定 `QuerySpec`，跑真实
analytics/retrieval/summarize/serialize 路径，避免 LLM 路由不稳定，但仍能抓住最终答案
结构和措辞回归。`kind: answer_quality_fixture` 只用于 terminal guard message 这类不应进入
SQL/RAG 的固定消息边界，不能用来替代证据型数据路径。

该 suite 的零容忍边界必须用 deterministic assertions（文本、结构、evidence fields）
表达；LLM-as-judge 只能作为额外审计信息，不能作为唯一断言。尚未实现的 Recall Profile
行为必须写成 `kind: expected_future`，带 `blocked_by` 和完整未来断言；runner 会显式
`SKIP`，不得静默当作 pass。Issue #43 实现 Recall Profile 时，必须把这些 expected-future
case 改成可执行 `ask` case，或新增更精确 case 后删除/替换 pending case。

## 如何添加 regression case

1. 在修 bug 的同一 PR 中添加最小可复现 case。
2. 保留完整断言，不要只断言“没有报错”。
3. 设置合适主 suite，并加上 `regression` 标签。
4. `risk` 写清楚防守面；如果关联 issue/PR，可在 `notes` 中记录。
5. 用 `--case <id>` 先跑新增 case，再跑相关 `--suite`。

本合同定义 suite / metadata / selection 规则；下方数据集指纹负责 stable fixture
preflight；baseline/report 与 compare-to-main 由 issue #60 提供。PR gate、RAG
benchmark 和 answer-quality 细分实现分别由后续 issue 承担。

## Baseline/report artifact 与 compare-to-main

`scripts/run_eval.py --report-json <path>` 会写出机器可读报告（schema
`fdaagent_eval_report_v1`），用于在 `main` 上留存 baseline，再让 feature 分支做
compare-to-main。报告包含：

- git SHA / branch / dirty 状态、运行时间、golden 版本和选择的 suite/case。
- provider 配置摘要（只记录 provider/model/configured/dimension，不记录密钥）。
- issue #59 的数据集指纹元数据；如果所选 case 不需要 DB，会标注 `required: false`。
- 每个 case 的 id、suite、kind、risk、前置条件、pass/fail/skip、failure detail、
  latency、timeout 标记，以及 `/ask` case 的 route / intent / data kind /
  retrieval mode / fallback reason。
- retrieval recall case 的 `metrics.recall_at_k`，便于后续 RAG suite 比较。

推荐 compare 流程：

1. 在最新 `main` 上生成 baseline，例如：
   `.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --suite core --report-json evals/baselines/core-main-report.json`
2. 在 feature 分支运行同一选择并对比：
   `.venv/bin/python scripts/run_eval.py --golden evals/golden/v1.json --suite core --compare-baseline evals/baselines/core-main-report.json`
3. 将 concise compare summary 粘贴到 PR 评论或正文。

比较会输出 improved / unchanged / regressed / added / removed cases；出现 regression、
removed case 或 suite pass-rate 低于阈值时返回非零。阈值可按 suite 配置：

- `--compare-pass-rate-threshold core=1.0`：核心 suite 默认要求 100% pass。
- `--compare-latency-tolerance-ms core=500`：当前 latency 比 baseline 慢超过容差才算退化。
- `--compare-recall-tolerance rag=0.05`：RAG recall@k 下降超过容差才算退化。

这些报告是运行产物，不会自动 bless 新基线；需要固定进仓库时，应在 PR 中说明生成命令、
源 branch/SHA 和数据集指纹。

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
