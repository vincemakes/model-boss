# fable-token-saver

**English** | [简体中文](README.zh-CN.md)

**Tiered model orchestration for Claude Code.** Keep your most expensive model (Fable, Opus — whatever tops the lineup) in the orchestrator's chair — decomposing, spec-writing, and reviewing — while cheaper models (Sonnet/Haiku tiers) do the implementation. Objective gates (typecheck/tests) filter out everything a machine can catch before the expensive model spends a single token reviewing.

Version-agnostic by design: the skill routes by **model alias/tier** (strongest / mid / cheapest), never by hardcoded model IDs, so it keeps working across model generations.

## Why

When you chat with Claude Code, the model you pick powers the *main loop* — but every subagent it spawns can run on a different, cheaper model. The quality gap between tiers lives almost entirely in **judgment** (what to build, how to structure it, is this diff right), not in typing out well-specified code. So:

- **Orchestrator** (your main-loop model) writes task packets and reviews diffs.
- **Implementer** (Sonnet tier) writes the code, and must pass your typecheck/test gate before returning.
- **Mechanic** (Haiku tier) does renames, batch edits, config changes — cheaper again.
- **Scout** (Haiku tier) reads the codebase so the orchestrator's context stays clean.

## Install

```bash
git clone https://github.com/<you>/fable-token-saver ~/.claude/skills/fable-token-saver
```

That's it for harnesses whose Agent tool supports per-subagent model override (current Claude Code does). Optionally, install the pinned worker agents for automatic routing:

```bash
mkdir -p .claude/agents
cp ~/.claude/skills/fable-token-saver/assets/agents/*.md .claude/agents/
```

## Use

```
/fable-token-saver build the CSV export feature described in issue #42
```

Or just ask naturally — "delegate this to cheaper models and review the result". The skill also triggers proactively on well-specified multi-file tasks **above the delegation floor** (see Benchmarks — this matters).

## Core mechanics

1. **Classify** — mechanical → cheapest tier; specifiable → mid tier; design-heavy → the orchestrator does it personally. Small-volume tasks are never delegated at all.
2. **Task packets** — workers get GOAL / CONTEXT (≤5 lines) / ALLOWED FILES / SPEC / GATE / DO-NOT-fence. Never the conversation transcript.
3. **Objective gates** — the worker runs `tsc && test` in *its own* context and self-fixes until green (max 3 attempts) before returning. The orchestrator never reviews ungated code.
4. **Diff-only review** — the orchestrator reviews the diff for design and intent, not syntax or types. The gate owns those.
5. **Delta retries** — a revise verdict goes back to the *same* worker with only the delta, not a fresh worker with a full packet.

## Benchmarks

All numbers from real headless `claude -p` runs (main loop = Claude Fable 5, workers = Sonnet 5 / Haiku 4.5), measured via the CLI's per-model usage JSON. Every run passed both gates (`tsc --noEmit` + `vitest`) and all quality assertions — **with and without the skill, output quality was identical**. The differences are cost and time.

### Small tasks: the skill makes things WORSE (that's the honest finding)

Three small tasks (≤ ~150 changed lines, ≤ 5 files):

| Task | Fable output tokens (with skill) | Fable output tokens (without) | Δ Fable | Total cost with / without | Time with / without |
|---|---|---|---|---|---|
| Multi-file feature (hook + tests) | 4,277 | 2,757 | **+55%** | $1.25 / $0.78 | 121s / 53s |
| Repo-wide mechanical rename | 1,865 | 1,124 | **+66%** | $0.82 / $0.63 | 83s / 36s |
| API error-envelope redesign + apply | 6,415 | 4,798 | **+34%** | $1.85 / $1.08 | 235s / 84s |

Below the break-even point, writing the task packet and reviewing the diff costs more expensive-model tokens than just typing the change. Quality assertions: 30/30 passed in both conditions — no quality gain to offset the cost.

**The delegation floor (now baked into the skill):** delegate only when ≥ ~300 new/changed lines, or ≥ ~6 files, or repetitive edits across many call sites. The skill refuses to orchestrate below this line and does the work directly in the main loop.

### Large tasks: the skill pays off

One large task — a greenfield 8-module shopping-cart subsystem (~1,100 lines of code + tests written from scratch):

| Metric | With skill | Without skill | Δ |
|---|---|---|---|
| Fable output tokens | 17,899 | 30,993 | **−42%** |
| Fable cost | $2.03 | $3.05 | **−34%** |
| Total cost (all tiers) | $2.91 | $3.05 | −5% |
| Wall-clock time | 465s | 335s | +39% |
| Tests produced (all passing) | 91 | 81 | +12% |

Sonnet 5 carried 25k output tokens of implementation while Fable spent its tokens on decomposition and review only. Quality assertions: 6/6 in both conditions — the with-skill run actually produced *more* test coverage.

**Read the two tables together and the story is simple:** below the delegation floor the skill taxes you; above it, it cuts your strongest-tier consumption by roughly a third to a half, at the price of slower wall-clock.

### What are you actually saving? (read this if −5% looks underwhelming)

There are two different wallets, and the skill serves one of them much better:

- **Pay-per-token API users** — total dollars is your metric, and at the crossover scale the saving is marginal (−5%): the implementation work still has to be done by *someone*, and Sonnet's share isn't free. The dollar gap widens with task size, because the orchestrator's packet+review overhead is roughly fixed while the delegated volume grows. If this is you, only reach for the skill on genuinely large tasks.
- **Subscription users (Claude Max and similar)** — your real constraint is the strongest model's **rate-limit quota**, not dollars. Cheaper tiers burn quota far slower, and the quota window resets regardless of what you spent it on. **−42% strongest-tier consumption means roughly 1.7× more orchestrated work per quota window.** This is the skill's primary audience: it is a quota-arbitrage tool first, a cost tool second.

### Methodology

- Each condition runs as an independent headless `claude -p` session in a fresh copy of the same fixture repo (real `pnpm` project, git-initialized).
- With-skill prompt invokes the skill; baseline prompt forbids skills. Same task text otherwise.
- Costs/tokens come from the CLI's `--output-format json` per-model usage breakdown — no estimation.
- Quality graded by identical objective assertions (gates re-run, greps, diff-scope checks) in both conditions.

## When not to use it

One-liners, pure analysis, design-heavy cores, and anything under the delegation floor above. The skill says so itself and steps aside — see the numbers for why.

## License

MIT
