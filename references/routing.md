# Token Saver routing

Routing is capability-based. Provider profiles supply convenient aliases; the core
does not branch on brand or model name.

## Main loop is input

The host-selected conversation model is immutable input. Neither profile, user,
project, nor run configuration may contain `main_loop`. Token Saver only resolves the
mode and spawned reviewer, worker, scout, or mechanic routes around that inherited
main loop.

Trust exact user-supplied identity facts first, then structured host metadata. Generic
identity prose is insufficient. If the canonical identity remains ambiguous, return
`needs_context` instead of guessing.

## Configuration precedence

Resolution order is:

1. built-in capability profile
2. user config
3. project config
4. per-run override

A higher route definition replaces the whole lower definition. Preference lists are
replaced independently. Provenance accompanies each selected value. Credentials are
environment-variable names mapped only at process launch; values never enter config,
hashes, packets, logs, or startup verdicts.

## Capability bands and roles

- **authority** may serve as a reviewer and may run Lite inline.
- **balanced** may coordinate Max or implement.
- **fast** may implement, perform mechanical work, or scout.

These are route declarations, not proof. Preflight separately establishes
reachability, identity, effective read-only reviewer enforcement, executable/native
agent availability, credentials by name, and verified write sandbox identity.

## Canonical fingerprints

Compare the normalized tuple
`provider_family:resolved_model_id:variant`. Route names, wrapper names, endpoints,
accounts, or aliases do not establish identity or independence. Two aliases resolving
to the same tuple collide. A Max reviewer with a hidden, ambiguous, or main-loop-equal
fingerprint is ineligible.

Accepted evidence sources are structured host metadata, a pinned adapter plus live
verification, a provider response, or an explicit identity handshake. Evidence must
describe the actual child invocation, not merely a config default.

## Auto-resolution matrix

| Main-loop facts | Reviewer facts | Result |
|---|---|---|
| authority fingerprint known | not required | Lite with inline authority |
| balanced fingerprint known | reachable, distinct authority and read-only | Max |
| balanced fingerprint known | unavailable/hidden/colliding/write-capable | `needs_context` in auto; `reviewer_unavailable` in explicit Max |
| main identity unresolved | any | `needs_context` |

An explicit mode wins. Explicit Lite does not invoke an external authority reviewer.
Explicit Max never degrades. Max may use the main loop itself for implementation when
no worker is selected, but it still requires the distinct authority reviewer.

## Custom-route eligibility

External commands must be non-empty argument arrays. Reviewer declarations require
read-only intent plus a hardened, live reviewer transport and exact resolved identity.
Worker declarations require a verified OS sandbox bound to the command, worktree,
route-state directory, and sandbox profile. A failed worker sandbox removes that
worker only; it never weakens reviewer rules or changes the selected mode.

## Startup verdict

Successful resolution prints exactly:

```text
Main loop: <route/model>
Resolved mode: <Lite|Max>
Authority: <inline main loop|reviewer route>
Worker: <route|main loop|none>
Resolution source: <explicit|project|user|profile>
```

Blocked resolutions return structured status and non-secret missing facts instead of
a successful verdict.
