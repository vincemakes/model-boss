"""Evidence-only reviewer transport and injectable route preflight."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .models import (
    FingerprintEvidenceSource,
    ModelFingerprint,
    Role,
    Route,
    RouteProbeResult,
    Status,
    Transport,
)
from .evidence import ApprovalBinding
from .process import ProcessResult, ProcessSpec, run_process
from .sandbox import (
    SandboxPolicy,
    SandboxPolicyError,
    UnavailableSandbox,
    VerifiedSandbox,
    select_verified_backend,
)


_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_VERDICT_KEYS = {
    "version",
    "decision",
    "approval_binding_hash",
    "review_packet_sha256",
    "message",
    "requested_changes",
}
_CLAUDE_REVIEW_SUFFIX = (
    "--safe-mode",
    "--no-session-persistence",
    "--permission-mode",
    "plan",
    "--tools",
    "",
    "-p",
    "-",
)


@dataclass(frozen=True)
class ReviewerVerdict:
    version: int
    decision: str
    approval_binding_hash: str
    review_packet_sha256: str
    message: str
    requested_changes: tuple[str, ...]


@dataclass(frozen=True)
class ReviewerTransportResult:
    status: Status
    verdict: ReviewerVerdict | None = None
    message: str = ""

    def __post_init__(self) -> None:
        try:
            status = Status(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("status must be a Token Saver status") from exc
        if status is Status.OK and not isinstance(self.verdict, ReviewerVerdict):
            raise ValueError("successful reviewer transport requires a verdict")
        if status is not Status.OK and self.verdict is not None:
            raise ValueError("failed reviewer transport cannot expose a verdict")
        object.__setattr__(self, "status", status)


def _failure(message: str) -> ReviewerTransportResult:
    return ReviewerTransportResult(Status.TRANSPORT_ERROR, message=message)


def _resolve_executable(command: Sequence[str]) -> tuple[str, ...] | None:
    if not command:
        return None
    candidate = Path(command[0])
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
    else:
        discovered = shutil.which(command[0])
        if discovered is None:
            return None
        try:
            resolved = Path(discovered).resolve(strict=True)
        except OSError:
            return None
    try:
        metadata = resolved.stat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        return None
    return (os.fspath(resolved), *tuple(command)[1:])


def _is_codex_command(command: Sequence[str]) -> bool:
    return any(Path(member).name.lower() == "codex" for member in command[:2])


def _reviewer_argv(
    command: tuple[str, ...],
    evidence_dir: Path,
    *,
    codex_command: bool | None = None,
) -> tuple[str, ...]:
    lowered = tuple(member.lower() for member in command)
    if any("bypass" in member or "danger-full-access" in member for member in lowered):
        raise ValueError("reviewer command contains a write-capable bypass")
    is_codex = _is_codex_command(command) if codex_command is None else codex_command
    if is_codex:
        codex_index = next(
            (
                index
                for index, member in enumerate(command[:2])
                if Path(member).name.lower() == "codex"
            ),
            0,
        )
        return (
            *command[: codex_index + 1],
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            os.fspath(evidence_dir),
            "-",
            *command[codex_index + 1 :],
        )
    return (*command, *_CLAUDE_REVIEW_SUFFIX)


def _canonical_packet(packet: bytes, expected_hash: str) -> bool:
    if type(packet) is not bytes or not packet or b"\0" in packet:
        return False
    if _SHA256.fullmatch(expected_hash) is None:
        return False
    try:
        value = json.loads(
            packet.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate_object,
        )
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    except (UnicodeError, ValueError, TypeError):
        return False
    if not isinstance(value, dict) or canonical != packet:
        return False
    try:
        binding = ApprovalBinding(
            source_snapshot_hash=value["source_snapshot_hash"],
            worker_delta_hash=value["worker_delta_hash"],
            projected_task_patch_hash=value["projected_task_patch_hash"],
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (
        value.get("approval_binding_hash") == expected_hash
        and binding.canonical_hash == expected_hash
    )


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse_verdict(
    raw: bytes,
    expected_hash: str,
    expected_packet_hash: str,
) -> ReviewerVerdict | None:
    try:
        text = raw.decode("utf-8", "strict")
        if not text or text != text.strip():
            return None
        value = json.loads(text, object_pairs_hook=_reject_duplicate_object)
    except (UnicodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != _VERDICT_KEYS:
        return None
    if value["version"] != 1 or value["decision"] not in {"approve", "revise"}:
        return None
    if value["approval_binding_hash"] != expected_hash:
        return None
    if value["review_packet_sha256"] != expected_packet_hash:
        return None
    message = value["message"]
    changes = value["requested_changes"]
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > 4096
        or not isinstance(changes, list)
        or not all(isinstance(change, str) and change.strip() for change in changes)
    ):
        return None
    if value["decision"] == "approve" and changes:
        return None
    if value["decision"] == "revise" and not changes:
        return None
    return ReviewerVerdict(
        version=1,
        decision=value["decision"],
        approval_binding_hash=expected_hash,
        review_packet_sha256=expected_packet_hash,
        message=message,
        requested_changes=tuple(changes),
    )


def _evidence_manifest(directory: Path) -> tuple[tuple[str, int, int, str], ...]:
    entries = []
    for child in directory.iterdir():
        metadata = child.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("review evidence directory contains an unexpected object")
        entries.append(
            (
                child.name,
                metadata.st_dev,
                metadata.st_ino,
                hashlib.sha256(child.read_bytes()).hexdigest(),
            )
        )
    return tuple(sorted(entries))


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _runtime_path(command: Sequence[str]) -> str:
    """Keep the validated launcher runtime reachable without inheriting secrets."""

    candidates = [
        os.fspath(Path(command[0]).parent),
        os.fspath(Path(sys.executable).resolve(strict=True).parent),
        *(os.environ.get("PATH") or os.defpath).split(os.pathsep),
    ]
    result: list[str] = []
    for member in candidates:
        if member and member not in result:
            result.append(member)
    return os.pathsep.join(result)


def _reviewer_environment(
    state: Path,
    command: Sequence[str],
    route: Route,
    credentials: Mapping[str, str],
) -> dict[str, str]:
    environment = {
        "HOME": os.fspath(state),
        "XDG_CONFIG_HOME": os.fspath(state),
        "XDG_CACHE_HOME": os.fspath(state),
        "XDG_STATE_HOME": os.fspath(state),
        "TMPDIR": os.fspath(state),
        "PATH": _runtime_path(command),
        "LANG": "C",
        "LC_ALL": "C",
    }
    for binding in route.credential_env:
        value = credentials.get(binding.source_name)
        if value:
            environment[binding.child_name] = value
    return environment


def _reviewer_runtime_roots(command: Sequence[str]) -> tuple[Path, ...]:
    """Resolve only the interpreter/package closure needed by a script launcher."""

    executable = Path(command[0])
    result: list[Path] = []
    try:
        with executable.open("rb") as stream:
            first_line = stream.readline(4096)
    except OSError:
        first_line = b""
    if first_line.startswith(b"#!"):
        try:
            words = shlex.split(first_line[2:].decode("utf-8", "strict").strip())
        except (UnicodeError, ValueError):
            words = []
        interpreter_name = ""
        if words:
            interpreter_name = words[1] if Path(words[0]).name == "env" and len(words) > 1 else words[0]
        if interpreter_name:
            discovered = (
                shutil.which(interpreter_name)
                if not Path(interpreter_name).is_absolute()
                else interpreter_name
            )
            if discovered:
                try:
                    result.append(Path(discovered).resolve(strict=True))
                except OSError:
                    pass
    for parent in executable.parents:
        if parent.name == "node_modules":
            result.append(parent)
            break
    deduplicated: list[Path] = []
    for root in result:
        if root not in deduplicated:
            deduplicated.append(root)
    return tuple(deduplicated)


def _verified_reviewer_launch(
    route: Route,
    command: tuple[str, ...],
    argv: tuple[str, ...],
    evidence: Path,
    state: Path,
    packet_path: Path,
    protected_roots: Sequence[Path],
    sandbox_factory: Callable[..., object],
) -> tuple[VerifiedSandbox, tuple[str, ...]] | None:
    try:
        policy = SandboxPolicy(
            worktree_root=evidence,
            route_state_root=state,
            readable_roots=(
                packet_path,
                *_reviewer_runtime_roots(command),
            ),
            protected_roots=tuple(protected_roots),
            network_required=True,
        )
        sandbox = sandbox_factory(
            policy,
            route_id=f"reviewer-{route.route_id}",
            argv=argv,
        )
        if not isinstance(sandbox, VerifiedSandbox):
            return None
        launch = sandbox.prepare(
            route_id=f"reviewer-{route.route_id}",
            argv=argv,
            policy=policy,
            cwd=evidence,
        )
        if not launch.available or launch.cwd != evidence or not launch.argv:
            return None
        return sandbox, launch.argv
    except (OSError, SandboxPolicyError, TypeError, ValueError):
        return None


def execute_reviewer(
    route: Route,
    packet: bytes,
    approval_binding_hash: str,
    *,
    evidence_parent: str | os.PathLike[str],
    route_state_root: str | os.PathLike[str],
    process_runner: Callable[[ProcessSpec], ProcessResult] = run_process,
    forbidden_roots: Sequence[str | os.PathLike[str]] = (),
    credentials: Mapping[str, str] | None = None,
    sandbox_factory: Callable[..., object] = select_verified_backend,
) -> ReviewerTransportResult:
    """Run a reviewer with packet-only stdin and an immutable evidence cwd."""

    if (
        not isinstance(route, Route)
        or route.transport is not Transport.EXTERNAL_CLI
        or Role.REVIEWER not in route.roles
        or not route.read_only
    ):
        return _failure("route is not an external read-only reviewer")
    if not _canonical_packet(packet, approval_binding_hash):
        return _failure("review packet is not canonical or binding-complete")
    resolved_forbidden: list[Path] = []
    for root in forbidden_roots:
        try:
            resolved_root = Path(root).resolve(strict=True)
            encoded = os.fsencode(resolved_root)
        except (OSError, TypeError, ValueError):
            return _failure("forbidden root could not be resolved")
        resolved_forbidden.append(resolved_root)
        if encoded and encoded in packet:
            return _failure("review packet names a forbidden repository root")
    command = _resolve_executable(route.command)
    if command is None:
        return _failure("reviewer executable is unavailable")
    try:
        parent = Path(evidence_parent).resolve(strict=True)
        state = Path(route_state_root).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return _failure("reviewer roots are unavailable")
    if (
        not parent.is_dir()
        or not state.is_dir()
        or _paths_overlap(parent, state)
        or any(
            _paths_overlap(candidate, forbidden)
            for candidate in (parent, state)
            for forbidden in resolved_forbidden
        )
    ):
        return _failure("reviewer roots are invalid")

    root = Path(tempfile.mkdtemp(prefix="token-saver-review-", dir=parent))
    evidence = root / "evidence"
    try:
        evidence.mkdir(mode=0o700)
        protected_sentinel = root / "protected"
        protected_sentinel.mkdir(mode=0o700)
        packet_path = evidence / "packet.json"
        descriptor = os.open(
            packet_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(packet)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(packet_path, 0o400)
        before = _evidence_manifest(evidence)
        reviewer_argv = _reviewer_argv(
            command,
            evidence,
            codex_command=_is_codex_command(route.command),
        )
        sandboxed = _verified_reviewer_launch(
            route,
            command,
            reviewer_argv,
            evidence,
            state,
            packet_path,
            (*resolved_forbidden, protected_sentinel),
            sandbox_factory,
        )
        if sandboxed is None:
            return _failure("reviewer OS sandbox is unavailable")
        reviewer_sandbox, argv = sandboxed
        credential_values = tuple(
            value for value in (credentials or {}).values() if value
        )
        environment = _reviewer_environment(
            state,
            command,
            route,
            credentials or {},
        )
        process_result = process_runner(
            ProcessSpec(
                argv=argv,
                cwd=evidence,
                stdin=packet,
                env=environment,
                timeout_seconds=route.timeout_seconds,
                stdout_limit=262_144,
                stderr_limit=262_144,
                redact_values=credential_values,
            )
        )
        try:
            after = _evidence_manifest(evidence)
        except (OSError, ValueError):
            return _failure("reviewer changed the evidence directory")
        if after != before or not reviewer_sandbox.policy.current:
            return _failure("reviewer changed the evidence directory")
        if (
            process_result.status is not Status.OK
            or process_result.returncode != 0
            or process_result.stdout_truncated
        ):
            return _failure("reviewer process did not return a complete verdict")
        verdict = _parse_verdict(
            process_result.stdout,
            approval_binding_hash,
            hashlib.sha256(packet).hexdigest(),
        )
        if verdict is None:
            return _failure("reviewer output is not the strict verdict schema")
        return ReviewerTransportResult(Status.OK, verdict=verdict)
    except (OSError, ValueError):
        return _failure("reviewer transport failed safely")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _identity_from_output(output: bytes) -> ModelFingerprint | None:
    try:
        value = json.loads(output.decode("utf-8", "strict"))
    except (UnicodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {
        "provider_family",
        "resolved_model_id",
        "variant",
    }:
        return None
    try:
        return ModelFingerprint(
            value["provider_family"],
            value["resolved_model_id"],
            value["variant"],
        )
    except (TypeError, ValueError):
        return None


def _probe_external_reviewer_identity(
    route: Route,
    command: tuple[str, ...],
    credentials: Mapping[str, str],
    process_runner: Callable[[ProcessSpec], ProcessResult],
    root: Path,
    sandbox_factory: Callable[..., object],
) -> ModelFingerprint | None:
    """Run identity discovery through the exact hardened reviewer composition."""

    evidence = root / "evidence"
    state = root / "state"
    evidence.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    evidence = evidence.resolve(strict=True)
    state = state.resolve(strict=True)
    packet = json.dumps(
        {
            "purpose": "identity",
            "required_output": (
                "one JSON object with provider_family, resolved_model_id, and variant"
            ),
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    packet_path = evidence / "identity.packet"
    protected = root / "protected"
    protected.mkdir(mode=0o700)
    protected = protected.resolve(strict=True)
    descriptor = os.open(
        packet_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(packet)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(packet_path, 0o400)
    before = _evidence_manifest(evidence)
    credential_values = tuple(value for value in credentials.values() if value)
    reviewer_argv = _reviewer_argv(
        command,
        evidence,
        codex_command=_is_codex_command(route.command),
    )
    sandboxed = _verified_reviewer_launch(
        route,
        command,
        reviewer_argv,
        evidence,
        state,
        packet_path,
        (protected,),
        sandbox_factory,
    )
    if sandboxed is None:
        return None
    reviewer_sandbox, launch_argv = sandboxed
    result = process_runner(
        ProcessSpec(
            argv=launch_argv,
            cwd=evidence,
            stdin=packet,
            env=_reviewer_environment(state, command, route, credentials),
            timeout_seconds=min(route.timeout_seconds, 30),
            stdout_limit=65_536,
            stderr_limit=65_536,
            redact_values=credential_values,
        )
    )
    try:
        after = _evidence_manifest(evidence)
    except (OSError, ValueError):
        return None
    if (
        after != before
        or not reviewer_sandbox.policy.current
        or result.status is not Status.OK
        or result.returncode != 0
        or result.stdout_truncated
    ):
        return None
    return _identity_from_output(result.stdout)


def probe_route(
    route: Route,
    role: Role,
    credentials: Mapping[str, str],
    sandbox_factory: Callable[..., object],
    process_runner: Callable[[ProcessSpec], ProcessResult],
) -> RouteProbeResult:
    """Produce credential-free preflight facts through injectable probes."""

    if not isinstance(route, Route):
        raise ValueError("route must be Route")
    try:
        role = Role(role)
    except (TypeError, ValueError) as exc:
        raise ValueError("role is unsupported") from exc
    declared = tuple(binding.source_name for binding in route.credential_env)
    configured = tuple(name for name in declared if bool(credentials.get(name)))
    missing = tuple(name for name in declared if name not in configured)

    if route.transport is Transport.HOST_SUBAGENT:
        probe_native = getattr(process_runner, "probe_native", None)
        metadata = probe_native(route, role) if callable(probe_native) else None
        fingerprint = metadata.get("fingerprint") if isinstance(metadata, dict) else None
        return RouteProbeResult(
            route_id=route.route_id,
            reachable=bool(metadata) and not missing,
            resolved_fingerprint=fingerprint if isinstance(fingerprint, ModelFingerprint) else None,
            fingerprint_evidence_source=(
                FingerprintEvidenceSource.HOST_METADATA
                if isinstance(fingerprint, ModelFingerprint)
                else None
            ),
            executable_available=False,
            native_agent_available=bool(metadata),
            reviewer_read_only_enforced=bool(
                isinstance(metadata, dict) and metadata.get("read_only") is True
            ),
            configured_credentials=configured,
            missing_credentials=missing,
        )

    command = _resolve_executable(route.command)
    if command is None:
        return RouteProbeResult(
            route_id=route.route_id,
            reachable=False,
            resolved_fingerprint=None,
            fingerprint_evidence_source=None,
            executable_available=False,
            native_agent_available=False,
            reviewer_read_only_enforced=False,
            configured_credentials=configured,
            missing_credentials=missing,
        )
    state = Path(tempfile.mkdtemp(prefix="token-saver-route-probe-"))
    try:
        executable_available = True
        fingerprint = None
        expected_identity = None
        verified_identity = None
        sandbox_ok = role is Role.REVIEWER
        if role is Role.WORKER:
            try:
                sandbox = sandbox_factory(route, command)
            except (TypeError, ValueError, OSError):
                sandbox = UnavailableSandbox("sandbox probe failed")
            if isinstance(sandbox, VerifiedSandbox):
                try:
                    expected_identity = sandbox.worker_identity(route)
                    verified_identity = expected_identity
                    sandbox_ok = True
                except (TypeError, ValueError):
                    sandbox_ok = False
        elif role is Role.REVIEWER:
            try:
                fingerprint = _probe_external_reviewer_identity(
                    route,
                    command,
                    credentials,
                    process_runner,
                    state,
                    sandbox_factory,
                )
            except (OSError, TypeError, ValueError):
                fingerprint = None
        reviewer_read_only = role is Role.REVIEWER and fingerprint is not None
        return RouteProbeResult(
            route_id=route.route_id,
            reachable=(
                executable_available
                and not missing
                and sandbox_ok
                and (role is not Role.REVIEWER or fingerprint is not None)
            ),
            resolved_fingerprint=fingerprint,
            fingerprint_evidence_source=(
                FingerprintEvidenceSource.IDENTITY_HANDSHAKE
                if fingerprint is not None
                else None
            ),
            executable_available=executable_available,
            native_agent_available=False,
            reviewer_read_only_enforced=reviewer_read_only,
            expected_worker_sandbox_identity=expected_identity,
            verified_worker_sandbox_identity=verified_identity,
            configured_credentials=configured,
            missing_credentials=missing,
        )
    finally:
        shutil.rmtree(state, ignore_errors=True)


__all__ = (
    "ReviewerTransportResult",
    "ReviewerVerdict",
    "execute_reviewer",
    "probe_route",
)
