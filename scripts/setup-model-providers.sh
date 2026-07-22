#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
runner="$script_dir/token-saver-route.py"

command -v python3 >/dev/null 2>&1 || {
  echo "token-saver: python3 is required" >&2
  exit 127
}

exec python3 "$runner" setup-providers "$@"
