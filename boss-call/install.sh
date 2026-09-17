#!/usr/bin/env bash
# Install boss-call for this user.
#   ~/.local/bin/boss-call               the CLI  (~/.local/bin is already on PATH here)
#   ~/.kiso/extensions/boss-call.mjs     the kiso EXTENSION: a plain `kiso` inside a member
#                                        repo is a member session — no env, no opening line
#   ~/.kiso/skills/boss-call             the skill (kiso), directory symlink
#   ~/.claude/skills/boss-call           the skill (Claude Code)
#   ~/.codex/skills/boss-call            the skill (Codex)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$here/boss-call.py"
mkdir -p "$HOME/.local/bin" "$HOME/.kiso/extensions"
ln -sfn "$here/boss-call.py" "$HOME/.local/bin/boss-call"
rm -f "$HOME/bin/boss-call"                                # pre-0.2 location
ln -sfn "$here/boss-call.mjs" "$HOME/.kiso/extensions/boss-call.mjs"
for h in .kiso .claude .codex; do
  mkdir -p "$HOME/$h/skills"
  rm -rf "$HOME/$h/skills/boss-call"
  ln -sfn "$here" "$HOME/$h/skills/boss-call"
done
echo "  $HOME/.local/bin/boss-call        -> $here/boss-call.py"
echo "  $HOME/.kiso/extensions/boss-call.mjs -> $here/boss-call.mjs"
echo "  ~/.kiso ~/.claude ~/.codex skills/boss-call -> $here"
command -v boss-call >/dev/null || echo "note: ~/.local/bin is not on PATH in this shell"
