from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.token_saver.evidence import ApprovalBinding
from runtime.token_saver.models import (
    CapabilityBand,
    ModelFingerprint,
    Role,
    Route,
    Transport,
)
from runtime.token_saver.process import ProcessResult, ProcessSpec, run_process
from runtime.token_saver.sandbox import (
    ConformanceProbe,
    SandboxPolicy,
    UnavailableSandbox,
    VerifiedSandbox,
    select_verified_backend,
)
from runtime.token_saver.transport import (
    _resolve_executable,
    _reviewer_runtime_roots,
    execute_reviewer,
    probe_route,
)


BINDING_VALUE = ApprovalBinding(
    source_snapshot_hash="1" * 64,
    worker_delta_hash="2" * 64,
    projected_task_patch_hash="3" * 64,
)
BINDING = BINDING_VALUE.canonical_hash
PACKET = json.dumps(
    {
        "version": 1,
        "source_snapshot_hash": "1" * 64,
        "worker_delta_hash": "2" * 64,
        "projected_task_patch_hash": "3" * 64,
        "approval_binding_hash": BINDING,
    },
    sort_keys=True,
    separators=(",", ":"),
).encode("ascii")
PACKET_HASH = hashlib.sha256(PACKET).hexdigest()


def _route(command: tuple[str, ...]) -> Route:
    return Route(
        route_id="reviewer",
        transport=Transport.EXTERNAL_CLI,
        band=CapabilityBand.AUTHORITY,
        roles=frozenset({Role.REVIEWER}),
        read_only=True,
        command=command,
        provider_family="example",
        model="authority-v1",
        variant="default",
    )


def _result(stdout: bytes, *, returncode: int = 0, status: str = "ok") -> ProcessResult:
    return ProcessResult(
        status=status,
        returncode=returncode,
        stdout=stdout,
        stderr=b"",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=status == "timeout",
        duration_seconds=0.01,
    )


def _verified_sandbox_factory(policy, *, route_id, argv, probe_parent=None):
    del probe_parent
    return VerifiedSandbox._from_successful_probe(
        backend="reviewer-unit-test",
        policy=policy,
        route_id=route_id,
        route_argv=argv,
        launcher_prefix=(os.fspath(Path("/usr/bin/env").resolve(strict=True)),),
        profile_hash="f" * 64,
        probe=ConformanceProbe(True, True, True, True, True),
    )


class ReviewerTransportTests(unittest.TestCase):
    def _run(self, command: tuple[str, ...], output: object, **kwargs):
        captured = []

        def runner(spec):
            captured.append(spec)
            if isinstance(output, ProcessResult):
                return output
            return _result(
                json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
            )

        with tempfile.TemporaryDirectory() as root:
            parent = Path(root) / "evidence-parent"
            state = Path(root) / "state"
            parent.mkdir()
            state.mkdir()
            result = execute_reviewer(
                _route(command),
                PACKET,
                BINDING,
                evidence_parent=parent,
                route_state_root=state,
                process_runner=runner,
                sandbox_factory=_verified_sandbox_factory,
                **kwargs,
            )
            self.assertEqual(tuple(parent.iterdir()), ())
        return result, captured

    def test_claude_exact_safe_argv_and_single_json_verdict(self) -> None:
        verdict = {
            "version": 1,
            "decision": "approve",
            "approval_binding_hash": BINDING,
            "review_packet_sha256": PACKET_HASH,
            "message": "looks good",
            "requested_changes": [],
        }
        result, captured = self._run((sys.executable,), verdict)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.verdict.decision, "approve")
        self.assertEqual(
            captured[0].argv[-8:],
            (
                "--safe-mode",
                "--no-session-persistence",
                "--permission-mode",
                "plan",
                "--tools",
                "",
                "-p",
                "-",
            ),
        )
        self.assertEqual(captured[0].stdin, PACKET)
        self.assertEqual(captured[0].cwd.name, "evidence")

    def test_codex_exact_ephemeral_read_only_argv(self) -> None:
        verdict = {
            "version": 1,
            "decision": "approve",
            "approval_binding_hash": BINDING,
            "review_packet_sha256": PACKET_HASH,
            "message": "ok",
            "requested_changes": [],
        }
        result, captured = self._run((sys.executable, "codex"), verdict)
        self.assertEqual(result.status, "ok")
        argv = captured[0].argv
        self.assertEqual(
            argv,
            (
                os.fspath(Path("/usr/bin/env").resolve(strict=True)),
                os.fspath(Path(sys.executable).resolve(strict=True)),
                "codex",
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "-C",
                os.fspath(captured[0].cwd),
                "-",
            ),
        )

    def test_codex_preflight_uses_live_identity_not_a_guessed_version_floor(self) -> None:
        fingerprint = ModelFingerprint("openai", "gpt-5.6-sol", "high")
        captured = []

        def runner(spec):
            captured.append(spec)
            return _result(
                json.dumps(
                    {
                        "provider_family": fingerprint.provider_family,
                        "resolved_model_id": fingerprint.resolved_model_id,
                        "variant": fingerprint.variant,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
            )

        report = probe_route(
            _route((sys.executable, "codex")),
            Role.REVIEWER,
            {},
            _verified_sandbox_factory,
            runner,
        )

        self.assertTrue(report.reachable)
        self.assertEqual(report.resolved_fingerprint, fingerprint)
        self.assertTrue(report.reviewer_read_only_enforced)
        self.assertEqual(len(captured), 1)
        self.assertIn("read-only", captured[0].argv)
        self.assertEqual(captured[0].cwd.name, "evidence")
        self.assertIn(b'"purpose":"identity"', captured[0].stdin)

    def test_installed_codex_script_runtime_closure_runs_inside_verified_sandbox(self) -> None:
        launcher = shutil.which("codex")
        if launcher is None:
            self.skipTest("Codex launcher is not installed")
        command = _resolve_executable((launcher,))
        self.assertIsNotNone(command)
        assert command is not None
        if not command[0].endswith(".js"):
            self.skipTest("installed Codex launcher is not the Node script layout")
        with tempfile.TemporaryDirectory(prefix="token-saver-codex-runtime-") as root_text:
            root = Path(root_text).resolve(strict=True)
            evidence = root / "evidence"
            state = root / "state"
            protected = root / "protected"
            for path in (evidence, state, protected):
                path.mkdir()
            packet = evidence / "packet"
            packet.write_text("runtime smoke\n", encoding="utf-8")
            packet.chmod(0o400)
            policy = SandboxPolicy(
                worktree_root=evidence,
                route_state_root=state,
                readable_roots=(packet, *_reviewer_runtime_roots(command)),
                protected_roots=(protected,),
                network_required=False,
            )
            sandbox = select_verified_backend(
                policy,
                route_id="codex-runtime-smoke",
                argv=(*command, "--version"),
            )
            if isinstance(sandbox, UnavailableSandbox):
                self.skipTest(sandbox.message)
            launch = sandbox.prepare(
                route_id="codex-runtime-smoke",
                argv=(*command, "--version"),
                policy=policy,
                cwd=evidence,
            )
            result = run_process(
                ProcessSpec(
                    argv=launch.argv,
                    cwd=evidence,
                    env={
                        "HOME": os.fspath(state),
                        "PATH": os.environ.get("PATH", os.defpath),
                        "LANG": "C",
                        "LC_ALL": "C",
                    },
                    timeout_seconds=30,
                )
            )
        self.assertEqual(result.status, "ok", result.stderr.decode(errors="replace"))
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"codex-cli", result.stdout)

    def test_forged_binding_hash_is_rejected_before_process_launch(self) -> None:
        forged = json.dumps(
            {
                "version": 1,
                "source_snapshot_hash": "1" * 64,
                "worker_delta_hash": "2" * 64,
                "projected_task_patch_hash": "4" * 64,
                "approval_binding_hash": BINDING,
                "review_packet_sha256": PACKET_HASH,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        calls = []
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root) / "parent"
            state = Path(root) / "state"
            parent.mkdir()
            state.mkdir()
            result = execute_reviewer(
                _route((sys.executable,)),
                forged,
                BINDING,
                evidence_parent=parent,
                route_state_root=state,
                process_runner=lambda spec: calls.append(spec),
                sandbox_factory=_verified_sandbox_factory,
            )
        self.assertEqual(result.status, "transport_error")
        self.assertEqual(calls, [])

    def test_revise_requires_changes_and_exact_binding(self) -> None:
        bad_outputs = (
            {
                "version": 1,
                "decision": "revise",
                "approval_binding_hash": BINDING,
                "review_packet_sha256": PACKET_HASH,
                "message": "change it",
                "requested_changes": [],
            },
            {
                "version": 1,
                "decision": "approve",
                "approval_binding_hash": "b" * 64,
                "review_packet_sha256": PACKET_HASH,
                "message": "wrong tuple",
                "requested_changes": [],
            },
        )
        for output in bad_outputs:
            with self.subTest(output=output):
                result, _ = self._run((sys.executable,), output)
                self.assertEqual(result.status, "transport_error")

    def test_unknown_fields_prefix_suffix_timeout_and_nonzero_fail_closed(self) -> None:
        valid = {
            "version": 1,
            "decision": "approve",
            "approval_binding_hash": BINDING,
            "review_packet_sha256": PACKET_HASH,
            "message": "ok",
            "requested_changes": [],
        }
        cases = (
            _result(json.dumps({**valid, "extra": True}).encode()),
            _result(b"prefix " + json.dumps(valid).encode()),
            _result(json.dumps(valid).encode() + b" suffix"),
            _result(b"", status="timeout", returncode=-15),
            _result(json.dumps(valid).encode(), returncode=7, status="transport_error"),
        )
        for output in cases:
            with self.subTest(output=output):
                result, _ = self._run((sys.executable,), output)
                self.assertEqual(result.status, "transport_error")

    def test_packet_may_not_name_a_forbidden_repository(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            forbidden = Path(root) / "repo"
            forbidden.mkdir()
            packet = PACKET + os.fsencode(forbidden)
            parent = Path(root) / "parent"
            state = Path(root) / "state"
            parent.mkdir()
            state.mkdir()
            result = execute_reviewer(
                _route((sys.executable,)),
                packet,
                BINDING,
                evidence_parent=parent,
                route_state_root=state,
                process_runner=lambda spec: _result(b"{}"),
                forbidden_roots=(forbidden,),
                sandbox_factory=_verified_sandbox_factory,
            )
        self.assertEqual(result.status, "transport_error")

    def test_reviewer_roots_may_not_overlap_each_other_or_forbidden_roots(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            forbidden = root / "repository"
            forbidden.mkdir()
            outside = root / "outside"
            outside.mkdir()
            cases = (
                (forbidden, outside),
                (forbidden / "child", outside),
                (root, outside),
                (outside, outside / "state"),
            )
            for parent, state in cases:
                parent.mkdir(parents=True, exist_ok=True)
                state.mkdir(parents=True, exist_ok=True)
                calls = []
                with self.subTest(parent=parent, state=state):
                    result = execute_reviewer(
                        _route((sys.executable,)),
                        PACKET,
                        BINDING,
                        evidence_parent=parent,
                        route_state_root=state,
                        process_runner=lambda spec: calls.append(spec),
                        forbidden_roots=(forbidden,),
                        sandbox_factory=_verified_sandbox_factory,
                    )
                    self.assertEqual(result.status, "transport_error")
                    self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
