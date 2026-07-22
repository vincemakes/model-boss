"""Guarded application of an exactly reviewed worker delta."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path

from .bundle import BundleError, read_sealed_delta_bundle
from .evidence import (
    ApprovalBinding,
    EvidenceRecord,
    MODE_EXECUTABLE,
    MODE_SYMLINK,
    RecordStatus,
    RecordTag,
    SourceSnapshot,
    WorkerDelta,
    encode_canonical_patch,
    encode_source_snapshot,
    encode_worker_delta,
)
from .models import Status
from .repository import (
    RepositoryError,
    capture_destination,
    create_worktree,
    materialize_snapshot,
    project_task_patch,
)
from .resources import (
    CleanupResult,
    InvocationResources,
    cleanup_invocation,
)


INTEGRATION_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_APPLY_PREFIX = (
    "git",
    "--no-pager",
    "-c",
    "apply.ignoreWhitespace=no",
    "apply",
    "--whitespace=nowarn",
)


@dataclass(frozen=True)
class Approval:
    """A reviewer decision bound to one exact three-hash tuple."""

    version: int
    decision: str
    binding: ApprovalBinding
    approval_binding_hash: str
    _contract_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.version != INTEGRATION_VERSION:
            raise ValueError("unsupported approval version")
        if self.decision not in {"approve", "revise"}:
            raise ValueError("decision must be approve or revise")
        if not isinstance(self.binding, ApprovalBinding):
            raise ValueError("binding must be ApprovalBinding")
        binding = ApprovalBinding(
            source_snapshot_hash=self.binding.source_snapshot_hash,
            worker_delta_hash=self.binding.worker_delta_hash,
            projected_task_patch_hash=self.binding.projected_task_patch_hash,
        )
        if (
            not isinstance(self.approval_binding_hash, str)
            or _SHA256_RE.fullmatch(self.approval_binding_hash) is None
        ):
            raise ValueError(
                "approval_binding_hash must be a lower-case SHA-256 digest"
            )
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "_contract_hash", _approval_contract_hash(self))


def _approval_contract_hash(approval: Approval) -> str:
    fields = (
        str(approval.version).encode("ascii"),
        approval.decision.encode("ascii"),
        approval.binding.source_snapshot_hash.encode("ascii"),
        approval.binding.worker_delta_hash.encode("ascii"),
        approval.binding.projected_task_patch_hash.encode("ascii"),
        approval.approval_binding_hash.encode("ascii"),
    )
    encoded = bytearray(b"TOKEN-SAVER-APPROVAL-CONTRACT\0")
    for value in fields:
        encoded.extend(len(value).to_bytes(8, "big"))
        encoded.extend(value)
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class IntegrationResult:
    """Versioned result returned for both applied and fail-closed outcomes."""

    version: int
    status: Status
    applied: bool
    projected_task_patch_hash: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if self.version != INTEGRATION_VERSION:
            raise ValueError("unsupported integration result version")
        try:
            status = Status(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("integration result status is unsupported") from exc
        if not isinstance(self.applied, bool):
            raise ValueError("applied must be a boolean")
        if self.projected_task_patch_hash is not None and (
            not isinstance(self.projected_task_patch_hash, str)
            or _SHA256_RE.fullmatch(self.projected_task_patch_hash) is None
        ):
            raise ValueError(
                "projected_task_patch_hash must be a lower-case SHA-256 digest"
            )
        if not isinstance(self.message, str):
            raise ValueError("message must be text")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class ManagedIntegrationResult:
    """Keep transaction truth and one-shot resource cleanup truth separate."""

    transaction: IntegrationResult
    cleanup: CleanupResult

    def __post_init__(self) -> None:
        if not isinstance(self.transaction, IntegrationResult):
            raise ValueError("transaction must be an IntegrationResult")
        if not isinstance(self.cleanup, CleanupResult):
            raise ValueError("cleanup must be a CleanupResult")


def _failure(status: Status, message: str) -> IntegrationResult:
    return IntegrationResult(
        version=INTEGRATION_VERSION,
        status=status,
        applied=False,
        message=message,
    )


def _rejected(message: str) -> IntegrationResult:
    return _failure(Status.APPROVAL_STALE, message)


def _git_environment() -> dict[str, str]:
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
    }
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return environment


def _run_apply_check(repo: Path, patch: bytes) -> bool:
    try:
        completed = subprocess.run(
            (*_GIT_APPLY_PREFIX, "--check", "--binary"),
            cwd=repo,
            env=_git_environment(),
            input=patch,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _run_apply(repo: Path, patch: bytes) -> bool:
    try:
        completed = subprocess.run(
            (*_GIT_APPLY_PREFIX, "--binary"),
            cwd=repo,
            env=_git_environment(),
            input=patch,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _render_untracked_patch(record: EvidenceRecord) -> bytes:
    if record.content is None:
        raise ValueError("content-backed worker record is missing content")
    with tempfile.TemporaryDirectory(prefix="model-boss-untracked-patch-") as root:
        root_path = Path(root)
        relative_path = Path(os.fsdecode(record.path))
        destination = root_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if record.new_mode == MODE_SYMLINK:
            os.symlink(record.content, os.fsencode(destination))
        else:
            destination.write_bytes(record.content)
            destination.chmod(
                0o755 if record.new_mode == MODE_EXECUTABLE else 0o644
            )
        completed = subprocess.run(
            (
                "git",
                "--no-pager",
                "diff",
                "--no-index",
                "--binary",
                "--full-index",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--",
                os.devnull,
                os.fsdecode(record.path),
            ),
            cwd=root_path,
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    if completed.returncode not in {0, 1} or not completed.stdout:
        raise ValueError("unable to render content-backed worker patch")
    return completed.stdout


def _quote_patch_path(path: bytes) -> bytes:
    value = b'a/' + path
    if all(0x21 <= byte <= 0x7E and byte not in b'\\"' for byte in value):
        return value
    escaped = bytearray(b'"')
    replacements = {
        0x09: b"\\t",
        0x0A: b"\\n",
        0x0D: b"\\r",
        0x22: b'\\"',
        0x5C: b"\\\\",
    }
    for byte in value:
        replacement = replacements.get(byte)
        if replacement is not None:
            escaped.extend(replacement)
        elif 0x20 <= byte <= 0x7E:
            escaped.append(byte)
        else:
            escaped.extend(f"\\{byte:03o}".encode("ascii"))
    escaped.extend(b'"')
    return bytes(escaped)


def _render_mode_patch(record: EvidenceRecord) -> bytes:
    old_path = _quote_patch_path(record.path)
    new_path = old_path.replace(b'a/', b'b/', 1)
    return b"".join(
        (
            b"diff --git ",
            old_path,
            b" ",
            new_path,
            b"\nold mode ",
            f"{record.old_mode:o}".encode("ascii"),
            b"\nnew mode ",
            f"{record.new_mode:o}".encode("ascii"),
            b"\n",
        )
    )


def _build_apply_patch(delta: WorkerDelta) -> bytes:
    chunks: list[bytes] = []
    for record in delta.records:
        if record.canonical_diff:
            chunk = record.canonical_diff
        elif record.tag is RecordTag.MODE_ONLY:
            chunk = _render_mode_patch(record)
        elif record.content is not None:
            chunk = _render_untracked_patch(record)
        else:
            raise ValueError("worker record cannot be represented as a Git patch")
        if not chunk.endswith(b"\n"):
            raise ValueError("canonical worker patch must end with a newline")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_numstat_paths(encoded: bytes) -> tuple[bytes, ...]:
    paths: list[bytes] = []
    cursor = 0
    while cursor < len(encoded):
        first_tab = encoded.find(b"\t", cursor)
        second_tab = encoded.find(b"\t", first_tab + 1)
        if first_tab < 0 or second_tab < 0:
            raise ValueError("malformed Git numstat output")
        cursor = second_tab + 1
        if cursor < len(encoded) and encoded[cursor] == 0:
            raise ValueError("rename/copy patch paths are not supported")
        terminator = encoded.find(b"\0", cursor)
        if terminator < 0:
            raise ValueError("unterminated Git numstat path")
        paths.append(encoded[cursor:terminator])
        cursor = terminator + 1
    return tuple(paths)


def _expected_patch_paths(delta: WorkerDelta) -> tuple[bytes, ...]:
    paths: list[bytes] = []
    for record in delta.records:
        multiplicity = (
            2
            if record.tag is RecordTag.SYMLINK
            and record.status is RecordStatus.TYPE_CHANGED
            else 1
        )
        paths.extend((record.path,) * multiplicity)
    return tuple(paths)


def _parse_git_quoted_token(line: bytes, cursor: int) -> tuple[bytes, int]:
    if cursor >= len(line):
        raise ValueError("missing Git patch path token")
    if line[cursor] != 0x22:
        terminator = line.find(b" ", cursor)
        if terminator < 0:
            terminator = len(line)
        token = line[cursor:terminator]
        if not token:
            raise ValueError("empty Git patch path token")
        return token, terminator

    cursor += 1
    decoded = bytearray()
    simple_escapes = {
        ord("a"): 0x07,
        ord("b"): 0x08,
        ord("t"): 0x09,
        ord("n"): 0x0A,
        ord("v"): 0x0B,
        ord("f"): 0x0C,
        ord("r"): 0x0D,
        ord('"'): 0x22,
        ord("\\"): 0x5C,
    }
    while cursor < len(line):
        byte = line[cursor]
        cursor += 1
        if byte == 0x22:
            return bytes(decoded), cursor
        if byte != 0x5C:
            decoded.append(byte)
            continue
        if cursor >= len(line):
            raise ValueError("truncated Git patch path escape")
        escaped = line[cursor]
        if escaped in b"01234567":
            digits = bytearray()
            while cursor < len(line) and len(digits) < 3:
                candidate = line[cursor]
                if candidate not in b"01234567":
                    break
                digits.append(candidate)
                cursor += 1
            value = int(digits, 8)
            if value > 0xFF:
                raise ValueError("Git patch path octal escape is out of range")
            decoded.append(value)
            continue
        replacement = simple_escapes.get(escaped)
        if replacement is None:
            raise ValueError("unknown Git patch path escape")
        decoded.append(replacement)
        cursor += 1
    raise ValueError("unterminated Git patch path quote")


def _diff_header_paths(patch: bytes) -> tuple[tuple[bytes, bytes], ...]:
    prefix = b"diff --git "
    headers: list[tuple[bytes, bytes]] = []
    lines = patch.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(prefix):
            continue
        old_token, cursor = _parse_git_quoted_token(line, len(prefix))
        if cursor >= len(line) or line[cursor] != 0x20:
            raise ValueError("Git patch header paths are not separated canonically")
        new_token, cursor = _parse_git_quoted_token(line, cursor + 1)
        if cursor != len(line):
            raise ValueError("Git patch header has trailing path data")
        if not old_token.startswith(b"a/") or not new_token.startswith(b"b/"):
            raise ValueError("Git patch header lacks canonical path prefixes")
        old_path, new_path = old_token[2:], new_token[2:]
        headers.append((old_path, new_path))

        for metadata in lines[index + 1 :]:
            if metadata.startswith(prefix) or metadata.startswith(b"@@"):
                break
            if metadata == b"GIT binary patch":
                break
            if metadata.startswith(b"--- "):
                token, end = _parse_git_quoted_token(metadata, 4)
                if end != len(metadata) or (
                    token != b"/dev/null"
                    and (not token.startswith(b"a/") or token[2:] != old_path)
                ):
                    raise ValueError("Git patch old metadata path is inconsistent")
            elif metadata.startswith(b"+++ "):
                token, end = _parse_git_quoted_token(metadata, 4)
                if end != len(metadata) or (
                    token != b"/dev/null"
                    and (not token.startswith(b"b/") or token[2:] != new_path)
                ):
                    raise ValueError("Git patch new metadata path is inconsistent")
    return tuple(headers)


def _inspect_patch_paths(repo: Path, patch: bytes) -> tuple[bytes, ...]:
    if not patch:
        return ()
    completed = subprocess.run(
        (*_GIT_APPLY_PREFIX, "--numstat", "-z", "--binary"),
        cwd=repo,
        env=_git_environment(),
        input=patch,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("Git rejected the worker patch path manifest")
    return _parse_numstat_paths(completed.stdout)


def _windows_patch_path_is_safe(path: bytes) -> bool:
    if b"\\" in path:
        return False
    components = path.split(b"/")
    if len(path) >= 2 and path[0:1].isalpha() and path[1:2] == b":":
        return False
    if any(
        b":" in component
        or component.endswith((b".", b" "))
        or component.lower() == b".git"
        for component in components
    ):
        return False
    reserved = {b"CON", b"PRN", b"AUX", b"NUL"}
    reserved.update(f"COM{number}".encode("ascii") for number in range(1, 10))
    reserved.update(f"LPT{number}".encode("ascii") for number in range(1, 10))
    return not any(
        component.split(b".", 1)[0].upper() in reserved
        for component in components
    )


def _path_is_safe(
    repo: Path,
    path: bytes,
    allowed_paths: frozenset[bytes],
    *,
    windows: bool | None = None,
) -> bool:
    if path not in allowed_paths or not path or b"\0" in path:
        return False
    if path.startswith((b"/", b"\\")):
        return False
    if (
        len(path) >= 3
        and path[0:1].lower() in b"abcdefghijklmnopqrstuvwxyz"
        and path[1:2] == b":"
        and path[2:3] in {b"/", b"\\"}
    ):
        return False
    components = path.split(b"/")
    if any(
        component in {b"", b".", b".."} or component.lower() == b".git"
        for component in components
    ):
        return False
    if windows is None:
        windows = os.name == "nt"
    if windows and not _windows_patch_path_is_safe(path):
        return False

    current = repo
    for component in components[:-1]:
        current = current / os.fsdecode(component)
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            return False
    try:
        current.resolve(strict=False).relative_to(repo)
    except (OSError, ValueError):
        return False
    return True


def _symlink_target_is_safe(
    record: EvidenceRecord,
    *,
    windows: bool | None = None,
) -> bool:
    """Validate a raw symlink target lexically on POSIX and Windows."""

    if record.new_mode != MODE_SYMLINK:
        return True
    target = record.content
    if target is None or not target or b"\0" in target:
        return False
    if windows is None:
        windows = os.name == "nt"
    normalized = target.replace(b"\\", b"/")
    if normalized.startswith(b"/") or re.match(br"[A-Za-z]:", normalized):
        return False

    components = list(record.path.split(b"/")[:-1])
    for component in normalized.split(b"/"):
        if component in {b"", b"."}:
            continue
        if component == b"..":
            if not components:
                return False
            components.pop()
            continue
        components.append(component)
    if any(component.lower() == b".git" for component in components):
        return False
    if not windows:
        return True

    reserved = {b"CON", b"PRN", b"AUX", b"NUL"}
    reserved.update(f"COM{number}".encode("ascii") for number in range(1, 10))
    reserved.update(f"LPT{number}".encode("ascii") for number in range(1, 10))
    for component in components:
        if (
            b":" in component
            or component.endswith((b".", b" "))
            or component.split(b".", 1)[0].upper() in reserved
        ):
            return False
    return True


def _symlink_target_resolves_inside(
    repo: Path,
    path: bytes,
    target: bytes,
) -> bool:
    """Resolve an independently observed link target against the real repo."""

    leaf = repo / os.fsdecode(path)
    try:
        resolved = (leaf.parent / os.fsdecode(target)).resolve(strict=False)
        relative = resolved.relative_to(repo)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return False
    return all(component.lower() != ".git" for component in relative.parts)


def _read_simulated_symlink_targets(
    worktree: Path,
    delta: WorkerDelta,
) -> tuple[tuple[bytes, bytes], ...]:
    """Read resulting link bytes from the simulation, never from delta metadata."""

    targets: list[tuple[bytes, bytes]] = []
    root = os.fsencode(worktree)
    for record in delta.records:
        if record.new_mode != MODE_SYMLINK:
            continue
        leaf = root + b"/" + record.path
        try:
            metadata = os.lstat(leaf)
            target = os.readlink(leaf)
        except OSError as exc:
            raise RepositoryError(
                "simulated symlink result could not be inspected safely"
            ) from exc
        if not stat.S_ISLNK(metadata.st_mode) or type(target) is not bytes:
            raise RepositoryError("simulated symlink result has the wrong type")
        targets.append((record.path, target))
    return tuple(targets)


class _SimulationConflict(RepositoryError):
    """The independently reconstructed delta did not apply exactly."""


def _remove_simulation_worktree(repository: Path, worktree: Path) -> None:
    try:
        completed = subprocess.run(
            (
                "git",
                "--no-pager",
                "-c",
                "core.hooksPath=/dev/null",
                "worktree",
                "remove",
                "--force",
                os.fspath(worktree),
            ),
            cwd=repository,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise RepositoryError("simulation worktree cleanup failed") from exc
    if completed.returncode != 0:
        raise RepositoryError("simulation worktree cleanup failed")


def _simulate_projected_patch_hash(
    repository: Path,
    snapshot: SourceSnapshot,
    delta: WorkerDelta,
    patch: bytes,
) -> tuple[str, tuple[tuple[bytes, bytes], ...]]:
    """Derive the reviewed result from delta records without trusting its cache."""

    with tempfile.TemporaryDirectory(prefix="model-boss-integration-check-") as root:
        worktree_path = Path(root) / "worktree"
        handle = None
        primary_error: BaseException | None = None
        try:
            handle = create_worktree(repository, snapshot, worktree_path)
            materialize_snapshot(handle, snapshot)
            if patch:
                if not _run_apply_check(handle.path, patch):
                    raise _SimulationConflict(
                        "worker delta conflicts with its captured source snapshot"
                    )
                if not _run_apply(handle.path, patch):
                    raise _SimulationConflict(
                        "worker delta could not be simulated atomically"
                    )
            simulated = capture_destination(handle.path, snapshot.allowed_paths)
            if simulated.private or simulated.staged != snapshot.staged:
                raise RepositoryError(
                    "simulated worker delta changed private state or the index"
                )
            projected = SourceSnapshot(
                baseline_oid=simulated.baseline_oid,
                allowed_paths=simulated.allowed_paths,
                staged=simulated.staged,
                unstaged=simulated.unstaged,
                untracked=simulated.untracked,
                private=snapshot.private,
            )
            canonical = project_task_patch(projected, WorkerDelta(records=()))
            projected_hash = hashlib.sha256(
                encode_canonical_patch(canonical)
            ).hexdigest()
            return projected_hash, _read_simulated_symlink_targets(
                handle.path,
                delta,
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if handle is not None:
                try:
                    _remove_simulation_worktree(repository, handle.path)
                except RepositoryError:
                    if primary_error is None:
                        raise


def integrate_reviewed_delta(
    repo: str | bytes | PathLike[str] | PathLike[bytes],
    snapshot: SourceSnapshot,
    delta: WorkerDelta,
    approval: Approval,
) -> IntegrationResult:
    """Apply ``delta`` only when every reviewed invariant still holds."""

    if not isinstance(approval, Approval):
        return _rejected("approval contract is invalid")
    try:
        validated_approval = Approval(
            version=approval.version,
            decision=approval.decision,
            binding=approval.binding,
            approval_binding_hash=approval.approval_binding_hash,
        )
        contract_unchanged = hmac.compare_digest(
            approval._contract_hash,
            validated_approval._contract_hash,
        )
    except (AttributeError, TypeError, ValueError):
        return _rejected("approval contract is invalid")
    if not contract_unchanged:
        return _rejected("approval contract changed after validation")
    approval = validated_approval
    if approval.decision != "approve":
        return _failure(
            Status.REVIEW_REVISE,
            "reviewer requested revision instead of authorizing integration",
        )
    try:
        expected_binding_hash = approval.binding.canonical_hash
    except ValueError:
        return _rejected("approval binding tuple is invalid")
    if approval.approval_binding_hash != expected_binding_hash:
        return _rejected("approval binding hash does not match its tuple")

    if not isinstance(snapshot, SourceSnapshot):
        return _rejected("source snapshot contract is invalid")
    if not isinstance(delta, WorkerDelta):
        return _rejected("worker delta contract is invalid")

    try:
        encoded_source_snapshot = encode_source_snapshot(snapshot)
        source_snapshot_hash = hashlib.sha256(encoded_source_snapshot).hexdigest()
    except ValueError:
        return _rejected("source snapshot evidence is invalid")
    if source_snapshot_hash != approval.binding.source_snapshot_hash:
        return _rejected("source snapshot hash changed after approval")

    try:
        worker_delta_hash = hashlib.sha256(encode_worker_delta(delta)).hexdigest()
    except (RepositoryError, ValueError):
        return _failure(
            Status.SCOPE_VIOLATION,
            "worker delta contains invalid raw path or record evidence",
        )
    if worker_delta_hash != approval.binding.worker_delta_hash:
        return _rejected("worker delta hash changed after approval")

    try:
        projected_patch = project_task_patch(snapshot, delta)
        projected_task_patch_hash = hashlib.sha256(
            encode_canonical_patch(projected_patch)
        ).hexdigest()
    except (RepositoryError, ValueError):
        return _failure(
            Status.SCOPE_VIOLATION,
            "projected task patch contains invalid evidence or an unsafe path",
        )
    if projected_task_patch_hash != approval.binding.projected_task_patch_hash:
        return _rejected("projected task patch hash changed after approval")

    try:
        destination = capture_destination(repo, snapshot.allowed_paths)
        destination_matches = (
            encode_source_snapshot(destination) == encoded_source_snapshot
        )
    except (OSError, RepositoryError, ValueError):
        return _failure(
            Status.DESTINATION_CHANGED,
            "destination could not be recaptured safely",
        )
    if not destination_matches:
        return _failure(
            Status.DESTINATION_CHANGED,
            "destination no longer matches the reviewed source snapshot",
        )

    allowed_paths = frozenset(snapshot.allowed_paths)
    if any(record.path not in allowed_paths for record in delta.records):
        return _failure(
            Status.SCOPE_VIOLATION,
            "worker delta contains a path outside the approved source allowlist",
        )
    if any(not _symlink_target_is_safe(record) for record in delta.records):
        return _failure(
            Status.SCOPE_VIOLATION,
            "worker delta contains a symlink target outside the repository",
        )

    try:
        repository_root = Path(os.fsdecode(repo)).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return _failure(
            Status.DESTINATION_CHANGED,
            "destination repository root could not be revalidated",
        )
    try:
        patch = _build_apply_patch(delta)
    except (OSError, ValueError):
        return _failure(
            Status.SCOPE_VIOLATION,
            "worker delta could not be represented as a safe Git patch",
        )
    expected_paths = _expected_patch_paths(delta)
    try:
        header_paths = _diff_header_paths(patch)
    except ValueError:
        return _failure(
            Status.SCOPE_VIOLATION,
            "worker patch contains malformed raw path headers",
        )
    if header_paths != tuple((path, path) for path in expected_paths):
        return _failure(
            Status.SCOPE_VIOLATION,
            "worker patch header paths do not exactly match approved records",
        )
    try:
        patch_paths = _inspect_patch_paths(repository_root, patch)
    except (OSError, ValueError):
        return _failure(
            Status.SCOPE_VIOLATION,
            "worker patch contains an invalid or unsafe path manifest",
        )
    if patch_paths != expected_paths or any(
        not _path_is_safe(repository_root, path, allowed_paths)
        for path in patch_paths
    ):
        return _failure(
            Status.SCOPE_VIOLATION,
            "worker patch paths do not exactly match the approved allowlist",
        )
    if patch and not _run_apply_check(repository_root, patch):
        return _failure(
            Status.TRANSPORT_ERROR,
            "worker delta is inconsistent with its unchanged source snapshot",
        )

    try:
        simulated_projected_hash, simulated_symlinks = _simulate_projected_patch_hash(
            repository_root,
            snapshot,
            delta,
            patch,
        )
    except _SimulationConflict:
        return _failure(
            Status.TRANSPORT_ERROR,
            "worker delta does not reproduce cleanly from its source snapshot",
        )
    except (OSError, RepositoryError, ValueError):
        return _failure(
            Status.TRANSPORT_ERROR,
            "worker delta could not be independently reconstructed safely",
        )
    if simulated_projected_hash != approval.binding.projected_task_patch_hash:
        return _failure(
            Status.TRANSPORT_ERROR,
            "projected task patch is not the result of the approved worker delta"
        )
    expected_symlink_targets = tuple(
        (record.path, record.content)
        for record in delta.records
        if record.new_mode == MODE_SYMLINK
    )
    if simulated_symlinks != expected_symlink_targets:
        return _failure(
            Status.SCOPE_VIOLATION,
            "worker symlink metadata does not match the simulated patch result",
        )
    if any(
        not _symlink_target_resolves_inside(repository_root, path, target)
        for path, target in simulated_symlinks
    ):
        return _failure(
            Status.SCOPE_VIOLATION,
            "simulated worker symlink resolves outside the destination repository",
        )

    try:
        destination_after_check = capture_destination(repo, snapshot.allowed_paths)
        destination_still_matches = (
            encode_source_snapshot(destination_after_check)
            == encoded_source_snapshot
        )
    except (OSError, RepositoryError, ValueError):
        return _failure(
            Status.DESTINATION_CHANGED,
            "destination could not be recaptured after the patch check",
        )
    if not destination_still_matches:
        return _failure(
            Status.DESTINATION_CHANGED,
            "destination changed after the patch check",
        )
    if any(
        not _symlink_target_resolves_inside(repository_root, path, target)
        for path, target in simulated_symlinks
    ):
        return _failure(
            Status.SCOPE_VIOLATION,
            "worker symlink target became unsafe before integration",
        )

    if not patch:
        try:
            final_noop_patch = project_task_patch(
                destination_after_check,
                WorkerDelta(records=()),
            )
            final_noop_hash = hashlib.sha256(
                encode_canonical_patch(final_noop_patch)
            ).hexdigest()
        except (RepositoryError, ValueError):
            return _failure(
                Status.TRANSPORT_ERROR,
                "verified no-op destination could not be projected safely",
            )
        if final_noop_hash != approval.binding.projected_task_patch_hash:
            return _rejected(
                "verified no-op destination does not match the approved projection"
            )
        return IntegrationResult(
            version=INTEGRATION_VERSION,
            status=Status.OK,
            applied=False,
            projected_task_patch_hash=final_noop_hash,
            message="approved worker delta contains no destination changes",
        )

    if not _run_apply(repository_root, patch):
        try:
            destination_after_failed_apply = capture_destination(
                repo,
                snapshot.allowed_paths,
            )
            unchanged_after_failed_apply = (
                encode_source_snapshot(destination_after_failed_apply)
                == encoded_source_snapshot
            )
        except (OSError, RepositoryError, ValueError):
            unchanged_after_failed_apply = False
        if unchanged_after_failed_apply:
            return _failure(
                Status.TRANSPORT_ERROR,
                "worker delta failed without changing the revalidated destination",
            )
        return IntegrationResult(
            version=INTEGRATION_VERSION,
            status=Status.TRANSPORT_ERROR,
            applied=True,
            message=(
                "the apply command failed after the destination may have been "
                "partially modified"
            ),
        )

    try:
        final_destination = capture_destination(repo, snapshot.allowed_paths)
        final_patch = project_task_patch(final_destination, WorkerDelta(records=()))
        final_projected_hash = hashlib.sha256(
            encode_canonical_patch(final_patch)
        ).hexdigest()
    except (OSError, RepositoryError, ValueError):
        return IntegrationResult(
            version=INTEGRATION_VERSION,
            status=Status.TRANSPORT_ERROR,
            applied=True,
            projected_task_patch_hash=simulated_projected_hash,
            message=(
                "worker delta was applied, but post-integration verification "
                "could not be completed"
            ),
        )
    if final_projected_hash != approval.binding.projected_task_patch_hash:
        return IntegrationResult(
            version=INTEGRATION_VERSION,
            status=Status.TRANSPORT_ERROR,
            applied=True,
            projected_task_patch_hash=final_projected_hash,
            message=(
                "worker delta was applied, but the destination changed during "
                "post-integration verification"
            ),
        )

    return IntegrationResult(
        version=INTEGRATION_VERSION,
        status=Status.OK,
        applied=True,
        projected_task_patch_hash=final_projected_hash,
        message="approved worker delta applied and verified",
    )


def integrate_sealed_delta_bundle(
    resources: InvocationResources,
    approval: Approval,
) -> ManagedIntegrationResult:
    """Consume one sealed bundle, integrate it, and clean its invocation once."""

    transaction: IntegrationResult
    try:
        try:
            bundle = read_sealed_delta_bundle(resources)
        except (BundleError, OSError, TypeError, ValueError):
            transaction = _failure(
                Status.TRANSPORT_ERROR,
                "sealed worker delta bundle is invalid or unavailable",
            )
        else:
            transaction = integrate_reviewed_delta(
                resources.repository_path,
                bundle.snapshot,
                bundle.delta,
                approval,
            )
    finally:
        try:
            cleanup = cleanup_invocation(resources)
        except Exception as exc:
            invocation_id = (
                resources.invocation_id
                if isinstance(resources, InvocationResources)
                else ""
            )
            cleanup = CleanupResult(
                status="rejected",
                invocation_id=invocation_id,
                message=f"invocation cleanup raised {type(exc).__name__}",
            )
    return ManagedIntegrationResult(transaction=transaction, cleanup=cleanup)


__all__ = (
    "Approval",
    "INTEGRATION_VERSION",
    "IntegrationResult",
    "ManagedIntegrationResult",
    "integrate_reviewed_delta",
    "integrate_sealed_delta_bundle",
)
