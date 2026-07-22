from __future__ import annotations

import json
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import runtime.token_saver.cli as cli_module
from runtime.token_saver.bundle import SealedGateEvidence, seal_delta_bundle
from runtime.token_saver.evidence import WorkerDelta
from runtime.token_saver.models import Status
from runtime.token_saver.repository import capture_source_snapshot
from runtime.token_saver.resources import CleanupResult, create_invocation_resources


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "token-saver-route.py"
COMMANDS = (
    "resolve",
    "review",
    "worker",
    "snapshot",
    "integrate",
    "validate-config",
    "setup-providers",
    "provider-exec",
    "cleanup",
)


class CliTests(unittest.TestCase):
    def _run(
        self,
        *args: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (sys.executable, str(SCRIPT), *args),
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_help_exposes_exact_command_surface(self) -> None:
        result = self._run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in COMMANDS:
            self.assertIn(command, result.stdout)
        self.assertNotIn("shell-command", result.stdout)

    def test_validate_config_prints_one_versioned_json_object(self) -> None:
        result = self._run(
            "validate-config",
            str(ROOT / "config" / "token-saver.example.json"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value, {"status": "ok", "version": 1})
        self.assertEqual(result.stderr, "")

    def test_configuration_error_uses_exit_code_two_and_json_stdout(self) -> None:
        result = self._run("validate-config", str(ROOT / "missing.json"))
        self.assertEqual(result.returncode, 2)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "needs_context")

    def test_provider_exec_requires_argument_terminator(self) -> None:
        result = self._run(
            "provider-exec",
            "--route",
            "kimi",
            "--policy",
            "safe",
        )
        self.assertEqual(result.returncode, 2)

    def test_sandboxed_provider_exec_fails_closed_without_manifest(self) -> None:
        result = self._run(
            "provider-exec",
            "--route",
            "kimi",
            "--policy",
            "sandboxed-worker",
            "--",
            "-p",
        )

        self.assertEqual(result.returncode, 3)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "sandbox_unavailable")

    def test_worker_cleanup_failure_reports_retained_recovery_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-cleanup-report-") as root_text:
            root = Path(root_text)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(("git", "-C", os.fspath(repository), "init", "-q"), check=True)
            temp_parent = root / "invocations"
            temp_parent.mkdir()
            resources = create_invocation_resources(repository, temp_parent)
            output = io.StringIO()
            with mock.patch.object(
                cli_module,
                "cleanup_invocation",
                return_value=CleanupResult(
                    status="rejected",
                    invocation_id=resources.invocation_id,
                    message="injected cleanup refusal",
                ),
            ), redirect_stdout(output):
                code = cli_module._emit_provider_worker_failure(
                    resources,
                    Status.GATE_FAILED,
                    message="gate failed",
                    exit_code=4,
                )

            value = json.loads(output.getvalue())
            self.assertEqual(code, 3)
            self.assertEqual(value["status"], "transport_error")
            self.assertEqual(value["transaction_status"], "gate_failed")
            self.assertEqual(value["cleanup_status"], "rejected")
            self.assertEqual(
                value["retained_manifest"],
                os.fspath(resources.manifest_path),
            )
            self.assertTrue(resources.manifest_path.is_file())

    def test_resolve_runs_explicit_main_through_preflight_and_finalization(self) -> None:
        result = self._run(
            "resolve",
            "--profile",
            "openai",
            "--main-route",
            "conversation",
            "--main-provider",
            "openai",
            "--main-model",
            "gpt-5.6-sol",
            "--main-variant",
            "high",
            "--main-band",
            "authority",
            "--host",
            "codex",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["mode"], "lite")
        self.assertEqual(value["authority"], "inline main loop")
        self.assertEqual(value["worker"], "none")
        self.assertEqual(
            value["main_loop_fingerprint"],
            "openai:gpt-5.6-sol:high",
        )
        self.assertIn("Main loop: conversation/gpt-5.6-sol", value["startup_verdict"])

    def test_resolve_native_max_without_host_telemetry_fails_closed(self) -> None:
        result = self._run(
            "resolve",
            "--profile",
            "openai",
            "--main-route",
            "conversation",
            "--main-provider",
            "openai",
            "--main-model",
            "gpt-5.6-terra",
            "--main-variant",
            "medium",
            "--main-band",
            "balanced",
            "--host",
            "codex",
            "--mode",
            "max",
            "--reviewer",
            "gpt-5.6-sol",
        )

        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "reviewer_unavailable")
        self.assertIsNone(value["mode"])
        self.assertIn("gpt-5.6-sol", " ".join(value["facts"]))

    def test_snapshot_outputs_only_hashes_and_redacted_counts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-cli-snapshot-") as root_text:
            root = Path(root_text)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(("git", "-C", os.fspath(repository), "init", "-q"), check=True)
            subprocess.run(
                (
                    "git",
                    "-C",
                    os.fspath(repository),
                    "-c",
                    "user.name=Token Saver",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-q",
                    "-m",
                    "base",
                ),
                check=True,
            )
            (repository / "allowed.txt").write_text("base\n", encoding="utf-8")
            git_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Token Saver",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Token Saver",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
            subprocess.run(
                ("git", "-C", os.fspath(repository), "add", "--", "allowed.txt"),
                check=True,
                env=git_environment,
            )
            subprocess.run(
                ("git", "-C", os.fspath(repository), "commit", "-q", "-m", "base"),
                check=True,
                env=git_environment,
            )
            (repository / "allowed.txt").write_text("changed\n", encoding="utf-8")
            private_name = "private-do-not-report.txt"
            private_value = "private-do-not-report-value"
            (repository / private_name).write_text(private_value, encoding="utf-8")
            task = root / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "prompt": "inspect the bounded source",
                        "allowed_paths": ["allowed.txt"],
                        "gates": [
                            {
                                "argv": ["true"],
                                "cwd": ".",
                                "timeout_seconds": 10,
                            }
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

            result = self._run(
                "snapshot",
                "--repo",
                os.fspath(repository),
                "--task",
                os.fspath(task),
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn(private_name, result.stdout)
            self.assertNotIn(private_value, result.stdout)
            value = json.loads(result.stdout)
            self.assertEqual(value["status"], "ok")
            self.assertEqual(len(value["source_snapshot_hash"]), 64)
            self.assertEqual(value["unstaged_count"], 1)
            self.assertEqual(value["private_record_count"], 1)
            self.assertEqual(len(value["private_aggregate_hash"]), 64)

    def test_review_executes_only_the_hardened_external_transport(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-cli-review-") as root_text:
            root = Path(root_text)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(("git", "-C", os.fspath(repository), "init", "-q"), check=True)
            subprocess.run(
                (
                    "git",
                    "-C",
                    os.fspath(repository),
                    "-c",
                    "user.name=Token Saver",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-q",
                    "-m",
                    "base",
                ),
                check=True,
            )
            temp_parent = root / "review-runs"
            temp_parent.mkdir()
            resources = create_invocation_resources(repository, temp_parent)
            snapshot = capture_source_snapshot(repository, ())
            seal_delta_bundle(
                resources,
                snapshot,
                WorkerDelta(records=()),
                gates=(
                    SealedGateEvidence(
                        argv=("true",),
                        cwd=".",
                        status="ok",
                        exit_code=0,
                        stdout_hash="1" * 64,
                        stderr_hash="2" * 64,
                        duration_milliseconds=1,
                    ),
                ),
                authority_mode="max",
            )
            identity = {
                "provider_family": "example",
                "resolved_model_id": "authority-v1",
                "variant": "default",
            }
            reviewer = root / "fake-reviewer.py"
            reviewer.write_text(
                f"#!{sys.executable}\n"
                "import hashlib, json, sys\n"
                "from pathlib import Path\n"
                "try:\n"
                "    (Path(__file__).parent / 'repository' / 'reviewer-write').write_text('forbidden')\n"
                "except OSError:\n"
                "    pass\n"
                "else:\n"
                "    raise SystemExit(88)\n"
                "raw = sys.stdin.buffer.read()\n"
                "value = json.loads(raw)\n"
                "if value.get('purpose') == 'identity':\n"
                f"    sys.stdout.write({json.dumps(identity, sort_keys=True, separators=(',', ':'))!r})\n"
                "else:\n"
                "    verdict = {\n"
                "        'version': 1,\n"
                "        'decision': 'approve',\n"
                "        'approval_binding_hash': value['approval_binding_hash'],\n"
                "        'review_packet_sha256': hashlib.sha256(raw).hexdigest(),\n"
                "        'message': 'bounded evidence approved',\n"
                "        'requested_changes': [],\n"
                "    }\n"
                "    sys.stdout.write(json.dumps(verdict, sort_keys=True, separators=(',', ':')))\n",
                encoding="utf-8",
            )
            reviewer.chmod(0o755)
            profile = root / "profile.json"
            profile.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "max",
                        "routes": {
                            "external-reviewer": {
                                "transport": "external-cli",
                                "band": "authority",
                                "roles": ["reviewer"],
                                "read_only": True,
                                "model": "authority-v1",
                                "provider_family": "example",
                                "variant": "default",
                                "command": [os.fspath(reviewer)],
                            }
                        },
                        "preferences": {
                            "reviewers": ["external-reviewer"],
                            "workers": [],
                            "scouts": [],
                            "mechanics": [],
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            context = root / "context.json"
            context.write_text(
                json.dumps(
                    {
                        "acceptance_criteria": ["trusted gate is green"],
                        "approved_plan": "review the exact sealed no-op patch",
                        "goal": "verify the bounded review path",
                        "main_loop_verdict": "approve",
                        "version": 1,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )

            inline_mismatch = self._run(
                "review",
                "--inline",
                "--main-fingerprint",
                "example:balanced-v1:default",
                "--manifest",
                os.fspath(resources.manifest_path),
                "--context",
                os.fspath(context),
            )
            self.assertEqual(inline_mismatch.returncode, 2)
            self.assertFalse(resources.final_evidence_path.exists())

            result = self._run(
                "review",
                "--profile",
                os.fspath(profile),
                "--route",
                "external-reviewer",
                "--main-fingerprint",
                "example:balanced-v1:default",
                "--manifest",
                os.fspath(resources.manifest_path),
                "--context",
                os.fspath(context),
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["status"], "ok")
            self.assertEqual(value["decision"], "approve")
            self.assertEqual(len(value["approval_binding_hash"]), 64)
            self.assertEqual(value["review_receipt"], os.fspath(resources.final_evidence_path))
            self.assertTrue(resources.final_evidence_path.is_file())
            self.assertFalse((repository / "reviewer-write").exists())

    def test_lite_inline_authority_seals_receipt_without_external_reviewer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-cli-lite-") as root_text:
            root = Path(root_text)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(("git", "-C", os.fspath(repository), "init", "-q"), check=True)
            subprocess.run(
                (
                    "git",
                    "-C",
                    os.fspath(repository),
                    "-c",
                    "user.name=Token Saver",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-q",
                    "-m",
                    "base",
                ),
                check=True,
            )
            temp_parent = root / "invocations"
            temp_parent.mkdir()
            resources = create_invocation_resources(repository, temp_parent)
            snapshot = capture_source_snapshot(repository, ())
            seal_delta_bundle(
                resources,
                snapshot,
                WorkerDelta(records=()),
                gates=(
                    SealedGateEvidence(
                        argv=("true",),
                        cwd=".",
                        status="ok",
                        exit_code=0,
                        stdout_hash="1" * 64,
                        stderr_hash="2" * 64,
                        duration_milliseconds=1,
                    ),
                ),
                authority_mode="lite",
            )
            context = root / "context.json"
            context.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "goal": "approve a bounded no-op",
                        "approved_plan": "verify the exact sealed evidence",
                        "acceptance_criteria": ["trusted gate is green"],
                        "main_loop_verdict": "approve",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )

            external_mismatch = self._run(
                "review",
                "--profile",
                os.fspath(root / "unused-profile.json"),
                "--route",
                "unused-reviewer",
                "--main-fingerprint",
                "openai:gpt-5.6-sol:high",
                "--manifest",
                os.fspath(resources.manifest_path),
                "--context",
                os.fspath(context),
            )
            self.assertEqual(external_mismatch.returncode, 2)
            self.assertFalse(resources.final_evidence_path.exists())

            reviewed = self._run(
                "review",
                "--inline",
                "--main-fingerprint",
                "openai:gpt-5.6-sol:high",
                "--manifest",
                os.fspath(resources.manifest_path),
                "--context",
                os.fspath(context),
            )

            self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
            reviewed_value = json.loads(reviewed.stdout)
            self.assertEqual(reviewed_value["mode"], "lite")
            self.assertEqual(reviewed_value["authority"], "inline-main-loop")
            self.assertTrue(resources.final_evidence_path.is_file())

            integrated = self._run("integrate", os.fspath(resources.manifest_path))
            self.assertEqual(integrated.returncode, 0, integrated.stdout + integrated.stderr)
            self.assertFalse(resources.invocation_root.exists())

    def test_cleanup_loads_and_consumes_an_exact_active_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-cli-cleanup-") as root_text:
            root = Path(root_text)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(
                ("git", "-C", os.fspath(repository), "init", "-q"),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            temp_parent = root / "invocations"
            temp_parent.mkdir()
            resources = create_invocation_resources(repository, temp_parent)

            result = self._run("cleanup", os.fspath(resources.manifest_path))

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["status"], "ok")
            self.assertEqual(value["cleanup_status"], "cleaned")
            self.assertEqual(value["invocation_id"], resources.invocation_id)
            self.assertFalse(resources.invocation_root.exists())

    def test_integrate_rejects_caller_supplied_approval_without_consuming_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="token-saver-cli-approval-") as root_text:
            root = Path(root_text)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(
                ("git", "-C", os.fspath(repository), "init", "-q"),
                check=True,
            )
            temp_parent = root / "invocations"
            temp_parent.mkdir()
            resources = create_invocation_resources(repository, temp_parent)
            approval = root / "approval.json"
            approval.write_text(
                '{"version":1,"version":1,"decision":"approve",'
                '"binding":{},"approval_binding_hash":"' + "a" * 64 + '"}\n',
                encoding="utf-8",
            )

            result = self._run(
                "integrate",
                os.fspath(resources.manifest_path),
                os.fspath(approval),
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("unrecognized arguments", result.stderr)
            self.assertTrue(resources.manifest_path.is_file())

    def test_one_shot_provider_worker_seals_then_integrates_only_approved_delta(
        self,
    ) -> None:
        if sys.platform != "darwin" and not sys.platform.startswith("linux"):
            self.skipTest("verified provider workers require macOS or Linux/WSL")
        if sys.platform.startswith("linux") and shutil.which("bwrap") is None:
            self.skipTest("Bubblewrap is unavailable")
        with tempfile.TemporaryDirectory(prefix="token-saver-cli-worker-") as root_text:
            root = Path(root_text)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(
                ("git", "-C", os.fspath(repository), "init", "-q"),
                check=True,
            )
            (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
            git_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Token Saver",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Token Saver",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
            subprocess.run(
                ("git", "-C", os.fspath(repository), "add", "--", "tracked.txt"),
                check=True,
                env=git_environment,
            )
            subprocess.run(
                ("git", "-C", os.fspath(repository), "commit", "-q", "-m", "base"),
                check=True,
                env=git_environment,
            )
            temp_parent = root / "invocations"
            temp_parent.mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            provider = fake_bin / "claude"
            provider.write_text(
                "#!/bin/sh\n"
                "mkdir -p \"$HOME/session\"\n"
                "printf 'state\\n' > \"$HOME/session/value\"\n"
                "test \"$(cat \"$HOME/session/value\")\" = state || exit 71\n"
                "if printf 'gitdir: forged\\n' > .git 2>/dev/null; then exit 75; fi\n"
                "git status --porcelain >/dev/null || exit 72\n"
                "git_dir=$(git rev-parse --absolute-git-dir) || exit 73\n"
                "case \"$git_dir\" in \"$HOME\"/*) ;; *) exit 74 ;; esac\n"
                "printf 'from worker\\n' > output.txt\n",
                encoding="utf-8",
            )
            provider.chmod(0o755)
            task_path = root / "task.json"
            task_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "prompt": "implement the bounded output",
                        "allowed_paths": ["output.txt"],
                        "gates": [
                            {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    (
                                        "import os; from pathlib import Path; "
                                        "assert Path('output.txt').read_text() == "
                                        "'from worker\\n'; "
                                        "marker = Path(os.environ['HOME']) / 'retry.marker'; "
                                        "already_failed = marker.exists(); "
                                        "marker.write_text('seen'); "
                                        "raise SystemExit(0 if already_failed else 9)"
                                    ),
                                ],
                                "cwd": ".",
                                "timeout_seconds": 10,
                            },
                            {
                                "argv": ["git", "status", "--porcelain"],
                                "cwd": ".",
                                "timeout_seconds": 10,
                            }
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "PATH": os.fspath(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                "HOME": os.fspath(root / "home"),
                "KIMI_BASE_URL": "https://kimi.invalid/",
                "KIMI_AUTH_TOKEN": "test-secret",
            }

            worker = self._run(
                "worker",
                "--repo",
                os.fspath(repository),
                "--temp-parent",
                os.fspath(temp_parent),
                "--route",
                "kimi",
                "--mode",
                "max",
                "--task",
                os.fspath(task_path),
                env=environment,
            )

            self.assertEqual(worker.returncode, 0, worker.stdout + worker.stderr)
            worker_value = json.loads(worker.stdout)
            self.assertEqual(worker_value["status"], "ok")
            self.assertEqual(worker_value["mode"], "max")
            self.assertEqual(worker_value["attempts"], 2)
            self.assertTrue(all(gate["status"] == "ok" for gate in worker_value["gates"]))
            self.assertFalse((repository / "output.txt").exists())
            manifest_path = Path(worker_value["manifest"])
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(Path(worker_value["bundle"]).is_file())
            reviewer = fake_bin / "reviewer"
            identity = {
                "provider_family": "example",
                "resolved_model_id": "authority-v1",
                "variant": "default",
            }
            reviewer.write_text(
                f"#!{sys.executable}\n"
                "import hashlib, json, sys\n"
                "raw = sys.stdin.buffer.read()\n"
                "value = json.loads(raw)\n"
                "if value.get('purpose') == 'identity':\n"
                f"    sys.stdout.write({json.dumps(identity, sort_keys=True, separators=(',', ':'))!r})\n"
                "else:\n"
                "    result = {\n"
                "        'version': 1, 'decision': 'approve',\n"
                "        'approval_binding_hash': value['approval_binding_hash'],\n"
                "        'review_packet_sha256': hashlib.sha256(raw).hexdigest(),\n"
                "        'message': 'approved sealed worker output',\n"
                "        'requested_changes': [],\n"
                "    }\n"
                "    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(',', ':')))\n",
                encoding="utf-8",
            )
            reviewer.chmod(0o755)
            profile = root / "review-profile.json"
            profile.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "max",
                        "routes": {
                            "reviewer": {
                                "transport": "external-cli",
                                "band": "authority",
                                "roles": ["reviewer"],
                                "read_only": True,
                                "model": "authority-v1",
                                "provider_family": "example",
                                "variant": "default",
                                "command": [os.fspath(reviewer)],
                            }
                        },
                        "preferences": {
                            "reviewers": ["reviewer"],
                            "workers": [],
                            "scouts": [],
                            "mechanics": [],
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            context = root / "review-context.json"
            context.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "goal": "create the bounded output",
                        "approved_plan": "delegate output.txt and run the exact gate",
                        "acceptance_criteria": ["output.txt contains the worker result"],
                        "main_loop_verdict": "approve",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )

            reviewed = self._run(
                "review",
                "--profile",
                os.fspath(profile),
                "--route",
                "reviewer",
                "--main-fingerprint",
                "example:balanced-v1:default",
                "--manifest",
                os.fspath(manifest_path),
                "--context",
                os.fspath(context),
                env=environment,
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
            self.assertEqual(json.loads(reviewed.stdout)["decision"], "approve")

            integrated = self._run(
                "integrate",
                os.fspath(manifest_path),
                env=environment,
            )

            self.assertEqual(
                integrated.returncode,
                0,
                integrated.stdout + integrated.stderr,
            )
            integrated_value = json.loads(integrated.stdout)
            self.assertEqual(integrated_value["status"], "ok")
            self.assertTrue(integrated_value["applied"])
            self.assertEqual(
                (repository / "output.txt").read_text(encoding="utf-8"),
                "from worker\n",
            )
            self.assertFalse(manifest_path.parent.exists())


if __name__ == "__main__":
    unittest.main()
