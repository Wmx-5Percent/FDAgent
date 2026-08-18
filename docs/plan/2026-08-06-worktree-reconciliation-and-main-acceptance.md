# FDAgent worktree 收尾与主分支验收计划

> 状态快照：2026-08-06。本文是供并行窗口读取的执行边界与验收合同，不替代
> `PROGRESS.md` 的项目级动态队列，也不授权直接合并、删除或关闭 GitHub 资源。

## 已确认状态

- 主工作区是 `/Users/waywei/Desktop/developer/fdaAgent`，唯一目标分支为 `main`。
- 审计时 `main` 为 `ca546d90fa5fcb27b09151cab9b7143d007510c9`，与 `origin/main` 同步且工作树干净。
- GitHub 没有打开的 Pull Request；不能把任何旧 issue 分支直接 merge 回 `main`。
- 已执行的官方回归门禁：

  ```bash
  .venv/bin/python scripts/run_eval.py --core-pr-gate
  ```

  结果为数据库指纹匹配、38/38 通过、0 失败、0 跳过。

## worktree 处置规则

所有以下目录在审计时均干净。它们是历史 issue worktree，不是待直接合并的变更来源：

| Worktree / issue | 已合入状态 | 本计划中的处置 |
| --- | --- | --- |
| `fdaAgent-issue-42-parent-group-rollup` / #42 | PR #66 已合入 | 不 merge、不关闭 issue；等待真实 parent-group 数据前置条件。 |
| `fdaAgent-issue-43-recall-profile-advisory` / #43 | 尚无 Recall Profile 实现 | 保持阻塞，不能在 #42 未完成时开工。 |
| `fdaAgent-issue-47-sql-debug-panel` / #47 | PR #65 已合入 | 不再从旧分支摘取或合并补丁。 |
| `fdaAgent-issue-56-openrouter-embeddings` / #56 | PR #64 已合入 | 不再合并。 |
| `fdaAgent-issue-57-baseline-system-integration` / #57 | PR #73 已合入 | 不再合并。 |
| `fdaAgent-issue-58-contract-suites` 至 `fdaAgent-issue-63-answer-quality-suite` / #58–#63 | PR #67–#72 已合入 | 不再合并。 |

后续专门的清理任务才可以在再次确认所有 worktree 干净后执行：

```bash
git worktree remove <明确的-worktree-路径>
git worktree prune
```

该清理任务不得使用 `--force`，不得删除本地或远程分支引用，也不得改变 #42/#43 的 issue 状态。恢复 #42 或 #43 时，必须从最新 `origin/main` 新建 worktree 和分支，不能复用上述旧目录。

## #42 / #43 的未完成前置条件

### #42：parent-group exposure

PR #66 仅把 sidecar 和展示前置代码合入了 `main`。issue 保持打开是有意的，因为尚未具备：

1. 可引用且人工确认的 `firm -> parent_group` 映射数据；
2. 覆盖高召回 firm 的可审计 parent edge；
3. 在当前 `main` 上对 firm sidecar 的确定性重建和验证。

未满足这些条件前，parent-group 结果必须保持 caveated；不能把未知、LLM-only 或未确认映射混入精确排名。

### #43：Recall Profile

#43 依赖 #42，当前没有可合并的 Recall Profile 实现。等 #42 的数据前置完成后，新窗口应从最新 `main` 开始，并保持既有产品约束：输出 FDA 召回画像，不给出“安全/不安全”结论；FDA 事实与推断的 parent rollup 必须分块展示；证据链接和身份不确定性必须可见。

## 主分支用户路径验收

在不运行数据写入、迁移或 ingest 的前提下，按以下顺序验证 `main`：

1. 启动本地服务：

   ```bash
   .venv/bin/python -m uvicorn src.api:app --host 127.0.0.1 --port 8000
   ```

2. 请求 `http://127.0.0.1:8000/health`。预期 `status` 为 `ok`、`engine` 为 `ready`，并只查看 provider 状态，不输出任何密钥。
3. 在浏览器打开 `http://127.0.0.1:8000/`，确认聊天 UI 正常加载。
4. 在 UI 提交：`How many Class I drug recalls have there been?`。
5. 验收标准：页面无前端错误；`/ask` 返回正常结果；答案为可解释的 FDA 召回统计并带证据，而不是空白页、500 或无依据的安全结论。
6. 结束本次启动的 uvicorn 进程，随后运行：

   ```bash
   git status --short --branch
   .venv/bin/python scripts/gen_index.py --check
   ```

若真实 `/ask` 因 provider 凭据、额度或网络失败，应记录为运行环境问题，不应据此修改或回退已通过核心门禁的 `main`。

## 并行窗口边界

- 本文所在任务只新增计划文档及其索引；不修改业务代码、不移除 worktree、不创建 commit 或 PR。
- 任何实现窗口在写代码前都应重新检查 `main`、目标 issue、对应 PR 和 worktree 状态。
- 没有新的、独立审查的 PR 时，禁止直接 merge 历史 issue 分支。
- #42 完成前，#43 只能进行读取、设计或验收准备，不能开始实现或宣称完成。
