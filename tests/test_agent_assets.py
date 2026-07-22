from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "assets" / "agents"
ROLES = ("reviewer", "implementer", "mechanic", "scout")

CLAUDE_MODELS = {
    "reviewer": "fable",
    "implementer": "sonnet",
    "mechanic": "haiku",
    "scout": "haiku",
}

CODEX_MODELS = {
    "reviewer": ("gpt-5.6-sol", "high", "read-only"),
    "implementer": ("gpt-5.6-terra", "medium", "workspace-write"),
    "mechanic": ("gpt-5.6-luna", "low", "workspace-write"),
    "scout": ("gpt-5.6-luna", "low", "read-only"),
}

CODEX_KEYS = {
    "name",
    "description",
    "model",
    "model_reasoning_effort",
    "sandbox_mode",
    "developer_instructions",
}


def _claude_file(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"{path} needs YAML frontmatter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise AssertionError(f"{path} frontmatter must be a mapping")
    return metadata, match.group(2)


class AgentAssetTests(unittest.TestCase):
    def test_each_adapter_has_exact_role_filenames(self) -> None:
        expected = {
            "prompts": {f"{role}.md" for role in ROLES},
            "claude-code": {f"{role}.md" for role in ROLES},
            "codex": {f"{role}.toml" for role in ROLES},
        }
        for directory, filenames in expected.items():
            with self.subTest(directory=directory):
                actual = {
                    path.name
                    for path in (AGENTS / directory).iterdir()
                    if path.is_file()
                }
                self.assertEqual(actual, filenames)

    def test_generic_prompts_are_model_independent(self) -> None:
        brands = re.compile(
            r"(?i)\b(fable|opus|sonnet|haiku|claude|codex|openai|anthropic|kimi|glm|sol|terra|luna)\b"
        )
        for role in ROLES:
            text = (AGENTS / "prompts" / f"{role}.md").read_text(encoding="utf-8")
            with self.subTest(role=role):
                self.assertFalse(text.startswith("---"))
                self.assertIsNone(brands.search(text))

    def test_claude_assets_have_narrow_frontmatter_and_profile_label(self) -> None:
        names: set[str] = set()
        for role in ROLES:
            metadata, body = _claude_file(AGENTS / "claude-code" / f"{role}.md")
            with self.subTest(role=role):
                self.assertEqual(set(metadata), {"name", "description", "model"})
                self.assertEqual(metadata["model"], CLAUDE_MODELS[role])
                self.assertIn("Token Saver default Anthropic profile", metadata["description"])
                self.assertIn("host main loop remains inherited", body.lower())
                names.add(str(metadata["name"]))
        self.assertEqual(len(names), len(ROLES))

    def test_codex_assets_have_exact_fields_and_defaults(self) -> None:
        names: set[str] = set()
        for role in ROLES:
            path = AGENTS / "codex" / f"{role}.toml"
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            model, effort, sandbox = CODEX_MODELS[role]
            with self.subTest(role=role):
                self.assertEqual(set(data), CODEX_KEYS)
                self.assertEqual(data["model"], model)
                self.assertEqual(data["model_reasoning_effort"], effort)
                self.assertEqual(data["sandbox_mode"], sandbox)
                self.assertIn("host main loop remains inherited", data["developer_instructions"].lower())
                names.add(data["name"])
        self.assertEqual(len(names), len(ROLES))

    def test_codex_assets_match_openai_profile_routes_and_preferences(self) -> None:
        profile = json.loads(
            (ROOT / "references" / "profiles" / "openai.json").read_text(
                encoding="utf-8"
            )
        )
        expected = {
            "reviewer": ("gpt-5.6-sol", "reviewer", "reviewers"),
            "implementer": ("gpt-5.6-terra", "worker", "workers"),
            "mechanic": ("gpt-5.6-luna", "mechanic", "mechanics"),
            "scout": ("gpt-5.6-luna", "scout", "scouts"),
        }
        for asset_role, (route_id, route_role, preference) in expected.items():
            asset = tomllib.loads(
                (AGENTS / "codex" / f"{asset_role}.toml").read_text(
                    encoding="utf-8"
                )
            )
            route = profile["routes"][route_id]
            with self.subTest(asset_role=asset_role):
                self.assertEqual(asset["model"], route["model"])
                self.assertEqual(asset["model_reasoning_effort"], route["variant"])
                self.assertIn(route_role, route["roles"])
                self.assertIn(route_id, profile["preferences"][preference])

        luna = profile["routes"]["gpt-5.6-luna"]
        self.assertEqual(set(luna["roles"]), {"worker", "mechanic", "scout"})
        self.assertIn("gpt-5.6-luna", profile["preferences"]["workers"])

    def test_reviewer_assets_are_evidence_only(self) -> None:
        generic = (AGENTS / "prompts" / "reviewer.md").read_text(encoding="utf-8")
        _, claude = _claude_file(AGENTS / "claude-code" / "reviewer.md")
        codex = tomllib.loads(
            (AGENTS / "codex" / "reviewer.toml").read_text(encoding="utf-8")
        )
        for label, text in (
            ("generic", generic),
            ("claude", claude),
            ("codex", codex["developer_instructions"]),
        ):
            with self.subTest(label=label):
                self.assertIn("source_snapshot_hash", text)
                self.assertIn("worker_delta_hash", text)
                self.assertIn("projected_task_patch_hash", text)
                self.assertRegex(text.lower(), r"never (implement|write code)")
        self.assertEqual(codex["model"], "gpt-5.6-sol")
        self.assertEqual(codex["sandbox_mode"], "read-only")
        self.assertIn(
            "runtime preflight must verify effective read-only permissions",
            codex["description"],
        )

    def test_worker_mechanic_and_scout_contracts(self) -> None:
        for role in ("implementer", "mechanic"):
            for kind, suffix in (("prompts", "md"), ("claude-code", "md"), ("codex", "toml")):
                path = AGENTS / kind / f"{role}.{suffix}"
                if kind == "prompts":
                    text = path.read_text(encoding="utf-8")
                elif kind == "claude-code":
                    _, text = _claude_file(path)
                else:
                    text = tomllib.loads(path.read_text(encoding="utf-8"))["developer_instructions"]
                with self.subTest(role=role, kind=kind):
                    self.assertIn("allowed paths", text.lower())
                    self.assertIn("gate", text.lower())

        for kind, suffix in (("prompts", "md"), ("claude-code", "md"), ("codex", "toml")):
            path = AGENTS / kind / f"scout.{suffix}"
            if kind == "prompts":
                text = path.read_text(encoding="utf-8")
            elif kind == "claude-code":
                _, text = _claude_file(path)
            else:
                text = tomllib.loads(path.read_text(encoding="utf-8"))["developer_instructions"]
            with self.subTest(kind=kind):
                self.assertRegex(text.lower(), r"(read-only|never write)")

    def test_legacy_flat_assets_are_absent(self) -> None:
        for filename in ("consultant.md", "implementer.md", "mechanic.md", "scout.md"):
            with self.subTest(filename=filename):
                self.assertFalse((AGENTS / filename).exists())


if __name__ == "__main__":
    unittest.main()
