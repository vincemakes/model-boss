from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "boss-dispatch"
SKILL = SKILL_ROOT / "SKILL.md"

APPROVED_STATES = (
    "RESOLVE",
    "PREFLIGHT",
    "CLASSIFY",
    "RECON",
    "DRAFT_PLAN",
    "AUTHORITY_PLAN_CHECK",
    "DISPATCH",
    "GATE",
    "PATCH_AUDIT",
    "MAIN_LOOP_REVIEW",
    "AUTHORITY_FINAL_CHECK",
    "INTEGRATE",
)

STRUCTURED_STATUSES = (
    "ok",
    "needs_context",
    "gate_failed",
    "provider_unavailable",
    "reviewer_unavailable",
    "timeout",
    "scope_violation",
    "transport_error",
    "review_revise",
    "approval_stale",
    "destination_changed",
    "sandbox_unavailable",
)


def _load_skill() -> tuple[dict[str, object], str]:
    text = SKILL.read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if match is None:
        raise AssertionError("SKILL.md must have one YAML frontmatter block")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise AssertionError("skill frontmatter must be a mapping")
    return metadata, match.group(2)


class SkillContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.metadata, cls.body = _load_skill()

    def test_frontmatter_keys_are_exactly_allowlisted(self) -> None:
        # Frontmatter is limited to the trigger pair plus the compatibility note;
        # no other keys are permitted.
        self.assertEqual(set(self.metadata), {"name", "description", "compatibility"})
        self.assertEqual(self.metadata["name"], "boss-dispatch")
        description = self.metadata["description"]
        self.assertIsInstance(description, str)
        self.assertTrue(description.startswith("Use when"))
        self.assertIn("migrate from Token Saver or fable-token-saver", description)
        self.assertLessEqual(len(description), 1024)

    def test_title_and_commands_use_model_boss_identity(self) -> None:
        self.assertRegex(self.body, r"(?m)^# Model Boss$")
        commands = set(re.findall(r"scripts/[a-z0-9-]+\.py", self.body))
        self.assertTrue(commands, "SKILL.md must name its command entry point")
        self.assertEqual(commands, {"scripts/model-boss.py"})

    def test_state_machine_has_exact_order(self) -> None:
        state_line = re.search(
            r"(?m)^RESOLVE(?:\s*->\s*[A-Z_]+)+$", self.body
        )
        self.assertIsNotNone(state_line, "missing executable state-machine line")
        states = tuple(re.findall(r"[A-Z][A-Z_]+", state_line.group(0)))
        self.assertEqual(states, APPROVED_STATES)

    def test_authority_binding_and_host_owned_main_loop(self) -> None:
        normalized = " ".join(self.body.lower().split())
        self.assertIn("host-owned", normalized)
        self.assertRegex(
            normalized,
            r"lite.{0,240}authority_plan_check.{0,240}main loop",
        )
        self.assertRegex(
            normalized,
            r"lite.{0,400}authority_final_check.{0,240}main loop",
        )
        self.assertRegex(
            normalized,
            r"max.{0,300}distinct canonical fingerprint",
        )

    def test_integration_requires_current_three_hash_approval(self) -> None:
        for field in (
            "source_snapshot_hash",
            "worker_delta_hash",
            "projected_task_patch_hash",
        ):
            self.assertIn(field, self.body)
        self.assertRegex(
            " ".join(self.body.lower().split()),
            r"integrat.{0,300}(current|fresh).{0,120}three-hash",
        )

    def test_revision_ceiling_is_exact(self) -> None:
        normalized = " ".join(self.body.lower().split())
        self.assertRegex(normalized, r"exactly two revision rounds")
        self.assertRegex(
            normalized,
            r"third [`']?revise[`']?.{0,160}review_revise",
        )
        self.assertRegex(normalized, r"review_revise.{0,160}never.{0,80}integrate")

    def test_all_structured_statuses_are_named(self) -> None:
        for status in STRUCTURED_STATUSES:
            with self.subTest(status=status):
                self.assertRegex(self.body, rf"`{re.escape(status)}`")

    def test_core_transition_contract_is_provider_neutral(self) -> None:
        match = re.search(
            r"## Unified state machine\n(.*?)(?=\n## )", self.body, re.DOTALL
        )
        self.assertIsNotNone(match, "missing Unified state machine section")
        transition_contract = match.group(1)
        for brand in ("Fable", "Sol", "Opus", "Kimi", "Claude", "Codex", "GLM"):
            with self.subTest(brand=brand):
                self.assertNotRegex(transition_contract, rf"(?i)\b{brand}\b")

    def test_external_bypass_requires_sealed_one_shot_entry(self) -> None:
        normalized = " ".join(self.body.lower().split())
        self.assertRegex(
            normalized,
            r"external adapter.{0,180}bypass.{0,220}sealed one-shot worker",
        )
        self.assertRegex(
            normalized,
            r"never.{0,100}(raw|direct).{0,100}bypass.{0,180}source repository",
        )

    def test_authority_mode_is_sealed_and_cannot_change_mid_invocation(self) -> None:
        normalized = " ".join(self.body.lower().split())
        self.assertIn("authority_mode", normalized)
        self.assertRegex(
            normalized,
            r"authority_mode.{0,240}(sealed|bundle).{0,240}(cannot|never).{0,120}(switch|change|downgrade)",
        )

    def test_every_relative_markdown_link_resolves(self) -> None:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", self.body):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                continue
            path_text = target.split("#", 1)[0]
            if not path_text:
                continue
            with self.subTest(target=target):
                self.assertTrue((SKILL_ROOT / path_text).is_file())

    def test_generated_openai_interface_is_narrow(self) -> None:
        metadata = yaml.safe_load(
            (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(set(metadata), {"interface"})
        interface = metadata["interface"]
        self.assertEqual(
            set(interface), {"display_name", "short_description", "default_prompt"}
        )
        self.assertEqual(interface["display_name"], "Model Boss")
        self.assertIn("$model-boss", interface["default_prompt"])

    def test_sealed_cli_documents_installed_root_and_max_plan_order(self) -> None:
        external = (SKILL_ROOT / "references" / "adapters" / "external-cli.md").read_text(
            encoding="utf-8"
        )
        protocol = (SKILL_ROOT / "references" / "protocol.md").read_text(encoding="utf-8")
        for label, text in (("skill", self.body), ("external", external)):
            with self.subTest(label=label):
                self.assertNotIn("python3 scripts/model-boss.py", text)
                self.assertIn("<model-boss-skill-root>/scripts/model-boss.py", text)
                normalized = " ".join(text.lower().split())
                self.assertRegex(
                    normalized,
                    r"directory containing [`']?skill\.md[`']?.{0,240}model-boss-skill-root",
                )
                plan = normalized.index("plan-review")
                worker = normalized.index("worker --manifest", plan)
                final = normalized.index("review", worker)
                integrate = normalized.index("integrate", final)
                self.assertLess(plan, worker)
                self.assertLess(worker, final)
                self.assertLess(final, integrate)
        combined = " ".join((self.body + external + protocol).lower().split())
        self.assertRegex(
            combined,
            r"optional worker.{0,260}host-native orchestration",
        )
        self.assertRegex(
            combined,
            r"sealed external cli.{0,220}requires.{0,120}worker invocation",
        )
        self.assertRegex(combined, r"verdict.{0,120}approve.{0,80}revise")
        self.assertNotRegex(
            combined,
            r"`approve`\s*,?\s*`revise`\s*,?\s*(?:or|and)\s*`needs_context`",
        )

    def test_reviewer_continuity_is_effective_identity_not_profile_path(self) -> None:
        documents = (
            self.body,
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "README.zh-CN.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references" / "protocol.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references" / "adapters" / "external-cli.md").read_text(
                encoding="utf-8"
            ),
        )
        combined = " ".join(" ".join(document.lower().split()) for document in documents)
        self.assertNotRegex(combined, r"same[- ]profile|同一(?:个)?\s*profile|相同\s*profile")
        self.assertRegex(
            combined,
            r"same effective reviewer (?:identity|configuration)|相同的?有效评审(?:身份|配置)",
        )


if __name__ == "__main__":
    unittest.main()
