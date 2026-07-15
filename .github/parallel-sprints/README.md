# Parallel Sprint Contracts

This directory stores machine-checkable contracts for multi-wave child-agent work.
The goal is to let every child agent start at the same time while later-wave
agents wait safely until their dependencies are actually satisfied.

## Model

Each sprint has a JSON contract listing child workstreams. Copy the template from
[CONTRACT-TEMPLATE.md](CONTRACT-TEMPLATE.md) into a sprint-specific JSON file or into the
local default path:

```text
.github/parallel-sprints/current.json
```

Use a committed sprint-specific contract when you want the dependency plan to be durable in
GitHub. Use the ignored `current.json` path when a local coordinator wants a disposable run
contract.

Each contract lists:

- `issue` — GitHub issue the child owns.
- `wave` — merge/dependency wave.
- `branch` — branch the coordinator creates for the child.
- `worktree` — local folder assigned to the child.
- `depends_on` — machine-checkable gates. Supported dependencies:
  - `{"kind": "issue", "issue": 39, "condition": "closed"}`
  - `{"kind": "issue", "issue": 39, "condition": "open"}`
  - `{"kind": "pr", "pr": 53, "condition": "merged"}`
  - `{"kind": "branch", "branch": "main", "condition": "synced_with_origin"}`
- `allowed_files`, `forbidden`, `validation` — scope and acceptance hints for prompts.

Use the checker before any child writes implementation code:

```bash
.venv/bin/python scripts/check_wave_gate.py --issue 46
```

The checker prints `READY` only when:

1. the contract's `base_branch` is synchronized with `origin/<base_branch>`;
2. all dependency issues in the contract are closed;
3. the child worktree is either not created yet, or is clean and contains `origin/<base_branch>`.

If it prints `BLOCKED`, the child may read docs/issues and post waiting status, but must not
edit files, commit implementation changes, or push feature work.

## Child-agent state machine

Every child prompt should follow this state machine:

```text
PRECHECK
  -> WAITING_FOR_DEPENDENCIES
  -> SYNC_FROM_MAIN
  -> DEVELOP
  -> DRAFT_PR
  -> READY_FOR_REVIEW
  -> STOP_AFTER_MERGE
```

Later-wave agents can be launched early, but while blocked they may only:

- run `scripts/check_wave_gate.py`;
- read the issue and repo docs;
- comment `WAITING: blocked by ...`;
- poll GitHub or wait for a coordinator `[CONTROL] Dependency satisfied` comment.

They must verify the gate themselves after any coordinator signal.

## Prompt templates

Use [PROMPT-TEMPLATE.md](PROMPT-TEMPLATE.md) when launching a coordinator and child agents.
The important contract is:

- active-wave children may code after `check_wave_gate.py` says `READY`;
- later-wave children may be launched early, but while `BLOCKED` they may only read, poll, and
  comment waiting status;
- every child must re-run the gate after a coordinator `[CONTROL] Dependency satisfied` message.

## Coordinator rules

The coordinator creates all planned worktrees from a clean, synchronized `main`, then launches
all child agents with prompts that reference the contract. When a PR merges, the coordinator
posts `[CONTROL] origin/main advanced` to open PRs and `[CONTROL] Dependency satisfied` to
newly unblocked waiting agents.

Do not use this contract to bypass review, validation, or the irreversible-action boundary in
`docs/adr/0005-autonomy-boundary-reversible-vs-irreversible.md`.
