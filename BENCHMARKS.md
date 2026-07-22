# Model Boss benchmarks

**English** | [简体中文](BENCHMARKS.zh-CN.md)

> Historical reference-stack notice: these runs measure the 2026 Claude/Fable/Opus stack. They do not predict savings for Sol, Kimi, or future model profiles. `-42%` and `-89%` are recorded strongest-model output-token changes; `-34%` and `-88%` are price-weighted strongest-model quota changes used only as a quota proxy. The blind bug-hunt is one observed probe, not general proof of capability parity.

> Publication provenance: the predecessor identity was normalized to neutral labels, and its captured absolute workspace prefix was normalized to `<historical-workspace>`. Recorded tasks, gate output, outcomes, and token/cost/time measurements were not changed; the placeholder does not claim that these runs occurred under a Model Boss directory.

These are predecessor measurements inherited by Model Boss; Model Boss did not run this corpus. All numbers come from real headless `claude -p` runs of that predecessor, measured via the CLI's per-model usage JSON — no estimation. Every run passed both gates (`tsc --noEmit` + `vitest`) and all quality assertions; **output quality was identical across conditions**. The differences are cost, quota, and time.

## Methodology

- Each task/configuration pair in the inherited four-eval corpus ran once as an independent headless `claude -p` session in a fresh copy of the same fixture repo (real `pnpm` project, git-initialized).
- The summary's ± values report sample dispersion across four distinct tasks, not repeated-trial variance or statistical confidence.
- Predecessor-skill prompts invoke the measured predecessor; baseline prompts forbid orchestration skills. Same task text otherwise.
- Costs/tokens come from `--output-format json`'s per-model usage breakdown.
- Quality graded by identical objective assertions (gates re-run, greps, diff-scope checks) in all conditions.
- Models: Fable 5 ($10/$50 per MTok), Opus 4.8 ($5/$25), Sonnet 5 ($3/$15), Haiku 4.5 ($1/$5).

## Predecessor result: small tasks were WORSE

Three small tasks (≤ ~150 changed lines, ≤ 5 files):

| Task | Fable output (with predecessor) | Fable output (baseline) | Δ Fable | Total cost with / baseline | Time with / baseline |
|---|---|---|---|---|---|
| Multi-file feature (module + tests) | 4,277 | 2,757 | **+55%** | $1.25 / $0.78 | 121s / 53s |
| Repo-wide mechanical rename | 1,865 | 1,124 | **+66%** | $0.82 / $0.63 | 83s / 36s |
| API error-envelope redesign + apply | 6,415 | 4,798 | **+34%** | $1.85 / $1.08 | 235s / 84s |

Below the break-even point, writing the task packet and reviewing the diff costs more strongest-tier tokens than just typing the change. Quality assertions: 30/30 in both conditions — no quality gain to offset the cost. This measurement is why the delegation floor (≥ ~300 lines / ≥ 6 files / repetitive many-site edits) is baked into Model Boss.

## Large task: the full four-way comparison

One large task — a greenfield 8-module shopping-cart subsystem (~1,100 lines of code + tests) — run four ways. All four passed every gate and assertion (8/8 modules, all tests green):

| Configuration | Fable output | Fable cost | Total cost | Time | Tests |
|---|---|---|---|---|---|
| Baseline — Fable does everything | 30,993 | $3.05 | $3.05 | 335s | 81 |
| **lite** — Fable orchestrates, Sonnet implements | 17,899 (−42%) | $2.03 | **$2.91 (lowest)** | 465s | **91** |
| **max** — Opus 4.8 main loop, Fable consultant | **3,278 (−89%)** | **$0.38** | $5.66 | **116s** | 83 |
| max variant — Sonnet 5 main loop | 12,152 (−61%) | $1.41 | $5.87 (highest) | 162s | 66 |

## The Fable quota bill, itemized

Subscription rate limits meter **weighted usage across every dimension** — input, output, cache writes, cache reads. Everything billed to Fable per configuration, same task:

| Configuration | Input | Output | Cache write | Cache read | Fable-attributed cost | Quota burn vs baseline |
|---|---|---|---|---|---|---|
| Baseline | 11,861 | 30,993 | 49,273 | 398,157 | $3.05 | — |
| lite | 11,595 | 17,899 | 40,451 | 205,528 | $2.03 | **−34%** |
| max (Opus loop) | 11,046 | 3,278 | 8,282 | **0** | $0.38 | **−88%** |
| max (Sonnet loop) | 22,614 | 12,152 | 41,098 | 65,919 | $1.41 | −54% |

The exact quota-weighting formula isn't public, but per-token prices weight the same dimensions the way compute does — the **Fable-attributed cost column is the best available proxy for quota burn**, and all savings percentages in this project anchor to it.

Note the zero in max-opus's cache-read column: the consultant never carries session context — two stateless brief-in/verdict-out calls, exactly as the protocol prescribes. That is where the 9× reduction comes from: in baseline and lite, the strongest model re-reads hundreds of thousands of cached context tokens every turn just by *being* the main loop.

## Capability probe: the hidden-bug gauntlet

The tables above prove cost differences on *constructive* work. This probe asks the harder question: **does moving Fable out of the main loop lose problem-solving ability on judgment-dense work?**

Setup: a metering/billing module with **6 planted production-grade bugs** (half-open-interval double billing, per-line rounding drift, internal-reference leak, async lost-update race, stale memoized aggregate, lexicographic numeric-key ordering). The visible test suite passes despite all six. Each configuration received only four symptom-level bug reports (two reports mixed two root causes each) plus a behavior spec — then was graded against a **blind test suite it never saw**, one targeted test per bug.

| | Baseline — Fable solo | max — Opus loop + Fable consultant |
|---|---|---|
| **Blind-suite score** | **6/6** | **6/6** |
| Fable-attributed cost | $1.58 (doing everything) | **$1.76 (consultant only — MORE)** |
| Total cost | **$1.58** | $3.45 (+118%) |
| Time | **152s** | 586s (3.9×) |

Two conclusions, one per direction:

- **Capability parity is real.** The max configuration found and correctly fixed every planted bug, including the race condition and the aliasing bug — root-cause reasoning survives the consultant architecture intact.
- **The economics invert on judgment-dense work.** In a debugging task, the *reasoning is the workload* — there is no cheap implementation volume to delegate. The consultant ended up doing more strongest-tier reasoning ($1.76) than solo Fable spent on the entire job ($1.58), with Opus and Sonnet billed on top. Orchestration here is pure bureaucracy: same quality, 2.2× the cost, 3.9× the time. This is measured confirmation of Model Boss's built-in rules — "do not trigger for debugging" and "design-heavy work stays with the orchestrator" were design intuitions; now they're data.

(Caveat: n=1 task, single run per configuration — a capability probe, not a statistical claim.)

## Findings

1. **Max mode delivers on its promise — for quota only.** With an Opus main loop, Fable spent $0.38 (two clean consultant checkpoints) — an 8× quota reduction. But total dollars went **up** 86%: the main-loop tax didn't disappear, it moved to Opus and grew (Opus emitted 60k output tokens in the role where Fable needed 17.9k).
2. **The main-loop model's discipline matters more than its price.** Sonnet 5 costs 60% of Opus per token but leaned on the Fable consultant 3.7× harder and churned 4.6M cache-read tokens — losing on both metrics. Max mode's recommended main loop is **Opus-class**.
3. **No orchestrated configuration beat the baseline on total dollars at this task size.** lite came closest (−5%). If dollars are your only metric, orchestration pays off only on tasks substantially larger than ~1,100 lines — or not at all.

## What are you actually saving?

Two different wallets:

- **Pay-per-token API users** — total dollars is your metric, and the savings are marginal at best (lite: −5%). The dollar gap widens with task size because the orchestrator's packet+review overhead is roughly fixed while delegated volume grows. Only reach for Model Boss on genuinely large tasks.
- **Subscription users (Claude Max and similar)** — your real constraint is the strongest model's **rate-limit quota**. Cheaper tiers burn quota far slower, and the quota window resets regardless of spend. **−88% strongest-tier quota means roughly 8× more orchestrated work per quota window.** This is the skill's primary audience: a quota-arbitrage tool first, a cost tool second.

## Reproduce

The eval prompts are in [evals/evals.json](evals/evals.json); raw benchmark data is in [benchmarks/](benchmarks/). The four recorded benchmark evals each contain one predecessor-skill observation and one baseline observation; they were not rerun for Model Boss — see Methodology above.
