# Claude Code adapter

Claude Code may keep its already selected session model as the Token Saver main loop
and spawn native agents for other roles. Install the default Anthropic profile from
`assets/agents/claude-code/`; those model aliases are examples, not core branches and
do not alter the selected conversation model.

When the host supports a model parameter on its Agent/Task tool, pass the selected
route directly. Otherwise install the role files under `.claude/agents/` or the user
agent directory and dispatch by their unique names. Reviewer frontmatter alone cannot
enforce filesystem read-only access, so external reviewer calls must use the hardened
runtime transport.

## Existing Kimi and GLM command compatibility

| Role | Reviewer transport base command | Sandboxed write route |
|---|---|---|
| Kimi | `claude-kimi` | `claude-kimi-bypass -p` |
| GLM | `claude-glm` | `claude-glm-bypass -p` |
| GLM fast | `claude-glm-turbo` | `claude-glm-turbo-bypass -p` |

A command name is never model identity. The plain commands are not read-only by
themselves. For a reviewer, Token Saver appends
`--safe-mode --no-session-persistence --permission-mode plan --tools "" -p`, sets a
new isolated evidence directory as `cwd`, provides only the canonical packet on stdin,
and verifies that no artifact changed. Only this hardened composition may become a
reviewer candidate after preflight pins the resolved canonical fingerprint.

Bypass commands are write-capable. Token Saver refuses them without a verified OS
sandbox and runs them only in a disposable worktree, never the user's repository. The
wrapper setup migrates provider credentials into private data files; it does not edit
shell startup files or make a route eligible by itself.

See [external CLI safety](external-cli.md) for the full boundary.
