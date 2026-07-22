from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

CANONICAL_PATHS = (
    "runtime/model_boss/__init__.py",
    "scripts/model-boss.py",
    "config/model-boss.example.json",
    "config/model-boss.schema.json",
    "dist/model-boss.skill",
)

OBSOLETE_PATHS = (
    "runtime/token_saver",
    "scripts/token-saver-route.py",
    "config/token-saver.example.json",
    "config/token-saver.schema.json",
    "dist/token-saver.skill",
)

FULL_FILE_ALLOWLIST = {
    "docs/superpowers/specs/2026-07-21-token-saver-cross-platform-design.md",
    "docs/superpowers/plans/2026-07-21-token-saver-cross-platform.md",
    "docs/superpowers/specs/2026-07-22-model-boss-rename-design.md",
    "docs/superpowers/plans/2026-07-22-model-boss-rename.md",
    "tests/test_branding.py",
}

MIGRATION_SECTIONS = {
    "README.md": "## Migrating from Token Saver",
    "README.zh-CN.md": "## 从 Token Saver 迁移",
    "docs/DEVNOTES.zh-CN.md": "## 从 Token Saver 迁移",
}

LINE_ALLOWLIST = {
    "SKILL.md": (
        r"^\s+.*migrat.*(?:token saver|fable-token-saver).*$",
    ),
    "runtime/model_boss/cli.py": (
        r'^\s*home / "\.claude" / "fable-token-saver" / "providers\.env"\s*$',
    ),
    "runtime/model_boss/package.py": (
        r'^\s*if .*\.name (?:==|in) .*"(?:token-saver|fable-token-saver)\.skill".*$',
    ),
    "tests/test_setup_credentials.py": (r"^.*fable-token-saver.*$",),
    "tests/test_setup_wrappers.py": (r"^.*fable-token-saver.*$",),
    "tests/test_docs.py": (
        r"^.*(?:Migrating from Token Saver|从 Token Saver 迁移).*$",
        r'^.*"2026-07-21-token-saver-cross-platform\.md".*$',
        r'^.*"runtime/token_saver/bundle\.py".*$',
    ),
    "tests/test_skill_content.py": (
        r"^.*migrate from Token Saver or fable-token-saver.*$",
    ),
    "tests/test_package.py": (
        r"^.*(?:token-saver|fable-token-saver)\.skill.*$",
    ),
    "tests/test_evals.py": (
        r"^.*(?:token saver|fable-token-saver).*$",
    ),
    "evals/evals.json": (
        r'^\s*"prompt": ".*(?:token saver|fable-token-saver).*"[,]?\s*$',
    ),
}

FORMER_BRAND = re.compile(r"token[-_ ]saver|fable-token-saver", re.IGNORECASE)
LEVEL_ONE_HEADING = re.compile(r"^#(?:\s|$)(?!#)")
LEVEL_TWO_HEADING = re.compile(r"^##(?:\s|$)(?!#)")
COMPILED_LINE_ALLOWLIST = {
    path: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for path, patterns in LINE_ALLOWLIST.items()
}

SKIPPED_DIRECTORY_NAMES = {
    ".git",
    "dist",
    "__pycache__",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def _is_cache_directory(name: str) -> bool:
    lowered = name.lower()
    return (
        name in SKIPPED_DIRECTORY_NAMES
        or lowered == "cache"
        or lowered.endswith(("-cache", "_cache", ".cache"))
    )


def _repository_files() -> list[Path]:
    command = [
        "git",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        stderr = getattr(error, "stderr", None)
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", errors="replace").strip()
        else:
            detail = str(stderr or error).strip()
        raise AssertionError(f"Git file enumeration failed: {detail}") from error

    files: list[Path] = []
    for encoded_relative in completed.stdout.split(b"\0"):
        if not encoded_relative:
            continue
        relative = Path(os.fsdecode(encoded_relative))
        if any(_is_cache_directory(part) for part in relative.parts):
            continue
        path = ROOT / relative
        if path.is_file():
            files.append(path)

    return sorted(
        files,
        key=lambda path: os.fsencode(path.relative_to(ROOT).as_posix()),
    )


def _read_text(path: Path) -> str | None:
    contents = path.read_bytes()
    if b"\0" in contents:
        return None
    try:
        return contents.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _former_brand_violations(relative: str, text: str) -> list[str]:
    violations: list[str] = []
    current_level_two_heading: str | None = None
    line_patterns = COMPILED_LINE_ALLOWLIST.get(relative, ())
    migration_heading = MIGRATION_SECTIONS.get(relative)

    for line_number, line in enumerate(text.splitlines(), start=1):
        if LEVEL_ONE_HEADING.match(line):
            current_level_two_heading = None
        elif LEVEL_TWO_HEADING.match(line):
            current_level_two_heading = line
        if FORMER_BRAND.search(line) is None:
            continue
        if (
            migration_heading is not None
            and current_level_two_heading == migration_heading
        ):
            continue
        if any(pattern.fullmatch(line) for pattern in line_patterns):
            continue
        violations.append(f"{relative}:{line_number}:{line}")

    return violations


class BrandingHelperTests(unittest.TestCase):
    def test_repository_files_use_git_and_filter_distribution_and_caches(self) -> None:
        command = [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ]
        completed = subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                b"README.md\0"
                b"dist/token-saver.skill\0"
                b"tests/__pycache__/ignored.pyc\0"
                b"tests/test_docs.py\0"
            ),
        )
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            files = _repository_files()

        run.assert_called_once_with(
            command,
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            [path.relative_to(ROOT).as_posix() for path in files],
            ["README.md", "tests/test_docs.py"],
        )

    def test_repository_files_report_git_enumeration_failure(self) -> None:
        error = subprocess.CalledProcessError(
            128,
            ["git", "ls-files"],
            stderr=b"fatal: not a git repository",
        )
        with mock.patch.object(subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(
                AssertionError,
                r"Git file enumeration failed.*fatal: not a git repository",
            ):
                _repository_files()

    def test_level_one_heading_ends_migration_section_allowance(self) -> None:
        document = """# Model Boss
## Migrating from Token Saver
Token Saver is mentioned in the migration section.
# Appendix
Token Saver is active again and must be rejected.
"""
        self.assertEqual(
            _former_brand_violations("README.md", document),
            [
                "README.md:5:Token Saver is active again and must be rejected.",
            ],
        )


class BrandingContractTests(unittest.TestCase):
    def test_canonical_files_exist_and_former_active_files_do_not(self) -> None:
        failures = [
            f"missing canonical path: {relative}"
            for relative in CANONICAL_PATHS
            if not (ROOT / relative).is_file()
        ]
        failures.extend(
            f"obsolete path still exists: {relative}"
            for relative in OBSOLETE_PATHS
            if os.path.lexists(ROOT / relative)
        )

        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_former_brand_only_appears_in_allowlisted_contexts(self) -> None:
        violations: list[str] = []

        for path in _repository_files():
            relative = path.relative_to(ROOT).as_posix()
            if relative == ".git" or relative in FULL_FILE_ALLOWLIST:
                continue

            text = _read_text(path)
            if text is None:
                continue

            violations.extend(_former_brand_violations(relative, text))

        self.assertEqual(violations, [], "\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
