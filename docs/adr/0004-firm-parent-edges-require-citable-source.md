# firm→parent 归属边需可引用结构化出处 + 人工确认才生效；LLM/web 仅提名

Status: accepted
Date: 2026-07-10

## 决定

一条 firm→parent 归属边要"生效"（进入推断块、影响合计），必须满足：

- 有**可引用的结构化出处**（**Wikidata** 优先：`parent organization` / `owned by` + QID + 收购年份；SEC EDGAR Exhibit 21 / GLEIF LEI 补充）；
- **头部边经人工确认**后才写入。

**LLM / web 只能提名候选、消歧，永不作为最终依据。** 查不到出处的 firm → **自成 parent（`unknown`），绝不编造**。每条生效边落库带 `source` / `provenance_tier=inferred_external` / as-of 日期 / citation。

walking skeleton 先用**极小手写种子**（`Pfizer` ← `Hospira`/`King`/`Meridian` 等几家大厂）跑通两块式，Wikidata 批量拉取当作紧接着的扩展。

## 为什么

parent 层 100% 是推断（[ADR-0003](0003-recall-profile-two-block-fact-vs-inferred.md)），是全系统唯一非 FDA 的一层，最易被 LLM 幻觉 / 过时污染。项目此前正是让 web/LLM 的 verdict **直接决定合并**，遭遇 OpenRouter 402、且有**静默污染计数**的风险（`deepseek/deepseek-v4-pro` 事故）。把"生效"门槛焊死在**可引用 + 人确认**上，让爆炸半径可控、可审计、可推翻。

## 取舍

- **LLM/web 直接写**：覆盖广、快，但会编、会过时、不可引用（已翻车）。
- **结构化 + 人确认**：慢、只头部划算，但可信可引。选后者；尾部宁可 `unknown` 也不编。

## 后果

- 需要一个 Wikidata 拉取 + 人工确认的头部种子流程；尾部 self-parent 直到有出处。
- 每条边可点开看出处；归属错了只影响推断块（[ADR-0003](0003-recall-profile-two-block-fact-vs-inferred.md)）。
