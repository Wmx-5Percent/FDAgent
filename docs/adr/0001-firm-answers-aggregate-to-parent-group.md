# 公司类答案聚合到 Parent Group，证据下钻到 recalling_firm

Status: accepted
Date: 2026-07-10

## 决定

C 端"这品牌 / 这公司在 FDA 安不安全"的回答，**默认聚合到 Parent Group（母公司集团）层**——把被收购子公司的召回并入母公司；但**每个数字永远可下钻到 `recalling_firm` 原始写法 + `recall_number`** 作为证据。品牌→firm→parent 的每一跳都带 **Provenance Tier**（`fda_fact` / `inferred_external_or_llm` / `unknown`）。归属层用"物化映射表 + 头部手工种子 + 增量刷新 + 人工复核闸门 + 实时检索仅作低置信兜底"保持新鲜。术语定义见 [CONTEXT.md](../../CONTEXT.md)。

## 为什么

FDA 数据里唯一存在的公司身份是 `recalling_firm`（1,634 个碎片写法），既没有母公司层，也没有品牌层。用户直觉里的"公司"是整个集团：实测 `Hospira` 165 条召回里 **153 条（93%）名字不含 "Pfizer"**，若不把 Hospira 并入 Pfizer，Pfizer 的真实 FDA 足迹被低估近一半。所以答案必须在 parent 层聚合才不误导。

## 取舍

- **只按 `recalling_firm` 分组（不并母公司）**：简单、纯 FDA 事实，但"Pfizer 安不安全"会漏掉一半召回 → 误导。
- **并到 parent（本决定）**：更真实，但母公司归属**不在 FDA 数据里**、靠外部知识、且随并购 / 拆分变化（J&J→Kenvue 2023），会出错 / 过时。
- 用两条纪律控风险：① 证据永远下钻到 FDA 原始 `recalling_firm` + `recall_number`（可审计、可推翻）；② 每跳标 Provenance Tier，把"FDA 事实"和"我们的推断"显式分开，归属带 as-of 日期。

## 后果

- 需要一层物化的 `firm → parent_group` 映射（`parent_group` 表 + `firm.parent_group_id`），且要能**增量刷新**——不是一次性 batch。
- web / LLM 得到的归属**绝不直接进精确计数**，只作展示或排队人工复核。
- 负结果表述为"FDA 未找到"，**不是"安全"**。
