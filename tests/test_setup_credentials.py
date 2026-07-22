from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from runtime.token_saver.setup import SetupError, migrate_legacy_credentials, parse_legacy_env


class LegacyParserTests(unittest.TestCase):
    def test_hostile_values_remain_literal_data(self) -> None:
        sentinel = Path(tempfile.gettempdir()) / "token-saver-parser-must-not-create"
        try:
            sentinel.unlink()
        except FileNotFoundError:
            pass
        raw = (
            "KIMI_BASE_URL= https://example.invalid/#literal  \n"
            f"KIMI_AUTH_TOKEN=$(touch {sentinel})`echo nope`;${{HOME}}\\literal\n"
            "GLM_BASE_URL='https://glm.invalid/#x'\n"
            'GLM_AUTH_TOKEN=" both \' quote kinds # literal "\n'
            "GLM_MODEL=glm;still-data\n"
            "GLM_SMALL_FAST_MODEL=\n"
        ).encode()

        parsed = parse_legacy_env(raw)

        self.assertEqual(parsed["KIMI_BASE_URL"], " https://example.invalid/#literal  ")
        self.assertIn("$(touch", parsed["KIMI_AUTH_TOKEN"])
        self.assertEqual(parsed["GLM_BASE_URL"], "https://glm.invalid/#x")
        self.assertEqual(parsed["GLM_AUTH_TOKEN"], " both ' quote kinds # literal ")
        self.assertEqual(parsed["GLM_MODEL"], "glm;still-data")
        self.assertEqual(parsed["GLM_SMALL_FAST_MODEL"], "")
        self.assertFalse(sentinel.exists())

    def test_rejects_unknown_duplicates_quotes_nul_and_invalid_utf8(self) -> None:
        cases = (
            b"UNKNOWN=x\n",
            b"KIMI_AUTH_TOKEN=a\nKIMI_AUTH_TOKEN=b\n",
            b"KIMI_AUTH_TOKEN='unterminated\n",
            b"KIMI_AUTH_TOKEN=x\0y\n",
            b"KIMI_AUTH_TOKEN=\xff\n",
            b"KIMI_AUTH_TOKEN=line\\\ncontinued\n",
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(SetupError):
                parse_legacy_env(raw)


class CredentialMigrationTests(unittest.TestCase):
    def test_migration_is_private_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            source = root / "providers.env"
            source_bytes = b"KIMI_AUTH_TOKEN=secret#literal\nGLM_MODEL=glm-x\n"
            source.write_bytes(source_bytes)
            source.chmod(0o640)
            config_root = root / "config"
            config_root.mkdir()
            destination = config_root / "token-saver" / "credentials.json"

            first = migrate_legacy_credentials(source, destination)
            second = migrate_legacy_credentials(source, destination)

            self.assertEqual(first.status, "migrated")
            self.assertEqual(second.status, "already_configured")
            value = json.loads(destination.read_text())
            self.assertEqual(value["version"], 1)
            self.assertEqual(value["credentials"]["KIMI_AUTH_TOKEN"], "secret#literal")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o640)

    def test_existing_destination_of_any_kind_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            source = root / "providers.env"
            source.write_bytes(b"KIMI_AUTH_TOKEN=new\n")
            parent = root / "token-saver"
            parent.mkdir()
            destination = parent / "credentials.json"
            destination.write_bytes(b"existing")

            result = migrate_legacy_credentials(source, destination)

            self.assertEqual(result.status, "already_configured")
            self.assertEqual(destination.read_bytes(), b"existing")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_sources_destinations_and_parents_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            real_source = root / "real.env"
            real_source.write_bytes(b"KIMI_AUTH_TOKEN=x\n")
            source_link = root / "source.env"
            os.symlink(real_source, source_link)
            parent = root / "config"
            parent.mkdir()
            destination = parent / "credentials.json"
            with self.assertRaises(SetupError):
                migrate_legacy_credentials(source_link, destination)

            destination_target = root / "target"
            destination_target.write_bytes(b"keep")
            os.symlink(destination_target, destination)
            result = migrate_legacy_credentials(real_source, destination)
            self.assertEqual(result.status, "already_configured")
            self.assertEqual(destination_target.read_bytes(), b"keep")

            destination.unlink()
            parent.rmdir()
            real_parent = root / "real-parent"
            real_parent.mkdir()
            os.symlink(real_parent, parent)
            with self.assertRaises(SetupError):
                migrate_legacy_credentials(real_source, parent / "credentials.json")


if __name__ == "__main__":
    unittest.main()
