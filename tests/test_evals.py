from __future__ import annotations

import hashlib
import json
import re
import statistics
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_OUTCOMES = {
    "authority-main-lite": ("lite", "ok"),
    "balanced-main-distinct-reviewer-max": ("max", "ok"),
    "alias-collision": ("max", "reviewer_unavailable"),
    "reviewer-unavailable": ("max", "reviewer_unavailable"),
    "revise-loop": ("max", "ok"),
    "revision-limit": ("max", "review_revise"),
    "approval-stale": ("max", "approval_stale"),
    "sandbox-unavailable": ("max", "sandbox_unavailable"),
}

INPUT_KEYS = {"host", "main_loop", "explicit_mode", "routes", "events"}
EXPECTED_KEYS = {"mode", "status", "authority", "worker", "states", "evidence"}

FORMER_BRAND = re.compile(r"token[-_ ]saver|fable-token-saver", re.IGNORECASE)
REPORT_NUMBER = re.compile(
    r"(?<![\w])[-+−]?\$?\d[\d,.]*(?:%|[kM×s])?(?![\w])"
)

RECORDED_MEASUREMENT_SHA256 = (
    "20acbf80a22c62e694acb3476a046198aec65ae877536be655a51d40e7587c4c"
)
RECORDED_REPORT_NUMBERS_SHA256 = (
    "cf1544b91c2612623c6dedbe91d0dc7184d364b05eca913a863e2f371785d728"
)

ACTIVE_LABELS = {
    "BENCHMARKS.md": "# Model Boss benchmarks",
    "BENCHMARKS.zh-CN.md": "# Model Boss benchmarks（实测数据）",
    "benchmarks/benchmark.md": "# Model Boss benchmark",
    "evals/skill-pressure-results.md": "# Model Boss skill pressure results",
    "evals/skill-pressure-scenarios.md": "# Model Boss skill pressure scenarios",
}


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _recorded_measurement_projection(benchmark: dict[str, object]) -> dict[str, object]:
    runs = benchmark["runs"]
    assert isinstance(runs, list)
    return {
        "runs": [
            {
                "eval_id": run["eval_id"],
                "configuration": run["configuration"],
                "run_number": run["run_number"],
                "result": run["result"],
            }
            for run in runs
        ],
        "run_summary": benchmark["run_summary"],
    }


def _report_number_projection() -> dict[str, list[str]]:
    paths = (
        "BENCHMARKS.md",
        "BENCHMARKS.zh-CN.md",
        "benchmarks/benchmark.md",
    )
    return {
        relative: REPORT_NUMBER.findall((ROOT / relative).read_text(encoding="utf-8"))
        for relative in paths
    }


class EvaluationBrandingTests(unittest.TestCase):
    def test_active_evaluation_labels_use_model_boss(self) -> None:
        for relative, expected_heading in ACTIVE_LABELS.items():
            with self.subTest(path=relative):
                first_line = (ROOT / relative).read_text(encoding="utf-8").splitlines()[0]
                self.assertEqual(first_line, expected_heading)

        benchmark = json.loads(
            (ROOT / "benchmarks" / "benchmark.json").read_text(encoding="utf-8")
        )
        evals = json.loads(
            (ROOT / "evals" / "evals.json").read_text(encoding="utf-8")
        )
        self.assertEqual(evals["skill_name"], "model-boss")

    def test_benchmark_identifies_neutral_predecessor_provenance(self) -> None:
        benchmark = json.loads(
            (ROOT / "benchmarks" / "benchmark.json").read_text(encoding="utf-8")
        )
        metadata = benchmark["metadata"]
        self.assertEqual(metadata["skill_name"], "historical-predecessor")
        self.assertEqual(
            metadata["skill_path"], "<historical-predecessor-skill-root>"
        )

        english = (ROOT / "BENCHMARKS.md").read_text(encoding="utf-8")
        chinese = (ROOT / "BENCHMARKS.zh-CN.md").read_text(encoding="utf-8")
        report = (ROOT / "benchmarks" / "benchmark.md").read_text(encoding="utf-8")
        self.assertIn("predecessor measurements inherited by Model Boss", english)
        self.assertIn("Model Boss 继承的前身测量", chinese)
        self.assertIn("predecessor measurements inherited by Model Boss", report)
        self.assertIn("With predecessor skill", report)
        self.assertIn("Without predecessor skill", report)
        self.assertIn("Predecessor result: small tasks", english)
        self.assertIn("前身结果:小任务", chinese)
        self.assertNotIn("With-Model-Boss prompts", english)
        self.assertNotIn("Small tasks: Model Boss makes", english)
        self.assertNotIn("with/without Model Boss", english)
        self.assertNotIn("With Model Boss", report)
        self.assertNotIn("Without Model Boss", report)

        evals = json.loads(
            (ROOT / "evals" / "evals.json").read_text(encoding="utf-8")
        )
        self.assertIn("historical predecessor skill", evals["note"])
        self.assertIn("not rerun for Model Boss", evals["note"])
        self.assertNotIn("with Model Boss / without Model Boss", evals["note"])

    def test_historical_evidence_paths_use_a_documented_neutral_placeholder(self) -> None:
        benchmark = json.loads(
            (ROOT / "benchmarks" / "benchmark.json").read_text(encoding="utf-8")
        )
        typecheck_evidence = [
            expectation["evidence"]
            for run in benchmark["runs"]
            for expectation in run["expectations"]
            if expectation["text"] == "gate-typecheck-passes"
            and "taskboard-fixture@ typecheck" in expectation["evidence"]
        ]

        self.assertEqual(len(typecheck_evidence), 6)
        for evidence in typecheck_evidence:
            with self.subTest(evidence=evidence):
                self.assertIn("<historical-workspace>/iteration-1/", evidence)
                self.assertNotIn("model-boss-workspace", evidence)

        english = (ROOT / "BENCHMARKS.md").read_text(encoding="utf-8")
        chinese = (ROOT / "BENCHMARKS.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn("normalized to `<historical-workspace>`", english)
        self.assertIn("归一化为 `<historical-workspace>`", chinese)
        self.assertIn("predecessor identity", english)
        self.assertIn("前身身份", chinese)

    def test_record_shape_and_summary_match_single_observations_across_tasks(self) -> None:
        benchmark = json.loads(
            (ROOT / "benchmarks" / "benchmark.json").read_text(encoding="utf-8")
        )
        metadata = benchmark["metadata"]
        runs = benchmark["runs"]
        self.assertEqual(metadata["runs_per_configuration"], 1)
        self.assertEqual(len(runs), 8)
        self.assertEqual({run["run_number"] for run in runs}, {1})
        self.assertEqual(
            Counter((run["eval_id"], run["configuration"]) for run in runs),
            Counter(
                (eval_id, configuration)
                for eval_id in metadata["evals_run"]
                for configuration in ("with_skill", "without_skill")
            ),
        )

        for configuration in ("with_skill", "without_skill"):
            selected = [
                run["result"]
                for run in runs
                if run["configuration"] == configuration
            ]
            for metric in ("pass_rate", "time_seconds", "tokens"):
                values = [result[metric] for result in selected]
                summary = benchmark["run_summary"][configuration][metric]
                self.assertEqual(summary["mean"], statistics.mean(values))
                self.assertEqual(summary["stddev"], round(statistics.stdev(values), 4))
                self.assertEqual(summary["min"], min(values))
                self.assertEqual(summary["max"], max(values))

        report = (ROOT / "benchmarks" / "benchmark.md").read_text(encoding="utf-8")
        english = (ROOT / "BENCHMARKS.md").read_text(encoding="utf-8")
        chinese = (ROOT / "BENCHMARKS.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn("(1 run each per configuration)", report)
        self.assertIn("dispersion across four distinct tasks", report)
        self.assertIn("dispersion across four distinct tasks", english)
        self.assertIn("四项不同任务之间的离散程度", chinese)
        for text in (report, english):
            self.assertIn("not repeated-trial", text)

    def test_recorded_measurement_projection_is_frozen(self) -> None:
        benchmark = json.loads(
            (ROOT / "benchmarks" / "benchmark.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            _canonical_sha256(_recorded_measurement_projection(benchmark)),
            RECORDED_MEASUREMENT_SHA256,
        )
        self.assertEqual(
            _canonical_sha256(_report_number_projection()),
            RECORDED_REPORT_NUMBERS_SHA256,
        )

    def test_pressure_artifacts_are_explicitly_historical_and_normalized(self) -> None:
        scenarios = (ROOT / "evals" / "skill-pressure-scenarios.md").read_text(
            encoding="utf-8"
        )
        results = (ROOT / "evals" / "skill-pressure-results.md").read_text(
            encoding="utf-8"
        )
        for text in (scenarios, results):
            self.assertIn("were not rerun for Model Boss", text)
            self.assertIn("<historical-predecessor-skill>", text)
            self.assertNotIn("/model-boss", text)
        self.assertIn(
            '"<historical-predecessor-skill> lite. Refactor', scenarios
        )
        self.assertIn("only the predecessor command token", scenarios)
        self.assertIn("only the predecessor command token", results)

    def test_every_positive_trigger_names_model_boss(self) -> None:
        triggers = json.loads(
            (ROOT / "benchmarks" / "trigger-eval.json").read_text(encoding="utf-8")
        )
        positive = [case for case in triggers if case["should_trigger"]]
        self.assertTrue(positive)
        for case in positive:
            with self.subTest(query=case["query"]):
                self.assertIn("model boss", case["query"].casefold())

    def test_exactly_two_migration_prompts_recognize_former_names(self) -> None:
        data = json.loads(
            (ROOT / "evals" / "evals.json").read_text(encoding="utf-8")
        )
        migration_evals = [
            case for case in data["evals"] if FORMER_BRAND.search(case["prompt"])
        ]

        self.assertEqual(
            [case["name"] for case in migration_evals],
            ["migration-from-legacy-product", "migration-from-legacy-repository"],
        )
        self.assertIn("Token Saver", migration_evals[0]["prompt"])
        self.assertIn("fable-token-saver", migration_evals[1]["prompt"])
        for case in migration_evals:
            with self.subTest(case=case["name"]):
                self.assertIn("model boss", case["prompt"].casefold())


class RoutingEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = json.loads(
            (ROOT / "evals" / "routing-evals.json").read_text(encoding="utf-8")
        )
        cls.cases = cls.data["cases"]
        cls.by_id = {case["id"]: case for case in cls.cases}

    def test_top_level_shape_and_unique_ids(self) -> None:
        self.assertEqual(set(self.data), {"version", "cases"})
        self.assertEqual(self.data["version"], 1)
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), set(REQUIRED_OUTCOMES))

    def test_case_shapes_and_fingerprints(self) -> None:
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertEqual(set(case), {"id", "input", "expected"})
                self.assertEqual(set(case["input"]), INPUT_KEYS)
                self.assertEqual(set(case["expected"]), EXPECTED_KEYS)
                self.assertEqual(len(case["input"]["main_loop"].split(":")), 3)
                self.assertIsInstance(case["expected"]["evidence"], dict)
                self.assertTrue(case["expected"]["evidence"])

    def test_required_mode_and_status_outcomes(self) -> None:
        for case_id, expected in REQUIRED_OUTCOMES.items():
            case = self.by_id[case_id]["expected"]
            with self.subTest(case=case_id):
                self.assertEqual((case["mode"], case["status"]), expected)

    def test_lite_authority_is_inline(self) -> None:
        self.assertEqual(
            self.by_id["authority-main-lite"]["expected"]["authority"], "inline"
        )

    def test_successful_max_checkpoint_order(self) -> None:
        for case_id in (
            "balanced-main-distinct-reviewer-max",
            "revise-loop",
        ):
            states = self.by_id[case_id]["expected"]["states"]
            with self.subTest(case=case_id):
                self.assertLess(states.index("AUTHORITY_PLAN_CHECK"), states.index("DISPATCH"))
                self.assertLess(
                    len(states) - 1 - states[::-1].index("AUTHORITY_FINAL_CHECK"),
                    states.index("INTEGRATE"),
                )

    def test_revise_loop_repeats_exactly_twice_then_integrates(self) -> None:
        case = self.by_id["revise-loop"]
        self.assertEqual(case["input"]["events"].count("revise"), 2)
        states = case["expected"]["states"]
        for state in (
            "DISPATCH",
            "GATE",
            "PATCH_AUDIT",
            "MAIN_LOOP_REVIEW",
            "AUTHORITY_FINAL_CHECK",
        ):
            self.assertEqual(states.count(state), 3)
        self.assertEqual(states[-1], "INTEGRATE")

    def test_revision_limit_stops_on_third_revise(self) -> None:
        case = self.by_id["revision-limit"]
        self.assertEqual(case["input"]["events"].count("revise"), 3)
        self.assertEqual(case["expected"]["status"], "review_revise")
        self.assertNotIn("INTEGRATE", case["expected"]["states"])
        self.assertIn("revision_rounds", case["expected"]["evidence"])

    def test_blocking_cases_never_integrate(self) -> None:
        for case_id in (
            "alias-collision",
            "reviewer-unavailable",
            "revision-limit",
            "approval-stale",
            "sandbox-unavailable",
        ):
            with self.subTest(case=case_id):
                self.assertNotIn("INTEGRATE", self.by_id[case_id]["expected"]["states"])


if __name__ == "__main__":
    unittest.main()
