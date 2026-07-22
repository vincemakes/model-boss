from __future__ import annotations

import os
import platform
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.token_saver.models import Status
from runtime.token_saver.sandbox import (
    SandboxPolicy,
    UnavailableSandbox,
    VerifiedSandbox,
    select_verified_backend,
)


_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


@unittest.skipUnless(
    platform.system() == "Darwin" and _SANDBOX_EXEC.is_file(),
    "macOS sandbox-exec is unavailable",
)
class MacOSSandboxConformanceTests(unittest.TestCase):
    def test_real_probe_passes_every_boundary_before_route_can_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-macos-") as temporary:
            root = Path(temporary).resolve()
            worktree = root / "worktree"
            state = root / "state"
            runtime = root / "runtime"
            source = root / "source"
            for path in (worktree, state, runtime, source):
                path.mkdir()
            worker_sentinel = source / "worker-launched"
            route_argv = (
                os.fspath(Path(sys.executable).resolve()),
                "-I",
                "-c",
                "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('launched')",
                os.fspath(worker_sentinel),
            )
            policy = SandboxPolicy(
                worktree_root=worktree,
                route_state_root=state,
                readable_roots=(runtime,),
                protected_roots=(source,),
                network_required=False,
            )

            selected = select_verified_backend(
                policy,
                route_id="macos-worker",
                argv=route_argv,
            )

            self.assertIsInstance(selected, VerifiedSandbox)
            self.assertEqual(selected.status, Status.OK)
            self.assertEqual(selected.backend, "macos-sandbox-exec")
            self.assertTrue(selected.probe.passed)
            self.assertTrue(selected.probe.allowed_read)
            self.assertTrue(selected.probe.protected_read_denied)
            self.assertTrue(selected.probe.worktree_write)
            self.assertTrue(selected.probe.outside_write_denied)
            self.assertFalse(worker_sentinel.exists())
            self.assertEqual(tuple(worktree.iterdir()), ())
            launch = selected.prepare(
                route_id="macos-worker",
                argv=route_argv,
                policy=policy,
                cwd=worktree,
            )
            self.assertTrue(launch.available)
            self.assertEqual(launch.argv[0], os.fspath(_SANDBOX_EXEC))
            self.assertEqual(launch.argv[-len(route_argv) :], selected.route_argv)

    def test_missing_backend_returns_unavailable_without_route_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-macos-missing-") as temporary:
            root = Path(temporary).resolve()
            worktree = root / "worktree"
            state = root / "state"
            runtime = root / "runtime"
            source = root / "source"
            for path in (worktree, state, runtime, source):
                path.mkdir()
            worker_sentinel = source / "worker-launched"
            route_argv = (
                os.fspath(Path(sys.executable).resolve()),
                "-I",
                "-c",
                "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('launched')",
                os.fspath(worker_sentinel),
            )
            policy = SandboxPolicy(
                worktree_root=worktree,
                route_state_root=state,
                readable_roots=(runtime,),
                protected_roots=(source,),
                network_required=False,
            )

            selected = select_verified_backend(
                policy,
                route_id="macos-worker",
                argv=route_argv,
                backend_executable=root / "missing-sandbox-exec",
            )

            self.assertIsInstance(selected, UnavailableSandbox)
            self.assertEqual(selected.status, Status.SANDBOX_UNAVAILABLE)
            self.assertFalse(worker_sentinel.exists())


if __name__ == "__main__":
    unittest.main()
