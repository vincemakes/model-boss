---
name: token-saver
description: >-
  Use when users ask to reduce high-tier model tokens or quota, invoke Token Saver Lite or Max, delegate implementation while retaining planning or review authority, configure Claude Code or Codex worker/reviewer routes, dispatch Kimi or GLM, or migrate from fable-token-saver; trigger phrases include token saver, save tokens, 省token, 分层干活, 用kimi开发你审核, and 让便宜模型写.
---

# Token Saver

Token Saver is a model-independent orchestration protocol. The model already selected
for the conversation is the main loop; keep it unchanged. Routes describe spawned
roles only. Lite and Max name where authority lives, not a provider, price, or quality
claim.

## 1. Resolve the inherited main loop and routes

Treat the main loop as immutable, host-owned input. Resolve its canonical fingerprint
from explicit user facts or host metadata; never infer identity from a wrapper name,
route name, endpoint, account, or model-family prose. A canonical fingerprint is
`provider_family:resolved_model_id:variant`.

Load routes in profile → user → project → per-run order. Configuration may select a
mode or spawned roles, but it must never select or replace the main loop. Run preflight
before promising a topology:

- A reviewer needs an authority-capable, reachable fingerprint distinct from the main
  loop and effective read-only enforcement.
- A native child declaration is a default, not proof of its effective identity or
  permissions after parent overrides.
- A write-capable external route needs a verified OS sandbox bound to that exact
  invocation. Otherwise it is not eligible.
- Missing or ambiguous main-loop identity returns `needs_context`. An explicit Max
  request with no eligible reviewer returns `reviewer_unavailable`; it never silently
  becomes Lite and never dispatches first.

See [routing rules](references/routing.md) for precedence, bands, fingerprints, and the
auto-resolution matrix.

## 2. Announce the Lite or Max topology

Before reconnaissance or implementation, print this non-secret verdict:

```text
Main loop: <route/model>
Resolved mode: <Lite|Max>
Authority: <inline main loop|reviewer route>
Worker: <route|main loop|none>
Resolution source: <explicit|project|user|profile>
```

Lite keeps planning and final review authority inline in the selected main loop. A
separate worker is optional.

Max keeps coordination in the selected main loop and places both authority
checkpoints in one eligible reviewer with a distinct canonical fingerprint. A cheaper
worker is optional: Max may be two levels (main loop + reviewer) or three levels (main
loop + reviewer + worker).

## 3. Eligibility and classification gate

Use Token Saver only when orchestration overhead is justified: a bounded task with
testable acceptance criteria, material implementation volume, or repeated mechanical
work. Step aside for a tiny edit, pure conversation, an unresolved bug whose cause
still needs diagnosis, or a design/security decision that cannot yet be expressed as
acceptance criteria. Stepping aside leaves the inherited main loop in charge; it does
not invent a different route.

Classify eligible work as implementation, mechanical editing, or read-only
reconnaissance. Split independent, non-overlapping packets. If the codebase facts are
insufficient, run read-only recon and request conclusions with file references, not
file dumps.

## Unified state machine

RESOLVE -> PREFLIGHT -> CLASSIFY -> RECON -> DRAFT_PLAN -> AUTHORITY_PLAN_CHECK -> DISPATCH -> GATE -> PATCH_AUDIT -> MAIN_LOOP_REVIEW -> AUTHORITY_FINAL_CHECK -> INTEGRATE

Never skip a state because of time pressure, sunk cost, a worker self-report, or a
human request to reuse stale approval.

- **RESOLVE → PREFLIGHT:** require the host-owned main-loop fingerprint, mode
  provenance, and candidate route declarations. Ambiguity stops with
  `needs_context`.
- **PREFLIGHT → CLASSIFY:** require live reachability, exact reviewer identity,
  effective read-only enforcement, and any external worker's current sandbox proof.
  A missing worker returns `provider_unavailable`; an invalid explicit Max reviewer
  returns `reviewer_unavailable`; a missing sandbox returns `sandbox_unavailable`.
- **CLASSIFY → RECON:** require a bounded, specifiable task above the delegation
  floor. Otherwise step aside without dispatch. RECON may be a recorded no-op when
  facts are already sufficient.
- **RECON → DRAFT_PLAN:** require concise conclusions, target paths, relevant
  interfaces, and unresolved questions. Missing facts return `needs_context`.
- **DRAFT_PLAN → AUTHORITY_PLAN_CHECK:** require goal, constraints, decomposition,
  acceptance criteria, scope fence, gates, and risks. No worker runs yet.
- **AUTHORITY_PLAN_CHECK → DISPATCH:** require an `approve` verdict bound to the plan.
  `revise` returns to DRAFT_PLAN. Unavailability, timeout, or malformed evidence
  returns `reviewer_unavailable`, `timeout`, or `transport_error` and blocks dispatch.
- **DISPATCH → GATE:** dispatch only the task packet into an eligible route. A launch
  failure returns `provider_unavailable`, `transport_error`, or `timeout`.
- **GATE → PATCH_AUDIT:** require every declared command, exit code, and bounded output
  hash. A red or missing gate returns `gate_failed`; workers get at most three
  self-fix attempts.
- **PATCH_AUDIT → MAIN_LOOP_REVIEW:** require the complete canonical task patch:
  staged, unstaged, untracked, binary, symlink, mode, and scope records. Any write
  outside allowed paths or any source-repository mutation returns `scope_violation`.
- **MAIN_LOOP_REVIEW → AUTHORITY_FINAL_CHECK:** require the main loop's intent,
  abstraction, edge-case, and scope verdict plus all gate and patch evidence. A main
  loop revision returns to implementation and never self-approves.
- **AUTHORITY_FINAL_CHECK → INTEGRATE:** require `approve` bound to the current
  three-hash tuple. Changed evidence returns `approval_stale`. Final `revise` repeats
  implementation, GATE, PATCH_AUDIT, MAIN_LOOP_REVIEW, and this same checkpoint for
  exactly two revision rounds. A third `revise` returns `review_revise` with the
  accumulated evidence; `review_revise` never reaches INTEGRATE.
- **INTEGRATE:** immediately recompute evidence. Destination drift stops INTEGRATE,
  returns `destination_changed`, and preserves the tree without stash, reset,
  overwrite, or fuzzy application. Before any later integration attempt, capture a
  fresh source/destination snapshot, rebuild the current evidence at PATCH_AUDIT, and
  repeat MAIN_LOOP_REVIEW plus AUTHORITY_FINAL_CHECK; never reuse the old approval.
  A `destination_changed` response must report that full recovery sequence—not merely
  “stop” or “return to review”—so the next authority decision cannot be mistaken for
  optional.
  Otherwise apply only the approved worker delta without forcing a conflict, then
  verify the resulting canonical patch hash. Success returns `ok`.

Lite binds AUTHORITY_PLAN_CHECK to the main loop and AUTHORITY_FINAL_CHECK to the main
loop, inline. Max binds both checkpoints to the same eligible reviewer with a distinct
canonical fingerprint. The main loop still drafts, coordinates, performs its own
review, and integrates; the reviewer never implements.

## 5. Task packet

Give a worker the packet, not the conversation:

```text
GOAL: <one sentence>
CONTEXT: <only decisions and facts the repository cannot reveal>
ALLOWED PATHS: <exact path list or narrow creation prefix>
SPEC:
  - <testable acceptance criterion>
GATES:
  - <direct argument array, cwd, timeout>
RETURN:
  - files changed
  - concise approach
  - exact gate results
  - questions or NEEDS_CONTEXT
DO NOT: <scope and dependency fences>
```

Do not pass secrets, shell-expanded command strings, raw conversation history, or
authority to merge. Preserve the same packet and source snapshot across a worker's
three gate attempts; a changed task needs a new snapshot and packet.

## 6. Gate and canonical evidence requirements

Worker claims are courier data. Authority decisions use independently captured Git
and process evidence. The approval binding contains exactly:

```text
source_snapshot_hash
worker_delta_hash
projected_task_patch_hash
```

The source snapshot includes the baseline commit, every in-scope staged/unstaged/
untracked record, and private hashes for out-of-scope dirty content. The worker delta
is captured only from a disposable worktree. The projected patch includes all
approved source changes plus the worker delta. Immediately before integration, a
current approval for this three-hash tuple is mandatory. A status code alone cannot
prove that out-of-scope contents are unchanged.

## 7. Lite inline-authority verdict

In Lite, the main loop performs both authority checkpoints inline. It must still
produce explicit plan and final verdicts, consume complete evidence, honor all retry
ceilings, and reject stale approval. A worker can implement and run gates but cannot
approve its own patch.

## 8. Max authority checkpoints

In Max, the distinct reviewer receives evidence packets only. Plan review covers the
draft plan, acceptance criteria, risks, and scope before dispatch. Final review covers
the approved plan, main-loop verdict, complete canonical patch, gate evidence, private
scope manifest, and all three hashes. The reviewer returns structured `approve`,
`revise`, or `needs_context` and echoes the binding hash; it never sees credentials,
edits files, or supplies implementation.

The same canonical reviewer must perform plan and final checkpoints. Identity or
effective permission changes invalidate preflight and return `reviewer_unavailable`.

## 9. Failure states and step-aside rules

The only structured run statuses are:

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

Failures contain a concise non-secret reason and the last safe state. They never dump
environment variables, authorization data, unrelated file contents, or hidden model
prompts. Preserve user changes and clean up only invocation-owned resources.

## 10. References and adapters

- [Full protocol and evidence contract](references/protocol.md)
- [Routing and capability resolution](references/routing.md)
- [Claude Code adapter](references/adapters/claude-code.md)
- [Codex adapter](references/adapters/codex.md)
- [External CLI safety contract](references/adapters/external-cli.md)
