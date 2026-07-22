# Codex adapter

Codex keeps the conversation's selected model as the main loop. Install role defaults
from `assets/agents/codex/` into `.codex/agents/` (project) or `$HOME/.codex/agents/`
(user). Each TOML supplies a spawned role's model, reasoning effort, sandbox mode, and
instructions; it does not configure the main loop.

The default OpenAI profile illustrates these topologies:

- Sol main loop with Terra or Luna implementation is Lite.
- Terra main loop with a distinct Sol reviewer is Max; Luna may optionally implement.

These are capability examples, not provider-specific control flow. Fable, Opus, Sol,
Kimi K3, and future models use the same fingerprints, role checks, sealed manifests,
and state transitions. The same external bridge can be driven from a Codex or Claude
Code main loop without changing its evidence format.

Check `codex --version` for diagnostics, but do not infer model support from a numeric
version alone. Preflight must prove that the current installation supports custom
agents, that every selected model ID is available in the current account/model
catalog, and that the requested sandbox and reasoning settings are accepted. Token
Saver does not automatically upgrade the CLI.

If native custom agents are unavailable, an adapter may use a direct argument array
such as `codex exec --model <resolved-model>`. The runtime uses a fresh non-Git
evidence directory, `--ephemeral`, `--ignore-user-config`, `--ignore-rules`,
`--skip-git-repo-check`, and `--sandbox read-only`, with the canonical packet on
stdin. These controls remove writes and inherited project/user instructions; they do
not claim that every Codex read tool has been removed.

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

For a write-capable external worker, `worker --mode lite|max` is mandatory and seals
`authority_mode` into the bundle. It cannot switch later. A Lite bundle is reviewed
with `review --inline --main-fingerprint ... --manifest ... --context ...`; a Max
bundle is reviewed with external `--profile` and `--route` arguments. Max may use a
lower-tier Codex main loop plus a higher-authority reviewer and still delegate
implementation to a lower worker. After an approving review, run
`integrate <manifest>`; there is no separate approval-file argument.

External worker models receive only `Read`, `Glob`, `Grep`, `Edit`, and `Write`; Bash
is disabled and Web/MCP are unavailable. Token Saver runs gates in the host. The
provider client still receives its route credentials, so use short-lived,
narrowly-scoped tokens and trusted executables. The sandbox cannot stop a malicious
provider binary from using its required network connection.

Verified external-writer backends currently cover macOS and Linux, including Linux
under WSL. Native Windows returns `sandbox_unavailable` for external writers, while
host-native Codex and Claude Code agents remain usable.
