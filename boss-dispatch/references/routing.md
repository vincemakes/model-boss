# Model Boss routing

Routing is capability-based. Provider profiles supply convenient aliases; the core
does not branch on brand or model name.

## Main loop is input

The host-selected conversation model is immutable input. Neither profile, user,
project, nor run configuration may contain `main_loop`. Model Boss only resolves the
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

The published configuration surfaces are
[`config/model-boss.example.json`](../config/model-boss.example.json) and
[`config/model-boss.schema.json`](../config/model-boss.schema.json). Project discovery
uses `.model-boss.json`; user discovery uses the `model-boss/config.json` location
under the selected absolute configuration root.

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

`effort` is not part of the tuple. It pins how hard a spawned call thinks
(`low`, `medium`, `high`, `xhigh`, `max`) and is validated against the model catalog
(Opus 4.6 and Sonnet 4.6 have no `xhigh`; Haiku 4.5 accepts no effort). One model at
two efforts is one identity: a Fable 5.1 main loop at `low` cannot use a Fable 5.1
reviewer at `max` for Max.

## Model catalog and spoken names

`runtime/model_boss/catalog.py` is a dated snapshot of the models the host picker
exposes, with exact IDs, accepted effort levels, per-token prices, and the minimum
cacheable prefix. The default Anthropic profile pins one reviewer route and one
write-capable worker route per authority model (`opus-5` and `opus-5-worker`), plus
balanced and fast routes. Aliases come from the catalog and from each route's
`aliases` list; `match-models` finds them in a request with longest-match-wins, so
"opus 4.6" selects `opus-4.6`/`opus-4.6-worker` while a bare "opus" selects the newest
Opus. A family name followed by an unknown version ("fable 6") is reported as unmatched
and resolution returns `needs_context` rather than guessing.

## Worker selection

Among eligible worker routes, the first whose resolved model differs from the main
loop wins. A same-model worker stays eligible (it isolates context) but is chosen only
when nothing distinct is available, and the fact is recorded either way. Reviewer
eligibility is unchanged: the reviewer must be a distinct canonical fingerprint.

## Quota pace and the dispatch estimate

A Claude Max subscription meters one shared weekly total and caps the strongest model's
share of it (Fable may use at most half). The default `pace` objective therefore does
not minimise spend: it picks the strongest topology that keeps the total and the capped
half on pace, given the three percentages the user reads off the usage view
(`--total-used`, `--fable-used`, `--week-elapsed`). Regimes are `on_pace`,
`fable_behind`, `fable_ahead`, `fable_exhausted`, and `total_exhausted`. Lite with an
Opus worker measured a 48% capped-model share on the recorded task, so it is the steady
state that depletes both halves together; judgment work inline pushes the share up, and
Max with an Opus main loop pulls it back down. The policy steps aside for judgment-dense
work, single-packet changes under roughly 200 lines, and an exhausted half.

Each route also carries `quota_weight` (default `1.0`) for the cost objectives:
`weighted` minimises the quota-weighted price proxy across every role and `main-model`
the spend billed to the main loop's own model, each requiring a 10% saving before a
hand-off. The coefficients are calibrated on the recorded runs in `BENCHMARKS.md` and
`benchmarks/fable-effort-dispatch-rerun.md`; the latter measured Fable 5.1 at low and
medium effort inline and with Opus 5 and Sonnet 5 workers, and is why `estimate` charges
delegated volume and prices subagent cache writes at the five-minute rate.

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
Main loop: <route/model[@effort]>
Resolved mode: <Lite|Max>
Authority: <inline main loop|route/model[@effort]>
Worker: <route/model[@effort]|main loop|none>
Resolution source: <explicit|project|user|profile>
```

Blocked resolutions return structured status and non-secret missing facts instead of
a successful verdict.
