#!/usr/bin/env python3
"""Check whether a parallel child-agent wave gate is READY or BLOCKED.

The script reads a JSON sprint contract and verifies machine-checkable
dependencies before a child agent is allowed to write implementation code. It
intentionally uses only stdlib + git/gh CLIs so waiting agents can run it from
any worktree without installing extra packages.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONTRACT = ".github/parallel-sprints/current.json"


@dataclass
class CheckResult:
    issue: int
    title: str
    ready: bool
    passed: list[str]
    blocked: list[str]
    warnings: list[str]


def repo_root() -> Path:
    out = run(["git", "rev-parse", "--show-toplevel"], check=True)
    return Path(out.strip())


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = False) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"{' '.join(cmd)} failed: {stderr}")
    return result.stdout.strip()


def load_contract(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"contract not found: {path}\n"
            "Create one from .github/parallel-sprints/CONTRACT-TEMPLATE.md "
            "or pass --contract <path>."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def issue_state(repo: str, issue: int) -> tuple[str | None, str | None]:
    try:
        raw = run(
            ["gh", "issue", "view", str(issue), "-R", repo, "--json", "state,closedAt"],
            check=True,
        )
        payload = json.loads(raw)
        return payload.get("state"), payload.get("closedAt")
    except (RuntimeError, json.JSONDecodeError) as exc:
        return None, f"gh issue view failed: {exc}"


def pr_state(repo: str, pr_number: int) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = run(
            [
                "gh", "pr", "view", str(pr_number), "-R", repo,
                "--json", "state,mergedAt,mergeCommit,headRefName,baseRefName",
            ],
            check=True,
        )
        return json.loads(raw), None
    except (RuntimeError, json.JSONDecodeError) as exc:
        return None, f"gh pr view failed: {exc}"


def branch_synced(root: Path, branch: str) -> tuple[bool, str]:
    try:
        run(["git", "fetch", "origin", branch, "--quiet"], cwd=root, check=True)
        counts = run(["git", "rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"],
                     cwd=root, check=True)
    except RuntimeError as exc:
        return False, str(exc)
    return counts == "0\t0" or counts == "0 0", f"{branch}...origin/{branch} = {counts}"


def worktree_status(path: Path, *, base_branch: str) -> tuple[bool, list[str]]:
    messages: list[str] = []
    if not path.exists():
        messages.append(f"worktree does not exist yet: {path}")
        return True, messages
    status = run(["git", "-C", str(path), "status", "--short"])
    if status:
        messages.append(f"worktree has uncommitted changes: {path}")
        messages.extend(f"  {line}" for line in status.splitlines())
        return False, messages
    try:
        counts = run(
            ["git", "-C", str(path), "rev-list", "--left-right", "--count", f"HEAD...origin/{base_branch}"],
            check=True,
        )
    except RuntimeError as exc:
        return False, [str(exc)]
    ahead, behind = (int(part) for part in counts.replace("\t", " ").split())
    if behind:
        return False, [f"worktree branch is behind origin/{base_branch} by {behind} commit(s): {path}"]
    messages.append(f"worktree is clean and contains origin/{base_branch} (ahead={ahead}, behind={behind})")
    return True, messages


def check_child(contract: dict[str, Any], child: dict[str, Any], root: Path) -> CheckResult:
    repo = contract["repo"]
    base_branch = str(contract.get("base_branch") or "main")
    issue = int(child["issue"])
    title = str(child["title"])
    passed: list[str] = []
    blocked: list[str] = []
    warnings: list[str] = []

    ok, detail = branch_synced(root, base_branch)
    if ok:
        passed.append(f"{base_branch} is synchronized with origin/{base_branch} ({detail})")
    else:
        blocked.append(f"{base_branch} is not synchronized: {detail}")

    for dep in child.get("depends_on", []):
        kind = dep.get("kind")
        condition = dep.get("condition")
        if kind == "issue" and condition in {"closed", "open"}:
            dep_issue = int(dep["issue"])
            state, error = issue_state(repo, dep_issue)
            expected = "CLOSED" if condition == "closed" else "OPEN"
            if state == expected:
                passed.append(f"dependency issue #{dep_issue} is {expected}")
            elif state is None:
                blocked.append(f"dependency issue #{dep_issue} could not be checked: {error}")
            else:
                blocked.append(f"dependency issue #{dep_issue} is {state}, not {expected}")
        elif kind == "pr" and condition == "merged":
            dep_pr = int(dep["pr"])
            payload, error = pr_state(repo, dep_pr)
            if payload is None:
                blocked.append(f"dependency PR #{dep_pr} could not be checked: {error}")
            elif payload.get("state") == "MERGED" and payload.get("mergedAt"):
                passed.append(f"dependency PR #{dep_pr} is MERGED")
            else:
                blocked.append(f"dependency PR #{dep_pr} is {payload.get('state')}, not MERGED")
        elif kind == "branch" and condition == "synced_with_origin":
            branch = str(dep.get("branch") or base_branch)
            ok, detail = branch_synced(root, branch)
            if ok:
                passed.append(f"dependency branch {branch} is synchronized ({detail})")
            else:
                blocked.append(f"dependency branch {branch} is not synchronized: {detail}")
        else:
            warnings.append(f"unknown dependency skipped: {dep}")

    worktree = Path(child["worktree"])
    ok, messages = worktree_status(worktree, base_branch=base_branch)
    if ok:
        passed.extend(messages)
    else:
        blocked.extend(messages)

    return CheckResult(
        issue=issue,
        title=title,
        ready=not blocked,
        passed=passed,
        blocked=blocked,
        warnings=warnings,
    )


def select_children(contract: dict[str, Any], issue: int | None) -> list[dict[str, Any]]:
    children = contract.get("children", [])
    if issue is None:
        return children
    selected = [child for child in children if int(child["issue"]) == issue]
    if not selected:
        raise SystemExit(f"issue #{issue} is not present in contract")
    return selected


def print_human(results: list[CheckResult]) -> None:
    for result in results:
        print(f"{'READY' if result.ready else 'BLOCKED'} issue #{result.issue}: {result.title}")
        for line in result.passed:
            print(f"  ✓ {line}")
        for line in result.blocked:
            print(f"  ✗ {line}")
        for line in result.warnings:
            print(f"  ! {line}")
        print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check a parallel child-agent dependency gate.")
    parser.add_argument("--contract", default=DEFAULT_CONTRACT,
                        help=f"sprint contract JSON path (default: {DEFAULT_CONTRACT})")
    parser.add_argument("--issue", type=int, default=None,
                        help="only check one child issue; checks all children when omitted")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    contract_path = root / args.contract
    contract = load_contract(contract_path)
    results = [
        check_child(contract, child, root)
        for child in select_children(contract, args.issue)
    ]
    if args.json:
        print(json.dumps([result.__dict__ for result in results], indent=2, ensure_ascii=False))
    else:
        print_human(results)
    return 0 if all(result.ready for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
