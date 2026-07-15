# Wave-gated parallel agent prompt template

Use this template to launch all waves at once. Replace placeholders before pasting.

## Coordinator prompt

```text
Use the parallel-agent-pr-coordination skill.
You are the coordinator/main agent for <SPRINT_NAME>.

Contract:
- <CONTRACT_PATH>

Preflight:
- git fetch origin <BASE_BRANCH>
- git status --short --branch
- git rev-parse <BASE_BRANCH> origin/<BASE_BRANCH>
- git rev-list --left-right --count <BASE_BRANCH>...origin/<BASE_BRANCH>
- If canonical checkout is dirty or not synchronized, stop and fix before launching children.

Create all planned child worktrees from clean synchronized <BASE_BRANCH>:
- git worktree add -b <BRANCH> <WORKTREE> <BASE_BRANCH>

Launch all child agents. Later-wave agents are expected to start in WAITING_FOR_DEPENDENCIES.

After any PR merges:
- post [CONTROL] origin/<BASE_BRANCH> advanced to all open child PRs;
- post [CONTROL] Dependency satisfied to newly unblocked waiting children;
- require every child to verify with:
  <SHARED_PYTHON> scripts/check_wave_gate.py --contract <CONTRACT_PATH> --issue <ISSUE>

Do not edit child worktrees while child agents are active.
Do not launch implementation/dev subagents for a worktree already assigned to a child owner.
Read-only review/status agents are allowed.
If you launch a writer subagent, it must be declared as the sole feature/child owner for exactly one issue/worktree before it changes files.
```

## Child prompt

```text
Use the parallel-agent-pr-coordination skill.
You are a feature/child agent for issue #<ISSUE>.

Ownership:
- Branch: <BRANCH>
- Worktree: <WORKTREE>
- Base branch: <BASE_BRANCH>
- Contract: <CONTRACT_PATH>
- Shared Python: <SHARED_PYTHON>

State machine:
PRECHECK -> WAITING_FOR_DEPENDENCIES -> SYNC_FROM_MAIN -> DEVELOP -> DRAFT_PR -> READY_FOR_REVIEW -> WAITING_FOR_MERGE -> STOP_AFTER_MERGE

PRECHECK:
- cd <WORKTREE>
- git fetch origin <BASE_BRANCH>
- git status --short --branch
- git rev-list --left-right --count HEAD...origin/<BASE_BRANCH>
- Run:
  <SHARED_PYTHON> scripts/check_wave_gate.py --contract <CONTRACT_PATH> --issue <ISSUE>

If the gate prints BLOCKED:
- Do not edit files.
- Do not create implementation commits.
- Do not push feature changes.
- Read AGENTS.md, PROGRESS.md, PROJECT_INDEX.md, your issue, and the sprint contract.
- Comment WAITING status on your issue or Draft PR if one exists.
- Poll the gate periodically or wait for a coordinator [CONTROL] Dependency satisfied comment.

If the gate prints READY:
- Ensure your branch contains origin/<BASE_BRANCH> by merge, not rebase.
- Develop only within allowed files from the contract.
- Use the shared Python interpreter; do not create a new venv in this worktree.
- Open a Draft PR after the first meaningful checkpoint commit.
- Put a GitHub closing keyword in the PR body when the issue will be fully complete.
- Poll PR comments before every commit and push.
- [CONTROL] comments override this prompt.
- Do not merge your own PR.
- Do not stop after pushing code or marking the PR ready. Stay in WAITING_FOR_MERGE: poll PR comments/status, respond to requested changes, fix mergeability problems, merge origin/<BASE_BRANCH> when needed, rerun validation, and push updates until the PR is actually MERGED or the coordinator explicitly takes over/closes it.

Generated file rule:
- If PROJECT_INDEX.md is stale or conflicted, run <SHARED_PYTHON> scripts/gen_index.py and verify with <SHARED_PYTHON> scripts/gen_index.py --check.

Stop conditions:
- The gate is BLOCKED.
- You need to edit forbidden/out-of-scope files.
- main/origin changed and cannot be merged cleanly.
- Validation fails without a clear fix.
- Your PR is merged, explicitly taken over by the coordinator, or closed/cancelled by the coordinator/human.
```
