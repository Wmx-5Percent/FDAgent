#!/usr/bin/env python3
"""Generate PROJECT_INDEX.md — a reliable, auto-derived map of the repository.

This is the engine of the project's *progressive-disclosure* harness:

    AGENTS.md  (always-on, tiny)  ->  PROJECT_INDEX.md  (this map)  ->  files (on demand)

Why generated, not hand-written?
    A hand-maintained index rots the moment someone adds a file. Here the index
    is *derived from the files themselves* (Python module docstrings + top-level
    symbols, Markdown headings/blockquotes, first comment of config files), so a
    single command keeps it perfectly in sync. ``--check`` fails when it is stale,
    so CI / a pre-commit hook can guarantee reliability as the project grows.

Usage
-----
    python scripts/gen_index.py            # (re)write PROJECT_INDEX.md
    python scripts/gen_index.py --check    # exit 1 if PROJECT_INDEX.md is stale
    python scripts/gen_index.py --stdout    # print to stdout, don't write

File listing respects .gitignore by using ``git ls-files`` (tracked + untracked,
excluding ignored), so virtualenvs, data products, and secrets never leak in.
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

INDEX_NAME = "PROJECT_INDEX.md"
MAX_SYMBOLS = 12
MAX_BYTES = 256_000  # don't parse descriptions out of very large files
TIMESTAMP_PREFIX = "> Last generated:"

# Extensions we parse for a description. Anything else is listed name-only.
TEXTLIKE = {".py", ".md", ".txt", ".cfg", ".ini", ".toml", ".yaml", ".yml",
            ".json", ".sh", ".example", ".sql"}


# --------------------------------------------------------------------------- #
# repo + file discovery
# --------------------------------------------------------------------------- #
def repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path(__file__).resolve().parent.parent


def list_files(root: Path) -> list[str]:
    """Tracked + untracked-but-not-ignored files, relative to root (sorted).

    Uses ``-z`` (NUL-separated) so non-ASCII names (e.g. Chinese docs) are not
    octal-escaped/quoted by git.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, text=True, check=True)
        paths = [p for p in out.stdout.split("\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
    # never index the venv, git internals, or caches even if git missed them
    skip = ("venv/", ".venv/", ".git/", "__pycache__/")
    paths = [p for p in paths if not any(s in f"{p}/" for s in skip)]
    return sorted(set(paths))


# --------------------------------------------------------------------------- #
# per-file description extraction
# --------------------------------------------------------------------------- #
def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                return fh.read(MAX_BYTES)
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def describe_python(text: str) -> tuple[str, list[str]]:
    desc, symbols = "", []
    try:
        tree = ast.parse(text)
        doc = ast.get_docstring(tree)
        if doc:
            desc = doc.strip().splitlines()[0].strip()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    symbols.append(node.name)
    except SyntaxError:
        pass
    return desc, symbols[:MAX_SYMBOLS]


def describe_markdown(text: str) -> str:
    title, blurb = "", ""
    for raw in text.splitlines():
        s = raw.strip()
        if not title and s.startswith("#"):
            title = s.lstrip("#").strip()
            continue
        if title and s.startswith(">"):
            blurb = s.lstrip(">").strip()
            break
        if title and s and not s.startswith("#"):
            blurb = s
            break
    if title and blurb:
        return f"{title} — {blurb}"
    return title or blurb


def describe_generic(text: str) -> str:
    """First meaningful comment / non-empty line (skipping a shebang + comment markers)."""
    lines = text.splitlines()
    start = 1 if lines and lines[0].startswith("#!") else 0
    for raw in lines[start:]:
        s = raw.strip().lstrip("#;/").strip().lstrip("-").strip()
        if s:
            return s[:120]
    return ""


def describe(root: Path, rel: str) -> tuple[str, list[str]]:
    # The index lists itself with a fixed description so output is deterministic
    # whether or not the file exists on disk yet (avoids a generate/--check race).
    if rel == INDEX_NAME:
        return "Auto-generated repository map (this file).", []
    path = root / rel
    suffix = path.suffix.lower()
    # Read text for known text types and for extensionless files (likely scripts).
    if suffix not in TEXTLIKE and suffix != "":
        return "", []
    text = read_text(path)
    if not text:
        return "", []
    if suffix == ".py":
        return describe_python(text)
    if suffix == ".md":
        return describe_markdown(text), []
    # config-ish, scripts, and extensionless files (incl. .gitignore and git hooks);
    # describe_generic already skips a leading shebang.
    return describe_generic(text), []


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def group_key(rel: str) -> str:
    parts = rel.split("/")
    return f"{parts[0]}/" if len(parts) > 1 else "(root)"


def render(root: Path, files: list[str], *, timestamp: str | None) -> str:
    groups: dict[str, list[str]] = {}
    missing = 0
    for rel in files:
        desc, symbols = describe(root, rel)
        if not desc and (root / rel).suffix.lower() in TEXTLIKE:
            missing += 1
        line = f"- [`{rel}`]({rel})"
        if desc:
            line += f" — {desc}"
        else:
            line += " — _(no description; add a one-line docstring/heading)_"
        if symbols:
            line += f"\n  - symbols: {', '.join(f'`{s}`' for s in symbols)}"
        groups.setdefault(group_key(rel), []).append(line)

    out: list[str] = []
    out.append(f"<!-- GENERATED by scripts/gen_index.py — DO NOT EDIT BY HAND. "
               f"Regenerate: python scripts/gen_index.py -->")
    out.append("# Project Index")
    out.append("")
    out.append("> Auto-generated map for fast agent navigation "
               "(progressive-disclosure layer 1).")
    out.append("> Find the right file here **before** grepping the tree; "
               "open files on demand for full detail.")
    out.append("> Regenerate after structural changes: `python scripts/gen_index.py` "
               "· verify in CI: `--check`.")
    if timestamp:
        out.append(f"{TIMESTAMP_PREFIX} {timestamp} · {len(files)} files"
                   + (f" · {missing} missing description" if missing else ""))
    out.append("")
    # (root) first, then directories alphabetically
    for key in sorted(groups, key=lambda k: (k != "(root)", k)):
        out.append(f"## {key}")
        out.extend(groups[key])
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def strip_volatile(content: str) -> str:
    return "\n".join(
        ln for ln in content.splitlines() if not ln.startswith(TIMESTAMP_PREFIX)
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the repository index.")
    ap.add_argument("--check", action="store_true",
                    help="Exit 1 if the on-disk index is out of date (no write).")
    ap.add_argument("--stdout", action="store_true",
                    help="Print to stdout instead of writing the file.")
    args = ap.parse_args(argv)

    root = repo_root()
    files = list_files(root)
    # Always list the index itself, even before it is first written.
    if INDEX_NAME not in files:
        files = sorted({*files, INDEX_NAME})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    content = render(root, files, timestamp=now)
    target = root / INDEX_NAME

    if args.stdout:
        sys.stdout.write(content)
        return 0

    if args.check:
        if not target.exists():
            print(f"✗ {INDEX_NAME} is missing. Run: python scripts/gen_index.py")
            return 1
        current = target.read_text(encoding="utf-8")
        if strip_volatile(current) != strip_volatile(content):
            print(f"✗ {INDEX_NAME} is stale. Run: python scripts/gen_index.py")
            return 1
        print(f"✓ {INDEX_NAME} is up to date ({len(files)} files).")
        return 0

    target.write_text(content, encoding="utf-8")
    print(f"✓ wrote {INDEX_NAME} ({len(files)} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
