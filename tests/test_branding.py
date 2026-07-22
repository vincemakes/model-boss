from __future__ import annotations

import os
import re
import unittest
from pathlib import Path


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
    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(ROOT):
        directory_names[:] = sorted(
            name for name in directory_names if not _is_cache_directory(name)
        )
        files.extend(Path(directory, name) for name in sorted(file_names))
    return files


def _read_text(path: Path) -> str | None:
    contents = path.read_bytes()
    if b"\0" in contents:
        return None
    try:
        return contents.decode("utf-8")
    except UnicodeDecodeError:
        return None


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

            current_level_two_heading: str | None = None
            line_patterns = COMPILED_LINE_ALLOWLIST.get(relative, ())
            migration_heading = MIGRATION_SECTIONS.get(relative)

            for line_number, line in enumerate(text.splitlines(), start=1):
                if LEVEL_TWO_HEADING.match(line):
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

        self.assertEqual(violations, [], "\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
