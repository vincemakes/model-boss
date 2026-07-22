# Codex adapter

Codex keeps the conversation's selected model as the main loop. Install role defaults
from `assets/agents/codex/` into `.codex/agents/` (project) or `$HOME/.codex/agents/`
(user). Each TOML supplies a spawned role's model, reasoning effort, sandbox mode, and
instructions; it does not configure the main loop.

The default OpenAI profile illustrates these topologies:

- Sol main loop with Terra or Luna implementation is Lite.
- Terra main loop with a distinct Sol reviewer is Max; Luna may optionally implement.

GPT-5.6 Sol requires Codex CLI 0.144.0 or later. Check `codex --version` before using
that profile. Token Saver does not automatically upgrade the CLI.

If native custom agents are unavailable, an adapter may use a direct argument array
such as `codex exec --model <resolved-model>`, with a fresh evidence-only directory,
an ephemeral read-only sandbox, no inherited tool surface, and the canonical review
packet on stdin.

## Reviewer eligibility

A native agent TOML is a default configuration layer, not a security boundary. A
native Codex reviewer is eligible for Max only when preflight receives both:

1. its resolved canonical fingerprint for the actual child invocation; and
2. host-reported effective child permissions after live parent overrides, with the
   sandbox exactly `read-only` and no write-capable tool surface.

Missing telemetry, danger-full-access or bypass parent flags, a fingerprint collision,
or any write capability makes the native reviewer ineligible. Use the hardened
ephemeral `codex exec` evidence-directory reviewer instead. If that transport also
fails, return `reviewer_unavailable`; never degrade explicit Max.

## External provider commands

Codex can invoke installed `claude-kimi*` and `claude-glm*` commands through Token
Saver's external adapter. This does not claim that Kimi Chat Completions is a native
Codex provider or that Kimi appears in the Codex model picker. External reviewers and
workers remain subject to the same identity, tool, sandbox, and evidence checks in
[the external CLI contract](external-cli.md).
