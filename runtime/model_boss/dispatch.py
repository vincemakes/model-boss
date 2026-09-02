"""Dispatch calculus: price the hand-off before deciding to delegate.

Model Boss used to gate delegation on a rule of thumb (roughly 300 changed lines or
six files).  This module makes the decision explicit: for one task shape it
estimates the tokens each role would consume under three plans and prices them
with the catalog's per-token rates, weighted by each route's ``quota_weight``.

* ``inline``: the main loop does everything itself (no worker, no reviewer).
* ``lite``:   the main loop plans, writes the packet, reviews, and integrates;
              a worker implements in a disposable worktree.
* ``max``:    a distinct reviewer holds authority; the main loop coordinates and
              either implements itself or dispatches a worker.

The coefficients are anchored to two recorded data sets: the historical
Fable 5 run in ``BENCHMARKS.md`` (a ~1,100-line greenfield subsystem at high
effort) and the 2026-09 Fable 5.1 rerun in
``benchmarks/fable-effort-dispatch-rerun.md`` (the same task at low and medium
effort, inline and with Opus 5 / Sonnet 5 workers).  They are deliberately
coarse: they exist to make the trade-off visible and tunable, not to predict a
bill.  Every number the estimate prints is a proxy; the recommendation applies a
margin so that a marginal saving never justifies a hand-off.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .catalog import CatalogModel, find_model
from .models import EFFORT_LEVELS

JUDGMENT_LEVELS: tuple[str, ...] = ("low", "medium", "high")
SPEC_LEVELS: tuple[str, ...] = ("clear", "partial", "unclear")
PLAN_NAMES: tuple[str, ...] = ("inline", "lite", "max")
OBJECTIVES: tuple[str, ...] = ("weighted", "main-model")

# tokens = fixed + per_line * changed_lines, at effort `high` (multiplier 1.0).
# Anchors: BENCHMARKS.md Fable 5 high run, 1,100 lines: out 30,993 / cache write
# 49,273 / cache read 398,157; Max reviewer in 11,046 / out 3,278 / write 8,282 /
# read 0; Max main (Opus implementing itself) ~60k output.  Fable 5.1 rerun
# (benchmarks/fable-effort-dispatch-rerun.json): inline low 904 lines out 15,833 /
# read 162,565 / write 23,824; inline medium 951 lines out 17,967 / read 192,449 /
# write 33,220; Lite main at medium out 6.2-6.5k / read 259-403k / write 31-42k;
# workers at high: Opus 5 out 33,456 / read 513k / write 48k for 1,859 lines,
# Sonnet 5 out 36,867 / read 1.07M / write 50k for 1,280 lines.  Uncached input
# is negligible on the CLI's accounting, so it is a small fixed term.
CALIBRATION: Mapping[str, Mapping[str, tuple[float, float]]] = {
    "inline_main": {
        "input": (600, 0.0),
        "output": (3_000, 25.4),
        "cache_read": (20_000, 344.0),
        "cache_write": (5_000, 40.0),
    },
    "lite_main": {
        "input": (600, 0.0),
        "output": (8_000, 0.4),
        "cache_read": (20_000, 200.0),
        "cache_write": (5_000, 20.0),
    },
    "lite_worker": {
        "input": (500, 0.0),
        "output": (2_000, 22.0),
        "cache_read": (10_000, 400.0),
        "cache_write": (8_000, 25.0),
    },
    "max_reviewer": {
        "input": (6_000, 4.6),
        "output": (2_500, 0.7),
        "cache_read": (0, 0.0),
        "cache_write": (3_000, 4.8),
    },
    "max_main": {
        "input": (600, 0.0),
        "output": (6_000, 49.0),
        "cache_read": (20_000, 2_400.0),
        "cache_write": (5_000, 120.0),
    },
}
# Which usage kinds the role's effort multiplier scales.  Inline work at lower
# effort takes fewer turns, so reads and writes shrink with output; a Lite main
# loop's reads and writes are driven by the worker's diff, not its own effort.
EFFORT_SCALED_KINDS: Mapping[str, tuple[str, ...]] = {
    "inline_main": ("output", "cache_read", "cache_write"),
    "lite_main": ("output",),
    "lite_worker": ("output",),
    "max_reviewer": ("output",),
    "max_main": ("output",),
}
# Delegated implementations of the same specification came out 35%-95% larger
# than the inline ones in the rerun (Sonnet 5: 1,280 lines, Opus 5: 1,859 lines,
# against 904-951 lines inline).  The worker's tokens and the main loop's review
# writes scale with that volume; the factor is the conservative end of the range.
WORKER_VOLUME_FACTOR = 1.35
REWORK_PROBABILITY: Mapping[str, float] = {"low": 0.10, "medium": 0.30, "high": 0.60}
MECHANICAL_REWORK_PROBABILITY = 0.05
REWORK_OUTPUT_FRACTION = 0.35
# Measured on Fable 5.1: low and medium produced 0.57x and 0.65x the output of
# the Fable 5 high anchor per line; xhigh/max remain extrapolations.
EFFORT_OUTPUT_MULTIPLIER: Mapping[str, float] = {
    "low": 0.60,
    "medium": 0.70,
    "high": 1.0,
    "xhigh": 1.35,
    "max": 1.8,
}
DELEGATION_FLOOR_LINES = 60
DELEGATION_MARGIN = 0.10
DEFAULT_CACHE_TTL = "1h"
SUBAGENT_CACHE_TTL = "5m"


@dataclass(frozen=True)
class TaskShape:
    """What the calculus needs to know about the task, nothing more."""

    changed_lines: int
    files: int
    judgment: str = "medium"
    spec: str = "clear"
    mechanical: bool = False

    def __post_init__(self) -> None:
        for name in ("changed_lines", "files"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.judgment not in JUDGMENT_LEVELS:
            raise ValueError("judgment must be low, medium, or high")
        if self.spec not in SPEC_LEVELS:
            raise ValueError("spec must be clear, partial, or unclear")
        if not isinstance(self.mechanical, bool):
            raise ValueError("mechanical must be a boolean")


@dataclass(frozen=True)
class ModelPrices:
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    cache_write_usd_per_mtok: float

    def __post_init__(self) -> None:
        for name in (
            "input_usd_per_mtok",
            "output_usd_per_mtok",
            "cache_read_usd_per_mtok",
            "cache_write_usd_per_mtok",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"{name} must be a non-negative number")

    @classmethod
    def from_catalog(cls, model: CatalogModel, ttl: str = DEFAULT_CACHE_TTL) -> ModelPrices:
        return cls(
            model.input_usd_per_mtok,
            model.output_usd_per_mtok,
            model.cache_read_usd_per_mtok,
            model.cache_write_usd_per_mtok(ttl),
        )


@dataclass(frozen=True)
class RoleSpec:
    """One participant: which model, at which effort, with which quota weight."""

    label: str
    provider_family: str
    model_id: str
    effort: str | None
    quota_weight: float
    prices: ModelPrices

    def __post_init__(self) -> None:
        if not self.label or not self.model_id or not self.provider_family:
            raise ValueError("role label, provider_family, and model_id are required")
        if self.effort is not None and self.effort not in EFFORT_LEVELS:
            raise ValueError("effort must be a supported level or null")
        if (
            isinstance(self.quota_weight, bool)
            or not isinstance(self.quota_weight, (int, float))
            or not math.isfinite(float(self.quota_weight))
            or float(self.quota_weight) <= 0
        ):
            raise ValueError("quota_weight must be a positive number")

    @classmethod
    def from_catalog(
        cls,
        label: str,
        provider_family: str,
        model_id: str,
        *,
        effort: str | None = None,
        quota_weight: float = 1.0,
        prices: ModelPrices | None = None,
        cache_ttl: str = DEFAULT_CACHE_TTL,
    ) -> RoleSpec:
        """Build a role from catalog prices.

        ``cache_ttl`` selects the cache-write price: a subscription's main
        conversation gets the one-hour TTL (2x input), while subagents get five
        minutes (1.25x input) unless the host is told otherwise.
        """

        if prices is None:
            model = find_model(provider_family, model_id)
            if model is None:
                raise LookupError(f"no catalog prices for {provider_family}:{model_id}")
            prices = ModelPrices.from_catalog(model, cache_ttl)
        return cls(label, provider_family, model_id, effort, float(quota_weight), prices)

    @property
    def effort_multiplier(self) -> float:
        return 1.0 if self.effort is None else EFFORT_OUTPUT_MULTIPLIER[self.effort]


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    def cost_usd(self, prices: ModelPrices) -> float:
        return (
            self.input_tokens * prices.input_usd_per_mtok
            + self.output_tokens * prices.output_usd_per_mtok
            + self.cache_read_tokens * prices.cache_read_usd_per_mtok
            + self.cache_write_tokens * prices.cache_write_usd_per_mtok
        ) / 1_000_000

    def plus(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    def scaled(self, factor: float) -> Usage:
        return Usage(
            round(self.input_tokens * factor),
            round(self.output_tokens * factor),
            round(self.cache_read_tokens * factor),
            round(self.cache_write_tokens * factor),
        )


@dataclass(frozen=True)
class RoleCost:
    role: RoleSpec
    usage: Usage
    cost_usd: float
    weighted_usd: float


@dataclass(frozen=True)
class PlanEstimate:
    plan: str
    roles: tuple[RoleCost, ...]
    total_usd: float
    weighted_usd: float
    main_model_usd: float
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DispatchEstimate:
    task: TaskShape
    objective: str
    plans: tuple[PlanEstimate, ...]
    recommendation: str
    reasons: tuple[str, ...]

    def plan(self, name: str) -> PlanEstimate | None:
        return next((plan for plan in self.plans if plan.plan == name), None)

    def table(self) -> str:
        """Render the comparison as fixed-width text for a startup verdict."""

        lines = [
            f"Task: {self.task.changed_lines} lines / {self.task.files} files / "
            f"judgment {self.task.judgment} / spec {self.task.spec}"
            + (" / mechanical" if self.task.mechanical else ""),
            f"{'plan':<8}{'role':<34}{'output':>9}{'reads':>10}{'usd':>8}{'weighted':>10}",
        ]
        for plan in self.plans:
            for index, role in enumerate(plan.roles):
                name = plan.plan if index == 0 else ""
                label = f"{role.role.label} {role.role.model_id}"
                if role.role.effort:
                    label = f"{label}@{role.role.effort}"
                lines.append(
                    f"{name:<8}{label[:33]:<34}{role.usage.output_tokens:>9,}"
                    f"{role.usage.cache_read_tokens:>10,}{role.cost_usd:>8.2f}"
                    f"{role.weighted_usd:>10.2f}"
                )
            lines.append(
                f"{'':<8}{'total':<34}{'':>9}{'':>10}{plan.total_usd:>8.2f}"
                f"{plan.weighted_usd:>10.2f}  main-model {plan.main_model_usd:.2f}"
            )
        lines.append(f"Recommendation ({self.objective}): {self.recommendation}")
        lines.extend(f"- {reason}" for reason in self.reasons)
        return "\n".join(lines)


def _usage(profile: str, lines: int, effort_multiplier: float = 1.0, volume: float = 1.0) -> Usage:
    """Tokens for one role profile at a task size, scaled by effort and delegated volume."""

    table = CALIBRATION[profile]
    scaled = EFFORT_SCALED_KINDS[profile]

    def tokens(kind: str) -> int:
        fixed, per_line = table[kind]
        value = fixed + per_line * lines * volume
        if kind in scaled:
            value *= effort_multiplier
        return round(value)

    return Usage(tokens("input"), tokens("output"), tokens("cache_read"), tokens("cache_write"))


def _rework_probability(task: TaskShape) -> float:
    if task.mechanical:
        return MECHANICAL_REWORK_PROBABILITY
    return REWORK_PROBABILITY[task.judgment]


def _cost(role: RoleSpec, usage: Usage) -> RoleCost:
    cost = usage.cost_usd(role.prices)
    return RoleCost(role, usage, round(cost, 4), round(cost * role.quota_weight, 4))


def _plan(
    plan: str,
    costs: tuple[RoleCost, ...],
    main: RoleSpec,
    notes: tuple[str, ...],
) -> PlanEstimate:
    """Total, quota-weighted total, and the share billed to the main loop's model."""

    main_model = main.model_id.lower()
    return PlanEstimate(
        plan=plan,
        roles=costs,
        total_usd=round(sum(cost.cost_usd for cost in costs), 4),
        weighted_usd=round(sum(cost.weighted_usd for cost in costs), 4),
        main_model_usd=round(
            sum(cost.cost_usd for cost in costs if cost.role.model_id.lower() == main_model),
            4,
        ),
        notes=notes,
    )


def estimate_dispatch(
    task: TaskShape,
    main: RoleSpec,
    *,
    worker: RoleSpec | None = None,
    reviewer: RoleSpec | None = None,
    objective: str = "weighted",
) -> DispatchEstimate:
    """Compare inline, Lite, and Max for one task and recommend one of them.

    ``main`` is the inherited conversation model; it is never replaced.  ``worker``
    and ``reviewer`` are the routes that would be dispatched.  The Max plan is
    only priced when a reviewer is supplied, and its note records that Max also
    needs a balanced main loop or an explicit Max request to resolve.

    ``objective`` selects what the recommendation minimises: ``weighted`` is the
    quota-weighted price proxy across every role; ``main-model`` is the spend
    billed to the main loop's own model, which is the window a subscription user
    usually watches.  Both are printed either way.
    """

    if not isinstance(task, TaskShape) or not isinstance(main, RoleSpec):
        raise ValueError("task must be TaskShape and main must be RoleSpec")
    if objective not in OBJECTIVES:
        raise ValueError("objective must be weighted or main-model")
    lines = task.changed_lines
    rework = _rework_probability(task)
    plans: list[PlanEstimate] = []

    inline_usage = _usage("inline_main", lines, main.effort_multiplier)
    plans.append(_plan("inline", (_cost(main, inline_usage),), main, ()))

    if worker is not None:
        main_usage = _usage("lite_main", lines, main.effort_multiplier, WORKER_VOLUME_FACTOR)
        worker_usage = _usage("lite_worker", lines, worker.effort_multiplier, WORKER_VOLUME_FACTOR)
        main_rework = Usage(0, 1_500, round(30 * lines), 0).scaled(rework)
        worker_rework = Usage(
            5_000,
            round(worker_usage.output_tokens * REWORK_OUTPUT_FRACTION),
            round(worker_usage.cache_read_tokens * 0.5),
            0,
        ).scaled(rework)
        notes = (
            f"rework probability {rework:.0%} priced into worker and main-loop review",
            "worker starts cold: caches are model-scoped and subagents do not share the main cache",
            f"delegated volume factor {WORKER_VOLUME_FACTOR}: workers wrote more for the same spec in the rerun",
        )
        if worker.model_id.lower() == main.model_id.lower():
            notes += ("worker is the same model as the main loop: context isolation only, no quota separation",)
        plans.append(
            _plan(
                "lite",
                (
                    _cost(main, main_usage.plus(main_rework)),
                    _cost(worker, worker_usage.plus(worker_rework)),
                ),
                main,
                notes,
            )
        )

    if reviewer is not None:
        reviewer_usage = _usage("max_reviewer", lines, reviewer.effort_multiplier)
        reviewer_rework = Usage(4_000, 1_500, 0, 1_000).scaled(rework)
        notes = (
            "Max resolves only with a balanced main loop or an explicit Max request",
            "reviewer receives evidence packets only; it never carries session context",
        )
        if worker is not None:
            main_usage = _usage("lite_main", lines, main.effort_multiplier, WORKER_VOLUME_FACTOR)
            worker_usage = _usage("lite_worker", lines, worker.effort_multiplier, WORKER_VOLUME_FACTOR)
            worker_rework = Usage(
                5_000,
                round(worker_usage.output_tokens * REWORK_OUTPUT_FRACTION),
                round(worker_usage.cache_read_tokens * 0.5),
                0,
            ).scaled(rework)
            costs = (
                _cost(reviewer, reviewer_usage.plus(reviewer_rework)),
                _cost(main, main_usage.plus(Usage(0, 1_500, round(30 * lines), 0).scaled(rework))),
                _cost(worker, worker_usage.plus(worker_rework)),
            )
        else:
            main_usage = _usage("max_main", lines, main.effort_multiplier)
            main_rework = Usage(0, round(main_usage.output_tokens * REWORK_OUTPUT_FRACTION), round(200 * lines), 0).scaled(rework)
            costs = (
                _cost(reviewer, reviewer_usage.plus(reviewer_rework)),
                _cost(main, main_usage.plus(main_rework)),
            )
        plans.append(_plan("max", costs, main, notes))

    recommendation, reasons = _recommend(task, tuple(plans), objective)
    return DispatchEstimate(
        task=task,
        objective=objective,
        plans=tuple(plans),
        recommendation=recommendation,
        reasons=reasons,
    )


def _objective_value(plan: PlanEstimate, objective: str) -> float:
    return plan.main_model_usd if objective == "main-model" else plan.weighted_usd


def _recommend(
    task: TaskShape,
    plans: tuple[PlanEstimate, ...],
    objective: str,
) -> tuple[str, tuple[str, ...]]:
    inline = next(plan for plan in plans if plan.plan == "inline")
    if task.spec == "unclear":
        return "needs_context", (
            "acceptance criteria cannot be stated yet; clarify before any dispatch",
        )
    if task.judgment == "high":
        return "inline", (
            "judgment-dense work: the reasoning is the workload, so a hand-off adds "
            "cost without removing it (BENCHMARKS blind bug-hunt: same result, +118% total)",
        )
    if task.changed_lines < DELEGATION_FLOOR_LINES:
        return "inline", (
            f"below the delegation floor ({DELEGATION_FLOOR_LINES} changed lines): packet "
            "and review overhead exceeds the delegated volume (BENCHMARKS small tasks +34%..+66%)",
        )
    candidates = [plan for plan in plans if plan.plan != "inline"]
    if not candidates:
        return "inline", ("no worker or reviewer route was offered for comparison",)
    best = min(candidates, key=lambda plan: _objective_value(plan, objective))
    baseline = _objective_value(inline, objective)
    value = _objective_value(best, objective)
    if baseline <= 0:
        return "inline", ("inline cost is zero; nothing to save",)
    saving = 1 - value / baseline
    if saving < DELEGATION_MARGIN:
        return "inline", (
            f"{best.plan} saves {saving:.0%} on the {objective} objective, under the "
            f"{DELEGATION_MARGIN:.0%} margin that a hand-off must clear",
        )
    reasons = [
        f"{best.plan} saves {saving:.0%} on the {objective} objective "
        f"({value:.2f} vs {baseline:.2f} inline)",
        f"main-loop-model cost {best.main_model_usd:.2f} vs {inline.main_model_usd:.2f} inline; "
        f"total {best.total_usd:.2f} vs {inline.total_usd:.2f}",
    ]
    if best.total_usd > inline.total_usd:
        reasons.append(
            "total spend rises: this is a quota-arbitrage win, not a dollar win"
        )
    reasons.extend(best.notes)
    return best.plan, tuple(reasons)


__all__ = (
    "CALIBRATION",
    "DEFAULT_CACHE_TTL",
    "DELEGATION_FLOOR_LINES",
    "DELEGATION_MARGIN",
    "SUBAGENT_CACHE_TTL",
    "WORKER_VOLUME_FACTOR",
    "DispatchEstimate",
    "JUDGMENT_LEVELS",
    "ModelPrices",
    "OBJECTIVES",
    "PLAN_NAMES",
    "PlanEstimate",
    "RoleCost",
    "RoleSpec",
    "SPEC_LEVELS",
    "TaskShape",
    "Usage",
    "estimate_dispatch",
)
