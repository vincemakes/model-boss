#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd -P)"
skill_root="$repo_root/boss-dispatch"
cd "$repo_root"

export PYTHONPATH="$skill_root${PYTHONPATH:+:$PYTHONPATH}"

command -v python3 >/dev/null 2>&1 || {
  echo "Model Boss: python3 is required" >&2
  exit 127
}
command -v node >/dev/null 2>&1 || {
  echo "Model Boss: Node.js 22+ is required for boss-call" >&2
  exit 127
}

python3 -m unittest discover -s tests -v
node --test boss-call/test/*.test.mjs

for json_file in \
  boss-dispatch/config/model-boss.schema.json \
  boss-dispatch/config/model-boss.example.json \
  boss-dispatch/references/profiles/anthropic.json \
  boss-dispatch/references/profiles/openai.json \
  boss-dispatch/references/profiles/kimi.json \
  evals/evals.json \
  evals/routing-evals.json \
  benchmarks/trigger-eval.json \
  benchmarks/benchmark.json; do
  python3 -m json.tool "$json_file" >/dev/null
done

bash -n scripts/package-skill.sh scripts/validate.sh boss-dispatch/scripts/setup-model-providers.sh
# The Codex skill validator predates the Agent Skills `compatibility` frontmatter
# field, which Claude Code reads and this skill keeps. Tolerate exactly that one
# complaint; any other finding, or any additional unexpected key, still fails.
codex_validator="${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py"
if [ -f "$codex_validator" ]; then
  if ! codex_output="$(python3 "$codex_validator" boss-dispatch 2>&1)"; then
    if [ "$(printf '%s\n' "$codex_output" | grep -c .)" -eq 1 ] \
      && printf '%s\n' "$codex_output" \
        | grep -q 'Unexpected key(s) in SKILL.md frontmatter: compatibility\. Allowed properties'; then
      echo "Model Boss: Codex validator does not know the 'compatibility' key; tolerated" >&2
    else
      printf '%s\n' "$codex_output" >&2
      exit 1
    fi
  fi
else
  echo "Model Boss: Codex quick_validate.py not installed; skipping that check" >&2
fi
python3 -m unittest tests.test_skill_content tests.test_docs -q

validation_tmp="$(mktemp -d)"
trap 'rm -rf -- "$validation_tmp"' EXIT HUP INT TERM
python3 -m runtime.model_boss.package \
  --repo-root "$repo_root" \
  --output "$validation_tmp/boss-dispatch.skill" >/dev/null
cmp dist/boss-dispatch.skill "$validation_tmp/boss-dispatch.skill"
python3 -m runtime.model_boss.package \
  --repo-root "$repo_root" \
  --validate "$validation_tmp/boss-dispatch.skill" >/dev/null
unzip -t "$validation_tmp/boss-dispatch.skill" >/dev/null
git diff --check
