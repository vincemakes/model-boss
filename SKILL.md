---
name: fable-token-saver
description: Tiered model orchestration for Claude Code. The main-loop model (whatever is strongest — Fable, Opus, or future tiers) acts ONLY as orchestrator and reviewer; cheaper subagent models (Sonnet/Haiku) do all implementation via task packets, objective gates (typecheck/tests must pass before any review) and diff-only review — minimizing strongest-tier token/quota burn. Use when the user wants to save tokens/quota on the strongest model or asks you to orchestrate instead of coding yourself — "delegate to a cheaper model", "farm this out to worker agents", "you plan and review, another model writes", "token saver", "分层干活", "省token", "让便宜模型写", "你统筹", "派sonnet干活". Also covers external-provider CLI workers — dispatching implementation to GLM/Kimi through `claude-glm`/`claude-kimi` wrappers while the main loop reviews, and one-click setup of those wrappers — "用kimi开发你审核", "让glm写代码", "派kimi干活", "接入glm/kimi", "配置模型alias", "use kimi as the worker", "set up claude-glm". Also use proactively for sizable implementation work (roughly 300+ lines or 6+ files — refactors, migrations, greenfield subsystems). Do NOT use for model pricing/choice questions, plain review of an existing diff, debugging a specific error, or small edits below the delegation floor.
---

# Fable Token Saver — Tiered Model Orchestration

## Modes: detect, don't configure

The skill has two modes, selected by which model powers the main loop — the user picks the mode by picking the session model (`/model`), and you adapt. Detecting which model that is has a procedure (below); run it before anything else, because your own idea of what model you are can be wrong. Mode names describe **saving intensity** (lite = saves less, max = saves most), not model strength:

**lite mode — the strongest tier IS the main loop (e.g. Fable).** You are both orchestrator and final authority. Follow the full workflow below. Measured effect: −42% strongest-tier consumption on large tasks.

**max mode — a mid tier is the main loop (e.g. Opus, Sonnet).** You orchestrate exactly the same way, but the strongest tier becomes a **Consultant subagent** you invoke at two mandatory checkpoints:

1. **Plan checkpoint** (before dispatching any packet): send a ≤15-line brief — goal, constraints, proposed decomposition, interface sketches. The consultant returns design verdicts and acceptance criteria; fold them into your packets.
2. **Pre-merge checkpoint** (after your own diff review): send the diff stat, your verdict, and open concerns. The consultant returns approve/revise with pointed deltas.

Division of labor at the plan checkpoint: **you draft** the decomposition and interface sketches (drafting is long-form and belongs on the cheap tier), the consultant only **rules** on them — keep/change verdicts, acceptance criteria, risks. Never ask the strongest tier to author what a mid tier can draft and it can judge.

Prefer an **Opus-class** main loop for max mode. In benchmarks, an Opus main loop used the consultant with discipline (two clean checkpoints, −89% strongest-tier burn), while a Sonnet-class main loop leaned on the consultant 3.7× harder and eroded most of the savings (−61%). If you ARE a Sonnet-class main loop: the two checkpoints are the ONLY consultant calls you get — batch your questions into them, never open ad-hoc consultations.

The consultant never implements and never sees the raw conversation — briefs only. If the strongest tier isn't available on this account, use the best tier above your own as consultant, or proceed as final authority if none exists. Skip the consultant only when the user explicitly opts out ("no fable", strongest-tier quota exhausted); then you are the final authority. Why this shape: the main loop pays strongest-tier rates for every tool result and every turn of bookkeeping — moving the main loop down a tier and buying strongest-tier judgment only at the two moments it actually differs (~3-5k tokens) cuts strongest-tier burn by an estimated 80-85%.

### Detect the mode before anything else

You cannot introspect your own weights, and the model-identity boilerplate in your system prompt ("You are powered by …", "This iteration of Claude is …") can be a static template that does not track the user's `/model` choice. Rank your sources:

1. **The user's explicit statement** ("this session is on Opus") — always wins. If it contradicts your identity string, the user is right; switch modes immediately and continue.
2. **Harness metadata naming the exact model ID** for this session — trust unless the user contradicts it.
3. **Identity boilerplate prose** — weakest signal; never sufficient on its own to pick lite mode.

The two misdetections are not symmetric. Wrongly picking **max** costs one wasted consultant call and exposes itself instantly — you would be consulting yourself. Wrongly picking **lite** silently deletes the strongest tier from the session: every design call gets made by a mid tier that believes it is the final authority, and nothing ever surfaces the loss. So when the main-loop model is uncertain, ask the user or default to **max** — never default to lite on the strength of the identity string alone.

Two mandatory guardrails:

- **Announce the verdict** in your first line of orchestration — "Main loop detected as `<model>` → `<mode>` mode" — so a wrong guess is visible and correctable while it is still cheap.
- **Contradiction check**: if the user's own words presume a consultant above you ("have Fable review it", "走 fable") while you have detected yourself as that tier, treat it as a detection failure, not a user error — confirm the session model before proceeding in lite. Explicit invocation of this skill is *not* by itself such a contradiction — lite is a legitimate strongest-tier mode — but it does raise the stakes of a silent misdetection, and defaulting to max when you truly are the strongest tier fails silently too (you would be consulting yourself, which produces no error). So under explicit invocation, resolve any remaining model uncertainty by **asking**, not by defaulting in either direction.

## The economics

You (the orchestrator) are the most expensive model in your session. Every token you spend reading code, writing code, or watching test output is money the user chose to spend on **judgment** — task decomposition, spec writing, design review, and catching what cheaper models miss. Everything else gets delegated.

The economics that make this work:

- A cheaper model given a **tight spec** produces near-identical code to yours. The quality gap lives almost entirely in *deciding what to build and how*, not in typing it out.
- Machines are cheaper than any model: a typecheck/test gate catches syntax errors, type errors, and broken behavior for free. Never spend your tokens finding what `tsc` finds.
- Your context window is the scarcest resource. Every file a worker reads inside its own context is a file that never pollutes yours.

## Roles

Delegate via the Agent/Task tool with an explicit `model` parameter. Use model aliases (they resolve to the latest version), never hardcoded model IDs:

| Role | Model | Use for |
|---|---|---|
| **Orchestrator** (you) | main loop | Decompose, write task packets, review diffs, make design calls, integrate |
| **Consultant** (max mode only) | `fable` | Plan-checkpoint design verdicts and pre-merge final review — briefs in, verdicts out, never implements |
| **Implementer** | `sonnet` | Any coding task with a written spec: features, refactors, bug fixes with known root cause, tests |
| **Mechanic** | `haiku` | Mechanical work: renames, batch replacements, config edits, running scripts, formatting |
| **Scout** | `haiku` (or `sonnet` for gnarly codebases) | Read-only reconnaissance: "how does X work", "which files touch Y", "what's the current schema" |
| **External Implementer** | `claude-glm-bypass` / `claude-kimi-bypass` (CLI, via Bash) | Same tier as Implementer — use when the user names an external provider or in-account cheap tiers are quota-exhausted |

If the harness's Agent tool has no `model` parameter, fall back to project subagents: tell the user to install the agent definitions from this skill's `assets/agents/` into `.claude/agents/` (each pins its model in frontmatter), then dispatch by agent name.

### External CLI workers (GLM / Kimi)

Implementation can also be farmed out to entirely different providers through Claude Code CLI wrappers — the orchestrator (whatever runs the main loop: Fable in lite mode, Opus/Sonnet in max mode) keeps judgment, an external model burns its own (cheaper) quota typing the code. This is orthogonal to mode: in max mode you still hit the two consultant checkpoints; only the Implementer seat changes. One-time setup installs `claude-kimi`, `claude-glm`, `claude-glm-turbo` and their `-bypass` variants; keys live in `~/.claude/fable-token-saver/providers.env` (chmod 600), never in a repo or shell rc:

```
bash scripts/setup-model-providers.sh
```

Use an external worker when the user names one ("用kimi写，你审核", "dispatch this to glm") or when in-account cheap tiers are unavailable. It slots in at the **Implementer** tier: same task packet, same gate, same diff-only review. Dispatch from the repo root via Bash — always the `-bypass` variant, because a headless worker cannot answer permission prompts:

```
claude-kimi-bypass -p "<task packet verbatim>"
```

Give the Bash call a generous timeout (10 min) or run it in the background for fan-out. Everything else about the Loop is unchanged, but external workers run in **your working tree** with permissions skipped, which adds four rules:

- **Dispatch only from a clean tree** (commit or stash first), so `git diff` afterward is exactly the worker's output — that diff is what you review.
- **No overlapping parallel dispatch.** Two workers sharing one tree conflict; serialize, or give each its own `git worktree`.
- **Scope fences are the only fence.** `--dangerously-skip-permissions` removes the harness guardrail, so ALLOWED FILES / DO NOT matter more — after each return, check `git status` for out-of-scope files.
- **The result packet arrives on stdout.** Courier only, as usual: the tree's diff is the ground truth.

The non-bypass variants (`claude-kimi`, `claude-glm`) are for the human's own interactive sessions, not for dispatch.

## The Loop

Every delegated unit of work goes through five stages. Do not skip stages; do not merge them.

### 1. Classify

Before touching anything, sort the work:

- **Mechanical** (unambiguous, no judgment) → Mechanic
- **Specifiable** (you can write acceptance criteria a stranger could satisfy) → Implementer
- Either way, first check the task clears the delegation floor in "When NOT to use" — small volume means you do it yourself regardless of type.
- **Design-heavy** (cross-module tradeoffs, novel architecture, tricky invariants, security-sensitive logic) → **do it yourself**. Delegating these produces plausible-looking code you'll spend more tokens un-reviewing than you saved.

When one user request contains all three kinds, split it: do the design-heavy core yourself first (interfaces, types, key decisions), then fan out the specifiable remainder.

### 2. Recon (only if needed)

If you don't know the codebase area well enough to write a spec, send a Scout — never read broadly yourself. Scout prompts must demand **conclusions, not contents**: "Return: the list of files that must change, the current type signatures involved, and any existing helper we should reuse. Do not paste file bodies."

Skip recon entirely when you already know the target files. Recon you don't need is the most common token leak.

### 3. Dispatch — the Task Packet

Workers get a task packet, never the conversation. A packet contains exactly:

```
GOAL: <one sentence>
CONTEXT: <2-5 lines of what the worker cannot infer from the code — decisions
  already made, constraints, gotchas. Not history, not rationale essays.>
ALLOWED FILES: <explicit list; "you may create new files under src/x/">
SPEC:
  - <acceptance criterion 1 — testable, concrete>
  - <acceptance criterion 2>
GATE: <exact command(s) that must pass, e.g. `pnpm tsc --noEmit && pnpm test`>
RETURN FORMAT:
  - files changed (list)
  - summary of approach (≤10 lines)
  - full gate output (last run)
  - open questions, or NEEDS_CONTEXT: <specific questions> if blocked
DO NOT: <scope fence — "do not touch the schema", "do not add dependencies",
  "do not refactor adjacent code">
```

Rules that keep packets cheap and workers effective:

- **Spec quality is the whole game.** A vague packet means a retry loop, and retry loops on your review time cost more than writing the spec carefully once. If you can't write concrete acceptance criteria, the task is design-heavy — reclassify.
- The worker runs the GATE itself and self-fixes until green, **up to 3 attempts**, before returning. It returns early only with `NEEDS_CONTEXT` + specific questions.
- Scope fences (`DO NOT`) prevent the classic cheap-model failure: helpful drive-by refactoring that balloons the diff you must review.

### 4. Gate

The gate runs in the worker's context, not yours. When the result packet comes back:

- Gate output missing or red → bounce it back ("return only after the gate passes, or explain precisely why it can't"), don't start reviewing. Reviewing ungated code wastes your tokens on what the machine finds for free.
- Worker failed the gate 3 times → escalate one tier (Mechanic→Implementer, Implementer→you) with the failure history attached. Don't loop a worker that's stuck; its context is already muddy.
- Gate output reached you through any channel other than the worker's own result packet (relayed, truncated, second-hand) → one local gate re-run is permitted, as authentication of the claim, not re-verification of the code.

### 5. Review — diff only, design only

Review the **diff**, not the files. Run `git diff --stat` first — scoped to source dirs (e.g. `git diff --stat -- src tests`) when the repo tracks vendored artifacts that would drown the signal. Read the full diff only for files whose change you can't judge from the stat + worker summary.

The diff is the ground truth; the result packet is a courier, not an authority. Once your own diff review supports a verdict, issue it — never block on the worker restating information you can already verify from the working tree. (If the two disagree, trust the tree.)

You are reviewing for exactly the things the gate cannot check:

- Does the approach match the spec's *intent*, not just its letter?
- Wrong layer / wrong abstraction / missed existing helper?
- Edge cases the spec implied but didn't enumerate?
- Anything outside the scope fence?

You are **not** reviewing for: syntax, types, whether tests pass, formatting. The gate owns those. If you catch yourself mentally re-typechecking code, stop.

Verdicts:

- **Accept** → integrate, move on. Say what shipped in one line.
- **Revise** → send the *same worker* a delta packet: only what's wrong and what right looks like. Same worker + delta beats fresh worker + full packet — its context already has the code.
- **Reject** (wrong approach entirely) → fresh worker, rewritten packet with the failure mode called out ("do NOT solve this with X; use Y because Z"). Two rejects on the same task means your spec is the problem or the task is design-heavy — take it over.

## Parallelism

Independent packets dispatch **in the same message** so workers run concurrently. Before fanning out, check file overlap: two workers writing the same file will conflict. Overlapping tasks either merge into one packet or serialize.

Dispatch mode matters: with a **single packet in flight** and no other orchestrator work queued, run the worker in the **foreground (blocking)** so its result packet returns synchronously as the tool result. Background dispatch is for genuine fan-out — a lone background worker just strands you waiting on a completion notification that may never route back cleanly.

While parallel workers run, do orchestrator work: write the next packets, review earlier returns. Never idle-wait, and never do a worker's task yourself out of impatience.

## Token Discipline (orchestrator commandments)

1. **Never read a file a worker could read for you.** You read: diffs, result packets, and the handful of files where you're personally making design decisions.
2. **Never write implementation code in the main loop** unless the task is classified design-heavy. "It's just a small edit" is how orchestration dies — small edits go to the Mechanic.
3. **Never paste conversation history into packets.** If the worker needs background, distill it into ≤5 CONTEXT lines. Distillation is your job; that's what the user pays you for.
4. **Never re-verify the gate.** Green gate output in the result packet is trusted. Spot-check only when the diff makes you suspicious the worker gamed the gate (e.g. tests deleted or skipped — check the diff stat for test files).
5. **Batch your reviews.** Reading three result packets in one turn beats three separate review turns.
6. **Keep your final report short.** The user wants: what shipped, what the gates proved, what needs their eyes. Not a play-by-play of the orchestration.

## When NOT to use this skill

The overhead floor is measured, not guessed: on small tasks (≤ ~150 new/changed lines across ≤ 5 files), benchmarks showed orchestration made strongest-tier token spend **34-66% worse** and doubled wall-clock time — writing the packet plus reviewing the diff cost more than just typing the change. Quality was identical either way; only the bill differed.

So delegate only when expected implementation volume clearly dwarfs the packet+review overhead. Working heuristic — delegate when at least one holds:

- ≥ ~300 new/changed lines expected, or
- ≥ ~6 files touched, or
- repetitive edits across many call sites (the per-site cost is near zero for a worker but linear for you).

Below that line, do it yourself in the main loop. Also skip orchestration when the change is a genuine one-liner, the task is pure conversation/analysis, or you're mid-flight in a design-heavy task where the spec would be longer than the code.

## Anti-patterns (each has burned someone)

- **Shadowing**: doing the work yourself "to compare" with the worker's output. You just paid twice.
- **Context dumping**: forwarding your whole conversation as packet CONTEXT. Costs input tokens on every worker and makes them worse (buried instructions).
- **Review creep**: reading every changed file top-to-bottom. That's re-implementation, priced at strongest-tier rates.
- **Tier vanity**: sending everything to `sonnet` "to be safe". Mechanical work on the Mechanic tier is 3-5× cheaper and just as reliable — the gate is the safety net, not the model tier.
- **Gate theater**: accepting "tests should pass" instead of pasted gate output. No output, no review.
