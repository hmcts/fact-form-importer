#!/bin/sh

set -eu

repo_root="$(git rev-parse --show-toplevel)"

cp "$repo_root/git-hooks/pre-push" "$repo_root/.git/hooks/pre-push"
chmod +x "$repo_root/.git/hooks/pre-push"

echo "Installed git pre-push hook."
