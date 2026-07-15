---
name: parallel-wave-gated-agents
description: "Launch and coordinate multi-wave child-agent development where all agents can start at once, but later-wave agents must wait on machine-checkable dependency gates before writing code. Use when: a sprint has multiple waves, dependency-gated issues, waiting child agents, a need for a sprint contract, or the user wants to avoid manually starting each wave. Not for: single-wave parallel PR work (use parallel-agent-pr-coordination), writing feature code, reviewing PRs, or deleting merged worktrees."
---

# Parallel Wave-Gated Agents

## Goal

Enable a multi-wave AI development sprint where the coordinator can launch all child agents at once, while every later-wave child is forced by a machine-checkable gate to wait until its dependencies are satisfied before editing files, committing, or pushing implementation work.

This skill turns "wave order in the human's head" into durable artifacts:

- a sprint contract under `.github/parallel-sprints/`;
- a gate command each child can run;
- coordinator and child prompts that encode the same state machine;
- PR comments and GitHub issue state as the shared coordination bus.

## Boundaries

- This skill designs and bootstraps wave-gated coordination. It does **not** implement the feature code owned by the child agents.
- For ordinary same-wave PR coordination, use `parallel-agent-pr-coordination`.
- For copy/paste prompt authoring without a gate contract, use `parallel-agent-prompt-pack`.
- For retiring merged local worktrees, use `parallel-dev-worktree-cleanup`.
- A blocked child may read issues/docs, run the gate checker, and post `WAITING` status, but must not edit files, create implementation commits, or push feature work.
- The coordinator may create worktrees and post `[CONTROL]` comments, but must not double-own a child worktree or launch a writer subagent for a worktree already assigned to a child.
- Read-only review/status subagents are allowed for the coordinator. Implementation/dev subagents are writers; they are allowed only when declared as the sole child owner for one issue/worktree before code changes.

## Required result

A successful wave-gated setup has:

- a contract file, usually `.github/parallel-sprints/current.json` for a local run or a named committed JSON for a durable sprint;
- every child issue represented with issue number, wave, branch, worktree, dependencies, allowed files, forbidden scope, validation, and PR title;
- every dependency represented as a machine-checkable gate, not prose such as "after the previous wave";
- a coordinator prompt and child prompts that explicitly reference the contract and `scripts/check_wave_gate.py`;
- all child worktrees created from a clean synchronized base branch by the coordinator;
- later-wave children launched in `WAITING_FOR_DEPENDENCIES` state until the gate returns `READY`;
- a visible PR/issue comment trail showing which children are active, waiting, ready for review, responding to review, or stopped after merge.

## Acceptance criteria

- `git status --short --branch` in the canonical checkout is clean before child worktrees are created.
- `git rev-list --left-right --count <base>...origin/<base>` returns `0 0` before children start.
- `scripts/check_wave_gate.py --contract <contract> --issue <N>` exits `0` and prints `READY` only when all dependencies are satisfied.
- The same command exits non-zero and prints `BLOCKED` for at least one intentionally blocked later-wave child in the contract self-check.
- Every child prompt includes this state machine: `PRECHECK -> WAITING_FOR_DEPENDENCIES -> SYNC_FROM_MAIN -> DEVELOP -> DRAFT_PR -> READY_FOR_REVIEW -> STOP_AFTER_MERGE`.
- Every later-wave child prompt says that `BLOCKED` means no file edits, no implementation commits, and no feature pushes.
- Every child prompt uses the shared interpreter path from the contract or canonical checkout and tells the child not to create a new per-worktree venv.
- Every child PR body includes the tracking issue closing keyword only when that PR fully completes the issue.
- After any PR merge, open/waiting children are told to merge `origin/<base>`, not rebase, then rerun the gate before coding.
- A child is not complete when it merely pushes code or marks its PR ready. It remains the owner until its PR is actually `MERGED`, the coordinator explicitly takes over, or the PR is closed/cancelled by the coordinator/human.

## Contract resources

Use these repository resources:

- `.github/parallel-sprints/README.md` — framework behavior and child state machine.
- `.github/parallel-sprints/CONTRACT-TEMPLATE.md` — JSON contract shape and supported dependency gates.
- `.github/parallel-sprints/PROMPT-TEMPLATE.md` — coordinator/child prompt text that can be specialized per sprint.
- `scripts/check_wave_gate.py` — machine gate checker.
- `.github/parallel-sprints/current.json` — ignored local default contract path for a disposable active sprint.

Supported dependency gates should stay simple and externally observable:

- GitHub issue is closed/open.
- GitHub PR is merged.
- Base branch is synchronized with origin.
- Child worktree is clean and contains origin base.

If a dependency cannot be checked by script, encode it as a human `[CONTROL]` requirement and keep the child blocked until the coordinator posts the approval.

## Child state machine contract

All child agents in all waves follow the same states:

```text
PRECHECK
  -> WAITING_FOR_DEPENDENCIES
  -> SYNC_FROM_MAIN
  -> DEVELOP
  -> DRAFT_PR
  -> READY_FOR_REVIEW
  -> WAITING_FOR_MERGE
  -> STOP_AFTER_MERGE
```

The state machine is a hard behavioral contract, not just documentation:

- `PRECHECK`: read the contract, fetch origin, verify branch/worktree identity, run the gate.
- `WAITING_FOR_DEPENDENCIES`: no implementation writes; only read, poll, and comment waiting status.
- `SYNC_FROM_MAIN`: merge `origin/<base>`, never rebase, then rerun the gate.
- `DEVELOP`: edit only allowed files and obey the generated-index rule.
- `DRAFT_PR`: open a Draft PR after the first meaningful checkpoint commit.
- `READY_FOR_REVIEW`: document validation and wait for coordinator/human review.
- `WAITING_FOR_MERGE`: keep polling PR comments/status, respond to review comments, fix requested changes, resolve mergeability problems by merging `origin/<base>`, rerun validation, and push updates. Do not abandon the workstream just because the PR is ready.
- `STOP_AFTER_MERGE`: stop pushing and leave cleanup to coordinator.

## Prompt requirements

Every generated coordinator prompt must specify:

- role: coordinator/main agent;
- canonical checkout, repo, base branch, shared Python path, contract path;
- preflight checks proving base is clean and synchronized;
- all child worktree creation commands;
- that all child agents may be launched immediately, including waiting children;
- that coordinator may launch read-only reviewers/status agents, but not writer subagents for already-owned worktrees;
- that any coordinator-launched writer subagent must be registered as the sole child owner for exactly one issue/worktree;
- `[CONTROL]` snippets for origin advancement, dependency satisfied, conflict/stale branch, scope violation, and PR merged.

Every generated child prompt must specify:

- role: feature/child agent and the exact issue it owns;
- branch, worktree, base branch, contract path, shared Python path;
- the gate command to run before coding;
- the `BLOCKED` behavior with explicit no-write/no-commit/no-push language;
- allowed files, forbidden scope, validation commands, PR title, and issue closing keyword policy;
- PR comment polling before commits/pushes and `[CONTROL]` precedence;
- generated-file conflict rule: regenerate `PROJECT_INDEX.md` with `scripts/gen_index.py`, never hand-edit conflict markers.
- waiting-for-merge rule: after pushing and marking a PR ready, stay active in a polling/responding loop until the PR is `MERGED`, explicitly taken over, or closed by the coordinator/human.

## Known pitfalls

- **Manual wave launching kept the human in the loop.** Fix by launching later-wave children immediately in waiting mode; the gate, not the human's memory, decides when they can code.
- **Coordinator launched implementation subagents and risked double ownership.** A coordinator can use read-only review/status agents, but a writer agent must be the sole owner of its worktree.
- **Children stopped after opening/pushing PRs.** The coordinator then waited on comments, mergeability, or requested changes with no owner responding. Completion must mean PR merged (or explicit coordinator takeover), not "code pushed".
- **Per-worktree venv creation wasted time.** Prompts should point children to the shared canonical interpreter and tell them not to create a new venv unless explicitly authorized.
- **Specific sprint artifacts were mistaken for the general method.** Keep the skill generic; put concrete issue maps in contracts, not in `SKILL.md`.
- **Prose dependencies are not enforceable.** "After wave 1" is insufficient; encode "issue #N closed", "PR #N merged", or "branch synced" so `check_wave_gate.py` can decide.
