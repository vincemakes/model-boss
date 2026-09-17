# External CLI adapter

External routes use direct argument arrays. They never use shell-expanded strings,
source startup files, inherit the full parent environment, or treat a binary name as
model identity.

## Compatibility map

| Role | Reviewer transport base command | Sandboxed write route |
|---|---|---|
| Kimi | `claude-kimi` | `claude-kimi-bypass -p` |
| GLM | `claude-glm` | `claude-glm-bypass -p` |
| GLM fast | `claude-glm-turbo` | `claude-glm-turbo-bypass -p` |

## Tool-disabled reviewers

The plain commands are not read-only by themselves. The runtime resolves the exact
executable, appends
`--safe-mode --no-session-persistence --permission-mode plan --tools "" -p`, and
runs it with:

- a newly created evidence directory as `cwd`
- only the canonical packet on stdin
- a minimal environment with only any reviewer-route credentials required by the
  provider client, never worker credentials
- bounded concurrent stdout/stderr capture and a process-group timeout
- before/after evidence-directory manifests
- strict structured verdict parsing

Any mutation, unexpected artifact, malformed verdict, non-zero exit, or timeout
returns `transport_error` or `timeout` and cannot approve. Preflight must still obtain
the exact resolved fingerprint and prove it differs from the main loop.

## OS-sandboxed writers

Bypass commands are refused without a verified OS sandbox. For each invocation Model
Boss records the source snapshot, creates a disposable worktree and private route
state, verifies the sandbox can write inside and cannot write to an outside sentinel,
then runs the worker only in that worktree. The source repository, other worktrees,
user config, credential files, and shell startup files remain outside its write scope.

Invocation state uses `model-boss-invocation-<invocation-id>` and `.model-boss-*`
receipt names, with the isolated worker ref `refs/heads/model-boss-worker`. The child
manifest binding is `MODEL_BOSS_INVOCATION_MANIFEST`; trusted gate-failure context is
`MODEL_BOSS_TRUSTED_GATE_FAILURES`.

Credentials selected through `MODEL_BOSS_CREDENTIALS` are copied by named binding
into the child environment only. They never enter prompts, logs, config hashes,
manifests, or review packets. On a platform with no verified backend, return
`sandbox_unavailable` without launching the bypass command.

This protects credential values from model context and recorded evidence, not from the
provider client process itself. That executable necessarily receives the route
credentials used to call its endpoint. Use a short-lived, narrowly scoped token and
the least permissions the provider supports. Treat the provider binary as trusted
code: the filesystem sandbox and tool allowlist cannot prevent a malicious or
compromised provider binary from exfiltrating credentials or readable data through
the network connection the provider route requires.

The external worker model tool allowlist is exactly `Read`, `Glob`, `Grep`, `Edit`, and
`Write`. Bash is disabled; Web and MCP are unavailable. Model Boss itself runs the
task's declared gate argument arrays after the model call. Gates are host operations,
not shell capability exposed to the model.

### One-shot sealed workflow

Resolve the directory containing `SKILL.md` for the installed skill and call it
`<model-boss-skill-root>`. The installed
`<model-boss-skill-root>/scripts/model-boss.py` entry point, backed by
`runtime.model_boss`, is the supported bridge from a Claude Code or Codex main loop to
the Kimi/GLM bypass names. Never assume that entry point exists relative to the target
repository. Calling a bypass wrapper from an ordinary checkout is intentionally
refused.

The task JSON has this exact schema:

```json
{
  "version": 1,
  "prompt": "implement the bounded change",
  "allowed_paths": ["src/example.py", "tests/test_example.py"],
  "gates": [
    {
      "argv": ["python3", "-m", "unittest", "tests.test_example", "-v"],
      "cwd": ".",
      "timeout_seconds": 300
    }
  ]
}
```

The task file contains direct argument arrays, never shell text or credentials. The
sealed external CLI currently requires a worker invocation. Optional worker topologies
apply to host-native orchestration, where the host can capture equivalent evidence
without this bridge.

For Max, create the invocation and obtain plan approval before dispatch. The plan
context has exactly `version`, `goal`, `proposed_plan`, `acceptance_criteria`, and
`risks`:

```bash
python3 <model-boss-skill-root>/scripts/model-boss.py plan-review \
  --repo /absolute/path/to/repository \
  --temp-parent /absolute/path/to/existing-temp-parent \
  --task /absolute/path/to/task.json \
  --context /absolute/path/to/plan-context.json \
  --profile /absolute/path/to/profile.json \
  --route <reviewer-route> \
  --main-fingerprint <provider:model:variant>

python3 <model-boss-skill-root>/scripts/model-boss.py worker --manifest <manifest> \
  --repo /absolute/path/to/repository \
  --temp-parent /absolute/path/to/existing-temp-parent \
  --route claude-kimi-bypass \
  --task /absolute/path/to/task.json \
  --mode max
```

`plan-review` seals a private one-shot receipt that binds the canonical task, bounded
plan context, source snapshot, main fingerprint, and distinct eligible reviewer. Max
`worker --manifest` revalidates all of them before launching the worker. A revised
plan, mutated task, changed source snapshot, missing receipt, or changed reviewer
identity blocks dispatch and requires a new plan review.

The worker seals `authority_mode=max` in the bundle. It cannot switch, change, or
downgrade later. Changing topology requires a new invocation.

Both review paths require a strict context JSON with this exact schema:

```json
{
  "version": 1,
  "goal": "complete the bounded change",
  "approved_plan": "implement only the declared paths and run the declared gates",
  "acceptance_criteria": ["the declared gates pass"],
  "main_loop_verdict": "approve"
}
```

After the main loop audits the complete sealed evidence, final Max review must use the
same effective reviewer identity/configuration as plan review: route, resolved
reviewer fingerprint, identity-evidence source, and read-only enforcement. It must
also use the same main-loop fingerprint. The profile path itself may differ when it
resolves to those same facts:

```bash
python3 <model-boss-skill-root>/scripts/model-boss.py review \
  --profile /absolute/path/to/profile.json \
  --route <same-reviewer-route> \
  --main-fingerprint <same-provider:model:variant> \
  --manifest <manifest> \
  --context /absolute/path/to/review-context.json

python3 <model-boss-skill-root>/scripts/model-boss.py integrate <manifest>
```

Reviewer verdicts are only `approve` or `revise`. Missing context is a preflight or
runtime `needs_context` status outside the verdict; a reviewer requests more work with
`revise` and a non-empty requested-change list.

For Lite, plan authority remains inline. The worker creates its own invocation and
rejects `--manifest`; final authority is also inline:

```bash
python3 <model-boss-skill-root>/scripts/model-boss.py worker \
  --repo /absolute/path/to/repository \
  --temp-parent /absolute/path/to/existing-temp-parent \
  --route claude-kimi-bypass \
  --task /absolute/path/to/task.json \
  --mode lite

python3 <model-boss-skill-root>/scripts/model-boss.py review --inline \
  --main-fingerprint <provider:model:variant> \
  --manifest <manifest> \
  --context /absolute/path/to/review-context.json

python3 <model-boss-skill-root>/scripts/model-boss.py integrate <manifest>
```

An approving final review persists an invocation-bound receipt containing the
reviewer identity, sealed `authority_mode`, decision, exact plan binding for Max, and
the `source_snapshot_hash`/`worker_delta_hash`/`projected_task_patch_hash` binding.
Integration accepts only the manifest. A missing, stale, wrong-mode, swapped-plan, or
invalid receipt blocks integration before the invocation is consumed.

Verified external writer backends currently cover macOS and Linux, including Linux
under WSL. Native Windows returns `sandbox_unavailable`; host-native Claude Code and
Codex agents can still be used without selecting an external writer.

After execution, compare the source repository's full private fingerprint, capture
the worker delta, project the canonical task patch, and bind approval to
`source_snapshot_hash`, `worker_delta_hash`, and `projected_task_patch_hash`. Before
integration, recompute the destination snapshot. Drift returns
`destination_changed`; a changed tuple returns `approval_stale`.

Apply only the exact approved delta. Never execute a worker in the user's repository,
stash or reset user changes, overwrite a conflict, or accept a fuzzy apply. Cleanup
uses the invocation manifest and removes only its recorded resources.
