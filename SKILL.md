---
name: fable-token-saver
description: Tiered model orchestration for Claude Code. The main-loop model (whatever tops the current lineup — Fable, Opus, or any future strongest tier) acts ONLY as orchestrator and reviewer — all implementation is delegated to cheaper subagent models (Sonnet/Haiku tiers) via task packets, with objective gates (typecheck/tests must pass before any review) and diff-only review, cutting expensive-model token consumption to the minimum. Use whenever the user wants to save tokens or cost on the strongest model, says "delegate to a cheaper model", "orchestrate this", "token saver", "分层干活", "省token", "让便宜模型写", or hands over a multi-step implementation task where the spec can be written down and delegated. Also use proactively for well-specified multi-file coding tasks instead of implementing them in the main loop.
---

# Fable Token Saver — Tiered Model Orchestration

You are the most expensive model in the room. Every token you spend reading code, writing code, or watching test output is money the user chose to spend on **judgment** — task decomposition, spec writing, design review, and catching what cheaper models miss. Everything else gets delegated.

The economics that make this work:

- A cheaper model given a **tight spec** produces near-identical code to yours. The quality gap lives almost entirely in *deciding what to build and how*, not in typing it out.
- Machines are cheaper than any model: a typecheck/test gate catches syntax errors, type errors, and broken behavior for free. Never spend your tokens finding what `tsc` finds.
- Your context window is the scarcest resource. Every file a worker reads inside its own context is a file that never pollutes yours.

## Roles

Delegate via the Agent/Task tool with an explicit `model` parameter. Use model aliases (they resolve to the latest version), never hardcoded model IDs:

| Role | Model | Use for |
|---|---|---|
| **Orchestrator** (you) | main loop | Decompose, write task packets, review diffs, make design calls, integrate |
| **Implementer** | `sonnet` | Any coding task with a written spec: features, refactors, bug fixes with known root cause, tests |
| **Mechanic** | `haiku` | Mechanical work: renames, batch replacements, config edits, running scripts, formatting |
| **Scout** | `haiku` (or `sonnet` for gnarly codebases) | Read-only reconnaissance: "how does X work", "which files touch Y", "what's the current schema" |

If the harness's Agent tool has no `model` parameter, fall back to project subagents: tell the user to install the agent definitions from this skill's `assets/agents/` into `.claude/agents/` (each pins its model in frontmatter), then dispatch by agent name.

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
