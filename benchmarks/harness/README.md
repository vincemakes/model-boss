# Dispatch benchmark harness

Re-runs the large constructive task from the recorded benchmark as independent
headless `claude -p` sessions so the `estimate` coefficients in
`runtime/model_boss/dispatch.py` can be checked against fresh usage data.

Every number comes from the CLI's `--output-format json` (`modelUsage` per model,
`total_cost_usd`, `duration_ms`). Nothing is estimated. Sessions run under the
caller's own Claude Code login and consume that plan's usage; the harness never
calls a model except in the cells themselves and in `--smoke`.

## Cells

| Cell | Main loop | Mode | Worker |
|---|---|---|---|
| `fable-low-inline` | Fable 5.1 @ low | inline | – |
| `fable-medium-inline` | Fable 5.1 @ medium | inline | – |
| `opus-high-inline` | Opus 5 @ high | inline | – |
| `fable-medium-lite-opus` | Fable 5.1 @ medium | Lite | Opus 5 @ high |
| `fable-medium-lite-sonnet` | Fable 5.1 @ medium | Lite | Sonnet 5 @ high |
| `sonnet-high-inline` | Sonnet 5 @ high | inline | – |

Fable runs only at `low` and `medium` by design; the recorded `high` baseline stays
as the reference point. Lite cells install a project-scope
`.claude/agents/model-boss-implementer.md` pinned to the worker model and effort, then
ask the main loop to use the Model Boss skill in Lite mode.

## Run

```bash
claude auth login                                   # once, in your own terminal
python3 benchmarks/harness/run_cells.py --smoke     # one Haiku call: is the login usable headlessly?
python3 benchmarks/harness/run_cells.py --list
python3 benchmarks/harness/run_cells.py --wave 3    # all six cells, three at a time
python3 benchmarks/harness/run_cells.py --cells fable-low-inline,opus-high-inline
python3 benchmarks/harness/run_cells.py --grade-only <runs-dir>
```

Runs land under `$TMPDIR/model-boss-bench/runs/<stamp>/<cell>/` with the raw CLI
JSON (`.bench-stdout.json`), gate logs, and a `.bench-summary.json`; the run
directory gets `summary.json` and `summary.md`. `--dry-run` prints the exact
commands without calling a model.

## Grading

Identical objective checks for every cell: `pnpm typecheck`, `pnpm test`, all eight
`src/cart/` modules present, at least 40 cart tests, edge-case keyword groups present
in `tests/cart/`, and no change outside `src/cart/` and `tests/cart/`.

## Recorded run

The 2026-09-02 run is archived as `benchmarks/fable-effort-dispatch-rerun.json` and
written up in `benchmarks/fable-effort-dispatch-rerun.md`. Fable 5.1 needs Claude Code
2.1.251 or newer; the harness checks the installed version before copying any fixture.

## Caveats

- Single runs per cell; treat differences under ~15% as noise or repeat the cell.
- `costUSD` is the CLI's list-price computation, used only as a quota proxy, exactly
  as in `BENCHMARKS.md`.
- Subagents get the five-minute cache TTL by default; the harness does not change it.
