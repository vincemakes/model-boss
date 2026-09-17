# Model Boss protocol

This reference expands the provider-neutral workflow in `SKILL.md`. Host adapters may
change how roles are launched, but they must not weaken these state, evidence, or
integration rules.

## Eligibility and delegation floor

Delegate only a bounded unit whose acceptance criteria and allowed paths can be stated
before implementation. The orchestration overhead is usually justified by a material
multi-file change, repeated edits, or independent packets that can run concurrently.
For tiny edits, pure analysis, unresolved root-cause debugging, or architecture whose
contract cannot yet be specified, step aside and let the inherited main loop work
normally.

The floor is decided, not guessed. `estimate` prices inline, Lite, and Max for the
task shape (changed lines, files, judgment density, specification clarity, independent
packets) using the catalog's per-token prices, and applies one of three objectives.
Under the default `pace` objective a hand-off is judged by task shape and the week's
quota pace: execution-heavy, specifiable work of roughly 200 changed lines or two or
more independent packets is delegated; judgment-dense work, small single-packet
changes, unclear specifications, and an exhausted half are not. Under the cost
objectives it charges the worker's cold start, the main loop's packet and review, and
the expected rework, then requires a 10% saving. The estimate is a proxy calibrated on
two recorded runs and is printed with the startup verdict so the trade-off is visible.

## Authority topologies

The Boss is the workflow authority holder. Lite assigns that authority to the
inherited main loop inline. Max assigns it to one distinct eligible reviewer while
the inherited main loop coordinates and audits.

Lite is a two-level topology when a worker is used:

```text
authority main loop  ──plans/reviews/integrates──>  worker
```

The worker may be omitted. The inherited main loop owns both authority checkpoints
inline.

Max is always anchored by a separate authority reviewer:

```text
authority reviewer  <──plan/final evidence──  balanced main loop
                                               │
                                               └── optional worker
```

The optional worker may be omitted in host-native orchestration. This produces either
two levels (reviewer + main loop) or three (reviewer + main loop + worker). The sealed
external CLI currently requires a worker invocation because its evidence contract is
defined around a disposable-worktree delta. The selected main loop never changes.

The resolved topology is not a cosmetic label. The worker entry seals an
`authority_mode` of `lite` or `max` into the invocation-bound delta bundle. That
field is covered by the bundle hash and external seal receipt, so the invocation
cannot switch, downgrade, or upgrade authority paths after worker execution. A Lite
bundle accepts only inline main-loop authority; a Max bundle accepts only a distinct
eligible reviewer that passes the current identity and read-only preflight.

## State and checkpoint contract

The canonical order is:

```text
RESOLVE -> PREFLIGHT -> CLASSIFY -> RECON -> DRAFT_PLAN
-> AUTHORITY_PLAN_CHECK -> DISPATCH -> GATE -> PATCH_AUDIT
-> MAIN_LOOP_REVIEW -> AUTHORITY_FINAL_CHECK -> INTEGRATE
```

`RECON` can record a no-op, but no state may disappear. Max plan approval precedes any
dispatch. Final approval follows a complete patch audit and the main loop's own review.
Lite executes those same two checkpoints inline.

For the sealed external CLI, the executable Max order is `plan-review`, `worker
--manifest`, final `review`, then `integrate`. Plan and final review must resolve the
same effective reviewer identity/configuration: reviewer route, canonical fingerprint,
identity-evidence source, and effective read-only enforcement. They must also resolve
the same main-loop fingerprint. The profile path itself need not be identical.
Reviewer verdicts are only
`approve` or `revise`; missing inputs produce a `needs_context` status outside the
verdict, while requested work uses `revise` with a non-empty change list.

## Task packet schema

A task packet is immutable for one source snapshot and contains:

- packet version and task ID
- one-sentence goal
- distilled context and decisions
- exact allowed paths and forbidden changes
- testable acceptance criteria
- gate commands as argument arrays, with relative cwd and timeout
- return schema
- source snapshot identity

It contains no shell fragments, raw secrets, full conversation, permission to merge,
or implied access outside the allowed paths.

## Gate policy

Workers may self-fix against the same packet at most three times. Each gate record
contains the argument array, validated relative cwd, status, exit code, stdout hash,
stderr hash, and duration. Missing output and non-zero gates are failures, not material
for authority review. A timeout terminates the exact process group.

## Canonical patch evidence

Evidence is deterministic and binary-safe. The source snapshot records the baseline
commit and all relevant staged, unstaged, untracked, binary, symlink, and mode facts.
Out-of-scope dirty data is represented to reviewers by a status manifest and aggregate
private fingerprint; unrelated contents are never disclosed.

Worker execution happens in a disposable worktree materialized from the source
snapshot. Its complete delta is captured independently of worker prose. Projection
replays that delta against the captured source state and produces the canonical task
patch.

The three authority hashes are:

1. `source_snapshot_hash`
2. `worker_delta_hash`
3. `projected_task_patch_hash`

The final packet contains the approved plan, acceptance criteria, full file manifest,
canonical patch bytes or numbered complete chunks, per-chunk and total hashes, scope
audit, gate results, main-loop verdict, and the three-hash tuple. Selective “important
hunks” are insufficient.

The runtime constructs that packet from the sealed bundle; callers cannot substitute
an arbitrary packet. It also binds `authority_mode`, invocation ID, sealed-bundle
SHA-256, approval-binding hash, and all three evidence hashes. A reviewer verdict must
echo both the approval-binding hash and the SHA-256 of the exact packet bytes.

## Plan authority receipt

Before Max dispatch, `plan-review` creates one private invocation and captures the
source snapshot for the task's exact allowed paths. The canonical plan packet binds
the invocation ID, canonical task hash, bounded plan-context hash, source-snapshot
hash, main-loop fingerprint, reviewer route and fingerprint, identity-evidence source,
and enforced read-only proof. An `approve` verdict creates the mode-0400
`plan_evidence_path` exactly once. `revise`, transport failure, and malformed evidence
clean the unapproved invocation and never leave an approvable manifest.

Max `worker --manifest` safely reopens that receipt and rehashes the exact task and
current source snapshot before any provider process launches. Final review uses the
same plan receipt and reviewer identity, includes the plan binding in its packet, and
integration revalidates the plan receipt again. Removing, replacing, weakening, or
swapping plan evidence blocks both final approval and integration.

## Final authority receipt

Public integration never accepts a caller-authored approval JSON file. An approved
final checkpoint creates one private, mode-bound, invocation-bound receipt containing:

- `authority_mode`, invocation ID, bundle SHA-256, and exact review-packet SHA-256
- the three evidence hashes and their recomputed approval-binding hash
- the main-loop fingerprint
- the reviewer route, canonical fingerprint, identity-evidence source, and effective
  read-only proof for Max, or the inline-main-loop marker for Lite
- the strict approve decision and an empty requested-change set

The receipt is created once, is not overwritten, and is revalidated against the
current sealed bundle before integration. `integrate` receives only the invocation
manifest; a missing, stale, wrong-mode, caller-supplied, or tampered receipt blocks the
transaction.

## External provider boundary

The sandboxed Claude-compatible worker exposes only `Read`, `Glob`, `Grep`, `Edit`,
and `Write` model tools. Bash/shell, Web, MCP, slash commands, and session persistence
are unavailable to the model, while trusted gate commands run separately under host
control with provider credentials removed. The provider client process must still
receive the selected route credential to call its API. Use short-lived, narrowly
scoped credentials: filesystem sandboxing and output redaction cannot prevent a
malicious or compromised provider binary from misusing a credential over its allowed
network connection.

## Revision loop

An authority `revise` verdict returns pointed deltas. Re-run implementation, all
gates, patch audit, main-loop review, and final authority review. At most two final
revision rounds are allowed. A third `revise` returns `review_revise`, includes the
accumulated evidence, and stops before integration.

Plan revision does not consume this final-review ceiling, but it must be approved
before dispatch.

## Integration guard

Approval authorizes only the exact three-hash tuple. Immediately before integration:

1. Recompute the destination snapshot, including private out-of-scope content hashes.
2. Return `destination_changed` if any destination fact differs, even when Git status
   codes are unchanged. Preserve the tree; before a later integration attempt, take a
   fresh snapshot, rebuild the patch audit, repeat main-loop review, and obtain a new
   authority final approval.
3. Recompute the approval tuple and return `approval_stale` if it is not the approved
   tuple.
4. Apply only the recorded worker delta without fuzzy application, reset, stash, or
   overwrite.
5. Verify that the resulting canonical task patch hash equals the approval.

Neither destination drift nor conflicts permit reuse of the old approval. Conflicts
preserve the user's tree. Cleanup is limited to the worktree, profiles,
evidence directory, and route state recorded in the invocation manifest.

## Structured statuses

`ok`, `needs_context`, `gate_failed`, `provider_unavailable`,
`reviewer_unavailable`, `timeout`, `scope_violation`, `transport_error`,
`review_revise`, `approval_stale`, `destination_changed`, and
`sandbox_unavailable` are the complete public status set.
