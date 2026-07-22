from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from runtime.model_boss.cli import prepare_provider_exec
from runtime.model_boss.models import Status
from runtime.model_boss.resources import create_invocation_resources
from runtime.model_boss.sandbox import (
    ConformanceProbe,
    UnavailableSandbox,
    VerifiedSandbox,
)


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", os.fspath(repository), *arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Model Boss",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Model Boss",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


class ProviderExecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="model-boss-provider-exec-test-"
        )
        self.base = Path(self.temporary.name)
        self.repository = self.base / "repository"
        self.repository.mkdir()
        _git(self.repository, "init", "-q")
        (self.repository / "tracked.txt").write_text("base\n", encoding="utf-8")
        _git(self.repository, "add", "--", "tracked.txt")
        _git(self.repository, "commit", "-q", "-m", "base")
        self.temp_parent = self.base / "invocations"
        self.temp_parent.mkdir()
        self.resources = create_invocation_resources(
            self.repository,
            self.temp_parent,
        )
        _git(
            self.repository,
            "worktree",
            "add",
            "--detach",
            os.fspath(self.resources.worktree_path),
            "HEAD",
        )
        self.provider = self.base / "claude"
        self.provider.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.provider.chmod(0o755)
        self.environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.fspath(self.base / "unused-home"),
            "LANG": "C",
            "LC_ALL": "C",
            "MODEL_BOSS_INVOCATION_MANIFEST": os.fspath(
                self.resources.manifest_path
            ),
            "KIMI_BASE_URL": "https://kimi.invalid/",
            "KIMI_AUTH_TOKEN": "secret-value",
            "UNRELATED_SECRET": "must-not-pass",
        }

    def tearDown(self) -> None:
        try:
            _git(
                self.repository,
                "worktree",
                "remove",
                "--force",
                os.fspath(self.resources.worktree_path),
            )
        except subprocess.CalledProcessError:
            pass
        self.temporary.cleanup()

    def _resolver(self, name: str) -> str | None:
        return os.fspath(self.provider) if name == "claude" else None

    def _verified_factory(self, policy, *, route_id, argv, probe_parent=None):
        self.assertIsNotNone(probe_parent)
        probe = ConformanceProbe(True, True, True, True, True)
        return VerifiedSandbox._from_successful_probe(
            backend="unit-test",
            policy=policy,
            route_id=route_id,
            route_argv=argv,
            launcher_prefix=(os.fspath(Path("/usr/bin/env").resolve(strict=True)),),
            profile_hash="a" * 64,
            probe=probe,
        )

    def test_sandboxed_worker_requires_a_sealed_manifest(self) -> None:
        environment = dict(self.environment)
        environment.pop("MODEL_BOSS_INVOCATION_MANIFEST")
        environment["TOKEN_SAVER_INVOCATION_MANIFEST"] = os.fspath(
            self.resources.manifest_path
        )

        plan = prepare_provider_exec(
            "kimi",
            "sandboxed-worker",
            ("-p",),
            environment=environment,
            cwd=self.resources.worktree_path,
            executable_resolver=self._resolver,
            sandbox_factory=self._verified_factory,
        )

        self.assertEqual(plan.status, Status.SANDBOX_UNAVAILABLE)
        self.assertEqual(plan.argv, ())

    def test_safe_wrapper_keeps_cwd_but_still_filters_the_child_environment(self) -> None:
        plan = prepare_provider_exec(
            "kimi",
            "safe",
            ("--version",),
            environment=self.environment,
            cwd=self.repository,
            executable_resolver=self._resolver,
            sandbox_factory=self._verified_factory,
        )

        self.assertEqual(plan.status, Status.OK)
        self.assertEqual(plan.executable, self.provider.resolve())
        self.assertEqual(plan.cwd, self.repository.resolve())
        self.assertNotIn("--dangerously-skip-permissions", plan.argv)
        self.assertNotIn("MODEL_BOSS_INVOCATION_MANIFEST", plan.environment)
        self.assertNotIn("UNRELATED_SECRET", plan.environment)

    def test_safe_wrapper_refuses_every_user_supplied_bypass_form(self) -> None:
        cases = (
            ("--dangerously-skip-permissions",),
            ("--dangerously-skip-permissions=true",),
            ("--permission-mode", "bypassPermissions"),
            ("--permission-mode=bypassPermissions",),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                plan = prepare_provider_exec(
                    "kimi",
                    "safe",
                    arguments,
                    environment=self.environment,
                    cwd=self.repository,
                    executable_resolver=self._resolver,
                    sandbox_factory=self._verified_factory,
                )
                self.assertEqual(plan.status, Status.PROVIDER_UNAVAILABLE)
                self.assertEqual(plan.argv, ())

    def test_sandboxed_worker_rejects_source_cwd_and_tampered_manifest(self) -> None:
        wrong_cwd = prepare_provider_exec(
            "kimi",
            "sandboxed-worker",
            ("-p",),
            environment=self.environment,
            cwd=self.repository,
            executable_resolver=self._resolver,
            sandbox_factory=self._verified_factory,
        )
        self.assertEqual(wrong_cwd.status, Status.SANDBOX_UNAVAILABLE)

        manifest = json.loads(
            self.resources.manifest_path.read_text(encoding="utf-8")
        )
        manifest["state"] = "forged"
        self.resources.manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self.resources.manifest_path.chmod(0o600)
        forged = prepare_provider_exec(
            "kimi",
            "sandboxed-worker",
            ("-p",),
            environment=self.environment,
            cwd=self.resources.worktree_path,
            executable_resolver=self._resolver,
            sandbox_factory=self._verified_factory,
        )
        self.assertEqual(forged.status, Status.SANDBOX_UNAVAILABLE)
        self.assertEqual(forged.argv, ())

    def test_sandboxed_worker_refuses_an_unavailable_reprobe(self) -> None:
        def unavailable(policy, *, route_id, argv, probe_parent=None):
            del policy, route_id, argv, probe_parent
            return UnavailableSandbox("injected missing backend")

        plan = prepare_provider_exec(
            "kimi",
            "sandboxed-worker",
            ("-p",),
            environment=self.environment,
            cwd=self.resources.worktree_path,
            executable_resolver=self._resolver,
            sandbox_factory=unavailable,
        )

        self.assertEqual(plan.status, Status.SANDBOX_UNAVAILABLE)
        self.assertEqual(plan.argv, ())

    def test_sandboxed_worker_builds_a_fresh_bound_launch_and_narrow_env(self) -> None:
        plan = prepare_provider_exec(
            "kimi",
            "sandboxed-worker",
            ("-p", "hostile value", "$(literal)"),
            environment=self.environment,
            cwd=self.resources.worktree_path,
            executable_resolver=self._resolver,
            sandbox_factory=self._verified_factory,
        )

        self.assertEqual(plan.status, Status.OK)
        self.assertEqual(plan.cwd, self.resources.worktree_path.resolve())
        provider_index = plan.argv.index(os.fspath(self.provider.resolve()))
        provider_argv = plan.argv[provider_index:]
        self.assertEqual(provider_argv[-3:], ("-p", "hostile value", "$(literal)"))
        self.assertEqual(plan.argv.count("--dangerously-skip-permissions"), 1)
        self.assertIn("--safe-mode", provider_argv)
        self.assertIn("--no-session-persistence", provider_argv)
        self.assertIn("--disable-slash-commands", provider_argv)
        tools_index = provider_argv.index("--tools")
        self.assertEqual(provider_argv[tools_index + 1], "Read,Glob,Grep,Edit,Write")
        self.assertNotIn("Bash", provider_argv[tools_index + 1])
        self.assertEqual(plan.environment["ANTHROPIC_AUTH_TOKEN"], "secret-value")
        self.assertEqual(plan.environment["ANTHROPIC_BASE_URL"], "https://kimi.invalid/")
        self.assertNotIn("KIMI_AUTH_TOKEN", plan.environment)
        self.assertNotIn("MODEL_BOSS_INVOCATION_MANIFEST", plan.environment)
        self.assertNotIn("UNRELATED_SECRET", plan.environment)
        self.assertTrue(Path(plan.environment["HOME"]).is_dir())
        self.assertNotEqual(Path(plan.environment["HOME"]), Path(self.environment["HOME"]))

    def test_host_backend_reprobe_accepts_the_sealed_fake_provider(self) -> None:
        supported = (
            platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file()
        ) or (platform.system() == "Linux" and shutil.which("bwrap") is not None)
        if not supported:
            self.skipTest("no supported host sandbox backend")

        plan = prepare_provider_exec(
            "kimi",
            "sandboxed-worker",
            ("--version",),
            environment=self.environment,
            cwd=self.resources.worktree_path,
            executable_resolver=self._resolver,
        )

        self.assertEqual(plan.status, Status.OK, plan.message)
        self.assertTrue(plan.argv)


if __name__ == "__main__":
    unittest.main()
