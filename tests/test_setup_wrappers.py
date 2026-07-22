from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from runtime.model_boss.setup import (
    WRAPPER_SPECS,
    SetupError,
    install_provider_wrappers,
    provider_child_environment,
    render_wrapper,
)


EXPECTED = {
    "claude-kimi": ("kimi", "safe"),
    "claude-kimi-bypass": ("kimi", "sandboxed-worker"),
    "claude-glm": ("glm", "safe"),
    "claude-glm-bypass": ("glm", "sandboxed-worker"),
    "claude-glm-turbo": ("glm-turbo", "safe"),
    "claude-glm-turbo-bypass": ("glm-turbo", "sandboxed-worker"),
}
ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "scripts" / "setup-model-providers.sh"


class WrapperTests(unittest.TestCase):
    def test_fresh_wrapper_only_setup_needs_no_legacy_or_new_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            home = root / "home"
            home.mkdir()
            config_root = root / "config"
            install = root / "bin"
            completed = subprocess.run(
                (
                    "bash",
                    os.fspath(SETUP_SCRIPT),
                    "--install-path",
                    os.fspath(install),
                ),
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": os.fspath(home),
                    "XDG_CONFIG_HOME": os.fspath(config_root),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertEqual(
                json.loads(completed.stdout)["setup_status"],
                "wrappers_configured",
            )
            self.assertEqual({path.name for path in install.iterdir()}, set(EXPECTED))
            self.assertFalse(config_root.exists())
            self.assertFalse(
                (home / ".claude" / "fable-token-saver" / "providers.env").exists()
            )

    def test_wrapper_only_setup_ignores_existing_default_legacy_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            home = root / "home"
            legacy = home / ".claude" / "fable-token-saver" / "providers.env"
            legacy.parent.mkdir(parents=True)
            original = b"KIMI_AUTH_TOKEN=must-not-import\n"
            legacy.write_bytes(original)
            config_root = root / "config"
            install = root / "bin"

            completed = subprocess.run(
                (
                    "bash",
                    os.fspath(SETUP_SCRIPT),
                    "--install-path",
                    os.fspath(install),
                ),
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": os.fspath(home),
                    "XDG_CONFIG_HOME": os.fspath(config_root),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout)["setup_status"],
                "wrappers_configured",
            )
            self.assertEqual({path.name for path in install.iterdir()}, set(EXPECTED))
            self.assertEqual(legacy.read_bytes(), original)
            self.assertFalse(
                (config_root / "model-boss" / "credentials.json").exists()
            )

    def test_exact_six_static_wrappers_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            runner = root / "runner with spaces.py"
            runner.write_text("#!/bin/sh\nexit 0\n")
            runner.chmod(0o755)
            install = root / "bin"

            result = install_provider_wrappers(runner, install)
            again = install_provider_wrappers(runner, install)

            self.assertEqual(result.status, "configured")
            self.assertEqual(again.status, "configured")
            self.assertEqual(set(WRAPPER_SPECS), set(EXPECTED))
            self.assertEqual({path.name for path in install.iterdir()}, set(EXPECTED))
            for name, (route, policy) in EXPECTED.items():
                path = install / name
                self.assertEqual(
                    path.read_text(),
                    render_wrapper(runner.resolve(), route, policy),
                )
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
                self.assertNotIn("source ", path.read_text())
                self.assertNotIn("eval ", path.read_text())
                self.assertNotIn("dangerously-skip-permissions", path.read_text())

    def test_wrapper_forwards_hostile_arguments_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            capture = root / "argv.json"
            runner = root / "fake runner.py"
            runner.write_text(
                "#!/usr/bin/env python3\n"
                "import json,os,sys\n"
                "open(os.environ['CAPTURE'], 'w').write(json.dumps(sys.argv[1:]))\n"
            )
            runner.chmod(0o755)
            install = root / "bin"
            install_provider_wrappers(runner, install)
            forwarded = ("space value", "'quotes'", "$(literal)", "semi;colon", "--lead")

            completed = subprocess.run(
                (os.fspath(install / "claude-kimi"), *forwarded),
                check=False,
                env={"PATH": os.environ.get("PATH", ""), "CAPTURE": os.fspath(capture)},
            )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(
                json.loads(capture.read_text()),
                ["provider-exec", "--route", "kimi", "--policy", "safe", "--", *forwarded],
            )

    def test_provider_environment_maps_only_approved_fields(self) -> None:
        credentials = {
            "KIMI_BASE_URL": "https://kimi.invalid/",
            "KIMI_AUTH_TOKEN": "kimi-secret",
            "GLM_BASE_URL": "https://glm.invalid/",
            "GLM_AUTH_TOKEN": "glm-secret",
            "GLM_MODEL": "glm-main",
            "GLM_SMALL_FAST_MODEL": "glm-fast",
        }
        base = {"PATH": "/bin", "HOME": "/safe-home", "PARENT_SECRET": "no"}
        kimi = provider_child_environment("kimi", credentials, base)
        glm = provider_child_environment("glm", credentials, base)
        turbo = provider_child_environment("glm-turbo", credentials, base)

        self.assertEqual(kimi["ANTHROPIC_BASE_URL"], credentials["KIMI_BASE_URL"])
        self.assertEqual(kimi["ANTHROPIC_AUTH_TOKEN"], "kimi-secret")
        self.assertNotIn("PARENT_SECRET", kimi)
        self.assertEqual(glm["ANTHROPIC_MODEL"], "glm-main")
        self.assertEqual(glm["ANTHROPIC_SMALL_FAST_MODEL"], "glm-fast")
        self.assertEqual(turbo["ANTHROPIC_MODEL"], "glm-fast")
        self.assertNotIn("GLM_AUTH_TOKEN", repr(glm))

    def test_renderer_rejects_unknown_routes_policies_and_symlink_runner(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            runner = root / "runner"
            runner.write_text("#!/bin/sh\n")
            runner.chmod(0o755)
            with self.assertRaises(SetupError):
                render_wrapper(runner, "unknown", "safe")
            with self.assertRaises(SetupError):
                render_wrapper(runner, "kimi", "bypass")
            if hasattr(os, "symlink"):
                link = root / "link"
                os.symlink(runner, link)
                with self.assertRaises(SetupError):
                    install_provider_wrappers(link, root / "bin")


if __name__ == "__main__":
    unittest.main()
