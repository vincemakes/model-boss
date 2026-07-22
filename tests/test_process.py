from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.model_boss.process import ProcessSpec, run_process


class ProcessRunnerTests(unittest.TestCase):
    def test_exact_argv_stdin_and_clean_environment(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            os.environ["MODEL_BOSS_PARENT_SENTINEL"] = "must-not-leak"
            code = (
                "import json,os,sys; "
                "print(json.dumps({'argv':sys.argv[1:],"
                "'stdin':sys.stdin.buffer.read().decode(),"
                "'sentinel':os.getenv('MODEL_BOSS_PARENT_SENTINEL')}))"
            )
            spec = ProcessSpec(
                argv=(sys.executable, "-c", code, "space value", "$(literal)", ";"),
                cwd=Path(root),
                stdin=b"packet bytes",
                env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
                timeout_seconds=5,
            )

            result = run_process(spec)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.returncode, 0)
            self.assertIn(b'"stdin": "packet bytes"', result.stdout)
            self.assertIn(b'"space value"', result.stdout)
            self.assertIn(b'"$(literal)"', result.stdout)
            self.assertIn(b'"sentinel": null', result.stdout)

    def test_nonzero_exit_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = run_process(
                ProcessSpec(
                    argv=(sys.executable, "-c", "raise SystemExit(17)"),
                    cwd=Path(root),
                    env={"PATH": os.defpath},
                )
            )
        self.assertEqual(result.status, "transport_error")
        self.assertEqual(result.returncode, 17)

    def test_stdout_stderr_are_bounded_and_credentials_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            secret = "highly-secret-value"
            code = (
                "import os,sys; s=os.environ['SECRET']; "
                "sys.stdout.write('Authorization: Bearer '+s+'\\n'+'x'*10000); "
                "sys.stderr.write(s+'\\n'+'y'*10000)"
            )
            result = run_process(
                ProcessSpec(
                    argv=(sys.executable, "-c", code),
                    cwd=Path(root),
                    env={"PATH": os.defpath, "SECRET": secret},
                    redact_values=(secret,),
                    stdout_limit=128,
                    stderr_limit=128,
                )
            )
        self.assertLessEqual(len(result.stdout), 128)
        self.assertLessEqual(len(result.stderr), 128)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)
        self.assertNotIn(secret.encode(), result.stdout + result.stderr)
        self.assertNotIn(b"Authorization: Bearer", result.stdout)

    def test_timeout_terminates_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "grandchild-survived"
            child = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c',"
                "'import pathlib,time,sys; time.sleep(2); pathlib.Path(sys.argv[1]).write_text(\"bad\")',"
                "sys.argv[1]]); time.sleep(30)"
            )
            result = run_process(
                ProcessSpec(
                    argv=(sys.executable, "-c", child, os.fspath(marker)),
                    cwd=Path(root),
                    env={"PATH": os.defpath},
                    timeout_seconds=0.2,
                    terminate_grace_seconds=0.2,
                )
            )
            self.assertEqual(result.status, "timeout")
            self.assertTrue(result.timed_out)
            self.assertFalse(marker.exists())

    def test_invalid_spec_is_rejected_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                ProcessSpec(argv=("relative-command",), cwd=Path(root), env={})
            with self.assertRaises(ValueError):
                ProcessSpec(
                    argv=(sys.executable,),
                    cwd=Path(root),
                    env={"BAD=NAME": "x"},
                )


if __name__ == "__main__":
    unittest.main()
