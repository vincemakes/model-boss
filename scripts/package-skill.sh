#!/usr/bin/env bash
# Package the boss-dispatch skill. The skill sources live in boss-dispatch/;
# this repository-root script is the build entry point.
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd -P)"
skill_root="$repo_root/boss-dispatch"

command -v python3 >/dev/null 2>&1 || {
  echo "Model Boss: python3 is required" >&2
  exit 127
}

mkdir -p "$repo_root/dist"
exec env PYTHONPATH="$skill_root${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m runtime.model_boss.package \
  --repo-root "$repo_root" \
  --output "$repo_root/dist/boss-dispatch.skill"
