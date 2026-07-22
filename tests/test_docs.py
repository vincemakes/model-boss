from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

EN_HEADINGS = (
    "Token Saver",
    "Should you use it?",
    "Lite and Max at a glance",
    "The main loop is already selected",
    "How the shared state machine works",
    "Model profiles, not model lock-in",
    "Claude Code setup",
    "Codex setup",
    "Kimi and GLM external routes",
    "Safety and failure behavior",
    "Reference benchmark snapshot",
    "When Token Saver steps aside",
    "License",
)

ZH_HEADINGS = (
    "Token Saver",
    "你是否应该使用它？",
    "Lite 与 Max 一览",
    "主循环已经选定",
    "共享状态机如何工作",
    "模型 Profile，而非模型锁定",
    "Claude Code 安装",
    "Codex 安装",
    "Kimi 与 GLM 外部路由",
    "安全与失败行为",
    "参考基准快照",
    "Token Saver 何时让开",
    "许可证",
)


def _headings(text: str) -> tuple[str, ...]:
    return tuple(
        match.group(2).strip()
        for match in re.finditer(r"^(#{1,2})\s+(.+?)\s*$", text, re.MULTILINE)
    )


class DocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.en = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        cls.bench_en = (ROOT / "BENCHMARKS.md").read_text(encoding="utf-8")
        cls.bench_zh = (ROOT / "BENCHMARKS.zh-CN.md").read_text(encoding="utf-8")
        cls.devnotes = (ROOT / "docs" / "DEVNOTES.zh-CN.md").read_text(
            encoding="utf-8"
        )
        cls.plan = (
            ROOT
            / "docs"
            / "superpowers"
            / "plans"
            / "2026-07-21-token-saver-cross-platform.md"
        ).read_text(encoding="utf-8")
        cls.schema = json.loads(
            (ROOT / "config" / "token-saver.schema.json").read_text(
                encoding="utf-8"
            )
        )

    def test_readmes_have_synchronized_information_architecture(self) -> None:
        self.assertEqual(_headings(self.en), EN_HEADINGS)
        self.assertEqual(_headings(self.zh), ZH_HEADINGS)
        self.assertEqual(len(_headings(self.en)), len(_headings(self.zh)))

    def test_canonical_identity_and_inherited_main_loop(self) -> None:
        canonical = "https://github.com/vincemakes/token-saver"
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                self.assertIn(canonical, text)
                self.assertNotIn("vincemakes/fable-token-saver", text)
                self.assertIn("main loop", text.lower())
        self.assertRegex(
            self.en,
            r"(?i)main loop.{0,160}(already selected|immutable|never replaces)",
        )
        self.assertRegex(self.zh, r"主循环.{0,120}(已经选定|不可变|不会替换)")

    def test_published_schema_uses_the_canonical_repository(self) -> None:
        self.assertEqual(
            self.schema["$id"],
            "https://github.com/vincemakes/token-saver/config/token-saver.schema.json",
        )

    def test_plan_maps_and_packages_the_sealed_bundle_module(self) -> None:
        self.assertGreaterEqual(
            self.plan.count("runtime/token_saver/bundle.py"),
            2,
        )

    def test_lite_and_max_topologies_include_optional_third_level(self) -> None:
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                self.assertIn("Lite", text)
                self.assertIn("Max", text)
                self.assertRegex(text, r"AUTHORITY_PLAN_CHECK.*AUTHORITY_FINAL_CHECK")
        self.assertRegex(self.en, r"(?is)Lite.{0,500}main loop.{0,500}worker")
        self.assertRegex(self.en, r"(?is)Max.{0,700}authority reviewer.{0,700}optional")
        self.assertRegex(self.zh, r"(?s)Lite.{0,500}主循环.{0,500}(Worker|执行模型)")
        self.assertRegex(self.zh, r"(?s)Max.{0,700}(权威|评审模型).{0,700}可选")

    def test_exact_install_destinations_cover_both_hosts_and_shells(self) -> None:
        required = (
            ".claude/skills/token-saver",
            ".claude/agents",
            ".agents/skills/token-saver",
            ".codex/agents",
            "$HOME/.claude/skills/token-saver",
            "$HOME/.agents/skills/token-saver",
            "$HOME/.codex/agents",
            "powershell",
        )
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                for fragment in required:
                    self.assertIn(fragment, text)
                self.assertGreaterEqual(text.count("git clone https://github.com/vincemakes/token-saver.git"), 8)

    def test_codex_capability_preflight_does_not_auto_upgrade(self) -> None:
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                self.assertIn("codex --version", text)
                self.assertNotIn("0.144.0", text)
                self.assertRegex(text, r"(?i)(catalog|availability|available|模型目录|可用性|可用)")
                self.assertRegex(text, r"(?i)(preflight|预检)")
                self.assertNotRegex(text, r"(?i)(npm|brew|pnpm).{0,40}(upgrade|update|@openai/codex)")

    def test_wrapper_install_examples_require_an_explicit_destination(self) -> None:
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                self.assertIn(
                    'scripts/setup-model-providers.sh --install-path "$HOME/.local/bin"',
                    text,
                )

    def test_runtime_and_external_writer_prerequisites_are_explicit(self) -> None:
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                self.assertRegex(text, r"Python\s+3\.11\+")
                self.assertIn("Git", text)
                self.assertIn("sandbox-exec", text)
                self.assertIn("bwrap", text)

    def test_external_worker_docs_expose_one_shot_flow_and_os_boundary(self) -> None:
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                self.assertIn("token-saver-route.py worker", text)
                self.assertIn("token-saver-route.py integrate", text)
                self.assertIn("--mode lite", text)
                self.assertIn("--mode max", text)
                self.assertIn("review --inline", text)
                self.assertNotIn("<approval.json>", text)
                self.assertIn("claude-kimi-bypass", text)
                self.assertIn("macOS", text)
                self.assertIn("Linux", text)
                self.assertIn("WSL", text)
                self.assertRegex(text, r"(?i)(native Windows|Windows 原生).{0,120}(fail|sandbox_unavailable|拒绝)")

    def test_authority_mode_and_provider_credential_boundaries_are_explicit(self) -> None:
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                self.assertRegex(
                    text,
                    r"(?is)(authority_mode|权威模式).{0,240}(sealed|密封).{0,240}(cannot|不可|不能)",
                )
                for tool in ("Read", "Glob", "Grep", "Edit", "Write"):
                    self.assertIn(tool, text)
                self.assertRegex(text, r"(?i)(Bash|shell).{0,100}(disabled|unavailable|禁用|不可用)")
                self.assertRegex(text, r"(?i)(short-lived|短期).{0,100}(token|凭据)")
                self.assertRegex(text, r"(?is)(provider binary|Provider 二进制).{0,180}(cannot|无法|不能)")

    def test_external_wrapper_mapping_and_fail_closed_language(self) -> None:
        commands = (
            "claude-kimi",
            "claude-kimi-bypass -p",
            "claude-glm",
            "claude-glm-bypass -p",
            "claude-glm-turbo",
            "claude-glm-turbo-bypass -p",
            "--safe-mode --no-session-persistence --permission-mode plan --tools \"\" -p",
            "sandbox_unavailable",
        )
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                for command in commands:
                    self.assertIn(command, text)
                self.assertRegex(text, r"(?i)command name.{0,120}(not|never).{0,80}(identity|身份)")

    def test_active_protocol_docs_do_not_expose_legacy_approval_files(self) -> None:
        active_paths = (
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
            ROOT / "SKILL.md",
            ROOT / "references" / "protocol.md",
            ROOT / "references" / "adapters" / "claude-code.md",
            ROOT / "references" / "adapters" / "codex.md",
            ROOT / "references" / "adapters" / "external-cli.md",
        )
        for path in active_paths:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("<approval.json>", text)
                self.assertNotRegex(text, r"integrate\s+<manifest>\s+\S*approval")

    def test_benchmark_claims_are_explicitly_scoped(self) -> None:
        for language, text in (("en", self.bench_en), ("zh", self.bench_zh)):
            with self.subTest(language=language):
                intro = text[:1200].lower()
                self.assertTrue("historical" in intro or "历史" in intro)
                self.assertIn("fable", intro)
                self.assertIn("opus", intro)
                self.assertTrue("do not predict" in intro or "不能预测" in intro)
                self.assertRegex(text, r"(?i)(-42%|−42%).{0,120}(-89%|−89%).{0,180}(output|输出)")
                self.assertRegex(text, r"(?i)(-34%|−34%).{0,120}(-88%|−88%).{0,200}(quota|额度).*(proxy|代理)")
                self.assertTrue("one observed probe" in text.lower() or "单次观察" in text)

    def test_development_notes_are_a_pre_rename_archive(self) -> None:
        self.assertRegex(self.devnotes[:600], r"(重命名前|pre-rename).*(存档|archive)")
        self.assertIn("https://github.com/vincemakes/token-saver", self.devnotes)
        self.assertIn("fable-token-saver", self.devnotes)

    def test_all_relative_markdown_links_resolve(self) -> None:
        for filename in (
            "README.md",
            "README.zh-CN.md",
            "BENCHMARKS.md",
            "BENCHMARKS.zh-CN.md",
            "docs/DEVNOTES.zh-CN.md",
        ):
            path = ROOT / filename
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                    continue
                relative = target.split("#", 1)[0]
                if not relative:
                    continue
                with self.subTest(filename=filename, target=target):
                    self.assertTrue((path.parent / relative).exists())


if __name__ == "__main__":
    unittest.main()
