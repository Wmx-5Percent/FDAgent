# Hybrid Search 动态 aliases、可解释融合与检索日志

> Tracking issue: [#76](https://github.com/Wmx-5Percent/FDAgent/issues/76).
> Development PR: [#75](https://github.com/Wmx-5Percent/FDAgent/pull/75)
> (`codex/openai-embedding-provider-plan`, Draft).
> Status snapshot: 2026-08-06. 本文是 Hybrid Search Debug UI 的实现交接计划，
> 覆盖动态 FTS aliases、可调 weighted RRF、排序解释和检索 trace 历史。

## Problem

当前 `/hybrid-search` 页面存在三个相互关联的问题：

- Example 为 `pills too strong` 预设了静态 aliases。用户更换 query 后，旧 aliases
  不会随之更新，导致无关关键词继续参与 FTS。
- 当前融合不是“语义分数乘固定百分比 + FTS 分数乘固定百分比”，而是两路等通道
  RRF：每路按 `1 / (60 + rank)` 贡献分数。但 UI 没有展示分项贡献，用户无法解释
  Top 结果为什么排在前面。
- aliases 对应的 FTS 查询结果目前逐个追加。同一 recall 被 query 或多个 alias 命中时，
  可能在 FTS 通道中被重复计分；alias 数量和顺序因此可能放大 FTS 的实际影响。
- `/hybrid-search` 已写入独立的 `hybrid_search_log`，但日志只保存运行摘要，没有两路
  候选排名和每条结果的融合贡献；Debug UI 也无法浏览历史运行。

## Decision

- aliases 使用当前 serving chat provider/model 动态生成。query 停止输入约 700ms 后
  自动请求，生成结果回填输入框并允许人工编辑。
- vector 与 FTS 使用 weighted RRF，Debug UI 提供联动百分比控制，默认
  vector 50% / FTS 50%。该设置只影响 retrieval lab，不改变 `/ask` 的生产检索默认值。
- FTS 必须先在通道内部按 recall/field 去重并形成一条稳定排名，再与 vector 排名融合；
  同一 recall 命中多个 query variants 时只获得一次 FTS 通道贡献。
- 每次 Debug UI 检索保存两路各 Top 100 的精简 trace，并在页面提供最近运行和单次
  详情。日志继续与 `/ask` 的 `query_log` 分离。

## API and data contract

### Alias generation

新增 `POST /hybrid-search/aliases`：

```json
{
  "query": "too weak",
  "field": "both"
}
```

成功响应：

```json
{
  "query": "too weak",
  "field": "both",
  "aliases": ["subpotent", "low potency", "below specification", "low assay"],
  "status": "generated",
  "provider": "openrouter",
  "model": "configured-chat-model"
}
```

约束：

- 使用结构化 LLM 输出，最多返回 8 个 aliases。
- 清理空项、重复项、原 query、泛化的 `drug` / `recall` / `problem` 等词和超长内容。
- 当前 FTS 使用 English text configuration，因此非英文 query 也生成英文概念词或
  同义短语。
- provider 不可用时返回安全的降级状态和空 aliases；搜索仍使用原 query，不复用旧
  query 的 aliases。
- 不返回或记录 API key、base URL、请求头或 provider 原始敏感错误。

### Hybrid search request

`POST /hybrid-search` 在现有字段上增加：

```json
{
  "vector_weight_percent": 50
}
```

- 取值范围为 `0` 至 `100`，默认 `50`。
- `vector_weight = vector_weight_percent / 100`。
- `fts_weight = 1 - vector_weight`。
- 现有 `aliases` 字段保留，代表 UI 最终确认或编辑后的 aliases。

融合公式：

```text
vector_contribution = vector_weight / (60 + vector_rank)
fts_contribution    = fts_weight / (60 + fts_rank)
rrf_score           = vector_contribution + fts_contribution
```

某通道未命中时，该通道贡献为 `0`。响应的每条结果增加：

- `vector_rrf_contribution`
- `fts_rrf_contribution`
- `vector_contribution_percent`
- `fts_contribution_percent`
- `matched_fts_queries`

响应 metadata 增加 `fusion_config`，明确返回 RRF 常数、两路权重和候选池限制。
CSV/JSON 导出同步包含这些字段。

### FTS channel aggregation

- 分别执行原 query 和 aliases 对应的 FTS 查询，但在进入 RRF 前按 fusion key 去重。
- `field=both` 时 fusion key 为 recall number；单 field 时为 recall number + field。
- 同一 key 命中多个 query variants 时，使用最高 `ts_rank_cd` 作为 FTS 原始分数，
  同时保存全部 `matched_fts_queries`。
- 对去重后的候选按 FTS 分数降序重新编号；并列时使用 vector distance、recall number
  和 field 做稳定 tie-break。
- alias 数量或排列顺序不得改变相同候选集合的 FTS 排名。

### Trace persistence and history

新增幂等迁移，在 `hybrid_search_log` 增加：

- `fusion_config jsonb`
- `alias_generation jsonb`
- `vector_trace jsonb`
- `fts_trace jsonb`
- `fused_trace jsonb`

每路 trace 最多保存 100 条：

- vector trace：recall ID、field、rank、distance、similarity、RRF contribution。
- FTS trace：recall ID、field、rank、FTS score、matched queries、RRF contribution。
- fused trace：最终 Top k 的 rank、总分、两路贡献和实际贡献比例。

Trace 不保存 embedding 向量、完整 `reason_for_recall`、完整 `product_description`
或其他可从 FDA 表重新读取的长文本。

新增只读接口：

- `GET /hybrid-search/logs?limit=25&before_id=<id>`：按 ID 倒序分页返回运行摘要；
  `limit` 范围为 1–50。
- `GET /hybrid-search/logs/{id}`：返回单次完整 trace，不存在时返回 404。

成功、embedding/LLM provider 降级、过滤校验失败和服务端错误都应保留日志；错误记录
允许 trace 为空，但必须保存已知的 request、alias 状态、权重、耗时和安全错误摘要。

## UI behavior

- query 输入停止约 700ms 后自动生成 aliases，并用 `AbortController` 取消旧请求。
- UI 维护 `generating`、`generated`、`manual`、`stale`、`error` 状态：
  - query 变化立即将旧 aliases 标记为 stale；
  - 新生成结果只在 query 版本仍匹配且用户尚未手工编辑时回填；
  - 用户编辑 aliases 后标记为 manual，未再次修改 query 前不被迟到响应覆盖；
  - 点击 Search 时若当前 query 的 alias 请求仍在进行，则等待该请求结束后再提交。
- “Load example” 只加载 query、field、k 和 filters，不再写入静态 aliases。
- 增加联动的 vector/FTS 百分比控件，默认 50/50，总和始终为 100%。
- Run metadata 展示实际 retrieval mode、RRF 公式、权重、aliases 来源、候选池数量和
  trace log ID。
- 结果表展示两路 rank、原始分数、分项 RRF contribution、实际贡献比例和命中的
  FTS query variants。
- 页面增加最近 25 次运行的历史面板。选择一条历史记录只读取日志详情，不重新执行
  embedding 或搜索。

## Validation

### Alias behavior

- 将 query 从 `pills too strong` 改为 `too weak` 后生成 subpotent 相关 aliases，
  不再保留 superpotent aliases。
- sterility query 和中文 query 都能得到适合 English FTS 的相关 aliases。
- 快速连续修改 query 时只采用最后一次生成结果。
- 人工编辑 aliases 后，迟到的 LLM 响应不能覆盖用户输入。
- alias provider 失败时仍可用原 query 搜索，并在响应和日志中标记降级。

### Ranking behavior

- 50/50、100/0 和 0/100 分别验证 weighted RRF 公式。
- 同一 recall 命中多个 aliases 时只获得一次 FTS 通道贡献。
- aliases 顺序变化不改变 FTS 排名和最终结果。
- `hybrid`、`fts_only`、只有 vector 命中、只有 FTS 命中和零命中时，贡献字段均符合
  定义且不产生除零错误。
- 对 `pills too strong` 的第一名可以由 vector rank、FTS rank 和两项贡献完整重建。

### Logging and regression

- 成功、provider 降级、400 过滤错误和 500 安全错误都产生历史记录。
- vector/FTS trace 各不超过 100 条，且不包含 embedding 或完整长文本。
- 历史分页稳定；日志详情中的 Top IDs、权重和分数与原 API 响应一致。
- 运行现有 hybrid filter smoke checks、RAG eval、API/UI smoke test和
  `python scripts/gen_index.py --check`。

## Implementation boundaries

- 不修改 `/ask` 的请求协议、默认检索权重或 `query_log` 表职责。
- 不引入新的 embedding 模型，也不重新生成 corpus vectors。
- Debug 历史按当前本地/管理用途实现；本轮不新增账号或权限系统。
- 实现时保留工作区内现有 embedding-provider、文档和 eval 的未提交改动，避免覆盖
  并行工作。
