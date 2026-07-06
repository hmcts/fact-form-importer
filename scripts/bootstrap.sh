#!/bin/sh

set -eu

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

python3 -m venv .venv

# shellcheck disable=SC1091
. .venv/bin/activate

pip3 install --upgrade pip setuptools wheel
pip3 install -e ".[dev]"

sh scripts/install-git-hooks.sh

echo "Bootstrap complete."
echo "Activate the venv with: source .venv/bin/activate"
