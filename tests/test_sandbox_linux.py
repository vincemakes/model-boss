from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.model_boss.models import Status
from runtime.model_boss.sandbox import SandboxPolicy, VerifiedSandbox, select_verified_backend


_BWRAP = shutil.which("bwrap")


@unittest.skipUnless(
    platform.system() == "Linux" and _BWRAP is not None,
    "Linux Bubblewrap executable is unavailable",
)
class LinuxSandboxConformanceTests(unittest.TestCase):
    def test_real_probe_passes_every_boundary_before_route_can_launch(self) -> None:
        assert _BWRAP is not None
        with tempfile.TemporaryDirectory(prefix="model-boss-linux-") as temporary:
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
                route_id="linux-worker",
                argv=route_argv,
            )

            self.assertIsInstance(selected, VerifiedSandbox)
            self.assertEqual(selected.status, Status.OK)
            self.assertEqual(selected.backend, "linux-bwrap")
            self.assertTrue(selected.probe.passed)
            self.assertFalse(worker_sentinel.exists())
            self.assertEqual(tuple(worktree.iterdir()), ())
            launch = selected.prepare(
                route_id="linux-worker",
                argv=route_argv,
                policy=policy,
                cwd=worktree,
            )
            self.assertTrue(launch.available)
            self.assertEqual(launch.argv[0], os.path.realpath(_BWRAP))
            self.assertEqual(launch.argv[-len(route_argv) :], selected.route_argv)


if __name__ == "__main__":
    unittest.main()
