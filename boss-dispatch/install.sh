#!/usr/bin/env bash
# Install boss-dispatch the way the vstack family is installed: the skill DIRECTORY
# is symlinked into each harness's skills dir, so an edit in the repo is live
# everywhere and `ls -la ~/.claude/skills` shows where it comes from.
#   ~/.claude/skills/boss-dispatch -> this directory   (Claude Code)
#   ~/.codex/skills/boss-dispatch  -> this directory   (Codex)
# kiso is not installed here: boss-dispatch is a Claude Code / Codex orchestration
# skill, not a kiso skill.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$here/scripts/model-boss.py" "$here/scripts/setup-model-providers.sh"
for h in .claude .codex; do
  mkdir -p "$HOME/$h/skills"
  rm -f "$HOME/$h/skills/model-boss"          # stale pre-rename symlink
  rm -rf "$HOME/$h/skills/boss-dispatch"
  ln -sfn "$here" "$HOME/$h/skills/boss-dispatch"
  echo "  $HOME/$h/skills/boss-dispatch -> $here"
done
