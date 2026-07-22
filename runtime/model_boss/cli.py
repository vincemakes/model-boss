"""Worker orchestration and machine-readable Model Boss command line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from .bundle import (
    BundleError,
    SealedGateEvidence,
    build_final_review_packet,
    read_final_review_receipt,
    read_sealed_delta_bundle,
    seal_delta_bundle,
    seal_final_review_receipt,
)
from .config import ConfigError, load_config
from .evidence import (
    ApprovalBinding,
    SourceSnapshot,
    WorkerDelta,
    encode_canonical_patch,
    encode_source_snapshot,
    encode_worker_delta,
)
from .integration import Approval, integrate_sealed_delta_bundle
from .models import (
    CapabilityBand,
    MainLoop,
    Mode,
    ModelFingerprint,
    Role,
    Route,
    RunOverrides,
    Status,
    Transport,
)
from .process import ProcessResult, ProcessSpec, run_process
from .repository import (
    RepositoryError,
    ScopeViolationError,
    WorktreeHandle,
    capture_destination,
    capture_source_snapshot,
    capture_worker_delta,
    create_worktree,
    materialize_snapshot,
    project_task_patch,
)
from .resources import (
    InvocationResources,
    cleanup_invocation,
    create_invocation_resources,
    load_invocation_resources,
)
from .routing import finalize_resolution, preflight_candidates, resolve_candidates
from .sandbox import (
    SandboxPolicy,
    SandboxPolicyError,
    UnavailableSandbox,
    VerifiedSandbox,
    select_verified_backend,
)
from .setup import (
    SetupError,
    install_provider_wrappers,
    load_credentials,
    migrate_legacy_credentials,
    provider_child_environment,
)
from .transport import execute_reviewer, probe_route


CLI_VERSION = 1
_COMMANDS = (
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
_COMMAND_HELP = {
    "resolve": "resolve Lite/Max from explicit main-loop facts and live route probes",
    "review": "review one sealed bundle and persist an invocation-bound approval receipt",
    "worker": "run an OS-sandboxed external worker and seal its delta",
    "snapshot": "print a redacted diagnostic source-snapshot hash without persisting it",
    "integrate": "integrate one sealed delta only with its sealed final-review receipt",
    "validate-config": "validate one Model Boss configuration file",
    "setup-providers": "explicitly migrate provider data and/or install compatibility wrappers",
    "provider-exec": "internal direct-argv provider wrapper entry",
    "cleanup": "consume and remove one abandoned invocation",
}


@dataclass(frozen=True)
class GateSpec:
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not isinstance(self.argv, (tuple, list)) or not self.argv or not all(
            isinstance(member, str) and member and "\0" not in member
            for member in self.argv
        ):
            raise ValueError("gate argv must be a non-empty argument tuple")
        path = Path(self.cwd)
        if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
            raise ValueError("gate cwd must be a validated relative directory")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < self.timeout_seconds <= 3600
        ):
            raise ValueError("gate timeout must be between zero and 3600 seconds")
        object.__setattr__(self, "argv", tuple(self.argv))


@dataclass(frozen=True)
class WorkerTask:
    prompt: bytes
    gates: tuple[GateSpec, ...]

    def __post_init__(self) -> None:
        if type(self.prompt) is not bytes or not self.prompt or b"\0" in self.prompt:
            raise ValueError("worker prompt must be non-empty NUL-free bytes")
        if not isinstance(self.gates, (tuple, list)) or not all(
            isinstance(gate, GateSpec) for gate in self.gates
        ):
            raise ValueError("gates must contain GateSpec values")
        object.__setattr__(self, "gates", tuple(self.gates))


@dataclass(frozen=True)
class GateEvidence:
    argv: tuple[str, ...]
    cwd: str
    status: Status
    exit_code: int | None
    stdout_hash: str
    stderr_hash: str
    duration_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", Status(self.status))


@dataclass(frozen=True)
class WorkerRunResult:
    status: Status
    attempts: int
    gates: tuple[GateEvidence, ...] = ()
    source_snapshot_hash: str | None = None
    worker_delta_hash: str | None = None
    projected_task_patch_hash: str | None = None
    delta: WorkerDelta | None = None
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", Status(self.status))


@dataclass(frozen=True)
class ProviderExecPlan:
    """An execve-ready provider launch with no shell interpretation."""

    status: Status
    executable: Path | None = None
    argv: tuple[str, ...] = ()
    cwd: Path | None = None
    environment: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    sandbox: VerifiedSandbox | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    message: str = ""

    def __post_init__(self) -> None:
        status = Status(self.status)
        argv = tuple(self.argv)
        environment = dict(self.environment)
        if status is Status.OK:
            if (
                self.executable is None
                or not argv
                or self.cwd is None
                or not all(
                    isinstance(member, str) and "\0" not in member
                    for member in argv
                )
                or not all(
                    isinstance(name, str)
                    and isinstance(value, str)
                    and "\0" not in name
                    and "\0" not in value
                    for name, value in environment.items()
                )
            ):
                raise ValueError("successful provider plans require a complete exec contract")
        elif self.executable is not None or argv or self.cwd is not None or environment:
            raise ValueError("failed provider plans cannot carry an executable contract")
        if self.sandbox is not None and not isinstance(self.sandbox, VerifiedSandbox):
            raise ValueError("sandbox must be a VerifiedSandbox or null")
        if status is not Status.OK and self.sandbox is not None:
            raise ValueError("failed provider plans cannot carry a sandbox")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "argv", argv)
        object.__setattr__(
            self,
            "environment",
            MappingProxyType(environment),
        )


def _worker_result(status: Status, attempts: int, **kwargs: object) -> WorkerRunResult:
    return WorkerRunResult(status=status, attempts=attempts, **kwargs)  # type: ignore[arg-type]


def _source_is_unchanged(repo: object, snapshot: SourceSnapshot) -> bool:
    try:
        current = capture_destination(repo, snapshot.allowed_paths)
        return encode_source_snapshot(current) == encode_source_snapshot(snapshot)
    except (OSError, RepositoryError, ValueError):
        return False


def _minimal_worker_env(
    route: Route,
    state_root: Path,
    credentials: Mapping[str, str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    environment = {
        "HOME": os.fspath(state_root),
        "XDG_CONFIG_HOME": os.fspath(state_root),
        "XDG_CACHE_HOME": os.fspath(state_root),
        "XDG_STATE_HOME": os.fspath(state_root),
        "TMPDIR": os.fspath(state_root),
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
    }
    secrets = []
    for binding in route.credential_env:
        value = credentials.get(binding.source_name)
        if not value:
            raise ValueError("required worker credential is missing")
        environment[binding.child_name] = value
        secrets.append(value)
    return environment, tuple(secrets)


def _resolve_gate_argv(argv: Sequence[str]) -> tuple[str, ...] | None:
    candidate = Path(argv[0])
    if candidate.is_absolute():
        try:
            executable = candidate.resolve(strict=True)
        except OSError:
            return None
    else:
        discovered = shutil.which(argv[0])
        if discovered is None:
            return None
        executable = Path(discovered).resolve(strict=True)
    try:
        mode = executable.stat().st_mode
    except OSError:
        return None
    if not stat.S_ISREG(mode) or not os.access(executable, os.X_OK):
        return None
    return (os.fspath(executable), *tuple(argv)[1:])


def _run_gate(
    gate: GateSpec,
    sandbox: VerifiedSandbox,
    environment: Mapping[str, str],
    process_runner: Callable[[ProcessSpec], ProcessResult],
) -> GateEvidence:
    gate_argv = _resolve_gate_argv(gate.argv)
    worktree = sandbox.policy.worktree_root
    try:
        cwd = (worktree / gate.cwd).resolve(strict=True)
        cwd.relative_to(worktree)
    except (OSError, ValueError):
        return GateEvidence(
            argv=gate.argv,
            cwd=gate.cwd,
            status=Status.GATE_FAILED,
            exit_code=None,
            stdout_hash=hashlib.sha256(b"").hexdigest(),
            stderr_hash=hashlib.sha256(b"invalid gate cwd").hexdigest(),
            duration_seconds=0.0,
        )
    if gate_argv is None or not cwd.is_dir() or not sandbox.policy.current:
        return GateEvidence(
            argv=gate.argv,
            cwd=gate.cwd,
            status=Status.GATE_FAILED,
            exit_code=None,
            stdout_hash=hashlib.sha256(b"").hexdigest(),
            stderr_hash=hashlib.sha256(b"gate unavailable").hexdigest(),
            duration_seconds=0.0,
        )
    result = process_runner(
        ProcessSpec(
            argv=(*sandbox.launcher_prefix, *gate_argv),
            cwd=cwd,
            env=environment,
            timeout_seconds=gate.timeout_seconds,
            stdout_limit=1_048_576,
            stderr_limit=1_048_576,
        )
    )
    if not sandbox.policy.current:
        return GateEvidence(
            argv=gate.argv,
            cwd=gate.cwd,
            status=Status.GATE_FAILED,
            exit_code=None,
            stdout_hash=hashlib.sha256(b"").hexdigest(),
            stderr_hash=hashlib.sha256(b"sandbox policy changed").hexdigest(),
            duration_seconds=result.duration_seconds,
        )
    status = Status.OK if result.status is Status.OK and result.returncode == 0 else (
        Status.TIMEOUT if result.status is Status.TIMEOUT else Status.GATE_FAILED
    )
    return GateEvidence(
        argv=gate.argv,
        cwd=gate.cwd,
        status=status,
        exit_code=result.returncode,
        stdout_hash=hashlib.sha256(result.stdout).hexdigest(),
        stderr_hash=hashlib.sha256(result.stderr).hexdigest(),
        duration_seconds=result.duration_seconds,
    )


def _retry_packet(prompt: bytes, evidence: Sequence[GateEvidence]) -> bytes:
    if not evidence:
        return prompt
    failures = [
        {
            "argv": list(item.argv),
            "cwd": item.cwd,
            "status": item.status.value,
            "exit_code": item.exit_code,
            "stdout_hash": item.stdout_hash,
            "stderr_hash": item.stderr_hash,
        }
        for item in evidence
    ]
    return prompt + b"\n\nMODEL_BOSS_TRUSTED_GATE_FAILURES=" + json.dumps(
        failures,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _run_worker_loop(
    repo: str | os.PathLike[str],
    snapshot: SourceSnapshot,
    handle: WorktreeHandle,
    route: Route,
    task: WorkerTask,
    *,
    launch_argv: tuple[str, ...],
    launch_cwd: Path,
    environment: Mapping[str, str],
    secrets: Sequence[str],
    gate_sandbox: VerifiedSandbox,
    credential_child_names: frozenset[str],
    process_runner: Callable[[ProcessSpec], ProcessResult],
) -> WorkerRunResult:
    if not _source_is_unchanged(repo, snapshot):
        return _worker_result(
            Status.SCOPE_VIOLATION,
            0,
            message="source repository changed before worker launch",
        )

    all_gate_evidence: list[GateEvidence] = []
    prior_failures: list[GateEvidence] = []
    maximum_attempts = route.retry_policy.worker_attempts
    for attempt in range(1, maximum_attempts + 1):
        worker = process_runner(
            ProcessSpec(
                argv=launch_argv,
                cwd=launch_cwd,
                stdin=_retry_packet(task.prompt, prior_failures),
                env=environment,
                timeout_seconds=route.timeout_seconds,
                redact_values=tuple(secrets),
            )
        )
        if not gate_sandbox.policy.current:
            return _worker_result(
                Status.SCOPE_VIOLATION,
                attempt,
                gates=tuple(all_gate_evidence),
                message="sandbox policy identity changed during worker execution",
            )
        if not _source_is_unchanged(repo, snapshot):
            return _worker_result(
                Status.SCOPE_VIOLATION,
                attempt,
                gates=tuple(all_gate_evidence),
                message="source repository changed during worker execution",
            )
        if worker.status is Status.TIMEOUT:
            return _worker_result(
                Status.TIMEOUT,
                attempt,
                gates=tuple(all_gate_evidence),
                message="worker timed out",
            )
        if worker.status is not Status.OK or worker.returncode != 0:
            return _worker_result(
                Status.TRANSPORT_ERROR,
                attempt,
                gates=tuple(all_gate_evidence),
                message="worker process failed",
            )

        gate_environment = {
            key: value
            for key, value in environment.items()
            if key not in credential_child_names
        }
        this_attempt = tuple(
            _run_gate(gate, gate_sandbox, gate_environment, process_runner)
            for gate in task.gates
        )
        all_gate_evidence.extend(this_attempt)
        if not _source_is_unchanged(repo, snapshot):
            return _worker_result(
                Status.SCOPE_VIOLATION,
                attempt,
                gates=tuple(all_gate_evidence),
                message="source repository changed during trusted gates",
            )
        failures = tuple(item for item in this_attempt if item.status is not Status.OK)
        if failures:
            prior_failures = list(failures)
            if attempt < maximum_attempts:
                continue
            return _worker_result(
                Status.GATE_FAILED,
                attempt,
                gates=tuple(all_gate_evidence),
                message="trusted gate retry limit reached",
            )

        try:
            delta = capture_worker_delta(handle, snapshot, snapshot.allowed_paths)
            projected = project_task_patch(snapshot, delta)
            source_hash = hashlib.sha256(encode_source_snapshot(snapshot)).hexdigest()
            delta_hash = hashlib.sha256(encode_worker_delta(delta)).hexdigest()
            projected_hash = hashlib.sha256(
                encode_canonical_patch(projected)
            ).hexdigest()
        except (OSError, RepositoryError, ScopeViolationError, ValueError):
            return _worker_result(
                Status.SCOPE_VIOLATION,
                attempt,
                gates=tuple(all_gate_evidence),
                message="worker delta failed scope or evidence capture",
            )
        return _worker_result(
            Status.OK,
            attempt,
            # Historical red gates are retry input, not sealable final evidence.
            # A successful result exposes only the last complete green gate set;
            # terminal failures above retain the full attempt history.
            gates=this_attempt,
            source_snapshot_hash=source_hash,
            worker_delta_hash=delta_hash,
            projected_task_patch_hash=projected_hash,
            delta=delta,
            message="worker delta is ready for main-loop review",
        )
    return _worker_result(Status.GATE_FAILED, maximum_attempts)


def orchestrate_worker(
    repo: str | os.PathLike[str],
    snapshot: SourceSnapshot,
    handle: WorktreeHandle,
    route: Route,
    sandbox: VerifiedSandbox | UnavailableSandbox,
    task: WorkerTask,
    *,
    credentials: Mapping[str, str] | None = None,
    process_runner: Callable[[ProcessSpec], ProcessResult] = run_process,
) -> WorkerRunResult:
    """Run worker/gates in a verified worktree and return evidence, never apply it."""

    if (
        not isinstance(snapshot, SourceSnapshot)
        or not isinstance(handle, WorktreeHandle)
        or not isinstance(route, Route)
        or route.transport is not Transport.EXTERNAL_CLI
        or not isinstance(task, WorkerTask)
    ):
        return _worker_result(Status.NEEDS_CONTEXT, 0, message="invalid worker context")
    if not isinstance(sandbox, VerifiedSandbox):
        return _worker_result(
            Status.SANDBOX_UNAVAILABLE,
            0,
            message="verified worker sandbox is unavailable",
        )
    launch = sandbox.prepare(
        route_id=route.route_id,
        argv=route.command,
        policy=sandbox.policy,
        cwd=handle.path,
    )
    if (
        not launch.available
        or launch.cwd != handle.path.resolve(strict=True)
        or sandbox.policy.route_state_root == handle.path
    ):
        return _worker_result(
            Status.SANDBOX_UNAVAILABLE,
            0,
            message="sandbox binding does not match the worker invocation",
        )
    try:
        environment, secrets = _minimal_worker_env(
            route,
            sandbox.policy.route_state_root,
            credentials or {},
        )
    except ValueError:
        return _worker_result(
            Status.PROVIDER_UNAVAILABLE,
            0,
            message="required worker credentials are unavailable",
        )
    return _run_worker_loop(
        repo,
        snapshot,
        handle,
        route,
        task,
        launch_argv=launch.argv,
        launch_cwd=launch.cwd or handle.path,
        environment=environment,
        secrets=secrets,
        gate_sandbox=sandbox,
        credential_child_names=frozenset(
            binding.child_name for binding in route.credential_env
        ),
        process_runner=process_runner,
    )


def orchestrate_prepared_worker(
    repo: str | os.PathLike[str],
    snapshot: SourceSnapshot,
    handle: WorktreeHandle,
    route: Route,
    plan: ProviderExecPlan,
    task: WorkerTask,
    *,
    process_runner: Callable[[ProcessSpec], ProcessResult] = run_process,
) -> WorkerRunResult:
    """Run a provider plan whose fresh sandbox was reconstructed from a manifest."""

    if (
        not isinstance(snapshot, SourceSnapshot)
        or not isinstance(handle, WorktreeHandle)
        or not isinstance(route, Route)
        or route.transport is not Transport.EXTERNAL_CLI
        or not isinstance(plan, ProviderExecPlan)
        or plan.status is not Status.OK
        or not isinstance(plan.sandbox, VerifiedSandbox)
        or not isinstance(task, WorkerTask)
        or plan.cwd != handle.path.resolve(strict=True)
    ):
        return _worker_result(
            Status.SANDBOX_UNAVAILABLE,
            0,
            message="prepared worker sandbox does not match the invocation",
        )
    rebound = plan.sandbox.prepare(
        route_id=plan.sandbox.route_id,
        argv=plan.sandbox.route_argv,
        policy=plan.sandbox.policy,
        cwd=handle.path,
    )
    if (
        not rebound.available
        or rebound.argv != plan.argv
        or rebound.cwd != plan.cwd
        or plan.sandbox.policy.route_state_root == handle.path
    ):
        return _worker_result(
            Status.SANDBOX_UNAVAILABLE,
            0,
            message="prepared worker sandbox binding is stale",
        )
    secret = plan.environment.get("ANTHROPIC_AUTH_TOKEN")
    return _run_worker_loop(
        repo,
        snapshot,
        handle,
        route,
        task,
        launch_argv=plan.argv,
        launch_cwd=plan.cwd,
        environment=plan.environment,
        secrets=(secret,) if secret else (),
        gate_sandbox=plan.sandbox,
        credential_child_names=frozenset(
            {
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL",
            }
        ),
        process_runner=process_runner,
    )


def _json_output(status: Status, **fields: object) -> str:
    value = {"version": CLI_VERSION, "status": status.value, **fields}
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _status_exit_code(status: Status) -> int:
    if status is Status.OK:
        return 0
    if status is Status.NEEDS_CONTEXT:
        return 2
    if status in {
        Status.PROVIDER_UNAVAILABLE,
        Status.REVIEWER_UNAVAILABLE,
        Status.SANDBOX_UNAVAILABLE,
        Status.TRANSPORT_ERROR,
    }:
        return 3
    if status is Status.TIMEOUT:
        return 5
    return 4


def _standalone_probe_sandbox_factory(subject: object, *args: object, **kwargs: object):
    if isinstance(subject, SandboxPolicy):
        return select_verified_backend(subject, *args, **kwargs)  # type: ignore[arg-type]
    return UnavailableSandbox(
        "standalone resolution has no invocation-bound worker sandbox"
    )


def _reject_duplicate_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_cli_json(path_value: str | os.PathLike[str]) -> dict[str, object]:
    path = Path(path_value)
    try:
        lexical = os.lstat(path)
    except OSError:
        raise ValueError("JSON input is unavailable") from None
    if (
        not stat.S_ISREG(lexical.st_mode)
        or stat.S_ISLNK(lexical.st_mode)
        or lexical.st_size <= 0
        or lexical.st_size > 1_048_576
    ):
        raise ValueError("JSON input must be a bounded non-symlink regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != lexical.st_dev
            or opened.st_ino != lexical.st_ino
            or opened.st_size != lexical.st_size
        ):
            raise ValueError("JSON input changed before it was opened")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                raise ValueError("JSON input was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("JSON input grew while it was read")
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        for candidate in (after, current):
            if (
                candidate.st_dev != opened.st_dev
                or candidate.st_ino != opened.st_ino
                or candidate.st_size != opened.st_size
                or candidate.st_mtime_ns != opened.st_mtime_ns
                or candidate.st_ctime_ns != opened.st_ctime_ns
            ):
                raise ValueError("JSON input changed while it was read")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(
            b"".join(chunks).decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate_json_object,
        )
    except (UnicodeError, ValueError):
        raise ValueError("JSON input is invalid") from None
    if not isinstance(value, dict):
        raise ValueError("JSON input must contain one object")
    return value


def _profile_selection(value: str) -> str | Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("profile must be a name or canonical absolute JSON path")
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            lexical = os.lstat(candidate)
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("profile path is unavailable") from None
        if (
            not stat.S_ISREG(lexical.st_mode)
            or stat.S_ISLNK(lexical.st_mode)
        ):
            raise ValueError("profile path must resolve to a regular file")
        return resolved
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("relative profile paths are not accepted")
    return value


def _canonical_directory(value: str | os.PathLike[str], label: str) -> Path:
    try:
        unresolved = Path(value)
        resolved = unresolved.resolve(strict=True)
        lexical = os.lstat(unresolved)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ValueError(f"{label} is unavailable") from None
    if (
        not unresolved.is_absolute()
        or not stat.S_ISDIR(lexical.st_mode)
        or stat.S_ISLNK(lexical.st_mode)
    ):
        raise ValueError(f"{label} must resolve from an absolute directory path")
    return resolved


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _optional_credentials_document(path_value: str | None) -> dict[str, str]:
    if path_value is None:
        return {}
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError("credentials path must be absolute")
    return load_credentials(path)


def _route_credentials(
    route: Route,
    environment: Mapping[str, str],
    document: Mapping[str, str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    credentials: dict[str, str] = {}
    missing: list[str] = []
    for binding in route.credential_env:
        value = environment.get(binding.source_name) or document.get(
            binding.source_name
        )
        if value:
            credentials[binding.source_name] = value
        else:
            missing.append(binding.source_name)
    return credentials, tuple(missing)


def _parse_canonical_fingerprint(value: str) -> ModelFingerprint:
    if not isinstance(value, str):
        raise ValueError("fingerprint must be text")
    parts = value.split(":")
    if len(parts) != 3 or any(not part.strip() for part in parts):
        raise ValueError("fingerprint must contain provider:model:variant")
    fingerprint = ModelFingerprint(*parts)
    if fingerprint.canonical != value:
        raise ValueError("fingerprint must already be canonical")
    return fingerprint


def _run_resolve_command(arguments: argparse.Namespace) -> int:
    try:
        profile = _profile_selection(arguments.profile)
        project_root = (
            _canonical_directory(arguments.project_root, "project root")
            if arguments.project_root
            else None
        )
        overrides = RunOverrides(
            mode=Mode(arguments.mode) if arguments.mode else None,
            reviewer=arguments.reviewer,
            worker=arguments.worker,
        )
        loaded = load_config(
            profile=profile,
            project_root=project_root,
            discover=arguments.discover,
        )
        main_loop = MainLoop(
            route_id=arguments.main_route,
            fingerprint=ModelFingerprint(
                arguments.main_provider,
                arguments.main_model,
                arguments.main_variant,
            ),
            band=CapabilityBand(arguments.main_band),
            host=arguments.host,
        )
        candidate = resolve_candidates(main_loop, loaded, overrides)
        credential_document = _optional_credentials_document(arguments.credentials)
        probes = {}
        reviewer_ids = set(candidate.reviewer_route_ids)
        for route_id in dict.fromkeys(
            (*candidate.reviewer_route_ids, *candidate.worker_route_ids)
        ):
            route = candidate.routes.get(route_id)
            if route is None:
                continue
            role = Role.REVIEWER if route_id in reviewer_ids else Role.WORKER
            credentials, _ = _route_credentials(
                route,
                os.environ,
                credential_document,
            )
            probes[route_id] = probe_route(
                route,
                role,
                credentials,
                _standalone_probe_sandbox_factory,
                run_process,
            )
        preflight = preflight_candidates(candidate, probes)
        resolution = finalize_resolution(candidate, preflight)
    except (
        BundleError,
        ConfigError,
        OSError,
        RepositoryError,
        SetupError,
        TypeError,
        ValueError,
    ):
        print(
            _json_output(
                Status.NEEDS_CONTEXT,
                message="resolution inputs or live route probes are invalid",
            )
        )
        return 2

    mode = resolution.mode.value if resolution.mode is not None else None
    authority = (
        resolution.authority_route_id or "inline main loop"
        if resolution.status is Status.OK
        else None
    )
    print(
        _json_output(
            resolution.status,
            main_loop_fingerprint=main_loop.fingerprint.canonical,
            mode=mode,
            authority=authority,
            worker=resolution.worker,
            resolution_source=resolution.resolution_source,
            eligible_reviewers=list(preflight.eligible_reviewer_route_ids),
            ineligible_reviewers=list(preflight.ineligible_reviewer_route_ids),
            eligible_workers=list(preflight.eligible_worker_route_ids),
            ineligible_workers=list(preflight.ineligible_worker_route_ids),
            facts=list(resolution.facts),
            startup_verdict=(
                resolution.startup_verdict()
                if resolution.status is Status.OK
                else None
            ),
        )
    )
    return _status_exit_code(resolution.status)


def _run_snapshot_command(arguments: argparse.Namespace) -> int:
    try:
        _, allowed_paths = _parse_worker_task(arguments.task)
        repository = _canonical_directory(arguments.repo, "repository")
        snapshot = capture_source_snapshot(repository, allowed_paths)
        source_hash = hashlib.sha256(encode_source_snapshot(snapshot)).hexdigest()
        summary = snapshot.private_summary
        private_counts = {
            status.name.lower(): count for status, count in summary.status_counts
        }
    except (OSError, RepositoryError, TypeError, ValueError):
        print(
            _json_output(
                Status.NEEDS_CONTEXT,
                message="source snapshot could not be captured safely",
            )
        )
        return 2
    print(
        _json_output(
            Status.OK,
            source_snapshot_hash=source_hash,
            baseline_oid=snapshot.baseline_oid.decode("ascii"),
            allowed_path_count=len(snapshot.allowed_paths),
            staged_count=len(snapshot.staged),
            unstaged_count=len(snapshot.unstaged),
            untracked_count=len(snapshot.untracked),
            private_record_count=sum(private_counts.values()),
            private_aggregate_hash=summary.aggregate_hash,
            private_status_counts=private_counts,
            persisted=False,
            message="diagnostic snapshot only; the worker recaptures sealed evidence",
        )
    )
    return 0


def _run_review_command(arguments: argparse.Namespace) -> int:
    try:
        resources = load_invocation_resources(Path(arguments.manifest))
        repository = resources.repository_path
        temp_parent = resources.temp_parent
        context = _read_cli_json(arguments.context)
        sealed = read_sealed_delta_bundle(resources)
        packet = build_final_review_packet(sealed, context)
        binding_hash = ApprovalBinding(
            source_snapshot_hash=sealed.metadata.source_snapshot_hash,
            worker_delta_hash=sealed.metadata.worker_delta_hash,
            projected_task_patch_hash=sealed.metadata.projected_task_patch_hash,
        ).canonical_hash
        main_fingerprint = _parse_canonical_fingerprint(arguments.main_fingerprint)

        if sealed.authority_mode == Mode.LITE.value:
            if (
                not arguments.inline
                or arguments.profile is not None
                or arguments.route is not None
                or arguments.discover
                or arguments.credentials is not None
            ):
                raise ValueError("Lite review must use only inline main-loop authority")
            receipt = seal_final_review_receipt(
                resources,
                packet=packet,
                decision="approve",
                approval_binding_hash=binding_hash,
                reviewer_route_id="inline-main-loop",
                reviewer_fingerprint=main_fingerprint.canonical,
                fingerprint_evidence_source="host-metadata",
                reviewer_read_only_enforced=False,
                main_fingerprint=main_fingerprint.canonical,
                message="inline main loop approved the exact sealed packet",
                requested_changes=(),
            )
            print(
                _json_output(
                    Status.OK,
                    mode=Mode.LITE.value,
                    authority="inline-main-loop",
                    reviewer_fingerprint=receipt.reviewer_fingerprint,
                    decision=receipt.decision,
                    approval_binding_hash=receipt.approval_binding_hash,
                    review_packet_sha256=receipt.review_packet_sha256,
                    review_receipt=os.fspath(resources.final_evidence_path),
                    message=receipt.message,
                    requested_changes=[],
                )
            )
            return 0

        if sealed.authority_mode != Mode.MAX.value or arguments.inline:
            raise ValueError("sealed authority mode is invalid for external review")
        if arguments.profile is None or arguments.route is None:
            raise ValueError("Max review requires an external reviewer profile and route")
        profile = _profile_selection(arguments.profile)
        loaded = load_config(
            profile=profile,
            project_root=repository,
            discover=arguments.discover,
        )
        route = loaded.routes.get(arguments.route)
        if route is None:
            raise ValueError("reviewer route is not configured")
        credential_document = _optional_credentials_document(arguments.credentials)
        credentials, missing = _route_credentials(
            route,
            os.environ,
            credential_document,
        )
        if missing:
            print(
                _json_output(
                    Status.PROVIDER_UNAVAILABLE,
                    configured_credentials=sorted(credentials),
                    missing_credentials=list(missing),
                    message="reviewer credentials are unavailable",
                )
            )
            return 3
        probe = probe_route(
            route,
            Role.REVIEWER,
            credentials,
            select_verified_backend,
            run_process,
        )
        main_loop = MainLoop(
            route_id="host-main-loop",
            fingerprint=main_fingerprint,
            band=CapabilityBand.BALANCED,
            host="external-review-cli",
        )
        candidate = resolve_candidates(
            main_loop,
            loaded,
            RunOverrides(mode=Mode.MAX, reviewer=route.route_id),
        )
        preflight = preflight_candidates(candidate, {route.route_id: probe})
        resolution = finalize_resolution(candidate, preflight)
        if resolution.status is not Status.OK:
            print(
                _json_output(
                    resolution.status,
                    facts=list(resolution.facts),
                    message="external reviewer did not pass live Max preflight",
                )
            )
            return _status_exit_code(resolution.status)

        with tempfile.TemporaryDirectory(
            prefix="model-boss-review-run-",
            dir=temp_parent,
        ) as root_text:
            root = Path(root_text).resolve(strict=True)
            root.chmod(0o700)
            evidence_parent = root / "evidence-parent"
            route_state = root / "route-state"
            evidence_parent.mkdir(mode=0o700)
            route_state.mkdir(mode=0o700)
            result = execute_reviewer(
                route,
                packet,
                binding_hash,
                evidence_parent=evidence_parent,
                route_state_root=route_state,
                forbidden_roots=(repository,),
                credentials=credentials,
            )
    except (
        BundleError,
        ConfigError,
        OSError,
        RepositoryError,
        SetupError,
        TypeError,
        ValueError,
    ):
        print(
            _json_output(
                Status.NEEDS_CONTEXT,
                message="review inputs could not be validated safely",
            )
        )
        return 2
    if result.status is not Status.OK or result.verdict is None:
        print(_json_output(result.status, message=result.message))
        return _status_exit_code(result.status)
    review_receipt = None
    if result.verdict.decision == "approve":
        try:
            receipt = seal_final_review_receipt(
                resources,
                packet=packet,
                decision=result.verdict.decision,
                approval_binding_hash=result.verdict.approval_binding_hash,
                reviewer_route_id=route.route_id,
                reviewer_fingerprint=(
                    probe.resolved_fingerprint.canonical
                    if probe.resolved_fingerprint is not None
                    else ""
                ),
                fingerprint_evidence_source=(
                    probe.fingerprint_evidence_source.value
                    if probe.fingerprint_evidence_source is not None
                    else ""
                ),
                reviewer_read_only_enforced=(
                    probe.reviewer_read_only_enforced
                ),
                main_fingerprint=main_fingerprint.canonical,
                message=result.verdict.message,
                requested_changes=result.verdict.requested_changes,
            )
            review_receipt = os.fspath(resources.final_evidence_path)
        except (BundleError, OSError, TypeError, ValueError):
            print(
                _json_output(
                    Status.TRANSPORT_ERROR,
                    message="review approval could not be sealed to the invocation",
                )
            )
            return 3
    print(
        _json_output(
            Status.OK,
            mode=Mode.MAX.value,
            authority=route.route_id,
            reviewer_fingerprint=(
                probe.resolved_fingerprint.canonical
                if probe.resolved_fingerprint is not None
                else None
            ),
            decision=result.verdict.decision,
            approval_binding_hash=result.verdict.approval_binding_hash,
            review_packet_sha256=result.verdict.review_packet_sha256,
            review_receipt=review_receipt,
            message=result.verdict.message,
            requested_changes=list(result.verdict.requested_changes),
        )
    )
    return 0


def _parse_worker_task(path: str | os.PathLike[str]) -> tuple[WorkerTask, tuple[bytes, ...]]:
    value = _read_cli_json(path)
    if set(value) != {"version", "prompt", "allowed_paths", "gates"}:
        raise ValueError("worker task has an invalid schema")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("worker task version is unsupported")
    prompt = value["prompt"]
    allowed = value["allowed_paths"]
    gate_values = value["gates"]
    if not isinstance(prompt, str) or not prompt or "\0" in prompt:
        raise ValueError("worker prompt must be non-empty text")
    if (
        not isinstance(allowed, list)
        or not allowed
        or not all(isinstance(member, str) and member for member in allowed)
    ):
        raise ValueError("allowed_paths must be a non-empty text array")
    encoded_paths: list[bytes] = []
    for member in allowed:
        assert isinstance(member, str)
        if (
            "\0" in member
            or member.startswith("/")
            or member.endswith("/")
            or any(part in {"", ".", ".."} for part in member.split("/"))
        ):
            raise ValueError("allowed path is not a canonical repository path")
        try:
            encoded_paths.append(member.encode("utf-8", "strict"))
        except UnicodeError:
            raise ValueError("allowed path is not strict UTF-8") from None
    if len(set(encoded_paths)) != len(encoded_paths):
        raise ValueError("allowed_paths contains a duplicate")
    if not isinstance(gate_values, list) or not gate_values:
        raise ValueError("gates must be a non-empty array")
    gates: list[GateSpec] = []
    for gate_value in gate_values:
        if not isinstance(gate_value, dict) or set(gate_value) != {
            "argv",
            "cwd",
            "timeout_seconds",
        }:
            raise ValueError("gate has an invalid schema")
        argv_value = gate_value["argv"]
        if (
            not isinstance(argv_value, list)
            or not argv_value
            or not all(isinstance(member, str) for member in argv_value)
            or not isinstance(gate_value["cwd"], str)
        ):
            raise ValueError("gate argv and cwd must use direct text values")
        gates.append(
            GateSpec(
                argv=tuple(argv_value),
                cwd=gate_value["cwd"],
                timeout_seconds=gate_value["timeout_seconds"],
            )
        )
    return (
        WorkerTask(prompt=prompt.encode("utf-8", "strict"), gates=tuple(gates)),
        tuple(encoded_paths),
    )


_PROVIDER_WORKER_ALIASES = {
    "kimi": ("kimi", "claude-kimi-bypass", CapabilityBand.BALANCED),
    "claude-kimi-bypass": (
        "kimi",
        "claude-kimi-bypass",
        CapabilityBand.BALANCED,
    ),
    "glm": ("glm", "claude-glm-bypass", CapabilityBand.BALANCED),
    "claude-glm-bypass": (
        "glm",
        "claude-glm-bypass",
        CapabilityBand.BALANCED,
    ),
    "glm-turbo": (
        "glm-turbo",
        "claude-glm-turbo-bypass",
        CapabilityBand.FAST,
    ),
    "claude-glm-turbo-bypass": (
        "glm-turbo",
        "claude-glm-turbo-bypass",
        CapabilityBand.FAST,
    ),
}


def _provider_worker_route(value: str) -> tuple[str, Route]:
    try:
        provider_route, command, band = _PROVIDER_WORKER_ALIASES[value]
    except KeyError:
        raise ValueError("worker route is unsupported") from None
    return provider_route, Route(
        route_id=value,
        transport=Transport.EXTERNAL_CLI,
        band=band,
        roles=frozenset({Role.WORKER}),
        read_only=False,
        command=(command, "-p"),
    )


def _gate_json(evidence: GateEvidence) -> dict[str, object]:
    return {
        "argv": list(evidence.argv),
        "cwd": evidence.cwd,
        "status": evidence.status.value,
        "exit_code": evidence.exit_code,
        "stdout_hash": evidence.stdout_hash,
        "stderr_hash": evidence.stderr_hash,
        "duration_seconds": evidence.duration_seconds,
    }


def _emit_provider_worker_failure(
    resources: InvocationResources | None,
    status: Status,
    *,
    message: str,
    exit_code: int,
    fields: Mapping[str, object] | None = None,
) -> int:
    payload = dict(fields or {})
    if resources is not None:
        cleanup = cleanup_invocation(resources)
        if not cleanup.cleaned:
            print(
                _json_output(
                    Status.TRANSPORT_ERROR,
                    transaction_status=status.value,
                    cleanup_status=cleanup.status,
                    invocation_id=resources.invocation_id,
                    retained_manifest=os.fspath(resources.manifest_path),
                    message=(
                        "worker failed and invocation cleanup was rejected; "
                        "use the retained manifest for explicit recovery"
                    ),
                    **payload,
                )
            )
            return _status_exit_code(Status.TRANSPORT_ERROR)
        payload.update(
            cleanup_status=cleanup.status,
            invocation_id=resources.invocation_id,
        )
    print(_json_output(status, message=message, **payload))
    return exit_code


def _run_provider_worker_command(arguments: argparse.Namespace) -> int:
    resources: InvocationResources | None = None
    try:
        task, allowed_paths = _parse_worker_task(arguments.task)
        provider_route, route = _provider_worker_route(arguments.route)
        repository = Path(arguments.repo).resolve(strict=True)
        temp_parent = Path(arguments.temp_parent).resolve(strict=True)
        if not repository.is_dir() or not temp_parent.is_dir():
            raise ValueError("worker roots must be directories")
        environment = dict(os.environ)
        if arguments.credentials:
            credential_path = Path(arguments.credentials)
            if not credential_path.is_absolute():
                raise ValueError("credentials path must be absolute")
            for name in (
                "KIMI_BASE_URL",
                "KIMI_AUTH_TOKEN",
                "GLM_BASE_URL",
                "GLM_AUTH_TOKEN",
                "GLM_MODEL",
                "GLM_SMALL_FAST_MODEL",
            ):
                environment.pop(name, None)
            environment["MODEL_BOSS_CREDENTIALS"] = os.fspath(credential_path)

        resources = create_invocation_resources(repository, temp_parent)
        snapshot = capture_source_snapshot(repository, allowed_paths)
        handle = create_worktree(repository, snapshot, resources.worktree_path)
        materialize_snapshot(handle, snapshot)
        environment["MODEL_BOSS_INVOCATION_MANIFEST"] = os.fspath(
            resources.manifest_path
        )
        plan = prepare_provider_exec(
            provider_route,
            "sandboxed-worker",
            route.command[1:],
            environment=environment,
            cwd=handle.path,
        )
        if plan.status is not Status.OK:
            return _emit_provider_worker_failure(
                resources,
                plan.status,
                message=plan.message,
                exit_code=_status_exit_code(plan.status),
            )
        result = orchestrate_prepared_worker(
            repository,
            snapshot,
            handle,
            route,
            plan,
            task,
        )
        if result.status is not Status.OK or result.delta is None:
            return _emit_provider_worker_failure(
                resources,
                result.status,
                message=result.message,
                exit_code=_status_exit_code(result.status),
                fields={
                    "attempts": result.attempts,
                    "gates": [_gate_json(item) for item in result.gates],
                },
            )
        sealed_gates = tuple(
            SealedGateEvidence(
                argv=gate.argv,
                cwd=gate.cwd,
                status=gate.status.value,
                exit_code=gate.exit_code if gate.exit_code is not None else -1,
                stdout_hash=gate.stdout_hash,
                stderr_hash=gate.stderr_hash,
                duration_milliseconds=max(
                    0,
                    min(3_600_000, round(gate.duration_seconds * 1000)),
                ),
            )
            for gate in result.gates
        )
        metadata = seal_delta_bundle(
            resources,
            snapshot,
            result.delta,
            gates=sealed_gates,
            authority_mode=arguments.mode,
        )
        if (
            metadata.source_snapshot_hash != result.source_snapshot_hash
            or metadata.worker_delta_hash != result.worker_delta_hash
            or metadata.projected_task_patch_hash
            != result.projected_task_patch_hash
        ):
            raise BundleError("sealed bundle hashes changed after worker capture")
        print(
            _json_output(
                Status.OK,
                invocation_id=resources.invocation_id,
                manifest=os.fspath(resources.manifest_path),
                bundle=os.fspath(resources.delta_bundle_path),
                mode=arguments.mode,
                attempts=result.attempts,
                gates=[_gate_json(item) for item in result.gates],
                source_snapshot_hash=result.source_snapshot_hash,
                worker_delta_hash=result.worker_delta_hash,
                projected_task_patch_hash=result.projected_task_patch_hash,
                message="worker delta is sealed and awaiting review",
            )
        )
        return 0
    except (
        BundleError,
        ConfigError,
        OSError,
        RepositoryError,
        SandboxPolicyError,
        SetupError,
        TypeError,
        ValueError,
    ):
        return _emit_provider_worker_failure(
            resources,
            Status.NEEDS_CONTEXT,
            message="worker invocation could not be prepared safely",
            exit_code=2,
        )


def _run_integrate_command(arguments: argparse.Namespace) -> int:
    try:
        resources = load_invocation_resources(Path(arguments.manifest))
        receipt = read_final_review_receipt(resources)
        approval = Approval(
            version=1,
            decision=receipt.decision,
            binding=ApprovalBinding(
                source_snapshot_hash=receipt.source_snapshot_hash,
                worker_delta_hash=receipt.worker_delta_hash,
                projected_task_patch_hash=receipt.projected_task_patch_hash,
            ),
            approval_binding_hash=receipt.approval_binding_hash,
        )
    except (BundleError, OSError, TypeError, ValueError):
        print(
            _json_output(
                Status.NEEDS_CONTEXT,
                message="sealed final approval or active invocation manifest is invalid",
            )
        )
        return 2
    managed = integrate_sealed_delta_bundle(resources, approval)
    public_status = managed.transaction.status
    if not managed.cleanup.cleaned:
        public_status = Status.TRANSPORT_ERROR
    print(
        _json_output(
            public_status,
            transaction_status=managed.transaction.status.value,
            cleanup_status=managed.cleanup.status,
            invocation_id=managed.cleanup.invocation_id,
            applied=managed.transaction.applied,
            projected_task_patch_hash=(
                managed.transaction.projected_task_patch_hash
            ),
            message=managed.transaction.message,
        )
    )
    return _status_exit_code(public_status)


def _provider_failure(status: Status, message: str) -> ProviderExecPlan:
    return ProviderExecPlan(status=status, message=message)


def _environment_config_root(environment: Mapping[str, str]) -> Path:
    xdg_value = environment.get("XDG_CONFIG_HOME")
    xdg_root = Path(xdg_value) if xdg_value else None
    if xdg_root is not None and xdg_root.is_absolute():
        return xdg_root
    home_value = environment.get("HOME")
    home = Path(home_value) if home_value else None
    if home is not None:
        if not home.is_absolute():
            raise SetupError("configuration home is unavailable")
        return home / ".config"
    userprofile_value = environment.get("USERPROFILE")
    userprofile = Path(userprofile_value) if userprofile_value else None
    if userprofile is None or not userprofile.is_absolute():
        raise SetupError("configuration home is unavailable")
    return userprofile / ".config"


def _provider_credentials_path(environment: Mapping[str, str]) -> Path:
    override = environment.get("MODEL_BOSS_CREDENTIALS")
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            raise SetupError("credential override must be absolute")
        return candidate
    return _environment_config_root(environment) / "model-boss" / "credentials.json"


def _provider_credentials(
    route: str,
    environment: Mapping[str, str],
) -> dict[str, str]:
    required = (
        ("KIMI_BASE_URL", "KIMI_AUTH_TOKEN")
        if route == "kimi"
        else (
            "GLM_BASE_URL",
            "GLM_AUTH_TOKEN",
            "GLM_MODEL",
            "GLM_SMALL_FAST_MODEL",
        )
    )
    inherited = {
        name: environment[name]
        for name in required
        if isinstance(environment.get(name), str) and environment[name]
    }
    if len(inherited) == len(required):
        return inherited
    return load_credentials(_provider_credentials_path(environment))


def _resolve_provider_executable(
    resolver: Callable[[str], str | None],
) -> Path:
    discovered = resolver("claude")
    if not discovered:
        raise SetupError("provider executable is unavailable")
    try:
        executable = Path(discovered).resolve(strict=True)
        metadata = executable.stat()
    except OSError:
        raise SetupError("provider executable is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(executable, os.X_OK):
        raise SetupError("provider executable is unavailable")
    return executable


def _create_private_state_directory(parent: Path, label: str) -> Path:
    if (
        not label
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in label)
    ):
        raise ValueError("state label is invalid")
    for _ in range(32):
        path = parent / f".{label}-{os.getpid()}-{secrets.token_hex(12)}"
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        path.chmod(0o700)
        metadata = os.lstat(path)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != getattr(os, "geteuid", lambda: -1)()
            or path.resolve(strict=True) != path
        ):
            raise ValueError("private provider state could not be secured")
        return path
    raise ValueError("private provider state could not be allocated")


def _requests_permission_bypass(arguments: Sequence[str]) -> bool:
    lowered = tuple(member.lower() for member in arguments)
    for index, member in enumerate(lowered):
        if member == "--dangerously-skip-permissions" or member.startswith(
            "--dangerously-skip-permissions="
        ):
            return True
        if member.startswith("--permission-mode=") and member.partition("=")[2] in {
            "bypasspermissions",
            "bypass",
        }:
            return True
        if (
            member == "--permission-mode"
            and index + 1 < len(lowered)
            and lowered[index + 1] in {"bypasspermissions", "bypass"}
        ):
            return True
    return False


def _isolated_git_environment(
    repository: Path,
    worktree: Path,
    state: Path,
) -> dict[str, str]:
    """Create a private Git admin/index so sandboxed tools never need source .git."""

    discovered = shutil.which("git")
    if discovered is None:
        raise SetupError("Git is unavailable for isolated worker tooling")
    try:
        git = Path(discovered).resolve(strict=True)
    except OSError:
        raise SetupError("Git is unavailable for isolated worker tooling") from None
    if not git.is_file() or not os.access(git, os.X_OK):
        raise SetupError("Git is unavailable for isolated worker tooling")
    git_dir = state / "git-admin"
    index_file = state / "git-index"
    if git_dir.exists() or git_dir.is_symlink() or index_file.exists() or index_file.is_symlink():
        raise SetupError("isolated Git state already exists")
    base_environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.fspath(state),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def run(arguments: Sequence[str], *, cwd: Path, environment=None) -> bytes:
        completed = subprocess.run(
            (os.fspath(git), *arguments),
            cwd=cwd,
            env=base_environment if environment is None else environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=120,
        )
        if completed.returncode != 0:
            raise SetupError("isolated Git state could not be prepared")
        return completed.stdout.strip()

    baseline = run(("rev-parse", "--verify", "HEAD"), cwd=repository)
    if not baseline or any(byte not in b"0123456789abcdef" for byte in baseline):
        raise SetupError("isolated Git baseline is invalid")
    try:
        run(("init", "--bare", "-q", os.fspath(git_dir)), cwd=state)
        run(
            (
                "--git-dir",
                os.fspath(git_dir),
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--force",
                os.fspath(repository),
                baseline.decode("ascii"),
            ),
            cwd=state,
        )
        run(
            (
                "--git-dir",
                os.fspath(git_dir),
                "update-ref",
                "refs/heads/model-boss-worker",
                baseline.decode("ascii"),
            ),
            cwd=state,
        )
        run(
            (
                "--git-dir",
                os.fspath(git_dir),
                "symbolic-ref",
                "HEAD",
                "refs/heads/model-boss-worker",
            ),
            cwd=state,
        )
        child = {
            **base_environment,
            "GIT_DIR": os.fspath(git_dir),
            "GIT_WORK_TREE": os.fspath(worktree),
            "GIT_INDEX_FILE": os.fspath(index_file),
            "GIT_OPTIONAL_LOCKS": "0",
        }
        run(("read-tree", baseline.decode("ascii")), cwd=worktree, environment=child)
        index_metadata = os.lstat(index_file)
        if not stat.S_ISREG(index_metadata.st_mode) or stat.S_ISLNK(index_metadata.st_mode):
            raise SetupError("isolated Git index is invalid")
        return {
            key: child[key]
            for key in (
                "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_DIR",
                "GIT_INDEX_FILE",
                "GIT_LITERAL_PATHSPECS",
                "GIT_NO_LAZY_FETCH",
                "GIT_OPTIONAL_LOCKS",
                "GIT_TERMINAL_PROMPT",
                "GIT_WORK_TREE",
            )
        }
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise SetupError("isolated Git state could not be prepared") from None


def prepare_provider_exec(
    route: str,
    policy: str,
    provider_args: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    executable_resolver: Callable[[str], str | None] = shutil.which,
    sandbox_factory: Callable[..., VerifiedSandbox | UnavailableSandbox] = (
        select_verified_backend
    ),
) -> ProviderExecPlan:
    """Validate and prepare one safe or freshly sandboxed provider launch."""

    source_environment = dict(os.environ if environment is None else environment)
    arguments = tuple(provider_args)
    if (
        route not in {"kimi", "glm", "glm-turbo"}
        or policy not in {"safe", "sandboxed-worker"}
        or isinstance(provider_args, (str, bytes))
        or not all(isinstance(member, str) and "\0" not in member for member in arguments)
    ):
        return _provider_failure(
            Status.PROVIDER_UNAVAILABLE,
            "provider route is invalid",
        )
    if _requests_permission_bypass(arguments):
        return _provider_failure(
            Status.PROVIDER_UNAVAILABLE,
            "provider permission bypass may only be injected after sandbox verification",
        )

    if policy == "sandboxed-worker":
        manifest_value = source_environment.get("MODEL_BOSS_INVOCATION_MANIFEST")
        if not manifest_value or not Path(manifest_value).is_absolute():
            return _provider_failure(
                Status.SANDBOX_UNAVAILABLE,
                "a sealed invocation manifest is required",
            )
        try:
            resources = load_invocation_resources(
                Path(manifest_value),
                require_registered_worktree=True,
            )
            actual_cwd = Path.cwd() if cwd is None else Path(cwd)
            resolved_cwd = actual_cwd.resolve(strict=True)
            if (
                resolved_cwd != resources.worktree_path
                or resolved_cwd == resources.repository_path
                or not resolved_cwd.is_dir()
            ):
                raise ValueError("cwd is not the registered invocation worktree")
        except (OSError, TypeError, ValueError):
            return _provider_failure(
                Status.SANDBOX_UNAVAILABLE,
                "sealed invocation or worktree validation failed",
            )
    else:
        resources = None
        try:
            resolved_cwd = (Path.cwd() if cwd is None else Path(cwd)).resolve(
                strict=True
            )
        except OSError:
            return _provider_failure(
                Status.PROVIDER_UNAVAILABLE,
                "provider working directory is unavailable",
            )

    try:
        executable = _resolve_provider_executable(executable_resolver)
        credentials = _provider_credentials(route, source_environment)
    except (OSError, SetupError, TypeError, ValueError):
        return _provider_failure(
            Status.PROVIDER_UNAVAILABLE,
            "provider route is unavailable",
        )

    if resources is None:
        try:
            child_environment = provider_child_environment(
                route,
                credentials,
                source_environment,
            )
        except (SetupError, TypeError, ValueError):
            return _provider_failure(
                Status.PROVIDER_UNAVAILABLE,
                "provider route is unavailable",
            )
        return ProviderExecPlan(
            status=Status.OK,
            executable=executable,
            argv=(os.fspath(executable), *arguments),
            cwd=resolved_cwd,
            environment=child_environment,
            message="provider route is ready",
        )

    try:
        provider_state = _create_private_state_directory(
            resources.route_state_path,
            f"provider-{route}",
        )
        probe_parent = _create_private_state_directory(
            resources.route_state_path,
            f"probe-{route}",
        )
        child_base = dict(source_environment)
        child_base["HOME"] = os.fspath(provider_state)
        child_environment = provider_child_environment(
            route,
            credentials,
            child_base,
        )
        child_environment.update(
            {
                "HOME": os.fspath(provider_state),
                "XDG_CONFIG_HOME": os.fspath(provider_state),
                "XDG_CACHE_HOME": os.fspath(provider_state),
                "XDG_STATE_HOME": os.fspath(provider_state),
                "TMPDIR": os.fspath(provider_state),
            }
        )
        child_environment.update(
            _isolated_git_environment(
                resources.repository_path,
                resources.worktree_path,
                provider_state,
            )
        )
        git_pointer = resources.worktree_path / ".git"
        git_pointer_metadata = os.lstat(git_pointer)
        if (
            not stat.S_ISREG(git_pointer_metadata.st_mode)
            or stat.S_ISLNK(git_pointer_metadata.st_mode)
        ):
            raise SandboxPolicyError("disposable worktree Git pointer is invalid")
        sandbox_policy = SandboxPolicy(
            worktree_root=resources.worktree_path,
            route_state_root=provider_state,
            readable_roots=(git_pointer,),
            protected_roots=(resources.repository_path,),
            network_required=True,
        )
        route_argv = (
            os.fspath(executable),
            "--dangerously-skip-permissions",
            "--safe-mode",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--tools",
            "Read,Glob,Grep,Edit,Write",
            *arguments,
        )
        sandbox = sandbox_factory(
            sandbox_policy,
            route_id=f"provider-{route}",
            argv=route_argv,
            probe_parent=probe_parent,
        )
        if not isinstance(sandbox, VerifiedSandbox):
            raise SandboxPolicyError("verified sandbox backend is unavailable")
        launch = sandbox.prepare(
            route_id=f"provider-{route}",
            argv=route_argv,
            policy=sandbox_policy,
            cwd=resources.worktree_path,
        )
        if (
            not launch.available
            or launch.cwd != resources.worktree_path
            or not launch.argv
        ):
            raise SandboxPolicyError("verified sandbox binding is unavailable")
        launcher = Path(launch.argv[0]).resolve(strict=True)
    except (OSError, SandboxPolicyError, SetupError, TypeError, ValueError):
        return _provider_failure(
            Status.SANDBOX_UNAVAILABLE,
            "fresh provider sandbox verification failed",
        )
    return ProviderExecPlan(
        status=Status.OK,
        executable=launcher,
        argv=launch.argv,
        cwd=resources.worktree_path,
        environment=child_environment,
        sandbox=sandbox,
        message="fresh verified provider sandbox is ready",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model-boss",
        description="Model-independent Model Boss routing and sealed evidence tools.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in _COMMANDS:
        child = subparsers.add_parser(
            name,
            help=_COMMAND_HELP[name],
            description=_COMMAND_HELP[name],
        )
        if name == "resolve":
            child.add_argument("--profile", required=True)
            child.add_argument("--project-root")
            child.add_argument("--discover", action="store_true")
            child.add_argument("--credentials")
            child.add_argument("--main-route", required=True)
            child.add_argument("--main-provider", required=True)
            child.add_argument("--main-model", required=True)
            child.add_argument("--main-variant", required=True)
            child.add_argument(
                "--main-band",
                choices=tuple(member.value for member in CapabilityBand),
                required=True,
            )
            child.add_argument("--host", required=True)
            child.add_argument(
                "--mode",
                choices=tuple(member.value for member in Mode),
            )
            child.add_argument("--reviewer")
            child.add_argument("--worker")
        elif name == "review":
            child.add_argument("--inline", action="store_true")
            child.add_argument("--profile")
            child.add_argument("--discover", action="store_true")
            child.add_argument("--route")
            child.add_argument("--main-fingerprint", required=True)
            child.add_argument("--manifest", required=True)
            child.add_argument("--context", required=True)
            child.add_argument("--credentials")
        elif name == "snapshot":
            child.add_argument("--repo", required=True)
            child.add_argument("--task", required=True)
        elif name == "validate-config":
            child.add_argument("path")
        elif name == "setup-providers":
            child.add_argument("--legacy-source")
            child.add_argument("--credentials")
            child.add_argument("--install-path")
        elif name == "provider-exec":
            child.add_argument("--route", required=True)
            child.add_argument("--policy", choices=("safe", "sandboxed-worker"), required=True)
            child.add_argument("provider_args", nargs=argparse.REMAINDER)
        elif name == "worker":
            child.add_argument("--repo", required=True)
            child.add_argument("--temp-parent", required=True)
            child.add_argument(
                "--route",
                choices=tuple(_PROVIDER_WORKER_ALIASES),
                required=True,
            )
            child.add_argument("--task", required=True)
            child.add_argument(
                "--mode",
                choices=(Mode.LITE.value, Mode.MAX.value),
                required=True,
            )
            child.add_argument("--credentials")
        elif name == "integrate":
            child.add_argument("manifest")
        elif name == "cleanup":
            child.add_argument("manifest")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if arguments.command == "resolve":
        return _run_resolve_command(arguments)
    if arguments.command == "review":
        return _run_review_command(arguments)
    if arguments.command == "snapshot":
        return _run_snapshot_command(arguments)
    if arguments.command == "validate-config":
        try:
            load_config(profile=Path(arguments.path), discover=False)
        except (ConfigError, OSError, ValueError):
            print(_json_output(Status.NEEDS_CONTEXT, message="configuration is invalid"))
            return 2
        print(_json_output(Status.OK))
        return 0
    if arguments.command == "setup-providers":
        try:
            legacy = (
                Path(arguments.legacy_source)
                if arguments.legacy_source is not None
                else None
            )
            if legacy is not None and not legacy.is_absolute():
                raise SetupError("legacy source must be absolute")
            if arguments.credentials is not None:
                credential_path = Path(arguments.credentials)
                if not credential_path.is_absolute():
                    raise SetupError("credential destination must be absolute")
            elif legacy is not None:
                credential_path = (
                    _environment_config_root(os.environ)
                    / "model-boss"
                    / "credentials.json"
                )
            else:
                credential_path = None
            if legacy is not None and credential_path is not None:
                setup_status = migrate_legacy_credentials(
                    legacy,
                    credential_path,
                ).status
            elif arguments.credentials is not None:
                raise SetupError("credential destination requires a legacy source")
            elif arguments.install_path:
                setup_status = "wrappers_configured"
            else:
                raise SetupError("no provider setup action is available")
            if arguments.install_path:
                runner = Path(__file__).resolve().parents[2] / "scripts" / "model-boss.py"
                install_provider_wrappers(runner, Path(arguments.install_path))
        except (OSError, SetupError, ValueError):
            print(_json_output(Status.NEEDS_CONTEXT, message="provider setup failed safely"))
            return 2
        print(_json_output(Status.OK, setup_status=setup_status))
        return 0
    if arguments.command == "worker":
        return _run_provider_worker_command(arguments)
    if arguments.command == "integrate":
        return _run_integrate_command(arguments)
    if arguments.command == "provider-exec":
        provider_args = tuple(arguments.provider_args)
        if not provider_args or provider_args[0] != "--":
            print(_json_output(Status.NEEDS_CONTEXT, message="provider argv requires --"))
            return 2
        plan = prepare_provider_exec(
            arguments.route,
            arguments.policy,
            provider_args[1:],
            environment=os.environ,
            cwd=Path.cwd(),
        )
        if plan.status is not Status.OK or plan.executable is None:
            print(_json_output(plan.status, message=plan.message))
            return 3
        os.execve(
            plan.executable,
            plan.argv,
            dict(plan.environment),
        )
        return 3
    if arguments.command == "cleanup":
        try:
            resources = load_invocation_resources(Path(arguments.manifest))
        except (OSError, TypeError, ValueError):
            print(
                _json_output(
                    Status.NEEDS_CONTEXT,
                    message="active invocation manifest is invalid or unavailable",
                )
            )
            return 2
        result = cleanup_invocation(resources)
        if result.cleaned:
            print(
                _json_output(
                    Status.OK,
                    cleanup_status=result.status,
                    invocation_id=result.invocation_id,
                    worktree_removed=result.worktree_removed,
                )
            )
            return 0
        print(
            _json_output(
                Status.TRANSPORT_ERROR,
                cleanup_status=result.status,
                invocation_id=result.invocation_id,
                message="invocation cleanup failed safely",
            )
        )
        return 3
    print(_json_output(Status.NEEDS_CONTEXT, message="command requires an invocation packet"))
    return 2


__all__ = (
    "GateEvidence",
    "GateSpec",
    "ProviderExecPlan",
    "WorkerRunResult",
    "WorkerTask",
    "build_parser",
    "main",
    "orchestrate_prepared_worker",
    "orchestrate_worker",
    "prepare_provider_exec",
)
