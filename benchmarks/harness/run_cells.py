#!/usr/bin/env python3
"""Run the Model Boss dispatch benchmark cells as independent headless `claude -p` sessions.

Each cell copies the fixture template into its own directory, launches one headless
Claude Code session with an exact model and effort, grades the result with the same
objective checks for every cell (gates, module presence, test count, edge-case
coverage, scope), and records the CLI's per-model usage JSON as the quota proxy.

The harness never estimates tokens: every number comes from `--output-format json`.
Sessions run with the caller's own Claude Code login, so they consume that account's
plan usage.  Nothing here calls a model except the cells themselves and `--smoke`.

Examples:

  python3 benchmarks/harness/run_cells.py --smoke
  python3 benchmarks/harness/run_cells.py --cells fable-low-inline,opus-high-inline --dry-run
  python3 benchmarks/harness/run_cells.py --wave 3
  python3 benchmarks/harness/run_cells.py --grade-only /tmp/model-boss-bench/runs/<stamp>
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
TASK_FILE = HARNESS / "tasks" / "large-greenfield-subsystem.md"
MAKE_FIXTURE = HARNESS / "make_fixture.sh"
REQUIRED_MODULES = (
    "types.ts",
    "errors.ts",
    "cart.ts",
    "pricing.ts",
    "inventory.ts",
    "coupons.ts",
    "events.ts",
    "serialization.ts",
)
BASELINE_TESTS = 4
REQUIRED_CART_TESTS = 40
EDGE_CASE_GROUPS: dict[str, tuple[str, ...]] = {
    "tier-boundaries": (r"\b9\b", r"\b10\b", r"\b49\b", r"\b50\b"),
    "coupons": (r"expired", r"unknown", r"fixed", r"percent"),
    "inventory": (r"stock", r"reserve", r"release"),
    "corrupt-payload": (r"corrupt", r"malformed|invalid|bad json|missing"),
    "event-ordering": (r"seq|sequence", r"order|replay"),
}
# Minimum Claude Code version that can address each exact model headlessly.  The CLI
# rejects older versions with `400 ... version X or newer is required`.
MODEL_MIN_CLI_VERSION: dict[str, tuple[int, ...]] = {
    "claude-fable-5-1": (2, 1, 251),
}
NESTED_SESSION_MARKERS = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_ENTRYPOINT",
)

INLINE_SUFFIX = (
    "\n\nDo this yourself in this repository. Do not use the model-boss skill, do not "
    "spawn subagents or agents, and do not ask questions. Run the gates yourself before "
    "you finish."
)
LITE_PREFIX = (
    "Use the model boss skill in Lite mode. You are the inherited main loop and keep both "
    "authority checkpoints inline. Dispatch the implementation to the "
    "`model-boss-implementer` agent configured for this project with a bounded task "
    "packet (goal, allowed paths, acceptance criteria, gates). Do not implement the cart "
    "modules yourself. After the worker returns, review its diff, run the gates yourself, "
    "and integrate or send it back for at most three fixes.\n\nThe task:\n\n"
)


@dataclass(frozen=True)
class Cell:
    name: str
    main_model: str
    main_effort: str
    mode: str  # "inline" | "lite"
    worker_model: str | None = None
    worker_effort: str | None = None

    @property
    def uses_fable(self) -> bool:
        return "fable" in self.main_model


CELLS: tuple[Cell, ...] = (
    Cell("fable-low-inline", "claude-fable-5-1", "low", "inline"),
    Cell("fable-medium-inline", "claude-fable-5-1", "medium", "inline"),
    Cell("opus-high-inline", "claude-opus-5", "high", "inline"),
    Cell("fable-medium-lite-opus", "claude-fable-5-1", "medium", "lite", "claude-opus-5", "high"),
    Cell("fable-medium-lite-sonnet", "claude-fable-5-1", "medium", "lite", "claude-sonnet-5", "high"),
    Cell("sonnet-high-inline", "claude-sonnet-5", "high", "inline"),
)
CELLS_BY_NAME = {cell.name: cell for cell in CELLS}


@dataclass
class Grade:
    typecheck_passed: bool = False
    tests_passed: bool = False
    tests_total: int = 0
    cart_tests: int = 0
    modules_present: list[str] = field(default_factory=list)
    modules_missing: list[str] = field(default_factory=list)
    edge_case_groups_covered: list[str] = field(default_factory=list)
    scope_violations: list[str] = field(default_factory=list)
    added_lines: int = 0
    changed_files: int = 0
    assertions: dict[str, bool] = field(default_factory=dict)
    pass_rate: float = 0.0


def _child_environment() -> dict[str, str]:
    """Minimal environment: the child must use the CLI's own login, not this session's."""

    keep = ("HOME", "PATH", "USER", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TERM")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.setdefault("TERM", "dumb")
    env.setdefault("LANG", "en_US.UTF-8")
    for marker in NESTED_SESSION_MARKERS:
        env.pop(marker, None)
    return env


def _run(cmd: list[str], cwd: Path, timeout: float, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def cli_version() -> tuple[int, ...] | None:
    try:
        completed = _run(["claude", "--version"], Path.cwd(), 30, _child_environment())
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", completed.stdout + completed.stderr)
    return tuple(int(part) for part in match.groups()) if match else None


def check_cli_supports(cells: list[Cell]) -> str | None:
    """Return a human-readable blocker when the installed CLI cannot run a cell's models."""

    version = cli_version()
    if version is None:
        return "could not determine the Claude Code CLI version; is `claude` on PATH?"
    needed: dict[str, tuple[int, ...]] = {}
    for cell in cells:
        for model in (cell.main_model, cell.worker_model):
            minimum = MODEL_MIN_CLI_VERSION.get(model or "")
            if minimum and version < minimum:
                needed[model or ""] = minimum
    if not needed:
        return None
    wanted = ", ".join(f"{model} needs {'.'.join(map(str, v))}" for model, v in needed.items())
    return (
        f"Claude Code {'.'.join(map(str, version))} is too old for: {wanted}. "
        "Update the CLI (`claude update`, or `npm install -g @anthropic-ai/claude-code@latest`) first."
    )


def ensure_template(template: Path) -> Path:
    if (template / "package.json").is_file() and (template / "node_modules").is_dir():
        return template
    print(f"[harness] building fixture template at {template}", flush=True)
    subprocess.run(["bash", str(MAKE_FIXTURE), str(template)], check=True)
    return template


def materialize(template: Path, run_dir: Path, cell: Cell) -> None:
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-R", str(template), str(run_dir)], check=True)
    if cell.mode == "lite":
        agents = run_dir / ".claude" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        effort_line = f"effort: {cell.worker_effort}\n" if cell.worker_effort else ""
        (agents / "model-boss-implementer.md").write_text(
            "---\n"
            "name: model-boss-implementer\n"
            'description: "Implementation worker for the Model Boss benchmark cell."\n'
            f"model: {cell.worker_model}\n"
            f"{effort_line}"
            "---\n\n"
            "The host main loop remains inherited; this file configures only a spawned worker.\n"
            "Implement only the packet's allowed paths and acceptance criteria. Respect dependency\n"
            "and scope fences. Run every gate and self-fix for at most three attempts. Return files\n"
            "changed, a concise approach, exact final gate results, and questions. Stop with\n"
            "`NEEDS_CONTEXT` on ambiguity; never merge or self-approve.\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=run_dir, check=True)
        subprocess.run(
            ["git", "-c", "user.name=bench", "-c", "user.email=bench@example.com", "commit", "-qm", "cell baseline"],
            cwd=run_dir,
            check=True,
        )


def build_prompt(cell: Cell) -> str:
    task = TASK_FILE.read_text(encoding="utf-8").strip()
    if cell.mode == "inline":
        return task + INLINE_SUFFIX
    return LITE_PREFIX + task


def claude_command(cell: Cell, prompt: str, max_turns: int | None) -> list[str]:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        cell.main_model,
        "--effort",
        cell.main_effort,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
    ]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    return cmd


def launch(cell: Cell, run_dir: Path, timeout: float, max_turns: int | None) -> dict[str, object]:
    prompt = build_prompt(cell)
    cmd = claude_command(cell, prompt, max_turns)
    (run_dir / ".bench-prompt.txt").write_text(prompt, encoding="utf-8")
    started = time.monotonic()
    status = "ok"
    try:
        completed = _run(cmd, run_dir, timeout, _child_environment())
        stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        returncode = -1
        status = "timeout"
    wall = time.monotonic() - started
    (run_dir / ".bench-stdout.json").write_text(stdout, encoding="utf-8")
    (run_dir / ".bench-stderr.log").write_text(stderr, encoding="utf-8")
    result: dict[str, object] = {}
    try:
        result = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        status = "bad-json"
    if isinstance(result, dict) and result.get("is_error"):
        status = "error"
    return {
        "status": status,
        "returncode": returncode,
        "wall_seconds": round(wall, 1),
        "result": result if isinstance(result, dict) else {},
    }


def grade(run_dir: Path) -> Grade:
    g = Grade()
    typecheck = _run(["pnpm", "typecheck"], run_dir, 300)
    g.typecheck_passed = typecheck.returncode == 0
    tests = _run(["pnpm", "test"], run_dir, 300)
    g.tests_passed = tests.returncode == 0
    match = re.search(r"Tests\s+(\d+)\s+passed", tests.stdout + tests.stderr)
    g.tests_total = int(match.group(1)) if match else 0
    g.cart_tests = max(0, g.tests_total - BASELINE_TESTS)
    (run_dir / ".bench-gates.log").write_text(
        "== typecheck ==\n" + typecheck.stdout + typecheck.stderr + "\n== test ==\n" + tests.stdout + tests.stderr,
        encoding="utf-8",
    )

    cart_dir = run_dir / "src" / "cart"
    for module in REQUIRED_MODULES:
        (g.modules_present if (cart_dir / module).is_file() else g.modules_missing).append(module)

    corpus = ""
    tests_dir = run_dir / "tests" / "cart"
    if tests_dir.is_dir():
        corpus = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in tests_dir.rglob("*.test.ts")).lower()
    for group, patterns in EDGE_CASE_GROUPS.items():
        if corpus and all(re.search(pattern, corpus) for pattern in patterns):
            g.edge_case_groups_covered.append(group)

    subprocess.run(["git", "add", "-A", "--", ".", ":!.bench-*"], cwd=run_dir, check=False, capture_output=True)
    numstat = _run(["git", "diff", "--cached", "--numstat"], run_dir, 60)
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, _removed, path = parts
        if path.startswith(".bench-") or path.startswith(".claude/"):
            continue
        g.changed_files += 1
        if added.isdigit():
            g.added_lines += int(added)
        if not (path.startswith("src/cart/") or path.startswith("tests/cart/")):
            g.scope_violations.append(path)

    g.assertions = {
        "gate-typecheck-passes": g.typecheck_passed,
        "gate-tests-pass": g.tests_passed,
        "all-eight-modules-present": not g.modules_missing,
        "forty-plus-cart-tests": g.cart_tests >= REQUIRED_CART_TESTS,
        "edge-cases-covered": len(g.edge_case_groups_covered) >= 4,
        "scope-respected": not g.scope_violations,
    }
    g.pass_rate = round(sum(g.assertions.values()) / len(g.assertions), 3)
    return g


def usage_summary(result: dict[str, object]) -> dict[str, object]:
    model_usage = result.get("modelUsage") if isinstance(result, dict) else None
    per_model: dict[str, dict[str, float]] = {}
    if isinstance(model_usage, dict):
        for model_id, raw in model_usage.items():
            if not isinstance(raw, dict):
                continue
            per_model[model_id] = {
                "input": int(raw.get("inputTokens", 0) or 0),
                "output": int(raw.get("outputTokens", 0) or 0),
                "cache_read": int(raw.get("cacheReadInputTokens", 0) or 0),
                "cache_write": int(raw.get("cacheCreationInputTokens", 0) or 0),
                "cost_usd": round(float(raw.get("costUSD", 0) or 0), 4),
            }
    fable_cost = round(sum(v["cost_usd"] for k, v in per_model.items() if "fable" in k.lower()), 4)
    return {
        "total_cost_usd": round(float(result.get("total_cost_usd", 0) or 0), 4),
        "fable_cost_usd": fable_cost,
        "duration_ms": int(result.get("duration_ms", 0) or 0),
        "num_turns": int(result.get("num_turns", 0) or 0),
        "per_model": per_model,
        "final_text": str(result.get("result", ""))[:2000],
    }


def run_cell(cell: Cell, template: Path, runs_dir: Path, timeout: float, max_turns: int | None, dry_run: bool) -> dict[str, object]:
    run_dir = runs_dir / cell.name
    if dry_run:
        cmd = claude_command(cell, "<prompt: " + str(TASK_FILE.name) + ">", max_turns)
        return {"cell": asdict(cell), "run_dir": str(run_dir), "dry_run": True, "command": cmd}
    if run_dir.exists():
        shutil.rmtree(run_dir)
    materialize(template, run_dir, cell)
    print(f"[harness] {cell.name}: launched", flush=True)
    launched = launch(cell, run_dir, timeout, max_turns)
    graded = grade(run_dir)
    summary = {
        "cell": asdict(cell),
        "run_dir": str(run_dir),
        "launch": {k: v for k, v in launched.items() if k != "result"},
        "usage": usage_summary(launched["result"]),  # type: ignore[arg-type]
        "grade": asdict(graded),
    }
    (run_dir / ".bench-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"[harness] {cell.name}: {launched['status']} in {launched['wall_seconds']}s, "
        f"pass_rate={graded.pass_rate}, fable=${summary['usage']['fable_cost_usd']}, "
        f"total=${summary['usage']['total_cost_usd']}",
        flush=True,
    )
    return summary


def render_table(summaries: list[dict[str, object]]) -> str:
    header = (
        "| cell | status | pass | tests | lines | Fable $ | total $ | time s | per-model output / cache-read |\n"
        "|---|---|---|---|---|---|---|---|---|"
    )
    rows = [header]
    for s in summaries:
        cell = s["cell"]
        if s.get("dry_run"):
            rows.append(f"| {cell['name']} | dry-run | | | | | | | `{' '.join(s['command'][:6])} …` |")
            continue
        usage = s["usage"]
        grade_ = s["grade"]
        per_model = ", ".join(
            f"{model.replace('claude-', '')}: {v['output']:,} / {v['cache_read']:,}"
            for model, v in usage["per_model"].items()
        )
        rows.append(
            f"| {cell['name']} | {s['launch']['status']} | {grade_['pass_rate']:.2f} | "
            f"{grade_['cart_tests']} | {grade_['added_lines']} | {usage['fable_cost_usd']:.2f} | "
            f"{usage['total_cost_usd']:.2f} | {s['launch']['wall_seconds']} | {per_model} |"
        )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--parent", default=os.path.join(tempfile.gettempdir(), "model-boss-bench"))
    parser.add_argument("--template", help="fixture template dir (default: <parent>/template)")
    parser.add_argument("--cells", help="comma-separated cell names (default: all)")
    parser.add_argument("--wave", type=int, default=3, help="cells to run concurrently")
    parser.add_argument("--timeout", type=float, default=2700.0, help="seconds per cell")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="print commands only; no model calls")
    parser.add_argument("--smoke", action="store_true", help="one cheap Haiku call to verify the CLI login")
    parser.add_argument("--grade-only", help="re-grade an existing runs directory and rewrite its summary")
    parser.add_argument("--runs-dir", help="append cells to this existing runs directory instead of a new stamp")
    parser.add_argument("--force", action="store_true", help="re-run cells that already have a summary in --runs-dir")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        for cell in CELLS:
            print(f"{cell.name:28} main={cell.main_model}@{cell.main_effort:<7} mode={cell.mode:<6} worker={cell.worker_model or '-'}@{cell.worker_effort or '-'}")
        return 0

    if args.smoke:
        with tempfile.TemporaryDirectory() as tmp:
            completed = _run(
                ["claude", "-p", "Reply with exactly the word ok.", "--model", "claude-haiku-4-5",
                 "--output-format", "json", "--no-session-persistence", "--max-turns", "1",
                 "--dangerously-skip-permissions"],
                Path(tmp), 120, _child_environment(),
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = {"is_error": True, "result": completed.stdout[:200] + completed.stderr[:200]}
        ok = completed.returncode == 0 and not payload.get("is_error")
        print("smoke:", "ok" if ok else "FAILED", "-", payload.get("result", "")[:160])
        if not ok:
            print("The CLI is not usable headlessly. Run `claude auth login` in a terminal first.")
        return 0 if ok else 1

    if args.grade_only:
        runs_dir = Path(args.grade_only)
        summaries = []
        for cell_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir() and p.name in CELLS_BY_NAME):
            previous = json.loads((cell_dir / ".bench-summary.json").read_text()) if (cell_dir / ".bench-summary.json").is_file() else {}
            previous["grade"] = asdict(grade(cell_dir))
            previous.setdefault("cell", asdict(CELLS_BY_NAME[cell_dir.name]))
            previous.setdefault("launch", {"status": "regraded", "wall_seconds": 0})
            previous.setdefault("usage", usage_summary({}))
            (cell_dir / ".bench-summary.json").write_text(json.dumps(previous, indent=2), encoding="utf-8")
            summaries.append(previous)
        (runs_dir / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        (runs_dir / "summary.md").write_text(render_table(summaries) + "\n", encoding="utf-8")
        print(render_table(summaries))
        return 0

    parent = Path(args.parent)
    template = Path(args.template) if args.template else parent / "template"
    selected = [CELLS_BY_NAME[name.strip()] for name in (args.cells.split(",") if args.cells else CELLS_BY_NAME)]
    if not args.dry_run:
        blocker = check_cli_supports(selected)
        if blocker:
            print(f"[harness] blocked: {blocker}", file=sys.stderr)
            return 2
        ensure_template(template)
    if args.runs_dir:
        runs_dir = Path(args.runs_dir)
        existing = [
            cell for cell in selected
            if (runs_dir / cell.name / ".bench-summary.json").is_file()
            and json.loads((runs_dir / cell.name / ".bench-summary.json").read_text()).get("launch", {}).get("status") == "ok"
        ]
        if existing and not args.force:
            print(f"[harness] skipping cells with an ok summary in {runs_dir}: {', '.join(c.name for c in existing)}")
            selected = [cell for cell in selected if cell not in existing]
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        runs_dir = parent / "runs" / stamp
    if not args.dry_run:
        runs_dir.mkdir(parents=True, exist_ok=True)
        print(f"[harness] runs directory: {runs_dir}", flush=True)
    if not selected:
        print("[harness] nothing to run")
        return 0

    summaries: list[dict[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.wave)) as pool:
        futures = [
            pool.submit(run_cell, cell, template, runs_dir, args.timeout, args.max_turns, args.dry_run)
            for cell in selected
        ]
        for future in futures:
            summaries.append(future.result())

    if not args.dry_run:
        merged: list[dict[str, object]] = []
        for cell in CELLS:
            path = runs_dir / cell.name / ".bench-summary.json"
            if path.is_file():
                merged.append(json.loads(path.read_text(encoding="utf-8")))
        summaries = merged or summaries
    table = render_table(summaries)
    if not args.dry_run:
        (runs_dir / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        (runs_dir / "summary.md").write_text(table + "\n", encoding="utf-8")
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
