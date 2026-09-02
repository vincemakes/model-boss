from __future__ import annotations

import unittest

from runtime.model_boss.catalog import (
    ANTHROPIC_MODELS,
    CATALOG_SNAPSHOT_DATE,
    effort_is_valid_for,
    find_model,
    match_model_mentions,
    normalize_mention,
    supported_effort_levels,
)
from runtime.model_boss.config import load_config
from runtime.model_boss.models import EFFORT_LEVELS


HOST_PICKER = (
    "claude-fable-5-1",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
)


class CatalogTests(unittest.TestCase):
    def test_catalog_covers_exactly_the_host_picker(self) -> None:
        self.assertEqual({model.model_id for model in ANTHROPIC_MODELS}, set(HOST_PICKER))
        self.assertRegex(CATALOG_SNAPSHOT_DATE, r"^\d{4}-\d{2}-\d{2}$")

    def test_effort_levels_follow_the_model_generation(self) -> None:
        self.assertEqual(supported_effort_levels("anthropic", "claude-fable-5-1"), EFFORT_LEVELS)
        self.assertEqual(supported_effort_levels("anthropic", "claude-opus-5"), EFFORT_LEVELS)
        self.assertEqual(
            supported_effort_levels("anthropic", "claude-opus-4-6"),
            ("low", "medium", "high", "max"),
        )
        self.assertEqual(
            supported_effort_levels("anthropic", "claude-sonnet-4-6"),
            ("low", "medium", "high", "max"),
        )
        self.assertEqual(supported_effort_levels("anthropic", "claude-haiku-4-5"), ())
        self.assertIsNone(supported_effort_levels("anthropic", "claude-future"))
        self.assertIsNone(supported_effort_levels("openai", "gpt-5.6-sol"))

    def test_effort_validity_helper(self) -> None:
        self.assertTrue(effort_is_valid_for("anthropic", "claude-opus-4-6", "max"))
        self.assertFalse(effort_is_valid_for("anthropic", "claude-opus-4-6", "xhigh"))
        self.assertFalse(effort_is_valid_for("anthropic", "claude-haiku-4-5", "low"))
        self.assertTrue(effort_is_valid_for("anthropic", "claude-haiku-4-5", None))
        self.assertTrue(effort_is_valid_for("openai", "gpt-5.6-sol", "xhigh"))
        self.assertFalse(effort_is_valid_for("anthropic", "claude-opus-5", "ultra"))

    def test_prices_and_cache_facts_match_the_published_tables(self) -> None:
        fable = find_model("anthropic", "claude-fable-5-1")
        opus = find_model("anthropic", "claude-opus-5")
        opus46 = find_model("anthropic", "claude-opus-4-6")
        assert fable and opus and opus46
        self.assertEqual((fable.input_usd_per_mtok, fable.output_usd_per_mtok), (10.0, 50.0))
        self.assertEqual(fable.cache_read_usd_per_mtok, 0.25)
        self.assertEqual(opus.cache_read_usd_per_mtok, 0.5)
        self.assertEqual(fable.cache_write_usd_per_mtok("1h"), 20.0)
        self.assertEqual(fable.cache_write_usd_per_mtok("5m"), 12.5)
        self.assertEqual(fable.min_cache_prefix_tokens, 512)
        self.assertEqual(opus46.min_cache_prefix_tokens, 4096)
        self.assertIsNone(find_model("anthropic", "claude-opus-9"))
        self.assertIs(find_model("Anthropic", " CLAUDE-OPUS-5 "), opus)

    def test_normalization_ignores_case_spacing_and_punctuation(self) -> None:
        self.assertEqual(normalize_mention("Opus 4.6"), "opus46")
        self.assertEqual(normalize_mention("claude-opus-4-6"), "claudeopus46")
        self.assertEqual(normalize_mention("让 Fable 审"), "让fable审")
        self.assertEqual(normalize_mention("Opus_5, please"), "opus5please")


class MentionMatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.routes = load_config("anthropic", discover=False).routes

    def _match(self, text: str):
        return match_model_mentions(text, self.routes)

    def test_bare_family_name_resolves_to_the_newest_route(self) -> None:
        report = self._match("走 model boss,让 opus 去开发,你来审核")
        self.assertEqual(report.unmatched, ())
        self.assertEqual([m.model_id for m in report.mentions], ["claude-opus-5"])
        self.assertEqual(report.mentions[0].route_ids, ("opus-5", "opus-5-worker"))

    def test_versioned_name_beats_the_bare_family_name(self) -> None:
        report = self._match("让 opus 4.6 去开发, fable 审计划")
        self.assertEqual(
            [(m.alias, m.model_id) for m in report.mentions],
            [("opus-4.6", "claude-opus-4-6"), ("fable", "claude-fable-5-1")],
        )
        self.assertEqual(report.route_ids, ("opus-4.6", "opus-4.6-worker", "fable-5.1", "fable-5.1-worker"))

    def test_exact_model_ids_and_route_ids_match(self) -> None:
        report = self._match("use claude-sonnet-4-6 and the haiku-4.5 scout")
        self.assertEqual([m.model_id for m in report.mentions], ["claude-sonnet-4-6", "claude-haiku-4-5"])
        self.assertEqual(report.mentions[1].route_ids, ("haiku-4.5",))

    def test_unknown_version_is_reported_not_guessed(self) -> None:
        report = self._match("让 fable 6 来审核")
        self.assertEqual(report.mentions, ())
        self.assertEqual(report.unmatched, ("fable6",))
        report = self._match("opus 4.9 should implement")
        self.assertEqual(report.mentions, ())
        self.assertEqual(report.unmatched, ("opus49",))

    def test_no_model_mentions_yield_an_empty_report(self) -> None:
        report = self._match("refactor the retry logic in src/http")
        self.assertEqual(report.mentions, ())
        self.assertEqual(report.unmatched, ())

    def test_custom_aliases_from_configuration_match(self) -> None:
        loaded = load_config(
            "anthropic",
            discover=False,
            user_config={
                "schema_version": 1,
                "routes": {
                    "cheap": {
                        "transport": "host-subagent",
                        "band": "balanced",
                        "roles": ["worker"],
                        "read_only": False,
                        "model": "claude-sonnet-5",
                        "provider_family": "anthropic",
                        "variant": "default",
                        "aliases": ["the cheap one", "便宜模型"],
                    }
                },
            },
        )
        report = match_model_mentions("让便宜模型写", loaded.routes)
        self.assertEqual([m.route_ids for m in report.mentions], [("cheap",)])


if __name__ == "__main__":
    unittest.main()
