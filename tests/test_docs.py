from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

EN_HEADINGS = (
    "Model Boss",
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
    "When Model Boss steps aside",
    "Migrating from Token Saver",
    "License",
)

ZH_HEADINGS = (
    "Model Boss",
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
    "Model Boss 何时让开",
    "从 Token Saver 迁移",
    "许可证",
)

CREDENTIALS_EXAMPLE = """{
  "version": 1,
  "credentials": {
    "GLM_AUTH_TOKEN": "<glm-auth-token>",
    "GLM_BASE_URL": "<glm-base-url>",
    "GLM_MODEL": "<glm-model>",
    "GLM_SMALL_FAST_MODEL": "<glm-small-fast-model>",
    "KIMI_AUTH_TOKEN": "<kimi-auth-token>",
    "KIMI_BASE_URL": "<kimi-base-url>"
  }
}"""


def _headings(text: str) -> tuple[str, ...]:
    return tuple(
        match.group(2).strip()
        for match in re.finditer(r"^(#{1,2})\s+(.+?)\s*$", text, re.MULTILINE)
    )


def _first_nonblank_lines(text: str, limit: int = 12) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines() if line.strip())[:limit]


def _level_two_section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    start = text.index(marker)
    following = re.search(r"^## (?!#)", text[start + len(marker) :], re.MULTILINE)
    end = start + len(marker) + following.start() if following else len(text)
    return text[start:end]


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
        cls.historical_plan = (
            ROOT
            / "docs"
            / "superpowers"
            / "plans"
            / "2026-07-21-token-saver-cross-platform.md"
        ).read_text(encoding="utf-8")
        cls.rename_plan = (
            ROOT
            / "docs"
            / "superpowers"
            / "plans"
            / "2026-07-22-model-boss-rename.md"
        ).read_text(encoding="utf-8")
        cls.schema_path = ROOT / "config" / "model-boss.schema.json"
        cls.schema = (
            json.loads(cls.schema_path.read_text(encoding="utf-8"))
            if cls.schema_path.is_file()
            else None
        )

    def test_readmes_have_synchronized_information_architecture(self) -> None:
        self.assertEqual(_headings(self.en), EN_HEADINGS)
        self.assertEqual(_headings(self.zh), ZH_HEADINGS)
        self.assertEqual(len(_headings(self.en)), len(_headings(self.zh)))

    def test_readmes_lead_with_model_boss_positioning(self) -> None:
        positioning_patterns = {
            "en": (
                r"(?is)(inherited main loop|main loop.{0,80}inherited)",
                r"(?is)(Boss.{0,160}authority holder|authority holder.{0,160}Boss)",
                r"(?is)(workflow-relative|relative to (?:the )?workflow)",
                r"(?is)(not|rather than).{0,120}universal.{0,80}(?:model )?ranking",
            ),
            "zh": (
                r"(?s)(继承的?主循环|主循环.{0,80}(继承|沿用))",
                r"(?s)(Boss.{0,160}(权威持有者|权威负责人|权威主体)|(权威持有者|权威负责人|权威主体).{0,160}Boss)",
                r"(?s)(相对于.{0,40}工作流|工作流.{0,40}(相对|角色))",
                r"(?s)(不是|并非|而非).{0,120}(通用|普遍|统一|全局).{0,80}(模型)?排名",
            ),
        }
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                opening_lines = _first_nonblank_lines(text)
                opening = "\n".join(opening_lines)
                self.assertEqual(opening_lines[0], "# Model Boss")
                self.assertIn("Big models think. Small models ship.", opening)
                self.assertIn("Cross-model coding orchestration", opening)
                for pattern in positioning_patterns[language]:
                    self.assertRegex(opening, pattern)

    def test_canonical_identity_and_inherited_main_loop(self) -> None:
        canonical = "https://github.com/vincemakes/model-boss"
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                self.assertIn(canonical, text)
                self.assertIn("main loop", text.lower())
        self.assertRegex(
            self.en,
            r"(?is)(inherited main loop|main loop.{0,160}(inherited|already selected|immutable|never replaces))",
        )
        self.assertRegex(
            self.zh,
            r"(?s)(继承的?主循环|主循环.{0,120}(继承|已经选定|不可变|不会替换))",
        )
        self.assertIn("## Migrating from Token Saver", self.en)
        self.assertIn("## 从 Token Saver 迁移", self.zh)
        self.assertIn("## 从 Token Saver 迁移", self.devnotes)

    def test_config_discovery_docs_require_absolute_xdg_or_home_fallback(self) -> None:
        documents = (
            ("en", self.en, "Migrating from Token Saver"),
            ("zh", self.zh, "从 Token Saver 迁移"),
            ("devnotes", self.devnotes, "从 Token Saver 迁移"),
        )
        exact_paths = (
            "$XDG_CONFIG_HOME/model-boss/config.json",
            "$HOME/.config/model-boss/config.json",
            "$XDG_CONFIG_HOME/model-boss/credentials.json",
            "$HOME/.config/model-boss/credentials.json",
            r"$HOME\.config\model-boss\config.json",
            r"$HOME\.config\model-boss\credentials.json",
        )
        for language, text, migration_heading in documents:
            with self.subTest(language=language):
                self.assertNotIn("${XDG_CONFIG_HOME:-$HOME/.config}", text)
                migration = _level_two_section(text, migration_heading)
                active = text[: text.index(f"## {migration_heading}")]
                for section_name, section in (("active", active), ("migration", migration)):
                    with self.subTest(language=language, section=section_name):
                        for path in exact_paths:
                            self.assertIn(path, section)
                        self.assertRegex(
                            section,
                            r"(?is)(XDG_CONFIG_HOME.{0,100}absolute|"
                            r"XDG_CONFIG_HOME.{0,100}绝对)",
                        )

    def test_migration_docs_use_explicit_provider_setup_and_json_policy(self) -> None:
        migrations = (
            ("en", _level_two_section(self.en, "Migrating from Token Saver")),
            ("zh", _level_two_section(self.zh, "从 Token Saver 迁移")),
            ("devnotes", _level_two_section(self.devnotes, "从 Token Saver 迁移")),
        )
        command = (
            "python3 scripts/model-boss.py setup-providers "
            "--legacy-source <absolute-old-providers.env>"
        )
        for language, migration in migrations:
            with self.subTest(language=language):
                self.assertIn(command, migration)
                self.assertRegex(
                    migration,
                    r"(?is)scripts/setup-model-providers\.sh.{0,100}(wrapper|\u5305装)",
                )
                self.assertRegex(
                    migration,
                    r"(?is)(old|legacy|\u65e7).{0,80}(JSON|json).{0,80}"
                    r"(never auto-copied|not auto-copied|\u4e0d会自动复制|\u7edd不自动复制)",
                )
                self.assertRegex(
                    migration,
                    r"(?is)(manually copy|manual copy|\u624b动复制).{0,120}"
                    r"(permissions|\u6743限)",
                )
                self.assertRegex(
                    migration,
                    r"(?is)(absolute MODEL_BOSS_CREDENTIALS|"
                    r"MODEL_BOSS_CREDENTIALS.{0,80}(absolute|\u7edd对))",
                )

    def test_migration_docs_map_exact_environment_variables_manually(self) -> None:
        migrations = (
            ("en", _level_two_section(self.en, "Migrating from Token Saver")),
            ("zh", _level_two_section(self.zh, "从 Token Saver 迁移")),
            ("devnotes", _level_two_section(self.devnotes, "从 Token Saver 迁移")),
        )
        mappings = (
            ("TOKEN_SAVER_CREDENTIALS", "MODEL_BOSS_CREDENTIALS"),
            (
                "TOKEN_SAVER_INVOCATION_MANIFEST",
                "MODEL_BOSS_INVOCATION_MANIFEST",
            ),
            (
                "TOKEN_SAVER_TRUSTED_GATE_FAILURES",
                "MODEL_BOSS_TRUSTED_GATE_FAILURES",
            ),
            ("TOKEN_SAVER_PROVIDER_API_KEY", "MODEL_BOSS_PROVIDER_API_KEY"),
        )
        for language, migration in migrations:
            with self.subTest(language=language):
                self.assertNotIn("TOKEN_SAVER_*", migration)
                for old, new in mappings:
                    self.assertRegex(
                        migration,
                        rf"(?m)^\| `{old}` \| `{new}` \|$",
                    )
                self.assertRegex(
                    migration,
                    r"(?is)(old variables|legacy variables|\u65e7环境变量).{0,100}"
                    r"(ignored|\u5ffd略)",
                )
                self.assertRegex(
                    migration,
                    r"(?is)(manual migration|manually migrate|\u624b动迁移).{0,100}"
                    r"(not compatibility|no compatibility|\u4e0d是兼容|\u975e兼容)",
                )

    def test_provider_setup_docs_make_legacy_import_explicit(self) -> None:
        documents = (
            ("en", self.en, "Migrating from Token Saver"),
            ("zh", self.zh, "从 Token Saver 迁移"),
            ("devnotes", self.devnotes, "从 Token Saver 迁移"),
        )
        command = (
            "python3 scripts/model-boss.py setup-providers "
            "--legacy-source <absolute-old-providers.env>"
        )
        for language, text, heading in documents:
            with self.subTest(language=language):
                active = text[: text.index(f"## {heading}")]
                migration = _level_two_section(text, heading)
                self.assertRegex(
                    active,
                    r"(?is)(--install-path.{0,180}(wrappers only|"
                    r"只安装.{0,40}wrapper)|wrappers only.{0,180}--install-path)",
                )
                self.assertRegex(
                    active,
                    r"(?is)(wrappers alone|wrappers 本身|wrapper 本身).{0,160}"
                    r"(not make|do not make|不会.{0,40}可用|无法.{0,40}可用)",
                )
                self.assertIn(command, migration)
                self.assertRegex(
                    migration,
                    r"(?is)(explicit --legacy-source|"
                    r"--legacy-source.{0,80}(required|必须|显式))",
                )
                self.assertRegex(
                    migration,
                    r"(?is)(default legacy|默认.{0,40}旧).{0,120}"
                    r"(not imported|不会导入)",
                )

    def test_fresh_provider_credentials_are_complete_private_and_outside_repo(self) -> None:
        documents = (
            ("en", self.en, "Migrating from Token Saver"),
            ("zh", self.zh, "从 Token Saver 迁移"),
            ("devnotes", self.devnotes, "从 Token Saver 迁移"),
        )
        required_names = (
            "KIMI_BASE_URL",
            "KIMI_AUTH_TOKEN",
            "GLM_BASE_URL",
            "GLM_AUTH_TOKEN",
            "GLM_MODEL",
            "GLM_SMALL_FAST_MODEL",
        )
        for language, text, heading in documents:
            with self.subTest(language=language):
                active = text[: text.index(f"## {heading}")]
                for name in required_names:
                    self.assertIn(name, active)
                self.assertIn(CREDENTIALS_EXAMPLE, active)
                self.assertIn("0700", active)
                self.assertIn("0600", active)
                self.assertRegex(
                    active,
                    r"(?is)(never|do not|绝不|不要).{0,120}"
                    r"(secret|秘密).{0,120}"
                    r"(repo|repository|config/model-boss\.example\.json|仓库)",
                )

    def test_windows_docs_match_home_userprofile_runtime_precedence(self) -> None:
        documents = (
            ("en", self.en, "Migrating from Token Saver"),
            ("zh", self.zh, "从 Token Saver 迁移"),
            ("devnotes", self.devnotes, "从 Token Saver 迁移"),
        )
        for language, text, heading in documents:
            with self.subTest(language=language):
                active = text[: text.index(f"## {heading}")]
                self.assertIn("$env:HOME", active)
                self.assertIn("$env:USERPROFILE", active)
                self.assertIn("$HOME", active)
                self.assertRegex(
                    active,
                    r"(?is)\$env:HOME.{0,160}(fall(?:s)? back|"
                    r"\$env:USERPROFILE|回退|否则).{0,160}"
                    r"\$env:USERPROFILE",
                )

    def test_published_schema_uses_the_canonical_repository(self) -> None:
        self.assertIsNotNone(
            self.schema,
            f"missing canonical schema: {self.schema_path.relative_to(ROOT)}",
        )
        self.assertEqual(
            self.schema["$id"],
            "https://github.com/vincemakes/model-boss/config/model-boss.schema.json",
        )

    def test_historical_plan_maps_and_packages_the_sealed_bundle_module(self) -> None:
        self.assertGreaterEqual(
            self.historical_plan.count("runtime/token_saver/bundle.py"),
            2,
        )

    def test_rename_plan_preserves_runtime_and_packaging_contracts(self) -> None:
        self.assertIn(
            "- `runtime/model_boss/` — renamed runtime package; protocol behavior "
            "remains unchanged.",
            self.rename_plan,
        )
        self.assertIn(
            "- `runtime/model_boss/package.py`, `scripts/package-skill.sh`, and "
            "`scripts/validate.sh` — deterministic `dist/model-boss.skill` production "
            "and validation.",
            self.rename_plan,
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
            ".claude/skills/model-boss",
            ".claude/agents",
            ".agents/skills/model-boss",
            ".codex/agents",
            "$HOME/.claude/skills/model-boss",
            "$HOME/.agents/skills/model-boss",
            "$HOME/.codex/agents",
            "powershell",
        )
        for language, text in (("en", self.en), ("zh", self.zh)):
            with self.subTest(language=language):
                for fragment in required:
                    self.assertIn(fragment, text)
                self.assertGreaterEqual(
                    text.count("git clone https://github.com/vincemakes/model-boss.git"),
                    8,
                )

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
                self.assertIn("model-boss.py worker", text)
                self.assertIn("model-boss.py integrate", text)
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

    def test_development_notes_have_the_canonical_migration_section(self) -> None:
        self.assertIn("## 从 Token Saver 迁移", self.devnotes)
        self.assertIn("https://github.com/vincemakes/model-boss", self.devnotes)

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
