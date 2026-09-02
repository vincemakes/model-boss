# Claude Code adapter

Claude Code may keep its already selected session model as the Model Boss main loop
and spawn native agents for other roles. Install the default Anthropic profile from
`assets/agents/claude-code/` as `model-boss-<role>.md`; those model aliases are
examples, not core branches and do not alter the selected conversation model.

When the host supports a model parameter on its Agent/Task tool, pass the selected
route directly. Otherwise install the role files under `.claude/agents/` or the user
agent directory and dispatch by their unique names. Reviewer frontmatter alone cannot
enforce filesystem read-only access, so external reviewer calls must use the hardened
runtime transport.

## Pinning versions and effort

Subagent frontmatter accepts exact model IDs and an effort level, so the shipped role
files pin both (`model: claude-opus-5`, `effort: high`). To dispatch another catalog
model, copy the role file and change those two lines, or pass the route's exact model
to the Agent tool. Aliases resolve host-side: `opus` means the newest Opus the host
exposes, and `ANTHROPIC_DEFAULT_OPUS_MODEL` (likewise `_SONNET_`, `_HAIKU_`, `_FABLE_`)
pins what an alias means for the whole host. The main loop's own effort is set with
`--effort`, `/effort`, or `CLAUDE_CODE_EFFORT_LEVEL`; Model Boss reports it in the
verdict and never changes it.

## Cache facts that shape the hand-off

Each model and each effort level has its own prompt cache; switching either restarts
the main conversation's cache. A subagent starts its own conversation and never reads
the parent's cache, so a task packet is uncached input for the worker. Subagents also
fall outside the main-conversation TTL bucket: on a subscription the main conversation
gets a one-hour cache while subagents get five minutes unless `subagentPromptCacheTtl`
(or `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`) is set to `1h`. Fable 5.1 reads its own
cache at a quarter of the usual rate, which lowers the cost of staying inline; what it
carries across a model switch is earlier thinking blocks, not cache entries, and only
in the direction onto Fable 5.1. In the recorded Lite runs the Fable main loop's spend
was dominated by cache writes, that is by the worker reports and diffs it read for the
first time at the one-hour write rate, not by its own output; a short worker report and
a tight diff save more Fable than a terse main loop does.

## Existing Kimi and GLM command compatibility

| Role | Reviewer transport base command | Sandboxed write route |
|---|---|---|
| Kimi | `claude-kimi` | `claude-kimi-bypass -p` |
| GLM | `claude-glm` | `claude-glm-bypass -p` |
| GLM fast | `claude-glm-turbo` | `claude-glm-turbo-bypass -p` |

A command name is never model identity. The plain commands are not read-only by
themselves. For a reviewer, Model Boss appends
`--safe-mode --no-session-persistence --permission-mode plan --tools "" -p`, sets a
new isolated evidence directory as `cwd`, provides only the canonical packet on stdin,
and verifies that no artifact changed. Only this hardened composition may become a
reviewer candidate after preflight pins the resolved canonical fingerprint.

Bypass commands are write-capable. Model Boss refuses them without a verified OS
sandbox and runs them only in a disposable worktree, never the user's repository. The
wrapper setup migrates provider credentials into private data files; it does not edit
shell startup files or make a route eligible by itself.

See [external CLI safety](external-cli.md) for the full boundary.
