# Fable 5.1 effort and dispatch rerun

**Recorded 2026-09-02.** Six independent headless `claude -p` sessions on the recorded
large greenfield task (eight `src/cart/` modules plus forty or more tests), each in a fresh
fixture copy, run with `benchmarks/harness/run_cells.py`. Raw per-cell usage is in
[`fable-effort-dispatch-rerun.json`](fable-effort-dispatch-rerun.json). Every number is the
CLI's own `--output-format json` accounting; `costUSD` is the list-price proxy used for quota,
exactly as in [BENCHMARKS.md](../BENCHMARKS.md). One run per cell, so differences under
about fifteen percent are noise.

Fable 5.1 ran only at `low` and `medium`; the recorded Fable 5 `high` baseline ($3.05) is the
reference for `high`. Fable cells ran on Claude Code 2.1.258 (Fable 5.1 needs 2.1.251 or
newer), the Opus 5 and Sonnet 5 cells on 2.1.246 earlier the same day.

## Results

All six cells passed all six objective checks: typecheck, tests, eight modules present,
forty or more cart tests, edge-case coverage, no change outside `src/cart/` and `tests/cart/`.

| Cell | Main loop | Worker | Cart tests | Lines added | Fable $ | Total $ | Wall | Turns |
|---|---|---|---|---|---|---|---|---|
| fable-low-inline | Fable 5.1 @ low | – | 59 | 904 | 1.31 | 1.31 | 161 s | 6 |
| fable-medium-inline | Fable 5.1 @ medium | – | 70 | 951 | 1.61 | 1.62 | 181 s | 7 |
| fable-medium-lite-sonnet | Fable 5.1 @ medium | Sonnet 5 @ high | 77 | 1,280 | 1.02 | 1.73 | 412 s | 9 |
| fable-medium-lite-opus | Fable 5.1 @ medium | Opus 5 @ high | 118 | 1,859 | 1.26 | 2.66 | 435 s | 13 |
| opus-high-inline | Opus 5 @ high | – | 104 | 1,734 | 0.00 | 2.19 | 479 s | 15 |
| sonnet-high-inline | Sonnet 5 @ high | – | 66 | 1,012 | 0.00 | 0.80 | 258 s | 24 |

Where the Fable spend went (main conversation, one-hour cache TTL, cache writes at $20/MTok):

| Cell | Fable output | Fable cache read | Fable cache write | Output $ | Read $ | Write $ | Fable $ |
|---|---|---|---|---|---|---|---|
| fable-low-inline | 15,833 | 162,565 | 23,824 | 0.79 | 0.04 | 0.48 | 1.31 |
| fable-medium-inline | 17,967 | 192,449 | 33,220 | 0.90 | 0.05 | 0.66 | 1.61 |
| fable-medium-lite-sonnet (main) | 6,537 | 259,005 | 31,231 | 0.33 | 0.07 | 0.62 | 1.02 |
| fable-medium-lite-opus (main) | 6,243 | 403,453 | 42,007 | 0.31 | 0.10 | 0.84 | 1.26 |

Workers (subagents, five-minute cache TTL): Sonnet 5 wrote 36,867 output tokens and read
1.07M cached tokens for $0.71; Opus 5 wrote 33,456 and read 513K for $1.39.

## Findings

1. **Fable 5.1 at low effort, inline, is the cheapest and fastest way to get the whole
   task done with Fable.** $1.31 and 2.7 minutes, all gates green, 57% below the recorded
   Fable 5 high baseline. Medium costs 23% more than low and buys eleven more tests.
2. **Effort is the lever, not orchestration.** Low versus medium saves 19%; against the
   recorded high baseline the two levels save 57% and 47%. Cache reads at $0.25/MTok are
   3% to 8% of Fable spend; they are not where the money goes.
3. **An Opus 5 worker under a Fable main loop is not worth it.** It saved 4% of Fable spend
   against inline low (22% against inline medium), doubled total spend, and took 2.7 times
   longer. The worker wrote 1,859 lines for a spec Fable met in 904.
4. **A Sonnet 5 worker is a quota trade, not a saving.** Fable spend fell 22% against
   inline low and 37% against inline medium; total spend rose 32% and 7%; wall time rose
   2.5 times. It pays only when the Fable window is the binding constraint.
5. **In Lite, the Fable main loop pays for reading, not writing.** Its output fell by
   about 60%, but cache writes rose to 60% to 67% of its spend: every token of worker
   report and diff it reads for the first time is billed at the one-hour write rate.
   Shorter worker reports and smaller diffs cut Fable cost more than fewer Fable words do.
6. **Delegated implementations are larger.** The same specification produced 904 to 951
   lines inline on Fable, 1,280 from Sonnet 5, 1,734 to 1,859 from Opus 5. Cost is per
   task, not per line, which is why `estimate` applies a delegated-volume factor.
7. **Quality did not separate the cells.** Six of six checks everywhere, 59 to 118 cart
   tests. A clear constructive spec is a ceiling for this kind of comparison; the recorded
   blind bug-hunt remains the only capability probe.

## Reading the same numbers as a quality-first subscription user

The findings above rank by spend. A Claude Max subscriber whose weekly quota is one
shared total with a cap on Fable's share, and who wants the best result rather than the
lowest bill, should read the same cells differently:

- Fable inline burns only the Fable half and leaves the other half idle. Lite with an
  Opus worker split its spend 48% Fable / 52% Opus, almost exactly the plan's shape, so
  it is the topology that uses the whole week while keeping Fable in charge of the plan
  and the review. Its Fable spend ($1.26) was about the inline-low rate ($1.31): it fills
  the other half, it does not stretch this one. Max is what stretches Fable.
- The Opus worker delivered the most (118 tests, 1,859 lines); "over-delivery" is a
  cost-side label. Nothing in these cells says Opus execution under Fable review is
  worse, and the single-worker layout hid the speed case for it: independent packets
  run as parallel workers, which Fable inline cannot do.
- Fable's spend in Lite is roughly flat in task size while inline spend grows with
  every line, so the case for a Fable main loop with Opus workers strengthens with task
  size. Extrapolating with the recalibrated model (not measured): at about 1,500 changed
  lines Lite spends roughly 45% less Fable than inline, at 3,000 about 50%, and below
  about 200 lines it spends more. Large refactors and multi-packet sprints were not run.

## Method notes and limits

- Lite here is the skill's host-native flow: the Fable main loop printed the verdict and
  estimate, drafted the packet, dispatched `model-boss-implementer` (project-scope agent
  pinned to the worker model and `effort: high`), reviewed, reran gates, and integrated. The
  worker edited the working tree directly; no sealed external worktree was involved.
- The Opus 5 worker is reported by the CLI as `claude-opus-5[1m]`; its `costUSD` matches
  standard Opus 5 rates with five-minute cache writes.
- Two CLI versions in one table (see above). Neither the model nor the pricing changed
  between them; the system prompt differs slightly.
- Recalibration: `runtime/model_boss/dispatch.py` now reproduces the four Fable cells
  within about 15% and the workers within about 30%; the worker-side error is the widest
  because Sonnet 5 churned cache reads and Opus 5 wrote twice the lines.

## 中文摘要

同一份 8 模块加 40+ 测试的任务，六个独立 headless 会话，全部六项客观检查通过。结论按重要性排：

- **Fable 5.1 开 low 自己干最便宜也最快**：$1.31 等价额度、2.7 分钟，比记录里的 Fable 5 high 基线省 57%。medium 比 low 贵 23%，多写了 11 个测试。
- **杠杆是 effort，不是编排。** 缓存读价只占 Fable 花费的 3% 到 8%。
- **Fable 主循环配 Opus 5 worker 不划算**：Fable 只省 4%（对 inline low）或 22%（对 inline medium），总额翻倍，时间 2.7 倍。Opus 为同一规格写了 1,859 行，Fable 只需 904 行。
- **配 Sonnet 5 worker 是拿总额换 Fable 窗口**：Fable 省 22% 到 37%，总额多 7% 到 32%，时间 2.5 倍。只有 Fable 窗口是硬约束时才值得。
- **Lite 里 Fable 的钱花在「读」上**：自己的输出降了六成，但读 worker 报告和 diff 的缓存写入占了六到七成花费（1 小时 TTL 下 $20/MTok）。让 worker 报告短、diff 小，比让 Fable 少说话更省。
- 质量在这类规格清晰的建设性任务上分不出差别，六格全绿。

如果你是质量优先、额度是「共用总池加 Fable 一半上限」的订阅用户，同一组数字要反过来读：Fable 自己干只烧
Fable 半区，另一半闲着；Fable 主循环配 Opus worker 的花费比例是 48 比 52，正好是套餐的形状，它是把整周
额度用满、又让 Fable 掌握计划和评审的拓扑，只是它不撑 Fable（每任务 Fable 花费与自己干相当），撑 Fable 的
是 Max。Opus worker 交付最厚，单 worker 的格子也没体现并行包的速度优势。按重校准后的模型外推（未实测）：
约 1,500 行时 Lite 比自己干省约 45% Fable，3,000 行约 50%，200 行以下反而更贵。大量重构和多包并行没有跑过。
