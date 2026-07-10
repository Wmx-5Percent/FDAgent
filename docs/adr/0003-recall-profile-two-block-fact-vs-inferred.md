# C 端答案两块式呈现：FDA 事实为锚 + 标注推断的母公司汇总

Status: accepted
Date: 2026-07-10

## 决定

Recall Profile 的头条**不揉成一个母公司数字**，而是分两块、用分割线隔开：

1. **FDA 事实块** —— firm 级（FDA 原样记录的公司名）的召回，100% `fda_fact`，是锚。
2. **推断块** —— 加上"我们推断属于该母公司"的子公司（如 `Hospira` → `Pfizer`），**整块打 `[inferred]`**，且每条归属边带出处（如 Wikidata / 收购年份）。合计数字属于推断块。

全部可下钻到 `recall_number`。本决定**精化了 [ADR-0001](0001-firm-answers-aggregate-to-parent-group.md)** 的"默认聚合到 Parent Group"。

## 为什么

Parent 层 **100% 是推断**：FDA 从不记录股权归属；NDC labeler 也只到 firm 级，给不出母公司。若把 firm 事实和推断归属揉成一个头条数字（如 "Pfizer 308 条"），头条约一半（`Hospira` 的 153 条）就建在可能出错 / 过时的归属推断上，直接违背项目"数字来自 FDA、可审计"的招牌。分块把事实和推断的**爆炸半径隔开**：归属错了只砸推断块，砸不到 FDA 事实块。

## 取舍

- **纯 parent 单数字**：最简洁，但头条含隐藏推断，错了砸招牌。
- **纯 firm**：最硬，但漏掉子公司、违背 Q2 的用户直觉（"Pfizer 安不安全"应含 Hospira）。
- **两块式（本决定）**：稍复杂，但同时满足"直觉的整体视图"和"事实 / 推断分离"。

## 后果

- 答案渲染需要一个"事实核 + 标注推断展开"的新形态（UI 分区 + `[inferred]` 徽标 + 出处可点开）。
- 每条 firm→parent 边必须携带 `provenance_tier` / `source` / as-of 日期。
- 合计（含推断）数字永远标 `[inferred]`；负结果表述为"FDA 未找到"，不是"安全"。
