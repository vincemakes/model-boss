from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import runtime.token_saver.package as package_module
from runtime.token_saver.package import (
    EXECUTABLE_PATHS,
    PACKAGE_MANIFEST,
    PackageError,
    build_package,
    validate_package,
)


ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def _build(self, directory: Path, name: str = "token-saver.skill") -> Path:
        output = directory / name
        result = build_package(ROOT, output)
        self.assertEqual(result.output_path, output.resolve())
        self.assertEqual(result.entries, tuple(sorted(PACKAGE_MANIFEST, key=lambda p: p.encode("utf-8"))))
        self.assertEqual(result.sha256, hashlib.sha256(output.read_bytes()).hexdigest())
        return output

    def test_archive_has_exact_safe_manifest_and_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-package-test-") as text:
            output = self._build(Path(text))
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                expected = [f"token-saver/{path}" for path in sorted(PACKAGE_MANIFEST, key=lambda p: p.encode("utf-8"))]
                self.assertEqual(names, expected)
                self.assertEqual(len(names), len(set(names)))
                self.assertTrue(all(name.startswith("token-saver/") for name in names))
                self.assertTrue(all("\\" not in name for name in names))
                self.assertTrue(all(not name.startswith("/") for name in names))
                self.assertTrue(all(".." not in Path(name).parts for name in names))
                forbidden = ("/.git/", "/tests/", "/evals/", "/benchmarks/", "__pycache__", ".pyc")
                self.assertFalse(any(part in name for name in names for part in forbidden))

    def test_manifest_wide_legacy_brand_occurrences_match_migration_allowlist(self) -> None:
        legacy = re.compile(rb"fable[-_ ]token[-_ ]saver", re.IGNORECASE)
        expected = {
            "README.md": 1,
            "README.zh-CN.md": 1,
            "SKILL.md": 1,
            "runtime/token_saver/cli.py": 1,
            "runtime/token_saver/package.py": 2,
        }
        actual = {}
        for relative in PACKAGE_MANIFEST:
            count = len(legacy.findall((ROOT / relative).read_bytes()))
            if count:
                actual[relative] = count
        self.assertEqual(actual, expected)

    def test_source_hash_links_permissions_and_metadata_are_stable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-package-test-") as text:
            output = self._build(Path(text))
            result = validate_package(ROOT, output)
            self.assertEqual(result.skill_sha256, hashlib.sha256((ROOT / "SKILL.md").read_bytes()).hexdigest())
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.comment, b"")
                self.assertEqual(archive.read("token-saver/SKILL.md"), (ROOT / "SKILL.md").read_bytes())
                for info in archive.infolist():
                    relative = info.filename.removeprefix("token-saver/")
                    with self.subTest(path=relative):
                        self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                        self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                        self.assertEqual(info.create_system, 3)
                        self.assertEqual(info.create_version, 20)
                        self.assertEqual(info.extract_version, 20)
                        self.assertEqual(info.extra, b"")
                        self.assertEqual(info.comment, b"")
                        expected_mode = 0o755 if relative in EXECUTABLE_PATHS else 0o644
                        self.assertEqual((info.external_attr >> 16) & 0o777, expected_mode)

    def test_two_builds_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-package-test-") as first_text, tempfile.TemporaryDirectory(prefix="token-saver-package-test-") as second_text:
            first = self._build(Path(first_text))
            second = self._build(Path(second_text))
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_unlisted_controlled_file_and_symlink_are_rejected(self) -> None:
        sentinel = ROOT / "references" / "unlisted-sentinel.txt"
        self.assertFalse(os.path.lexists(sentinel))
        try:
            sentinel.write_text("must reject\n", encoding="utf-8")
            with tempfile.TemporaryDirectory(prefix="token-saver-package-test-") as text:
                with self.assertRaisesRegex(PackageError, "unlisted"):
                    build_package(ROOT, Path(text) / "token-saver.skill")
        finally:
            sentinel.unlink(missing_ok=True)

        if hasattr(os, "symlink"):
            link = ROOT / "references" / "unlisted-sentinel.txt"
            try:
                os.symlink(ROOT / "SKILL.md", link)
                with tempfile.TemporaryDirectory(prefix="token-saver-package-test-") as text:
                    with self.assertRaisesRegex(PackageError, "symlink|unlisted"):
                        build_package(ROOT, Path(text) / "token-saver.skill")
            finally:
                link.unlink(missing_ok=True)

    def test_atomic_validation_failure_preserves_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-package-test-") as text:
            directory = Path(text)
            output = directory / "token-saver.skill"
            output.write_bytes(b"existing-package")
            with mock.patch.object(
                package_module,
                "_validate_archive",
                side_effect=PackageError("injected validation failure"),
            ):
                with self.assertRaisesRegex(PackageError, "injected"):
                    build_package(ROOT, output)
            self.assertEqual(output.read_bytes(), b"existing-package")
            self.assertEqual({path.name for path in directory.iterdir()}, {"token-saver.skill"})

    def test_old_artifact_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-package-test-") as text:
            with self.assertRaisesRegex(PackageError, "obsolete"):
                build_package(ROOT, Path(text) / "fable-token-saver.skill")

    def test_manifest_sources_are_regular_non_symlinks(self) -> None:
        for relative in PACKAGE_MANIFEST:
            path = ROOT / relative
            metadata = os.lstat(path)
            with self.subTest(path=relative):
                self.assertTrue(stat.S_ISREG(metadata.st_mode))
                self.assertFalse(stat.S_ISLNK(metadata.st_mode))


if __name__ == "__main__":
    unittest.main()
