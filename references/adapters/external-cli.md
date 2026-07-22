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
- a minimal environment with no worker credentials
- bounded concurrent stdout/stderr capture and a process-group timeout
- before/after evidence-directory manifests
- strict structured verdict parsing

Any mutation, unexpected artifact, malformed verdict, non-zero exit, or timeout
returns `transport_error` or `timeout` and cannot approve. Preflight must still obtain
the exact resolved fingerprint and prove it differs from the main loop.

## OS-sandboxed writers

Bypass commands are refused without a verified OS sandbox. For each invocation Token
Saver records the source snapshot, creates a disposable worktree and private route
state, verifies the sandbox can write inside and cannot write to an outside sentinel,
then runs the worker only in that worktree. The source repository, other worktrees,
user config, credential files, and shell startup files remain outside its write scope.

Credentials are copied by named binding into the child environment only. They never
enter prompts, logs, config hashes, manifests, or review packets. On a platform with no
verified backend, return `sandbox_unavailable` without launching the bypass command.

After execution, compare the source repository's full private fingerprint, capture
the worker delta, project the canonical task patch, and bind approval to
`source_snapshot_hash`, `worker_delta_hash`, and `projected_task_patch_hash`. Before
integration, recompute the destination snapshot. Drift returns
`destination_changed`; a changed tuple returns `approval_stale`.

Apply only the exact approved delta. Never execute a worker in the user's repository,
stash or reset user changes, overwrite a conflict, or accept a fuzzy apply. Cleanup
uses the invocation manifest and removes only its recorded resources.
