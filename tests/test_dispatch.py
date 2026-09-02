from __future__ import annotations

import unittest

from runtime.model_boss.dispatch import (
    DELEGATION_FLOOR_LINES,
    LITE_FLOOR_LINES,
    Budget,
    ModelPrices,
    RoleSpec,
    TaskShape,
    estimate_dispatch,
)


def _weighted(*args, **kwargs):
    kwargs.setdefault("objective", "weighted")
    return estimate_dispatch(*args, **kwargs)


def _role(
    label: str,
    model: str,
    effort: str | None = "high",
    weight: float = 1.0,
    ttl: str = "1h",
) -> RoleSpec:
    return RoleSpec.from_catalog(
        label, "anthropic", model, effort=effort, quota_weight=weight, cache_ttl=ttl
    )


FABLE = _role("main loop", "claude-fable-5-1")
FABLE_LOW = _role("main loop", "claude-fable-5-1", effort="low")
FABLE_MEDIUM = _role("main loop", "claude-fable-5-1", effort="medium")
OPUS_WORKER = _role("worker opus-5-worker", "claude-opus-5", ttl="5m")
SONNET_WORKER = _role("worker sonnet-5", "claude-sonnet-5", ttl="5m")
FABLE_REVIEWER = _role("reviewer fable-5.1", "claude-fable-5-1", ttl="5m")
OPUS_MAIN = _role("main loop", "claude-opus-5")


class TaskShapeTests(unittest.TestCase):
    def test_shape_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            TaskShape(-1, 1)
        with self.assertRaises(ValueError):
            TaskShape(10, 1, judgment="extreme")
        with self.assertRaises(ValueError):
            TaskShape(10, 1, spec="vague")
        with self.assertRaises(ValueError):
            RoleSpec("x", "anthropic", "claude-opus-5", "ultra", 1.0, ModelPrices(1, 1, 1, 1))
        with self.assertRaises(ValueError):
            RoleSpec("x", "anthropic", "claude-opus-5", None, 0.0, ModelPrices(1, 1, 1, 1))
        with self.assertRaises(LookupError):
            RoleSpec.from_catalog("x", "anthropic", "claude-unknown")


class EstimateTests(unittest.TestCase):
    """Cost objectives (`weighted`, `main-model`); pace policy is covered below."""

    def test_anchors_match_the_recorded_fable_5_1_rerun_within_tolerance(self) -> None:
        """benchmarks/fable-effort-dispatch-rerun.json, CLI costUSD per cell."""

        low = _weighted(TaskShape(904, 14, "low"), FABLE_LOW, worker=SONNET_WORKER).plan("inline")
        medium = _weighted(TaskShape(951, 14, "low"), FABLE_MEDIUM, worker=SONNET_WORKER).plan("inline")
        assert low and medium
        self.assertAlmostEqual(low.total_usd, 1.31, delta=0.20)  # measured fable-low-inline
        self.assertAlmostEqual(medium.total_usd, 1.61, delta=0.20)  # measured fable-medium-inline
        self.assertLess(low.total_usd, medium.total_usd)

        # Lite main loop at medium: measured $1.02 (Sonnet worker) and $1.26 (Opus worker);
        # the estimate takes the spec size, so compare at the inline line counts scaled by volume.
        lite_sonnet = _weighted(TaskShape(951, 14, "low"), FABLE_MEDIUM, worker=SONNET_WORKER).plan("lite")
        assert lite_sonnet
        self.assertAlmostEqual(lite_sonnet.roles[0].cost_usd, 1.02, delta=0.25)
        # Worker-side error is the widest: Sonnet 5 churned 1.07M cache reads, Opus 5 wrote 1,859 lines.
        self.assertAlmostEqual(lite_sonnet.roles[1].cost_usd, 0.71, delta=0.32)

        # The historical Fable 5 high anchor still reproduces at effort high.
        high = _weighted(TaskShape(1100, 8, "low"), FABLE, worker=SONNET_WORKER).plan("inline")
        assert high
        self.assertEqual(high.roles[0].usage.output_tokens, 30_940)
        self.assertEqual(high.roles[0].usage.cache_read_tokens, 398_400)

    def test_subagent_ttl_prices_cache_writes_at_the_five_minute_rate(self) -> None:
        one_hour = _role("worker", "claude-sonnet-5", ttl="1h")
        five_minutes = _role("worker", "claude-sonnet-5", ttl="5m")
        self.assertEqual(one_hour.prices.cache_write_usd_per_mtok, 4.0)
        self.assertEqual(five_minutes.prices.cache_write_usd_per_mtok, 2.5)

    def test_measured_directions_hold(self) -> None:
        """The rerun's qualitative results must survive recalibration."""

        task = TaskShape(1000, 14, "low")
        # An Opus worker under a Fable main loop never wins on total spend.
        for main in (FABLE_LOW, FABLE_MEDIUM, FABLE):
            lite = _weighted(task, main, worker=OPUS_WORKER)
            assert lite.plan("lite") and lite.plan("inline")
            self.assertGreater(lite.plan("lite").total_usd, lite.plan("inline").total_usd * 0.9)
        # A Sonnet worker beats Fable at high effort but not Fable at low or medium.
        self.assertEqual(_weighted(task, FABLE, worker=SONNET_WORKER).recommendation, "lite")
        self.assertEqual(_weighted(task, FABLE_LOW, worker=SONNET_WORKER).recommendation, "inline")
        self.assertEqual(_weighted(task, FABLE_MEDIUM, worker=SONNET_WORKER).recommendation, "inline")
        # On the main-model objective a Sonnet worker still cuts Fable spend by roughly a third.
        by_fable = _weighted(task, FABLE_MEDIUM, worker=SONNET_WORKER, objective="main-model")
        self.assertEqual(by_fable.recommendation, "lite")
        lite_plan = by_fable.plan("lite")
        inline_plan = by_fable.plan("inline")
        assert lite_plan and inline_plan
        self.assertLess(lite_plan.main_model_usd, inline_plan.main_model_usd * 0.75)

    def test_small_tasks_stay_inline(self) -> None:
        estimate = _weighted(TaskShape(DELEGATION_FLOOR_LINES - 1, 2), FABLE, worker=OPUS_WORKER)
        self.assertEqual(estimate.recommendation, "inline")
        self.assertIn("delegation floor", estimate.reasons[0])

    def test_judgment_dense_work_stays_inline_regardless_of_size(self) -> None:
        estimate = _weighted(TaskShape(2000, 20, "high"), FABLE, worker=SONNET_WORKER)
        self.assertEqual(estimate.recommendation, "inline")
        self.assertIn("reasoning is the workload", estimate.reasons[0])

    def test_unclear_spec_needs_context(self) -> None:
        estimate = _weighted(TaskShape(900, 9, spec="unclear"), FABLE, worker=SONNET_WORKER)
        self.assertEqual(estimate.recommendation, "needs_context")

    def test_opus_worker_only_pays_when_fable_quota_is_weighted_scarcer(self) -> None:
        task = TaskShape(1100, 8, "low")
        unweighted = _weighted(task, FABLE, worker=OPUS_WORKER)
        self.assertEqual(unweighted.recommendation, "inline")
        scarce_fable = _role("main loop", "claude-fable-5-1", weight=3.0)
        weighted = _weighted(task, scarce_fable, worker=OPUS_WORKER)
        self.assertEqual(weighted.recommendation, "lite")
        main_model = _weighted(task, FABLE, worker=OPUS_WORKER, objective="main-model")
        self.assertEqual(main_model.recommendation, "lite")
        self.assertTrue(any("quota-arbitrage" in reason for reason in main_model.reasons))

    def test_sonnet_worker_wins_on_the_price_proxy_for_large_constructive_work(self) -> None:
        estimate = _weighted(TaskShape(1100, 8, "low"), FABLE, worker=SONNET_WORKER)
        self.assertEqual(estimate.recommendation, "lite")
        lite = estimate.plan("lite")
        inline = estimate.plan("inline")
        assert lite and inline
        self.assertLess(lite.total_usd, inline.total_usd)
        self.assertLess(lite.main_model_usd, inline.main_model_usd)

    def test_lower_main_effort_shrinks_inline_cost_and_favors_inline(self) -> None:
        low = _role("main loop", "claude-fable-5-1", effort="low")
        high = _weighted(TaskShape(600, 6, "low"), FABLE, worker=SONNET_WORKER)
        cheap = _weighted(TaskShape(600, 6, "low"), low, worker=SONNET_WORKER)
        high_inline = high.plan("inline")
        cheap_inline = cheap.plan("inline")
        assert high_inline and cheap_inline
        self.assertLess(cheap_inline.total_usd, high_inline.total_usd)
        self.assertEqual(cheap.recommendation, "inline")

    def test_rework_probability_raises_delegated_cost(self) -> None:
        low = _weighted(TaskShape(800, 8, "low"), FABLE, worker=SONNET_WORKER).plan("lite")
        medium = _weighted(TaskShape(800, 8, "medium"), FABLE, worker=SONNET_WORKER).plan("lite")
        mechanical = _weighted(
            TaskShape(800, 8, "medium", mechanical=True), FABLE, worker=SONNET_WORKER
        ).plan("lite")
        assert low and medium and mechanical
        self.assertLess(low.total_usd, medium.total_usd)
        self.assertLess(mechanical.total_usd, low.total_usd)

    def test_max_is_priced_with_the_reviewer_listed_first(self) -> None:
        estimate = _weighted(TaskShape(1100, 8, "low"), OPUS_MAIN, reviewer=FABLE_REVIEWER)
        max_plan = estimate.plan("max")
        inline = estimate.plan("inline")
        assert max_plan and inline
        self.assertEqual(max_plan.roles[0].role.label, "reviewer fable-5.1")
        # The reviewer's two evidence-only checkpoints stay cheap (BENCHMARKS: $0.38 on Fable 5).
        self.assertLess(max_plan.roles[0].cost_usd, 0.6)
        # main-model spend counts only the Opus main loop, not the Fable reviewer.
        self.assertEqual(max_plan.main_model_usd, max_plan.roles[1].cost_usd)
        self.assertTrue(any("balanced main loop" in note for note in max_plan.notes))
        # Against an Opus main loop already doing the work inline, Max adds spend on both
        # objectives: it is a quality/authority choice, not a quota saving.
        for objective in ("weighted", "main-model"):
            result = _weighted(
                TaskShape(1100, 8, "low"), OPUS_MAIN, reviewer=FABLE_REVIEWER, objective=objective
            )
            self.assertEqual(result.recommendation, "inline", objective)

    def test_same_model_worker_is_flagged(self) -> None:
        estimate = _weighted(TaskShape(900, 9, "low"), FABLE, worker=_role("worker fable-5.1-worker", "claude-fable-5-1", effort="medium"))
        lite = estimate.plan("lite")
        assert lite
        self.assertTrue(any("same model as the main loop" in note for note in lite.notes))

    def test_table_renders_every_plan_and_the_recommendation(self) -> None:
        estimate = _weighted(TaskShape(1100, 8, "low"), FABLE, worker=SONNET_WORKER, reviewer=_role("reviewer opus-5", "claude-opus-5"))
        table = estimate.table()
        for token in ("inline", "lite", "max", "Recommendation (weighted):", "main-model"):
            self.assertIn(token, table)


if __name__ == "__main__":
    unittest.main()


class PacePolicyTests(unittest.TestCase):
    """Default objective: strongest topology within the week's pace."""

    def test_regime_classification(self) -> None:
        self.assertEqual(Budget(40, 40, 40).regime(), "on_pace")
        self.assertEqual(Budget(40, 60, 40).regime(), "fable_ahead")  # capped half burning fast
        self.assertEqual(Budget(60, 70, 40).regime(), "fable_ahead")  # share of spend 58%
        self.assertEqual(Budget(40, 20, 40).regime(), "fable_behind")
        self.assertEqual(Budget(50, 98, 60).regime(), "fable_exhausted")
        self.assertEqual(Budget(100, 50, 90).regime(), "total_exhausted")
        self.assertEqual(Budget(2, 3, 3).regime(), "on_pace")  # too early to judge
        self.assertAlmostEqual(Budget(40, 40, 40).capped_spend_share() or 0, 0.5)
        with self.assertRaises(ValueError):
            Budget(-1, 0, 0)

    def test_execution_heavy_work_goes_lite_by_default(self) -> None:
        estimate = estimate_dispatch(TaskShape(1200, 15, "low"), FABLE_MEDIUM, worker=OPUS_WORKER)
        self.assertEqual(estimate.objective, "pace")
        self.assertEqual(estimate.regime, "unknown")
        self.assertEqual(estimate.recommendation, "lite")
        self.assertTrue(any("50%" in reason for reason in estimate.reasons))

    def test_parallel_packets_go_lite_even_when_small(self) -> None:
        estimate = estimate_dispatch(TaskShape(150, 6, "low", packets=3), FABLE_MEDIUM, worker=OPUS_WORKER)
        self.assertEqual(estimate.recommendation, "lite")
        self.assertTrue(any("3 independent packets" in reason for reason in estimate.reasons))

    def test_the_four_exceptions_stay_inline_or_switch(self) -> None:
        # judgment-dense
        self.assertEqual(
            estimate_dispatch(TaskShape(900, 9, "high"), FABLE_MEDIUM, worker=OPUS_WORKER).recommendation,
            "inline",
        )
        # small single packet
        small = estimate_dispatch(TaskShape(LITE_FLOOR_LINES - 1, 3, "low"), FABLE_MEDIUM, worker=OPUS_WORKER)
        self.assertEqual(small.recommendation, "inline")
        self.assertIn(str(LITE_FLOOR_LINES), small.reasons[0])
        # capped half ahead of pace with a Fable main loop: Lite still, but advised to switch next session
        ahead = estimate_dispatch(
            TaskShape(900, 9, "low"), FABLE_MEDIUM, worker=OPUS_WORKER, budget=Budget(40, 70, 40)
        )
        self.assertEqual(ahead.regime, "fable_ahead")
        self.assertEqual(ahead.recommendation, "lite")
        self.assertTrue(any("next session" in reason for reason in ahead.reasons))
        # capped half exhausted with a Fable main loop
        exhausted = estimate_dispatch(
            TaskShape(900, 9, "low"), FABLE_MEDIUM, worker=OPUS_WORKER, budget=Budget(60, 99, 70)
        )
        self.assertEqual(exhausted.recommendation, "switch-main-loop")

    def test_opus_main_loop_ahead_of_pace_uses_max(self) -> None:
        estimate = estimate_dispatch(
            TaskShape(900, 9, "low"), OPUS_MAIN, reviewer=FABLE_REVIEWER, budget=Budget(40, 70, 40)
        )
        self.assertEqual(estimate.recommendation, "max")

    def test_unclear_spec_and_exhausted_total_need_context(self) -> None:
        self.assertEqual(
            estimate_dispatch(TaskShape(900, 9, spec="unclear"), FABLE_MEDIUM, worker=OPUS_WORKER).recommendation,
            "needs_context",
        )
        self.assertEqual(
            estimate_dispatch(TaskShape(900, 9), FABLE_MEDIUM, worker=OPUS_WORKER, budget=Budget(100, 50, 90)).recommendation,
            "needs_context",
        )

    def test_table_shows_budget_and_regime(self) -> None:
        estimate = estimate_dispatch(
            TaskShape(900, 9, "low", packets=2), FABLE_MEDIUM, worker=OPUS_WORKER, budget=Budget(40, 40, 40)
        )
        table = estimate.table()
        self.assertIn("regime on_pace", table)
        self.assertIn("2 packets", table)
        self.assertIn("Recommendation (pace): lite", table)
