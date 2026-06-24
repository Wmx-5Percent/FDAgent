#!/usr/bin/env bash
# Install the version-controlled git hooks into .git/hooks.
# Run once after cloning:  bash scripts/hooks/install.sh
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
ln -sf ../../scripts/hooks/pre-commit "$root/.git/hooks/pre-commit"
chmod +x "$root/scripts/hooks/pre-commit"
echo "✓ installed pre-commit hook (-> scripts/hooks/pre-commit)"
