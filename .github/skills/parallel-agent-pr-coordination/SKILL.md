---
name: parallel-agent-pr-coordination
description: "Coordinate multi-terminal or multi-agent parallel development through git worktrees and GitHub PRs. Use when: starting a main/producer agent plus feature subagents; launching parallel PR work; designing subagent prompts; monitoring PR progress across terminals; recovering from conflicting parallel PRs; enforcing main/origin sync gates, draft PR visibility, PR-comment control protocol, dependency waves, generated PROJECT_INDEX.md conflict handling, and safe cleanup. Not for: single-agent coding tasks, generic project management, or deleting completed worktrees after merge (use parallel-dev-worktree-cleanup for retirement)."
---

# Parallel-Agent PR Coordination

## Metadata
- **Type**: Workflow + BestPractice
- **Scope**: multi-terminal Copilot CLI / AI-agent development using git worktrees or clones, GitHub branches, PRs, and comments as the durable coordination surface
- **Created**: 2026-07-08

## Goal
Run parallel AI-agent development so that every child agent starts from the same pushed base, exposes progress early through GitHub, reacts to coordinator control messages, and integrates in a predictable merge order without losing work or requiring the human to repeatedly discover hidden drift.

This skill has two operating modes:
- **Coordinator / main agent**: owns preflight gates, branch/PR map, merge waves, control comments, and cleanup handoff.
- **Feature / child agent**: owns one branch/worktree/PR, keeps it current with `origin/main`, reports progress on the PR, and does not merge itself.

## Boundaries
- This skill coordinates work. It does **not** replace code review, QA, validation, or the cleanup-specific safe-delete gate in `parallel-dev-worktree-cleanup`.
- A coordinator may inspect and comment on child worktrees/PRs, but must not edit a child worktree while its child agent is still active. Either the child fixes it, or the child stops and the coordinator explicitly takes over.
- A child agent must not start from stale `origin/main`, must not rebase feature branches during collaboration, and must not merge its own PR unless the user explicitly delegates merge authority.
- GitHub PRs, issue comments, and local git state are the durable coordination surface. Terminal chat is transient; independent terminal sessions do not share live state.
- Generated files are handled by their generator, not by hand-editing conflict markers.
- `PROGRESS.md` is shared coordinator state, not a child-agent status file. A child may update the specific item it completed or append a short integration note, but must not rewrite "Next action" / "Next up" to contain only that child's task. Per-branch next steps belong in the PR body/comments unless the coordinator explicitly asks for a holistic `PROGRESS.md` reorder.

## Acceptance criteria
Before parallel development starts:
- Canonical `main` is clean and synchronized: `main` and `origin/main` point to the same commit, with no uncommitted work in the canonical checkout.
- Every planned child has an explicit owner, branch, worktree/clone path, base branch, dependency status, allowed file scope, validation expectations, and PR title.
- The prompt for each terminal states whether the agent is the **coordinator/main agent** or a **feature/child agent**; skills do not reliably infer authority from terminal position alone.

During development:
- Each child records the base SHA it started from and opens a Draft PR after the first meaningful checkpoint commit, not only at the end of the task.
- Each open PR contains enough status for another agent to recover: scope, current validation results or blockers, known overlap, and dependency notes.
- Owner comments that start with `[CONTROL]` are treated as higher priority than the original prompt, and the child replies after completing the requested action.
- After any PR merges into `main`, all still-open PR branches either merge the new `origin/main` or explicitly document why they are blocked.
- Any `PROGRESS.md` update preserves the complete multi-workstream "Next up" queue, including items owned by other agents and dependency gates.

Before any PR is merged:
- The PR is not `CONFLICTING`.
- The branch includes current `origin/main` by merge, not rebase, unless the user explicitly chose another policy.
- The PR's required targeted validation has been rerun after the last merge from `origin/main`.
- Generated-index changes are consistent with `scripts/gen_index.py --check`.

After a PR merges:
- The coordinator announces the merge to dependent/open PRs, updates the branch/PR map, and only then runs or delegates `parallel-dev-worktree-cleanup` for the merged worktree.

## Coordination protocol
Use GitHub as the shared bus because separate terminals cannot see each other's internal state. A good PR body or tracking issue includes:
- branch, worktree/clone path, base SHA, and owner terminal
- dependency wave: starts now / waits for PR N / sidecar independent
- allowed files and forbidden files
- validation commands and latest results
- overlap risks and generated-file rules

Each child prompt should require:
- stop immediately if `main` and `origin/main` are not synchronized before creating the worktree/branch
- create a Draft PR after the first meaningful checkpoint commit
- poll its PR comments before every commit, before every push, and while waiting
- treat `[CONTROL]` owner comments as authoritative
- merge `origin/main`, never rebase, when the coordinator reports that main advanced
- comment validation results after every corrective push
- keep `PROGRESS.md` holistic: do not replace "Next action" / "Next up" with the child's single local task; report child-local next steps in PR comments.

The coordinator should monitor artifacts, not hidden terminal state:
- `gh pr list --state all --json number,title,state,mergeable,headRefName,url`
- `gh pr view <n> --json state,mergeable,comments,headRefName,baseRefName`
- `git worktree list`
- `git -C <worktree> status --short --branch`
- `git -C <worktree> rev-list --left-right --count HEAD...origin/main`

## Merge-wave guidance
Parallelism is safest when the merge order is explicit:
- Core/API PRs that define response shapes or contracts merge before dependent UI PRs.
- UI PRs that depend on API shape must wait until the core PR is merged, then branch from fresh `origin/main`.
- Sidecar/offline PRs may develop in parallel, but must merge current `origin/main` before final validation and merge.
- After each merge to `main`, the coordinator posts `[CONTROL]` to all open PRs that may now be behind.

## Generated file rule
`PROJECT_INDEX.md` is auto-generated and was a repeated conflict source in this project. Treat that as expected parallel-development friction, not a business-logic conflict.

Hard rules:
- Do not hand-edit conflict markers in `PROJECT_INDEX.md`.
- Resolve by running `.venv/bin/python scripts/gen_index.py`, then `git add PROJECT_INDEX.md`.
- Verify with `.venv/bin/python scripts/gen_index.py --check`.
- If a generator changes semantics or fails, stop and report the exact error.

## Known pitfalls from the 2026-07-08 parallel sprint
- **Local main was ahead of GitHub.** Child agents that followed `origin/main` started from a stale base, even though the human's local checkout looked current. Prevention: coordinator preflight must prove `main == origin/main` before launching children.
- **PRs appeared too late.** "Do not create empty PRs" avoided noise but hid work until the end, so conflicts were discovered late. Better: create Draft PRs after the first meaningful checkpoint commit.
- **PR comments were not a live interrupt.** Owner comments only worked when pasted into terminals or when children were told to poll. Prevention: put PR-comment polling and `[CONTROL]` precedence in every child prompt.
- **`PROJECT_INDEX.md` caused repeated conflicts.** The fix was deterministic regeneration, not manual merge work.
- **The coordinator could not observe independent terminals directly.** It could only inspect git/GitHub artifacts. Tooling like an observer window is helpful per session, but it does not make separate terminal sessions share live state.
- **Dependency gates need post-merge sync too.** A frontend child correctly waited for the backend PR to merge, but later sidecar merges still made its branch conflict. Prevention: every open PR syncs after every main merge, not only after its direct dependency.
- **Do not double-own a worktree.** If a child is active, coordinator comments or asks it to stop. Direct coordinator edits to the same worktree risk racing the child agent.
- **Subagents collapsed `PROGRESS.md` to their own next step.** In parallel planning, the human may pre-stage multiple next-up items before launching several agents. Prevention: child prompts and review must treat `PROGRESS.md` "Next up" as the shared queue; a child can mark its item done/blocked but must not delete or reorder unrelated workstreams.

## Output
No required file artifact. A successful coordination run leaves:
- a visible PR map with owners, dependencies, branch names, and merge status
- PR comments documenting control actions and validation results
- no hidden stale-base work
- merged branches cleaned up through `parallel-dev-worktree-cleanup`
