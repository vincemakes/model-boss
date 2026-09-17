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

import runtime.model_boss.cli as cli_module
from runtime.model_boss.bundle import (
    SealedGateEvidence,
    build_plan_review_packet,
    seal_delta_bundle,
    seal_plan_review_receipt,
)
from runtime.model_boss.evidence import WorkerDelta
from runtime.model_boss.models import (
    FingerprintEvidenceSource,
    ModelFingerprint,
    RouteProbeResult,
    Status,
)
from runtime.model_boss.repository import capture_source_snapshot
from runtime.model_boss.resources import CleanupResult, create_invocation_resources
from runtime.model_boss.transport import ReviewerTransportResult, ReviewerVerdict


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "boss-dispatch" / "scripts" / "model-boss.py"
COMMANDS = (
    "resolve",
    "match-models",
    "estimate",
    "plan-review",
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
    def _approved_plan(
        self,
        repository: Path,
        temp_parent: Path,
        task_path: Path,
    ):
        resources = create_invocation_resources(repository, temp_parent)
        _, allowed_paths, canonical_task = cli_module._read_worker_task(task_path)
        snapshot = capture_source_snapshot(repository, allowed_paths)
        packet = build_plan_review_packet(
            canonical_task,
            snapshot,
            {
                "version": 1,
                "goal": "perform the bounded task",
                "proposed_plan": "change only the approved paths",
                "acceptance_criteria": ["the gate passes"],
                "risks": [],
            },
            invocation_id=resources.invocation_id,
            main_fingerprint="example:balanced-v1:default",
            reviewer_route_id="reviewer",
            reviewer_fingerprint="example:authority-v1:default",
            fingerprint_evidence_source="identity-handshake",
            reviewer_read_only_enforced=True,
        )
        binding = json.loads(packet)["approval_binding_hash"]
        seal_plan_review_receipt(
            resources,
            packet=packet,
            decision="approve",
            approval_binding_hash=binding,
            reviewer_route_id="reviewer",
            reviewer_fingerprint="example:authority-v1:default",
            fingerprint_evidence_source="identity-handshake",
            reviewer_read_only_enforced=True,
            main_fingerprint="example:balanced-v1:default",
            message="approved exact plan",
            requested_changes=(),
        )
        return resources

    def test_plan_review_and_worker_manifest_contract_is_explicit(self) -> None:
        parser = cli_module.build_parser()
        plan = parser.parse_args(
            (
                "plan-review",
                "--repo",
                "/repo",
                "--temp-parent",
                "/tmp/parent",
                "--task",
                "/tmp/task.json",
                "--context",
                "/tmp/context.json",
                "--profile",
                "/tmp/profile.json",
                "--route",
                "reviewer",
                "--main-fingerprint",
                "example:balanced-v1:default",
            )
        )
        self.assertEqual(plan.command, "plan-review")
        worker = parser.parse_args(
            (
                "worker",
                "--repo",
                "/repo",
                "--temp-parent",
                "/tmp/parent",
                "--route",
                "kimi",
                "--task",
                "/tmp/task.json",
                "--mode",
                "max",
                "--manifest",
                "/tmp/manifest.json",
            )
        )
        self.assertEqual(worker.manifest, "/tmp/manifest.json")

    def test_max_worker_fails_closed_on_task_or_source_mutation(self) -> None:
        for mutation in ("task", "source"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root_text:
                root = Path(root_text)
                repository = root / "repo"
                repository.mkdir()
                subprocess.run(("git", "-C", os.fspath(repository), "init", "-q"), check=True)
                (repository / "allowed.txt").write_text("base\n", encoding="utf-8")
                subprocess.run(("git", "-C", os.fspath(repository), "add", "allowed.txt"), check=True)
                subprocess.run(
                    (
                        "git", "-C", os.fspath(repository), "-c", "user.name=Model Boss",
                        "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "base",
                    ),
                    check=True,
                )
                temp_parent = root / "tmp"
                temp_parent.mkdir()
                task_path = root / "task.json"
                task_value = {
                    "version": 1,
                    "prompt": "change allowed.txt",
                    "allowed_paths": ["allowed.txt"],
                    "gates": [{"argv": ["true"], "cwd": ".", "timeout_seconds": 10}],
                }
                task_path.write_text(
                    json.dumps(task_value, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                resources = self._approved_plan(repository, temp_parent, task_path)
                if mutation == "task":
                    task_value["prompt"] = "different task"
                    task_path.write_text(
                        json.dumps(task_value, sort_keys=True, separators=(",", ":")),
                        encoding="utf-8",
                    )
                else:
                    (repository / "allowed.txt").write_text("changed\n", encoding="utf-8")

                result = self._run(
                    "worker",
                    "--repo", os.fspath(repository),
                    "--temp-parent", os.fspath(temp_parent),
                    "--route", "kimi",
                    "--mode", "max",
                    "--task", os.fspath(task_path),
                    "--manifest", os.fspath(resources.manifest_path),
                )

                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], "needs_context")
                self.assertFalse(resources.invocation_root.exists())

    def test_worker_manifest_is_required_only_for_max(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            repository = root / "repo"
            repository.mkdir()
            subprocess.run(("git", "-C", os.fspath(repository), "init", "-q"), check=True)
            subprocess.run(
                (
                    "git", "-C", os.fspath(repository), "-c", "user.name=Model Boss",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-q", "-m", "base",
                ),
                check=True,
            )
            temp_parent = root / "tmp"
            temp_parent.mkdir()
            task_path = root / "task.json"
            task_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "prompt": "create output",
                        "allowed_paths": ["output.txt"],
                        "gates": [{"argv": ["true"], "cwd": ".", "timeout_seconds": 10}],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            unrelated = create_invocation_resources(repository, temp_parent)
            lite = self._run(
                "worker", "--repo", os.fspath(repository), "--temp-parent", os.fspath(temp_parent),
                "--route", "kimi", "--mode", "lite", "--task", os.fspath(task_path),
                "--manifest", os.fspath(unrelated.manifest_path),
            )
            maximum = self._run(
                "worker", "--repo", os.fspath(repository), "--temp-parent", os.fspath(temp_parent),
                "--route", "kimi", "--mode", "max", "--task", os.fspath(task_path),
            )
            self.assertEqual(lite.returncode, 2)
            self.assertEqual(maximum.returncode, 2)
            self.assertTrue(unrelated.manifest_path.exists())

    def test_plan_revise_cleans_the_unapproved_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            repository = root / "repo"
            repository.mkdir()
            subprocess.run(("git", "-C", os.fspath(repository), "init", "-q"), check=True)
            subprocess.run(
                (
                    "git", "-C", os.fspath(repository), "-c", "user.name=Model Boss",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-q", "-m", "base",
                ),
                check=True,
            )
            temp_parent = root / "tmp"
            temp_parent.mkdir()
            task = root / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "version": 1, "prompt": "create output",
                        "allowed_paths": ["output.txt"],
                        "gates": [{"argv": ["true"], "cwd": ".", "timeout_seconds": 10}],
                    },
                    sort_keys=True, separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            context = root / "context.json"
            context.write_text(
                json.dumps(
                    {
                        "version": 1, "goal": "create output",
                        "proposed_plan": "change output.txt",
                        "acceptance_criteria": ["gate passes"], "risks": [],
                    },
                    sort_keys=True, separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            profile = root / "profile.json"
            profile.write_text(
                json.dumps(
                    {
                        "schema_version": 1, "mode": "max",
                        "routes": {
                            "reviewer": {
                                "transport": "external-cli", "band": "authority",
                                "roles": ["reviewer"], "read_only": True,
                                "provider_family": "example", "model": "authority-v1",
                                "variant": "default", "command": [sys.executable],
                            }
                        },
                        "preferences": {"reviewers": ["reviewer"], "workers": [], "scouts": [], "mechanics": []},
                    },
                    sort_keys=True, separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            arguments = cli_module.build_parser().parse_args(
                (
                    "plan-review", "--repo", os.fspath(repository),
                    "--temp-parent", os.fspath(temp_parent), "--task", os.fspath(task),
                    "--context", os.fspath(context), "--profile", os.fspath(profile),
                    "--route", "reviewer", "--main-fingerprint", "example:balanced-v1:default",
                )
            )
            probe = RouteProbeResult(
                route_id="reviewer", reachable=True,
                resolved_fingerprint=ModelFingerprint("example", "authority-v1", "default"),
                fingerprint_evidence_source=FingerprintEvidenceSource.IDENTITY_HANDSHAKE,
                executable_available=True, native_agent_available=False,
                reviewer_read_only_enforced=True,
            )

            def revise(_route, packet, binding_hash, **_kwargs):
                return ReviewerTransportResult(
                    Status.OK,
                    ReviewerVerdict(
                        version=1, decision="revise", approval_binding_hash=binding_hash,
                        review_packet_sha256=__import__("hashlib").sha256(packet).hexdigest(),
                        message="needs a narrower plan", requested_changes=("narrow scope",),
                    ),
                )

            output = io.StringIO()
            with (
                mock.patch.object(cli_module, "probe_route", return_value=probe),
                mock.patch.object(cli_module, "execute_reviewer", side_effect=revise),
                redirect_stdout(output),
            ):
                code = cli_module._run_plan_review_command(arguments)

            value = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(value["decision"], "revise")
            self.assertEqual(value["cleanup_status"], "cleaned")
            leftovers = tuple(temp_parent.iterdir())
            self.assertTrue(leftovers)
            self.assertTrue(
                all(path.name.startswith(".model-boss-consumed-") for path in leftovers)
            )
            self.assertFalse(any(path.name.startswith("model-boss-invocation-") for path in leftovers))

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
        self.assertTrue(result.stdout.startswith("usage: model-boss "))
        self.assertNotIn("model-boss-route", result.stdout)
        self.assertIn("Model Boss", result.stdout)
        for command in COMMANDS:
            self.assertIn(command, result.stdout)
        self.assertNotIn("shell-command", result.stdout)
        self.assertIn("explicitly migrate provider data", result.stdout)

    def test_credentials_discovery_uses_only_model_boss_contract(self) -> None:
        legacy_credentials = "TOKEN" + "_SAVER_CREDENTIALS"
        self.assertEqual(
            cli_module._provider_credentials_path(
                {"HOME": "/home/test", legacy_credentials: "/legacy.json"}
            ),
            Path("/home/test/.config/model-boss/credentials.json"),
        )
        self.assertEqual(
            cli_module._provider_credentials_path(
                {
                    "HOME": "/home/test",
                    "XDG_CONFIG_HOME": "/xdg",
                    "MODEL_BOSS_CREDENTIALS": "/explicit/credentials.json",
                }
            ),
            Path("/explicit/credentials.json"),
        )
        with self.assertRaises(cli_module.SetupError):
            cli_module._provider_credentials_path(
                {"HOME": "/home/test", "MODEL_BOSS_CREDENTIALS": "relative.json"}
            )

    def test_credentials_discovery_uses_userprofile_only_without_home(self) -> None:
        try:
            userprofile_path = cli_module._provider_credentials_path(
                {"USERPROFILE": "/windows/user"}
            )
        except cli_module.SetupError as exc:
            self.fail(f"absolute USERPROFILE was rejected: {exc}")
        self.assertEqual(
            userprofile_path,
            Path("/windows/user/.config/model-boss/credentials.json"),
        )
        self.assertEqual(
            cli_module._provider_credentials_path(
                {"HOME": "/preferred/home", "USERPROFILE": "/windows/user"}
            ),
            Path("/preferred/home/.config/model-boss/credentials.json"),
        )
        self.assertEqual(
            cli_module._provider_credentials_path(
                {
                    "XDG_CONFIG_HOME": "/xdg/config",
                    "HOME": "/preferred/home",
                    "USERPROFILE": "/windows/user",
                }
            ),
            Path("/xdg/config/model-boss/credentials.json"),
        )
        with self.assertRaises(cli_module.SetupError):
            cli_module._provider_credentials_path(
                {"HOME": "relative/home", "USERPROFILE": "/windows/user"}
            )
        with self.assertRaises(cli_module.SetupError):
            cli_module._provider_credentials_path(
                {"USERPROFILE": "relative/user"}
            )

    def test_validate_config_prints_one_versioned_json_object(self) -> None:
        result = self._run(
            "validate-config",
            str(ROOT / "boss-dispatch" / "config" / "model-boss.example.json"),
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
        with tempfile.TemporaryDirectory(prefix="model-boss-cleanup-report-") as root_text:
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
        with tempfile.TemporaryDirectory(prefix="model-boss-cli-snapshot-") as root_text:
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
                    "user.name=Model Boss",
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
                "GIT_AUTHOR_NAME": "Model Boss",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Model Boss",
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
        with tempfile.TemporaryDirectory(prefix="model-boss-cli-review-") as root_text:
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
                    "user.name=Model Boss",
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
            plan_task = b'{"allowed_paths":["review.txt"],"gates":[{"argv":["true"],"cwd":".","timeout_seconds":10}],"prompt":"review no-op evidence","version":1}'
            plan_packet = build_plan_review_packet(
                plan_task,
                snapshot,
                {
                    "version": 1,
                    "goal": "verify the bounded review path",
                    "proposed_plan": "review the exact sealed no-op patch",
                    "acceptance_criteria": ["trusted gate is green"],
                    "risks": [],
                },
                invocation_id=resources.invocation_id,
                main_fingerprint="example:balanced-v1:default",
                reviewer_route_id="external-reviewer",
                reviewer_fingerprint="example:authority-v1:default",
                fingerprint_evidence_source="identity-handshake",
                reviewer_read_only_enforced=True,
            )
            plan_binding = json.loads(plan_packet)["approval_binding_hash"]
            seal_plan_review_receipt(
                resources,
                packet=plan_packet,
                decision="approve",
                approval_binding_hash=plan_binding,
                reviewer_route_id="external-reviewer",
                reviewer_fingerprint="example:authority-v1:default",
                fingerprint_evidence_source="identity-handshake",
                reviewer_read_only_enforced=True,
                main_fingerprint="example:balanced-v1:default",
                message="approved exact plan",
                requested_changes=(),
            )
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
        with tempfile.TemporaryDirectory(prefix="model-boss-cli-lite-") as root_text:
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
                    "user.name=Model Boss",
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
        with tempfile.TemporaryDirectory(prefix="model-boss-cli-cleanup-") as root_text:
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
        with tempfile.TemporaryDirectory(prefix="model-boss-cli-approval-") as root_text:
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
        with tempfile.TemporaryDirectory(prefix="model-boss-cli-worker-") as root_text:
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
                "GIT_AUTHOR_NAME": "Model Boss",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Model Boss",
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
                "        'message': 'approved exact evidence',\n"
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
            missing_profile = root / "missing-reviewer-credential-profile.json"
            missing_value = json.loads(profile.read_text(encoding="utf-8"))
            missing_value["routes"]["reviewer"]["credential_env"] = [
                {
                    "child_name": "REVIEWER_API_KEY",
                    "source_name": "MODEL_BOSS_TEST_MISSING_REVIEWER_KEY",
                }
            ]
            missing_profile.write_text(
                json.dumps(missing_value, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            environment.pop("MODEL_BOSS_TEST_MISSING_REVIEWER_KEY", None)
            plan_context = root / "plan-context.json"
            plan_context.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "goal": "create the bounded output",
                        "proposed_plan": "delegate output.txt and run the exact gate",
                        "acceptance_criteria": ["output.txt contains the worker result"],
                        "risks": ["do not change tracked.txt"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            missing_plan = self._run(
                "plan-review",
                "--repo",
                os.fspath(repository),
                "--temp-parent",
                os.fspath(temp_parent),
                "--task",
                os.fspath(task_path),
                "--context",
                os.fspath(plan_context),
                "--profile",
                os.fspath(missing_profile),
                "--route",
                "reviewer",
                "--main-fingerprint",
                "example:balanced-v1:default",
                env=environment,
            )
            self.assertEqual(missing_plan.returncode, 3)
            self.assertEqual(
                json.loads(missing_plan.stdout)["status"],
                "reviewer_unavailable",
            )
            planned = self._run(
                "plan-review",
                "--repo",
                os.fspath(repository),
                "--temp-parent",
                os.fspath(temp_parent),
                "--task",
                os.fspath(task_path),
                "--context",
                os.fspath(plan_context),
                "--profile",
                os.fspath(profile),
                "--route",
                "reviewer",
                "--main-fingerprint",
                "example:balanced-v1:default",
                env=environment,
            )
            self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
            plan_value = json.loads(planned.stdout)
            self.assertEqual(plan_value["decision"], "approve")
            plan_manifest = Path(plan_value["manifest"])

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
                "--manifest",
                os.fspath(plan_manifest),
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
            missing_final = self._run(
                "review",
                "--profile",
                os.fspath(missing_profile),
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
            self.assertEqual(missing_final.returncode, 3)
            self.assertEqual(
                json.loads(missing_final.stdout)["status"],
                "reviewer_unavailable",
            )

            mismatched_profile = root / "mismatched-review-profile.json"
            mismatched_value = json.loads(profile.read_text(encoding="utf-8"))
            mismatched_value["routes"]["other-reviewer"] = dict(
                mismatched_value["routes"]["reviewer"]
            )
            mismatched_value["preferences"]["reviewers"] = ["other-reviewer"]
            mismatched_profile.write_text(
                json.dumps(mismatched_value, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            mismatched = self._run(
                "review",
                "--profile",
                os.fspath(mismatched_profile),
                "--route",
                "other-reviewer",
                "--main-fingerprint",
                "example:balanced-v1:default",
                "--manifest",
                os.fspath(manifest_path),
                "--context",
                os.fspath(context),
                env=environment,
            )
            self.assertEqual(mismatched.returncode, 3)
            self.assertEqual(json.loads(mismatched.stdout)["status"], "reviewer_unavailable")

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


class RoutingUpgradeCliTests(unittest.TestCase):
    """match-models and estimate are pure, credential-free helpers for CLASSIFY."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )

    def test_match_models_maps_spoken_versions_to_configured_routes(self) -> None:
        result = self._run(
            "match-models",
            "--profile",
            "anthropic",
            "--text",
            "走 model boss,让 opus 4.6 去开发,你来审核",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["unmatched"], [])
        self.assertEqual(len(value["mentions"]), 1)
        mention = value["mentions"][0]
        self.assertEqual(mention["model"], "claude-opus-4-6")
        self.assertEqual(mention["routes"], ["opus-4.6", "opus-4.6-worker"])
        self.assertEqual(mention["roles"]["opus-4.6-worker"], ["worker"])

    def test_match_models_refuses_to_guess_an_unknown_version(self) -> None:
        result = self._run(
            "match-models",
            "--profile",
            "anthropic",
            "--text",
            "let fable 6 handle the review",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "needs_context")
        self.assertEqual(value["unmatched"], ["fable6"])
        self.assertEqual(value["mentions"], [])

    def test_estimate_prices_inline_lite_and_max_and_recommends(self) -> None:
        result = self._run(
            "estimate",
            "--profile",
            "anthropic",
            "--main-model",
            "claude-fable-5-1",
            "--main-effort",
            "high",
            "--worker",
            "sonnet-5",
            "--reviewer",
            "opus-5",
            "--lines",
            "1100",
            "--files",
            "8",
            "--judgment",
            "low",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "ok")
        self.assertEqual([plan["plan"] for plan in value["plans"]], ["inline", "lite", "max"])
        self.assertEqual(value["recommendation"], "lite")
        self.assertEqual(value["objective"], "pace")
        self.assertIn("Recommendation (pace): lite", value["table"])
        lite = value["plans"][1]
        self.assertEqual([role["model"] for role in lite["roles"]], ["claude-fable-5-1", "claude-sonnet-5"])
        self.assertLess(lite["main_model_usd"], value["plans"][0]["main_model_usd"])

    def test_estimate_steps_aside_below_the_floor_and_for_judgment_work(self) -> None:
        small = self._run(
            "estimate", "--profile", "anthropic", "--main-model", "claude-fable-5-1",
            "--worker", "opus-5-worker", "--lines", "30", "--files", "1",
        )
        self.assertEqual(json.loads(small.stdout)["recommendation"], "inline")
        debugging = self._run(
            "estimate", "--profile", "anthropic", "--main-model", "claude-fable-5-1",
            "--worker", "opus-5-worker", "--lines", "800", "--files", "9", "--judgment", "high",
        )
        self.assertEqual(json.loads(debugging.stdout)["recommendation"], "inline")
        unclear = self._run(
            "estimate", "--profile", "anthropic", "--main-model", "claude-fable-5-1",
            "--worker", "opus-5-worker", "--lines", "800", "--files", "9", "--spec", "unclear",
        )
        self.assertEqual(unclear.returncode, 2)
        self.assertEqual(json.loads(unclear.stdout)["recommendation"], "needs_context")

    def test_estimate_requires_catalog_prices(self) -> None:
        result = self._run(
            "estimate", "--profile", "anthropic", "--main-model", "claude-unknown-9",
            "--worker", "opus-5-worker", "--lines", "500", "--files", "5",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "needs_context")

    def test_resolve_prints_effort_in_the_startup_verdict(self) -> None:
        result = self._run(
            "resolve", "--profile", "anthropic", "--main-route", "conversation",
            "--main-provider", "anthropic", "--main-model", "claude-fable-5-1",
            "--main-variant", "default", "--main-band", "authority", "--host", "claude-code",
            "--main-effort", "medium", "--mode", "lite",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["main_loop_effort"], "medium")
        self.assertIn("Main loop: conversation/claude-fable-5-1@medium", value["startup_verdict"])

    def test_estimate_accepts_budget_pace_and_packets(self) -> None:
        result = self._run(
            "estimate", "--profile", "anthropic", "--main-model", "claude-fable-5-1",
            "--main-effort", "medium", "--worker", "opus-5-worker", "--lines", "900", "--files", "9",
            "--judgment", "low", "--packets", "3", "--total-used", "40", "--fable-used", "70", "--week-elapsed", "40",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["regime"], "fable_ahead")
        self.assertEqual(value["recommendation"], "lite")
        self.assertAlmostEqual(value["budget"]["capped_share_of_spend"], 0.875)
        self.assertTrue(any("next session" in reason for reason in value["reasons"]))
        partial = self._run(
            "estimate", "--profile", "anthropic", "--main-model", "claude-fable-5-1",
            "--worker", "opus-5-worker", "--lines", "900", "--files", "9", "--total-used", "40",
        )
        self.assertEqual(partial.returncode, 2)
