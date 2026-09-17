#!/usr/bin/env bash
# Install boss-call the way the vstack family is installed: the skill DIRECTORY
# is symlinked into each harness's skills dir, so an edit in the repo is live
# everywhere and `ls -la ~/.claude/skills` shows where it comes from.
#   ~/bin/boss-call                 the CLI
#   ~/.kiso/skills/boss-call   ->   this directory      (kiso)
#   ~/.claude/skills/boss-call ->   this directory      (Claude Code)
#   ~/.codex/skills/boss-call  ->   this directory      (Codex)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$here/boss-call.py"
mkdir -p "$HOME/bin"
ln -sfn "$here/boss-call.py" "$HOME/bin/boss-call"
for h in .kiso .claude .codex; do
  mkdir -p "$HOME/$h/skills"
  rm -rf "$HOME/$h/skills/boss-call"
  ln -sfn "$here" "$HOME/$h/skills/boss-call"
  echo "  $HOME/$h/skills/boss-call -> $here"
done
echo "  $HOME/bin/boss-call -> $here/boss-call.py"
case ":$PATH:" in *":$HOME/bin:"*) ;; *) echo "note: add \$HOME/bin to PATH (the harness's shell tool must find boss-call)";; esac
