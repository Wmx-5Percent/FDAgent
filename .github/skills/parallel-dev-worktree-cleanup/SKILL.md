---
name: parallel-dev-worktree-cleanup
description: "Retire the local working directories used for parallel multi-agent feature development (git worktrees or clones) after their branches merge, without destroying unmerged or unpushed work. Use when: a per-feature worktree/clone folder should be removed after its PR merged; deciding whether a per-feature checkout is safe to delete; wiring post-merge local cleanup; pruning stale worktree metadata or a shared-venv symlink. Not for: discarding in-progress/unmerged work, remote-branch deletion policy, or CI/release automation."
---

# Parallel-Dev Worktree Cleanup

## Metadata
- **Type**: Workflow + BestPractice
- **Scope**: local git working directories + worktree metadata on the developer machine
- **Created**: 2026-07-08

## Goal
After parallel multi-agent development — each feature built in its own git **worktree** (or clone) + branch, integrated via PR/issue — leave the machine with **only the canonical checkout**. Every retired per-feature folder is removed, and removal carries a **proof that no unmerged or unpushed work was destroyed**.

The recommended lifecycle this skill closes out:
`isolate (one worktree+branch per parallel agent)` → `integrate (PR/issue → merge to integration/main)` → **`verify merged+pushed`** → **`git worktree remove`** → `prune metadata` → *(optional, surfaced to user)* delete merged branches.

## Boundaries
- **Retire a folder ONLY when its work is provably preserved** (merged into the integration/main line **and** that line exists on the remote), or the user explicitly authorizes discard.
- **Never** delete a folder that has uncommitted changes, commits that are both unpushed and unmerged, or that cannot be mapped to a known branch.
- **Never `rm -rf` a git worktree** — it orphans admin files under `.git/worktrees/`. Use `git worktree remove`.
- This skill governs **local** folders and metadata. Remote-branch deletion is a policy decision → **surface it, do not do it unilaterally**.
- Prefer verification-then-delete over force. If any check fails, **stop and report**, don't brute-force.

## Acceptance criteria (testable)
- `git worktree list` shows only the canonical checkout; the retired folders no longer exist on disk.
- For every removed folder, its branch HEAD was an ancestor of the **pushed** integration/main branch — verifiable with `git merge-base --is-ancestor <branch> origin/<integration>` (exit 0) — OR the user explicitly confirmed discard.
- Each worktree was clean (`git -C <path> status -s` empty) at removal, unless the user explicitly authorized `--force`.
- `git worktree prune` leaves no dangling entries.
- If authoring/altering this skill: `.github/skills/INDEX.md` updated in the same change and `python scripts/gen_index.py` re-run.

## The safe-to-delete gate (three independent checks)
Completeness must not depend on any single check — take all three, and if any is inconclusive, stop:
1. **Clean tree** — `git -C <path> status -s` is empty (no modified/untracked tracked files).
2. **Nothing unpushed-and-unmerged** — the branch HEAD is an ancestor of the pushed integration/main line (`git merge-base --is-ancestor <branch> origin/<integration>`).
3. **Identity** — the folder maps to a known branch (`git -C <path> rev-parse --abbrev-ref HEAD`) and (`--git-common-dir`) tells worktree vs standalone clone.

Then remove with `git worktree remove <path>`; fall back to `--force` **only** when the sole obstacle is *ignored* files (e.g. a local `.venv`) and all three checks passed. Finish with `git worktree prune`. Merged local branches may be deleted with the self-protecting `git branch -d` (refuses unless merged); leave remote branches to the user.

## Known pitfalls (observed, not invented)
- **A feature branch can have no upstream of its own.** `git log @{u}..` / "is it pushed?" then fails silently, and it is *wrong* to conclude "no upstream ⇒ unsafe to delete." The work is still safe if the branch was **merged into a pushed integration branch** — verify against that line with `git merge-base --is-ancestor`, not only `@{u}`. (Hit this on `feat/observability-eval`, which had no `origin/` tracking ref but was merged into the pushed `integration/parallel-next-up`.)
- **`.venv` symlink vs real directory.** `.gitignore`'s `.venv/` ignores a real dir but **not** a `.venv` *symlink*; a worktree may symlink to another checkout's venv. A careless `rm -rf`/`--force` could delete the **shared target**. Check `ls -ld <path>/.venv`; if it is a symlink, remove only the link. (In practice these worktrees held independent real venvs — safe — but verify every time.)
- **Bare `rm -rf` on a worktree** leaves stale entries in `.git/worktrees/`; `git worktree list` keeps showing ghosts. Use `git worktree remove`, or repair after the fact with `git worktree prune`.

## Output
No artifact file. The result is the cleaned machine state plus a short report: which folders were removed, the verification evidence (branch → ancestor-of-pushed-integration), and any items surfaced for the user (e.g. merged local/remote branches left in place).
