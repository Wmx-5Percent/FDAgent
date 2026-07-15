#!/usr/bin/env bash
# Install the version-controlled git hooks into the shared git hooks directory.
# Run once after cloning:  bash scripts/hooks/install.sh
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
git_common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
hook_dir="$git_common_dir/hooks"
mkdir -p "$hook_dir"

install_hook() {
  local hook="$1"
  ln -sf "$root/scripts/hooks/$hook" "$hook_dir/$hook"
  chmod +x "$root/scripts/hooks/$hook"
  echo "✓ installed $hook hook (-> scripts/hooks/$hook)"
}

install_hook pre-commit
install_hook post-checkout
chmod +x "$root/scripts/ensure_worktree_venv.py"
