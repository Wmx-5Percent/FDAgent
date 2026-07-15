---
name: parallel-agent-prompt-pack
description: "Create copy/paste prompt packs for multi-terminal AI-agent parallel development. Use when: after planning a sprint or feature split, the user asks for prompts for a main/coordinator agent and multiple subagents; turning a PR plan into terminal-specific startup prompts; deciding what each terminal should paste; embedding role declarations, skill loading, main/origin preflight gates, branch/worktree ownership, Draft PR rules, [CONTROL] comment polling, dependency gates, validation commands, generated-file conflict rules, and cleanup handoff. Not for: executing the development itself, reviewing code, or coordinating already-running PRs without writing prompts (use parallel-agent-pr-coordination)."
---

# Parallel-Agent Prompt Pack

## Metadata
- **Type**: Workflow + BestPractice
- **Scope**: authoring terminal-specific prompts for multi-agent software work
- **Created**: 2026-07-08

## Goal
Turn a discussed plan into a **copy/paste-ready prompt pack**: one coordinator/main-agent prompt plus one prompt per child terminal. Each prompt must make the agent's authority, branch/worktree ownership, dependency gates, GitHub communication behavior, validation duties, and stopping conditions unambiguous.

This skill authors prompts. The generated prompts should normally instruct agents to use `parallel-agent-pr-coordination`; cleanup prompts should reference `parallel-dev-worktree-cleanup`.

## Boundaries
- Do not start coding, create branches, or open PRs while authoring the prompt pack unless the user separately asks for execution.
- Do not hide missing decisions. If branch names, worktree paths, dependencies, or validation commands are unknown, either infer them from repo conventions and mark the inference, or ask the user before finalizing.
- Do not rely on "main terminal" or "Terminal 1" alone to imply authority. Every generated prompt must explicitly declare `coordinator/main agent` or `feature/child agent`.
- Do not generate child prompts that let multiple agents own the same files unless the overlap and merge order are explicit.
- Do not generate child prompts that let a feature agent rewrite `PROGRESS.md` "Next action" / "Next up" as that feature's single local task. In parallel plans, `PROGRESS.md` is a shared multi-workstream queue; child-local status belongs in PR bodies/comments.
- Coordinator prompts must prohibit implementation/dev subagents for child-owned worktrees. Read-only review/status subagents are allowed; writer subagents are allowed only if the prompt declares them as the sole child owner for that issue/worktree.
- Avoid long background exposition in the output. The deliverable is prompts the user can paste, not a lecture.

## Acceptance criteria
A successful prompt pack contains:
- A terminal map table: terminal number/name, role, scope, branch, worktree/clone path, dependency gate, PR title, and allowed files.
- A coordinator prompt that requires preflight verification that canonical `main` is clean and synchronized with `origin/main` before launching children.
- A coordinator prompt that explicitly says read-only reviewer/status subagents are allowed, but implementation/dev subagents must not write to already child-owned worktrees.
- One child prompt per parallel workstream with explicit role declaration, required skill loading, base branch, branch name, worktree path, file ownership, forbidden scope, Draft PR timing, PR comment polling, validation commands, and "do not merge your own PR" rule.
- Dependency gates are executable: a waiting child knows exactly what PR/branch/title/merge condition satisfies the gate.
- Each prompt includes what to do when `origin/main` advances: merge, not rebase; regenerate generated files; rerun validation; push; comment results.
- Generated-file conflict policy is present, especially for `PROJECT_INDEX.md`: run `scripts/gen_index.py`, add the generated file, then run `scripts/gen_index.py --check`.
- Each child prompt includes a `PROGRESS.md` rule: preserve the complete multi-agent "Next up" queue; only update the child's item or a short integrated note when asked; use PR comments for branch-local next steps.
- The pack includes short `[CONTROL]` snippets the coordinator can paste later for common events: main advanced, PR conflicting, PR merged/stop, dependency satisfied.
- The output is copy/paste-friendly: separate fenced text blocks for each terminal, with no nested bullets that make terminal prompts hard to select.

## Prompt design requirements
Every generated prompt should include these concepts, adapted to the workstream:
- **Role and authority**: "You are the coordinator/main agent" or "You are a feature/child agent for X."
- **Subagent boundary**: coordinator may use read-only review/status subagents; writer subagents are forbidden unless they are explicitly the sole feature child owner for one worktree.
- **Skills to load/use**: at least `parallel-agent-pr-coordination`; cleanup work uses `parallel-dev-worktree-cleanup`.
- **Repo facts**: canonical repo path, remote repo, base branch, branch name, worktree/clone path, venv/tooling conventions.
- **Ownership**: files/directories the agent may touch, and explicit out-of-scope files.
- **Preflight or dependency gate**: conditions that must hold before creating a branch or writing code.
- **Visibility**: first meaningful checkpoint commit opens a Draft PR; PR body records base SHA, scope, validation, overlap, and risks.
- **Communication**: poll PR comments; `[CONTROL]` owner comments override prior prompt; reply with results.
- **Progress docs**: `PROGRESS.md` updates must preserve all planned parallel workstreams and dependency gates; a child must not collapse "Next action" / "Next up" to its own feature.
- **Validation**: targeted commands, acceptable blockers, and where to document failures.
- **Integration**: merge `origin/main` instead of rebasing; never merge own PR unless explicitly authorized.
- **Stop conditions**: stale base, conflicting instructions, unauthorized file overlap, unavailable credentials, failed validation without a clear fix, or PR merged by someone else.

## Recommended output shape
Use this structure when the user asks for prompts to paste into terminals:

````markdown
## Terminal map

| Terminal | Role | Scope | Branch | Worktree | Dependency | PR |
|---|---|---|---|---|---|---|

## Terminal 0 — Coordinator / main agent

```text
Use the parallel-agent-pr-coordination skill.
You are the coordinator/main agent for this parallel sprint.
...
```

## Terminal 1 — <feature child>

```text
Use the parallel-agent-pr-coordination skill.
You are a feature/child agent for <scope>.
...
```

## Control snippets

```text
[CONTROL] origin/main advanced. Merge origin/main, do not rebase...
```
````

The exact wording may vary, but the generated pack must preserve the authority boundaries and coordination gates.

## Known pitfalls from the 2026-07-08 parallel sprint
- **Prompts assumed GitHub `origin/main` was current.** Add a coordinator preflight and child stop condition so no branch starts from a stale remote.
- **Children opened PRs only after finishing.** Prompt for Draft PRs after the first meaningful checkpoint commit so the coordinator can monitor early.
- **PR comments were used as control messages without requiring polling.** Prompt every child to poll PR comments and treat `[CONTROL]` as higher priority.
- **Role ambiguity caused coordination friction.** A skill can describe modes, but each terminal prompt must explicitly assign the role.
- **Coordinator-launched dev subagents caused ownership ambiguity.** Prompts must say that writer subagents are child owners, not invisible coordinator helpers; no two writers may touch the same worktree.
- **Dependency gates were only checked once.** A child can become stale after any other PR merges; prompts must require resync after every main advance.
- **Generated index conflicts recurred.** Put the generator-based conflict rule directly in every child prompt that may add/move/delete files.
- **Children overwrote shared Next Up with their own task.** Parallel prompt packs must explicitly say that `PROGRESS.md` is coordinator-owned shared state; child-local next steps go in PR comments, not in the global "Next action".

## Output
The output is a Markdown prompt pack that the user can copy into terminals. It is acceptable to save it under the session artifact folder if the user asks for a file, but do not create committed planning docs unless explicitly requested.
