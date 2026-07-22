from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.token_saver.cli import GateSpec, WorkerTask, orchestrate_worker
from runtime.token_saver.models import CapabilityBand, Role, Route, Transport
from runtime.token_saver.repository import (
    capture_source_snapshot,
    create_worktree,
    materialize_snapshot,
)
from runtime.token_saver.resources import create_invocation_resources
from runtime.token_saver.sandbox import (
    ConformanceProbe,
    SandboxPolicy,
    UnavailableSandbox,
    VerifiedSandbox,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", "-C", os.fspath(repo), *args),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Token Saver",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Token Saver",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


class WorkerOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="token-saver-worker-test-")
        root = Path(self.temporary.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        (self.repo / "source.txt").write_bytes(b"source\n")
        _git(self.repo, "add", "--", "source.txt")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.temp_parent = root / "invocations"
        self.temp_parent.mkdir()
        self.resources = create_invocation_resources(self.repo, self.temp_parent)
        self.snapshot = capture_source_snapshot(
            self.repo,
            (b"source.txt", b"worker-output.txt"),
        )
        self.handle = create_worktree(
            self.repo,
            self.snapshot,
            self.resources.worktree_path,
        )
        materialize_snapshot(self.handle, self.snapshot)
        self.protected = root / "protected"
        self.protected.mkdir()
        self.route = Route(
            route_id="worker",
            transport=Transport.EXTERNAL_CLI,
            band=CapabilityBand.BALANCED,
            roles=frozenset({Role.WORKER}),
            read_only=False,
            command=(
                sys.executable,
                "-c",
                "from pathlib import Path; Path('worker-output.txt').write_text('done\\n')",
            ),
            timeout_seconds=5,
        )
        policy = SandboxPolicy(
            worktree_root=self.handle.path,
            route_state_root=self.resources.route_state_path,
            protected_roots=(self.protected,),
        )
        probe = ConformanceProbe(True, True, True, True, True)
        self.sandbox = VerifiedSandbox._from_successful_probe(
            backend="unit-test",
            policy=policy,
            route_id=self.route.route_id,
            route_argv=self.route.command,
            launcher_prefix=(os.fspath(Path("/usr/bin/env").resolve(strict=True)),),
            profile_hash="a" * 64,
            probe=probe,
        )

    def tearDown(self) -> None:
        try:
            _git(
                self.repo,
                "worktree",
                "remove",
                "--force",
                os.fspath(self.handle.path),
            )
        except subprocess.CalledProcessError:
            pass
        self.temporary.cleanup()

    def test_worker_runs_only_in_verified_worktree_and_never_integrates(self) -> None:
        task = WorkerTask(
            prompt=b"implement the bounded change",
            gates=(
                GateSpec(
                    argv=(
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path('worker-output.txt').read_text() == 'done\\n'",
                    ),
                    cwd=".",
                    timeout_seconds=5,
                ),
            ),
        )

        result = orchestrate_worker(
            self.repo,
            self.snapshot,
            self.handle,
            self.route,
            self.sandbox,
            task,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.attempts, 1)
        self.assertTrue(result.source_snapshot_hash)
        self.assertTrue(result.worker_delta_hash)
        self.assertTrue(result.projected_task_patch_hash)
        self.assertEqual((self.handle.path / "worker-output.txt").read_bytes(), b"done\n")
        self.assertFalse((self.repo / "worker-output.txt").exists())
        self.assertTrue(all(gate.status == "ok" for gate in result.gates))

    def test_unavailable_or_mismatched_sandbox_blocks_worker_launch(self) -> None:
        task = WorkerTask(prompt=b"must not run", gates=())
        unavailable = orchestrate_worker(
            self.repo,
            self.snapshot,
            self.handle,
            self.route,
            UnavailableSandbox("missing backend"),
            task,
        )
        self.assertEqual(unavailable.status, "sandbox_unavailable")
        self.assertFalse((self.handle.path / "worker-output.txt").exists())

        wrong_route = Route(
            route_id="wrong",
            transport=self.route.transport,
            band=self.route.band,
            roles=self.route.roles,
            read_only=False,
            command=self.route.command,
        )
        mismatched = orchestrate_worker(
            self.repo,
            self.snapshot,
            self.handle,
            wrong_route,
            self.sandbox,
            task,
        )
        self.assertEqual(mismatched.status, "sandbox_unavailable")
        self.assertFalse((self.handle.path / "worker-output.txt").exists())

    def test_red_gate_is_redispatched_only_to_the_retry_limit(self) -> None:
        task = WorkerTask(
            prompt=b"try and fix the gate",
            gates=(
                GateSpec(
                    argv=(sys.executable, "-c", "raise SystemExit(9)"),
                    cwd=".",
                    timeout_seconds=5,
                ),
            ),
        )

        result = orchestrate_worker(
            self.repo,
            self.snapshot,
            self.handle,
            self.route,
            self.sandbox,
            task,
        )

        self.assertEqual(result.status, "gate_failed")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(result.gates), 3)
        self.assertIsNone(result.worker_delta_hash)
        self.assertFalse((self.repo / "worker-output.txt").exists())

    def test_success_after_red_gate_returns_only_the_final_green_evidence(self) -> None:
        task = WorkerTask(
            prompt=b"fix the first trusted gate failure",
            gates=(
                GateSpec(
                    argv=(
                        sys.executable,
                        "-c",
                        (
                            "import os; from pathlib import Path; "
                            "marker = Path(os.environ['HOME']) / 'retry.marker'; "
                            "already_failed = marker.exists(); "
                            "marker.write_text('seen'); "
                            "raise SystemExit(0 if already_failed else 9)"
                        ),
                    ),
                    cwd=".",
                    timeout_seconds=5,
                ),
            ),
        )

        result = orchestrate_worker(
            self.repo,
            self.snapshot,
            self.handle,
            self.route,
            self.sandbox,
            task,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(result.gates), 1)
        self.assertTrue(all(gate.status == "ok" for gate in result.gates))
        self.assertTrue(result.projected_task_patch_hash)


if __name__ == "__main__":
    unittest.main()
