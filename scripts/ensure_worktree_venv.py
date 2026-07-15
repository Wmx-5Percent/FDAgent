#!/usr/bin/env python3
"""Link linked worktrees to the primary checkout's reusable Python virtualenv."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _git_optional(repo: Path, *args: str) -> str | None:
    try:
        out = _git(repo, *args)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return out or None


def repo_root(repo: Path) -> Path:
    out = _git(repo, "rev-parse", "--show-toplevel")
    return Path(out).resolve()


def git_common_dir(repo: Path) -> Path:
    out = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(out).resolve()


def primary_worktree_root(repo: Path) -> Path:
    common = git_common_dir(repo)
    if common.name == ".git":
        return common.parent.resolve()

    # Fallback for less common layouts: prefer main/master, else the first worktree.
    listing = _git_optional(repo, "worktree", "list", "--porcelain")
    if listing:
        worktrees: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in listing.splitlines():
            if not line:
                if current:
                    worktrees.append(current)
                    current = {}
                continue
            if line.startswith("worktree "):
                current["path"] = line.removeprefix("worktree ")
            elif line.startswith("branch "):
                current["branch"] = line.removeprefix("branch ")
        if current:
            worktrees.append(current)

        for branch in ("refs/heads/main", "refs/heads/master"):
            for worktree in worktrees:
                if worktree.get("branch") == branch and worktree.get("path"):
                    return Path(worktree["path"]).resolve()
        if worktrees and worktrees[0].get("path"):
            return Path(worktrees[0]["path"]).resolve()

    return repo_root(repo)


def configured_shared_venv(repo: Path, primary_root: Path, explicit_target: str | None) -> Path:
    raw = (
        explicit_target
        or os.environ.get("FDAAGENT_SHARED_VENV")
        or _git_optional(repo, "config", "--get", "fdaagent.sharedVenv")
    )
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = primary_root / path
        return path.resolve(strict=False)
    return (primary_root / ".venv").resolve(strict=False)


def venv_is_usable(path: Path) -> bool:
    return path.exists() and (path / "bin" / "python").exists()


def same_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def symlink_target(path: Path) -> Path:
    raw = os.readlink(path)
    target = Path(raw)
    if not target.is_absolute():
        target = path.parent / target
    return target.resolve(strict=False)


def link_text(target: Path, parent: Path) -> str:
    try:
        return os.path.relpath(target, parent)
    except ValueError:
        return str(target)


def info(message: str, *, quiet: bool) -> None:
    if not quiet:
        print(message)


def warn(message: str) -> None:
    print(message, file=sys.stderr)


def ensure_venv(repo: Path, *, target: str | None, force: bool, quiet: bool) -> int:
    root = repo_root(repo)
    primary_root = primary_worktree_root(root)
    destination = root / ".venv"
    shared = configured_shared_venv(root, primary_root, target)

    if root == primary_root and same_path(destination, shared):
        if venv_is_usable(destination):
            info(f"primary worktree already has a usable .venv: {destination}", quiet=quiet)
        else:
            warn(
                "primary worktree has no reusable .venv yet; create it once with:\n"
                f"  python3 -m venv {destination}\n"
                f"  {destination}/bin/python -m pip install -r {root / 'requirements.txt'}"
            )
        return 0

    if destination.is_symlink():
        current_target = symlink_target(destination)
        if same_path(current_target, shared):
            if venv_is_usable(shared):
                info(f".venv already links to shared virtualenv: {shared}", quiet=quiet)
                return 0
            warn(f".venv already points at {shared}, but that virtualenv is not usable.")
            return 0
        if not force and destination.exists():
            info(
                f"leaving existing .venv symlink -> {current_target}; "
                "rerun with --force to repoint it",
                quiet=quiet,
            )
            return 0
        destination.unlink()
    elif destination.exists():
        info(f"leaving existing local .venv in place: {destination}", quiet=quiet)
        return 0

    if not venv_is_usable(shared):
        warn(
            "no reusable shared virtualenv found; create/install it once in the primary "
            f"checkout at {shared}"
        )
        return 0

    os.symlink(link_text(shared, root), destination, target_is_directory=True)
    info(f"linked {destination} -> {shared}", quiet=quiet)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Link this git worktree's .venv to the primary checkout's reusable .venv. "
            "Set FDAAGENT_SHARED_VENV or git config fdaagent.sharedVenv to override."
        )
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="repository/worktree path to operate on (default: current directory)",
    )
    parser.add_argument(
        "--target",
        help="explicit shared virtualenv path (default: primary worktree .venv)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing .venv symlink; never removes a real directory",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress success messages")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return ensure_venv(
        Path(args.repo).expanduser(),
        target=args.target,
        force=args.force,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    raise SystemExit(main())
