# 受托/自主 Agent 只可自主执行可逆动作；不可逆动作须人工确认

Status: accepted
Date: 2026-07-11

## 决定

Agent（无论"自主循环"还是"受托 CLI"）能自主做什么，按**动作的可逆性**分两类，而**不是**按"是哪个任务"、也不是按"有没有人盯着"：

- **可逆动作 —— Agent 可全自主**：改 git 追踪的代码、跑 dry-run、产出报告/校准数字、本地 `commit`、开 Draft PR。这些出错都能用 `git` 撤销。
- **不可逆动作 —— 必须人工确认后才可执行**：向数据库写身份/标签（`src/firm/resolve.py --apply` 写 `firm`/`firm_alias`/`parent_group`/`brand_alias`；`src/classify/*.py --apply` 写 taxonomy / `recall_label`）、裁决 `needs_review`、冻结 taxonomy 版本、`merge` / `push` 到 `main`、`git reset --hard` / `push --force` 等破坏性 git。

## 为什么

`git` 只回滚**代码**，回滚不了 **Postgres**。`resolve.py --apply` 是 `INSERT ... ON CONFLICT (normalized_name) DO UPDATE` 的原地 upsert，本项目**没有 DB 快照 / 一键恢复**；一次错误合并（例：把 `Sun … Inc`(美) 与 `Sun … Ltd`(印) 合并，违反 [CONTEXT.md](../../CONTEXT.md) 与 [ADR-0004](0004-firm-parent-edges-require-citable-source.md)）在库里落地后，"大不了 git 回滚"是**空话**。

更隐蔽的风险：以"任务完成 / 测试绿"为目标的 Agent 有**结构性动机去凑指标**——把 485 个 `needs_review` 全 accept 来达成"0 needs_review"，或无出处编 `parent` 边——这类**伪装成成功的正确性 bug**，测试和 `git` 都不会报警。把闸焊在"不可逆动作"上，爆炸半径才可控。项目此前正是让 web/LLM 直接决定合并，在 PR #18 上翻过车（OpenRouter 402 + 静默污染计数风险）。

## 取舍

- **让 Agent 连 `--apply` / `merge` 也自主**：快、真"无人值守"，但一次错写就污染实体图且不可逆（已翻车）。
- **不可逆动作留人工**：慢一点、需要人一次性批量裁决，但可控、可审计、可推翻。选后者；尾部宁可 `unknown` 也不编。

## 后果

- 夜间 / 受托运行只产出**可逆产物**（代码 + dry-run 提案 + 本地 commit + Draft PR）；`--apply` / 冻结 / 裁决 / `merge` 全部排队等人工。
- 这条边界是所有受托 Agent prompt 的护栏依据，也是给"协调 Agent / 审查 Agent"划权限的依据——**审查 Agent 只评论、不越权改**，最终对不可逆步骤的裁决权留在人手里。
- 未来若要真·无人值守放行不可逆动作，前置条件是先建"机器可查的谓词门 + DB 快照/恢复"；在那之前不放开。
