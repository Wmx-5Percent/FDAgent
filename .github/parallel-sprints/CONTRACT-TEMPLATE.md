# Parallel sprint contract template

Copy this JSON into `.github/parallel-sprints/current.json` for a local run, or into a
committed sprint-specific file such as `.github/parallel-sprints/2026-08-example.json`.

```json
{
  "name": "YYYY-MM-short-name",
  "repo": "Wmx-5Percent/FDAgent",
  "base_branch": "main",
  "canonical_checkout": "/Users/waywei/Desktop/developer/fdaAgent",
  "shared_python": "/Users/waywei/Desktop/developer/fdaAgent/.venv/bin/python",
  "children": [
    {
      "issue": 100,
      "title": "short human-readable issue title",
      "wave": 1,
      "branch": "feat/issue-100-short-name",
      "worktree": "/Users/waywei/Desktop/developer/fdaAgent-issue-100-short-name",
      "pr_title": "feat: short PR title",
      "depends_on": [],
      "allowed_files": [
        "src/example.py",
        "web/example.js",
        "PROJECT_INDEX.md"
      ],
      "forbidden": [
        "Do not edit unrelated workstreams.",
        "Do not run irreversible DB writes."
      ],
      "validation": [
        ".venv/bin/python -m py_compile src/example.py",
        ".venv/bin/python scripts/gen_index.py --check",
        "git diff --check"
      ]
    },
    {
      "issue": 101,
      "title": "later-wave task waiting for issue 100",
      "wave": 2,
      "branch": "feat/issue-101-later-task",
      "worktree": "/Users/waywei/Desktop/developer/fdaAgent-issue-101-later-task",
      "pr_title": "feat: later task",
      "depends_on": [
        {
          "kind": "issue",
          "issue": 100,
          "condition": "closed"
        },
        {
          "kind": "branch",
          "branch": "main",
          "condition": "synced_with_origin"
        }
      ],
      "allowed_files": [
        "src/later.py",
        "PROJECT_INDEX.md"
      ],
      "forbidden": [
        "Do not code until the gate returns READY."
      ],
      "validation": [
        ".venv/bin/python -m py_compile src/later.py",
        ".venv/bin/python scripts/gen_index.py --check",
        "git diff --check"
      ]
    }
  ]
}
```

Supported dependency entries:

```json
{"kind": "issue", "issue": 100, "condition": "closed"}
{"kind": "issue", "issue": 100, "condition": "open"}
{"kind": "pr", "pr": 123, "condition": "merged"}
{"kind": "branch", "branch": "main", "condition": "synced_with_origin"}
```
