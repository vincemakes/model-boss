# Token Saver Cross-Platform Redesign

**Date:** 2026-07-21  
**Status:** Approved for implementation planning  
**Repository:** `vincemakes/token-saver`

## Purpose

Rename `fable-token-saver` to `token-saver` and redesign it as a genuinely cross-platform, model-independent orchestration skill for Claude Code and Codex.

The product keeps two execution modes because they represent materially different placements of reasoning authority and main-loop context cost:

- **Lite:** an authority-capable model is the selected main loop. It owns reasoning, planning, orchestration, and final review while lower-cost workers implement.
- **Max:** a lower-cost model is the selected main loop. A separate authority-capable model owns key design decisions and final review. The main loop may implement directly or delegate further to a still cheaper worker.

The skill never selects or changes the user's main-loop model. The host application has already selected it before Token Saver runs.

## Product Principles

1. **The current main loop is an immutable input.** Token Saver resolves worker and reviewer routes around it.
2. **Models fill capabilities, not brand-specific branches.** Fable, Opus, GPT-5.6 Sol, Kimi K3, and future models are profiles or routes, not control-flow conditions.
3. **Authority placement defines the mode.** Lite binds authority to the main loop; Max binds authority to a distinct external reviewer.
4. **Automatic selection must be safe.** Explicit user choices win. Unknown or ambiguous models cause a question, not a guess.
5. **Max must fail closed.** It cannot dispatch or integrate without a verified, distinct authority reviewer.
6. **Objective gates precede model review.** Typechecks and tests own machine-verifiable correctness.
7. **Reviews use complete evidence.** New files, staged changes, tracked changes, scope, gate results, and the exact patch hash are all included.
8. **Historical evidence remains historical.** Existing Fable/Opus benchmarks are preserved and labeled as one reference stack, not rewritten as universal claims.

## Terminology

| Term | Meaning |
|---|---|
| Main loop | The model already selected for the current Claude Code or Codex session |
| Authority | The model with final responsibility for design decisions and approval |
| Reviewer | A read-only authority route used by Max |
| Worker | A write-capable implementation route at the same or lower cost/capability band |
| Scout | A read-only reconnaissance route |
| Mechanic | A fast route for unambiguous mechanical work |
| Authority band | Models suitable for final design and review judgment |
| Balanced band | Models suitable for orchestration and general implementation |
| Fast band | Models suitable for reconnaissance and mechanical work |

“Low-cost” and “lower-tier” describe the role relative to the selected authority for this workflow. The documentation must not describe such models as intrinsically “low quality.”

## Mode Semantics

### Lite: inline authority

```text
Authority-capable main loop
  reasoning + planning + orchestration + final review
                         |
                         v
              lower-cost worker(s)
                    implementation
```

Lite properties:

- `authority_route == main_loop_route`.
- The authority sees the full session context and pays the main-loop context cost.
- Implementation is delegated down when the task clears the delegation floor.
- No external reviewer call is required.
- The main loop may directly handle work below the delegation floor.

### Max: external authority

```text
        authority reviewer
     design verdict + final review
                ^
                | compact evidence packets
                |
       balanced main loop
  full context + orchestration + audit
                |
                v optional
          lower-cost worker
            implementation
```

Max properties:

- The authority and main loop resolve to different canonical model fingerprints; different route names or aliases are insufficient.
- The balanced main loop bears the full session and bookkeeping context cost.
- The authority reviewer receives compact evidence at two checkpoint types: plan and final.
- The main loop may implement when no lower worker is configured, or delegate to a lower worker when available.
- Integration is impossible without an authority approval tied to the current patch hash.
- “Two checkpoints” does not mean exactly two calls. A `revise` verdict may repeat the same checkpoint within a bounded loop.

## Mode Resolution

Resolution order:

1. Explicit user instruction for `lite` or `max` and any explicit routes.
2. Project configuration in `.token-saver.json`.
3. User configuration in `${XDG_CONFIG_HOME:-$HOME/.config}/token-saver/config.json`.
4. Exact host metadata and built-in model profiles.
5. Ask the user if the topology remains ambiguous.

The resolver treats the main loop as inherited and never writes a replacement into configuration.

Default automatic behavior:

- A known authority-band main loop defaults to Lite. Initial examples include Fable, Opus, GPT-5.6 Sol, and Kimi K3.
- A known balanced-band main loop resolves to Max only if a reachable, distinct authority reviewer passes preflight.
- An unknown main-loop model does not get classified from marketing language or identity boilerplate.
- Explicit Max can bind a stronger reviewer even when the main model is normally authority-capable, such as an Opus main loop with a Fable reviewer.
- If a user explicitly requests Max and no reviewer is available, return `REVIEWER_UNAVAILABLE`; do not silently degrade to Lite.

### Canonical model identity

Route IDs select transports, but Max independence is validated with a canonical model fingerprint:

```text
<provider family>:<resolved model ID>:<capability/reasoning variant>
```

Endpoint aliases, wrapper names, profiles, and account names do not make the same underlying model independent. For example, two aliases that both resolve to the same provider model have the same fingerprint even if one uses a proxy.

Preflight obtains the resolved model ID from exact host metadata, an explicit pinned adapter model, or a provider model response. If a reviewer route hides the actual model and cannot prove its fingerprint, it cannot serve as a Max authority. Route inequality alone never satisfies this check.

Every activation begins by reporting:

```text
Main loop: <route/model>
Resolved mode: <Lite|Max>
Authority: <inline main loop|reviewer route>
Worker: <route|main loop|none>
Resolution source: <explicit|project|user|profile>
```

## Unified State Machine

Both modes use one state machine. The authority binding is the only mode-dependent control-flow input.

```text
RESOLVE
  -> PREFLIGHT
  -> CLASSIFY
  -> RECON
  -> DRAFT_PLAN
  -> AUTHORITY_PLAN_CHECK
  -> DISPATCH
  -> GATE
  -> PATCH_AUDIT
  -> MAIN_LOOP_REVIEW
  -> AUTHORITY_FINAL_CHECK
  -> INTEGRATE
```

### State requirements

#### RESOLVE

- Identify the host, exact current main-loop route when available, explicit mode, and configuration sources.
- Resolve authority, worker, mechanic, and scout candidates by capability.
- Never infer Lite from identity prose alone.

#### PREFLIGHT

- Confirm every required executable or native agent route is available.
- Resolve canonical model fingerprints and confirm the Max reviewer differs from the main loop.
- Confirm read-only enforcement for the reviewer transport; route metadata or prompt instructions alone are insufficient.
- Confirm external write-capable workers can run inside an enforced filesystem sandbox whose write allowlist is limited to the disposable worktree and route-owned temporary state. A Git worktree alone is not a sandbox.
- Confirm the configured model is reachable without printing credentials.

#### CLASSIFY

- Mechanical work uses a mechanic route.
- Specifiable implementation uses a worker route.
- Design-heavy work keeps design authority in the authority checkpoint and delegates only the bounded implementation.
- Automatic activation steps aside for small work, judgment-dense debugging, and tasks below the measured delegation floor.
- An explicit invocation may continue after warning that orchestration overhead can exceed savings.

#### RECON

- Scouts return conclusions and exact references, not file dumps.
- The main loop gathers only the evidence required to draft a plan.

#### DRAFT_PLAN

- In Lite, the authority-capable main loop authors the plan and acceptance criteria.
- In Max, the balanced main loop prepares a compact proposal from reconnaissance.

#### AUTHORITY_PLAN_CHECK

- In Lite, this is an inline authority decision.
- In Max, the reviewer receives the goal, constraints, reconnaissance conclusions, proposed decomposition, interface sketch, and open risks.
- The authority returns architecture decisions, acceptance criteria, and risk guards.
- Dispatch cannot begin until this state passes.

#### DISPATCH

- Task packets contain goal, compact context, allowed files, acceptance criteria, exact gates, return format, and scope fences.
- Native host agents are preferred when they support the required model and permissions.
- CLI routes are used when native routing cannot select the desired model or provider.
- External write-capable workers run in disposable worktrees based on a recorded commit.

#### GATE

- Workers run exact typecheck and test commands and self-fix for at most three attempts.
- Missing or red gate evidence blocks review.
- Three failed attempts return `GATE_FAILED` to the main loop. The main loop may take over the bounded implementation or stop; the router does not silently select a different model.

#### PATCH_AUDIT

The audit covers all of:

- `git status --porcelain`
- tracked unstaged diff
- staged diff
- untracked files
- allowed-file scope
- baseline commit
- deterministic patch hash

`git diff` alone is never described as the complete ground truth.

The canonical task patch uses a sorted path manifest. Text changes use normalized Git binary-safe patch output; untracked and binary entries include path, mode, size, and SHA-256 content hash, with their bytes carried in the evidence bundle. Out-of-scope dirty content is represented by a private fingerprint containing path, mode, size, and content or canonical-diff hashes; the reviewer receives only its aggregate hash and status manifest, not unrelated contents. This same representation is used for review, hashing, destination-change detection, and post-integration verification.

#### MAIN_LOOP_REVIEW

- The main loop reviews the complete audited patch for intent, abstraction, scope, and missing implied edge cases.
- It does not spend review time re-performing typechecking owned by the gate.
- In Max, it creates a concise preliminary verdict for the authority reviewer.

#### AUTHORITY_FINAL_CHECK

- In Lite, the main loop issues the final decision inline.
- In Max, the reviewer receives the approved plan, acceptance criteria, complete canonical task patch, status and scope audit, gate commands and exit codes, patch hash, and preliminary verdict.
- The canonical task patch includes every in-scope staged, unstaged, and untracked change plus every worker-created file. It also includes a manifest of out-of-scope repository changes so scope violations cannot be hidden.
- Large patches may be split into numbered chunks only when the reviewer receives every chunk, the complete file manifest, per-chunk hashes, and the total canonical patch hash. “Design-relevant hunks only” is not an acceptable substitute.
- `approve` authorizes only the supplied patch hash.
- `revise` returns pointed deltas. The implementation, gate, audit, and final checkpoint repeat for at most two revision rounds.
- Exceeding the revision bound returns control to the user with evidence.

#### INTEGRATE

- Recompute the patch hash immediately before integration.
- Any change after approval invalidates the verdict and returns to `AUTHORITY_FINAL_CHECK`.
- Confirm the destination snapshot still matches the snapshot used to construct the reviewed patch. A changed destination returns `destination_changed`; Token Saver does not overwrite, reset, or auto-resolve it.
- Apply only the reviewed worker delta, then verify that the resulting canonical task patch matches the approved hash. An apply conflict stops without destructive cleanup.
- Max cannot integrate without a current external authority approval.

## Route and Configuration Model

Most users need no configuration. Built-in profiles provide common defaults, while optional JSON allows project or user overrides.

The configuration schema contains no `main_loop` key. A route declares:

- stable route ID
- transport
- model or durable alias when required
- capability band
- allowed roles
- read/write capability
- command argument array for CLI routes
- timeout and retry policy

Example:

```json
{
  "version": 1,
  "mode": "auto",
  "routes": {
    "sol-reviewer": {
      "transport": "codex-native",
      "model": "gpt-5.6",
      "band": "authority",
      "roles": ["reviewer"],
      "read_only": true
    },
    "kimi-worker": {
      "transport": "external-cli",
      "command": ["claude-kimi-bypass", "-p"],
      "band": "balanced",
      "roles": ["implementer"],
      "read_only": false
    }
  },
  "preferences": {
    "reviewers": ["sol-reviewer"],
    "workers": ["kimi-worker"]
  }
}
```

Command routes use argument arrays and direct process execution. They never pass user configuration through `eval`, `sh -c`, or interpolated command strings.

Credentials are not stored in `.token-saver.json`. Adapters use existing CLI authentication or an environment variable named by the provider configuration. Setup and diagnostics print only variable names and configured/unconfigured state.

## Cross-Platform Adapters

### Claude Code

- Prefer the native Agent model parameter when it can select the requested route.
- Provide Claude-specific fallback agent definitions under `assets/agents/claude-code/`.
- Retain external Anthropic-compatible wrappers for Kimi and GLM as CLI routes.
- Render model-specific fallback definitions from profiles rather than hard-coding Fable in the generic reviewer prompt.

### Codex

- Prefer native Codex custom agents under `.codex/agents/*.toml` for OpenAI models.
- Custom agents may pin model, reasoning effort, sandbox mode, and role instructions.
- Ship installable examples for authority reviewer, balanced implementer, mechanic, and scout roles.
- Use `codex exec --model` only when a native agent route is unavailable or an isolated non-interactive run is required.
- Preflight requires a Codex version supporting the selected model. GPT-5.6 Sol requires Codex CLI 0.144.0 or later.

### Existing Kimi and GLM wrappers

The current machine already exposes these commands on `PATH`:

- `claude-kimi`
- `claude-kimi-bypass`
- `claude-glm`
- `claude-glm-bypass`
- GLM turbo variants

They launch Claude Code against Anthropic-compatible provider endpoints. Codex can invoke them as external CLI routes without treating them as native Codex models.

This distinction is mandatory:

- **Supported now:** a Codex main loop dispatches Kimi/GLM as a worker or reviewer through the existing wrapper.
- **Not claimed:** selecting Kimi directly from the native Codex model picker through the current wrapper.

Codex custom model providers currently require the Responses API. Kimi's public API currently documents OpenAI-compatible Chat Completions, so the wrapper cannot be converted into a native Codex provider by renaming environment variables. A future Kimi Responses endpoint or an explicit protocol-translating adapter can add that transport later without changing the core state machine.

Reviewer calls through `claude-kimi` are tool-disabled, sandboxed evidence-only calls. Worker calls use the bypass variant only inside an isolated worktree.

### External reviewer enforcement contract

External reviewer transports must enforce read-only behavior independently of the prompt:

1. Create a dedicated temporary evidence directory outside the repository and any worker worktree.
2. Put only the canonical review packet in that directory; do not provide repository paths or credentials in the packet.
3. Start the reviewer with no write-capable, shell, browser, MCP, or repository tools. For the current Claude wrapper this means safe mode, no session persistence, plan permissions, and an empty tool allowlist. Codex CLI reviewers use an ephemeral read-only sandbox.
4. Send the packet through process stdin using a direct argument array, not a shell-expanded command string.
5. Require a structured verdict matching the reviewer result schema.
6. Record the evidence-directory manifest before and after the call. Any mutation, unexpected child artifact, timeout, or non-zero exit returns `transport_error` and cannot approve.
7. Terminate the exact child process group on timeout and remove only the temporary directory created for that call.

A route declaration such as `read_only: true` is descriptive metadata; it does not replace these enforcement steps.

### Custom providers

- A custom provider can register a native host agent or an explicit executable argument array.
- The route must declare capabilities and read/write behavior.
- A custom route is eligible for automatic selection only after it appears in project or user configuration. An unconfigured route can be used only when the user explicitly selects it for the current run; Token Saver does not create an unspecified persistent approval record.
- Arbitrary shell snippets are not accepted as route definitions.

## External Worker Isolation

External write-capable workers never run against a shared tree, whether that tree is clean or dirty.

1. Verify the target repository and record `HEAD` without changing it.
2. Capture a deterministic source snapshot containing the staged patch, unstaged patch, in-scope untracked files with modes and content hashes, and private content/diff hashes for every out-of-scope dirty or untracked path. Ignored files are excluded unless the user explicitly names them.
3. Compute `source_snapshot_hash` from the baseline commit and canonical snapshot representation.
4. Create a disposable worktree in a narrowly scoped temporary directory at the recorded commit.
5. Materialize all in-scope source-snapshot changes into the disposable worktree. If they cannot be applied exactly, stop before dispatch.
6. Launch the worker through a supported OS sandbox backend. The backend allows writes only to the disposable worktree and a newly created route-state directory; it explicitly denies the original repository, user configuration, credential files, and every other path. Required credentials are passed as narrowly scoped environment values, and global provider state is read-only.
7. Use `--dangerously-skip-permissions` only inside that verified OS sandbox. If no sandbox backend is available, return `sandbox_unavailable` and do not launch the external write route.
8. Run the worker in the sandboxed worktree and capture its delta relative to the materialized source snapshot.
9. Build the projected canonical task patch from all in-scope source changes plus the complete worker delta. Record `worker_delta_hash` and `projected_task_patch_hash`.
10. Capture exit status, stdout result packet, Git status, complete patch, untracked files, scope audit, and all three hashes.
11. Recompute the original repository fingerprint after the worker exits. Any change proves a sandbox contract breach, returns `scope_violation`, and blocks all integration.
12. Reject timeouts, partial failures, and scope violations without copying changes back.
13. Before integration, recompute the full original destination snapshot, including private hashes for out-of-scope dirty contents. Any destination change since dispatch returns `destination_changed`, even if status codes are unchanged or Git could attempt a fuzzy apply.
14. If the destination is unchanged, apply only the worker delta. Conflicts stop and preserve the user's tree; Token Saver never resets or overwrites to force the patch through.
15. Verify the destination now produces the approved `projected_task_patch_hash`.
16. Clean up only the specific disposable worktree, sandbox profile, and temporary directory created for the run.

Authority approval binds the tuple `(source_snapshot_hash, worker_delta_hash, projected_task_patch_hash)`. It never authorizes an abstract worker summary or a patch projected onto a different destination state.

If the task requires ignored files or dirty out-of-scope files that Token Saver cannot safely snapshot, it asks for direction instead of copying them implicitly.

### Worker sandbox contract

Supported external-worker sandbox backends may include the host's native Codex sandbox, a macOS write-deny profile, a Linux namespace sandbox such as Bubblewrap, or an explicitly configured container. Every backend must implement the same contract:

- read access only to the materialized worktree and the minimum runtime/provider files
- write access only to the disposable worktree and route-owned temporary state
- no write access to the source repository, user configuration, credentials, shell startup files, or other worktrees
- network access limited to what the selected provider route requires when the backend supports network filtering
- termination of the complete process group on timeout
- cleanup scoped to resources created for that invocation

Route setup validates the backend with a controlled probe that must successfully write inside the worktree and fail to write to a sentinel path outside it. Runtime still compares the original repository fingerprint before and after every worker call. Failure of either control disables the route.

The implementation must never commit, stash, reset, or overwrite the user's existing uncommitted changes to prepare an external worker.

## Structured Results and Errors

Every routed call reports:

- role
- route ID
- attempt number
- status
- exit code when applicable
- baseline commit
- files changed
- gate result
- patch hash when applicable
- concise message

Defined statuses:

- `ok`
- `needs_context`
- `gate_failed`
- `provider_unavailable`
- `reviewer_unavailable`
- `timeout`
- `scope_violation`
- `transport_error`
- `review_revise`
- `approval_stale`
- `destination_changed`
- `sandbox_unavailable`

Errors are surfaced without provider secrets, raw authorization headers, or full environment dumps.

## Repository Structure

```text
SKILL.md
README.md
README.zh-CN.md
BENCHMARKS.md
BENCHMARKS.zh-CN.md
references/
  protocol.md
  routing.md
  adapters/
    claude-code.md
    codex.md
    external-cli.md
  profiles/
    anthropic.json
    openai.json
    kimi.json
assets/agents/
  prompts/
    reviewer.md
    implementer.md
    mechanic.md
    scout.md
  claude-code/
  codex/
config/
  token-saver.example.json
scripts/
  token-saver-route.py
  setup-model-providers.sh
  validate.sh
  package-skill.sh
tests/
  test_route.py
  test_package.py
media/
  og.png
dist/
  token-saver.skill
docs/superpowers/
  specs/
  plans/
```

The exact number of generated adapter examples may be reduced during planning if one reusable role file can serve multiple routes without losing explicit model selection.

## Brand and Migration

- Canonical product name: `Token Saver`.
- Canonical repository, skill name, slash command, installation directory, package root, and artifact name: `token-saver`.
- Update the local Git remote to `https://github.com/vincemakes/token-saver.git`.
- Do not publish a duplicate legacy skill solely to preserve `/fable-token-saver`.
- Keep `fable-token-saver` as a migration trigger phrase and in historical evidence where accurate.
- Copy legacy provider configuration only when the new destination is absent; preserve mode `0600`; never delete the old credential file automatically.
- Preserve wrapper command names such as `claude-kimi` and `claude-glm`, because they identify provider transports rather than the old product brand.
- Replace `dist/fable-token-saver.skill` with `dist/token-saver.skill`.
- Regenerate the social card around the generic Lite two-level and Max three-level topology.

## Documentation Strategy

The English and Chinese READMEs use the same information architecture:

1. One-screen explanation of Lite and Max.
2. Main-loop-as-input principle.
3. Generic topology diagrams.
4. Model profile examples for Anthropic, OpenAI, Kimi, and custom routes.
5. Claude Code and Codex installation instructions.
6. Existing Kimi/GLM wrapper integration.
7. Safety, preflight, isolation, and failure semantics.
8. Reference benchmark summary with explicit scope limits.
9. When Token Saver should step aside.

Benchmark documents retain original model names, prices, dates, and measurements. They add a prominent reference-stack notice and distinguish:

- strongest-model output-token reductions (`-42%`, `-89%` in the recorded large-task runs)
- price-weighted quota proxies (`-34%`, `-88%`)

Claims such as capability parity are phrased as observations from the recorded probe, not universal proof.

`docs/DEVNOTES.zh-CN.md` remains a historical archive. Old paths stay where necessary to reproduce the original runs, with an explicit pre-rename label and a current repository pointer.

## Packaging

Add a reproducible package script that:

- builds a `token-saver/` archive root
- includes `SKILL.md`, README files, benchmark reports, references, profiles, agent assets, scripts, media, and LICENSE
- checks the frontmatter name
- enforces the skill-description length limit
- verifies all relative README links exist inside the package
- verifies packaged `SKILL.md` matches the source hash
- excludes credentials, local configuration, tests, Git metadata, and historical workspace paths not needed at runtime
- writes `dist/token-saver.skill` atomically

The obsolete artifact is removed from the canonical distribution.

## Verification Strategy

### Route resolution

- Known authority main loops such as Fable, Opus, Sol, and Kimi K3 default to Lite.
- Known balanced main loops resolve to Max only with a distinct reachable reviewer.
- Explicit mode and route choices override profiles.
- Unknown models require user input.
- Reviewer/main-loop identity collisions fail preflight.

### State machine

- Max plan approval occurs before dispatch.
- Max final approval occurs before integration.
- A final `revise` verdict loops through implementation, gate, audit, and re-review.
- Reviewer unavailability, timeout, or stale approval blocks integration.
- Lite makes no external reviewer call.

### Patch integrity

- Tracked, staged, and untracked changes all enter the patch evidence.
- Out-of-scope files are detected.
- Post-approval changes invalidate the patch hash.
- External worker failures leave the user's tree untouched.
- Sandbox conformance tests prove an external worker can write inside its disposable worktree but cannot write to a sentinel path outside it.

### Configuration and security

- JSON parsing rejects unknown schema versions and invalid capabilities.
- CLI commands execute as argument arrays without shell evaluation.
- Tokens containing quotes, whitespace, backslashes, or shell syntax remain data and are never executed.
- Logs redact configured credential variables.
- Legacy credential migration is non-destructive and idempotent.

### Repository and package

- Frontmatter description remains within the packaging limit.
- New brand and URLs appear everywhere except an explicit historical/compatibility allowlist.
- English and Chinese documentation have matching sections.
- All links and referenced package files exist.
- The archive root, skill name, artifact name, and source hashes agree.
- The setup script passes shell syntax validation.
- JSON data and eval fixtures parse successfully.

## Out of Scope

- Changing the main-loop model from inside Token Saver.
- Installing or upgrading Claude Code, Codex, or Kimi without an explicit user request.
- Claiming native Codex support for a provider that does not expose the required Responses protocol.
- Re-running paid cross-model benchmarks as part of the rename.
- General-purpose arbitrary shell orchestration unrelated to model routes.
- Rewriting historical benchmark evidence to current model names.

## Success Criteria

The redesign is complete when:

1. The repository and packaged skill use `token-saver` as the canonical identity.
2. Core mode logic contains no provider-specific model branch.
3. The selected main loop remains inherited from the host.
4. Lite and Max resolve from authority placement with explicit, safe failure behavior.
5. Codex can use native lower-cost OpenAI custom agents and can dispatch the existing Kimi/GLM wrappers as external routes.
6. Claude Code retains native routing and external wrapper support through adapter documentation.
7. Max cannot bypass either authority checkpoint or integrate a stale patch.
8. External write-capable workers are isolated from the user's current worktree.
9. Historical benchmark claims are accurately scoped.
10. Automated validation passes and the new `.skill` package is reproducible and internally complete.
