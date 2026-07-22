"""Sealed, invocation-bound persistence for captured worker deltas.

The bundle is data only.  Reading it neither invokes Git nor evaluates repository
content.  Every evidence value is reconstructed through the strict evidence data
classes before its hashes are accepted.  Source ``private`` records are retained
only in this invocation-internal bundle for destination checks; reviewer packets
must use ``CanonicalPatch``, whose private projection is aggregate-only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import platform
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - native Windows is rejected explicitly.
    fcntl = None  # type: ignore[assignment]

from . import resources as resources_module
from .evidence import (
    ApprovalBinding,
    EvidenceRecord,
    PrivateRecord,
    SourceSnapshot,
    WorkerDelta,
    encode_canonical_patch,
    encode_source_snapshot,
    encode_worker_delta,
)
from .repository import (
    RepositoryError,
    project_task_patch,
    replay_worker_delta_projection,
)
from .resources import InvocationResources


SCHEMA_VERSION = 1
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_DIR_FD_CAPABLE = all(
    function in os.supports_dir_fd for function in (os.open, os.stat, os.unlink)
)


class BundleError(ValueError):
    """A sealed bundle or its invocation binding is invalid."""


class BundleAlreadySealedError(BundleError):
    """The invocation already has an object at its one-shot bundle path."""


class BundleTooLargeError(BundleError):
    """The serialized bundle exceeds the bounded persistence envelope."""


class BundleUnsupportedPlatformError(BundleError):
    """Native sealed bundles are unavailable without the POSIX safety primitives."""


@dataclass(frozen=True)
class BundleCapability:
    """Fail-closed native-platform capability result for sealed persistence."""

    supported: bool
    system: str
    reason: str = ""


def probe_bundle_capability(
    *,
    system: str | None = None,
    os_name: str | None = None,
) -> BundleCapability:
    """Report native bundle support; Windows is supported only through WSL/Linux."""

    detected_system = platform.system() if system is None else system
    detected_os_name = os.name if os_name is None else os_name
    if detected_os_name != "posix" or detected_system not in {"Darwin", "Linux"}:
        return BundleCapability(
            supported=False,
            system=detected_system,
            reason="native Windows is unsupported; use WSL with the Linux sandbox",
        )
    required = (
        hasattr(os, "O_NOFOLLOW"),
        hasattr(os, "fchmod"),
        hasattr(os, "fsync"),
        hasattr(os, "geteuid"),
        _DIR_FD_CAPABLE,
        fcntl is not None,
    )
    if not all(required):
        return BundleCapability(
            supported=False,
            system=detected_system,
            reason="required POSIX no-follow, ownership, or dir-fd primitives are missing",
        )
    return BundleCapability(supported=True, system=detected_system)


def _require_supported_platform() -> None:
    capability = probe_bundle_capability()
    if not capability.supported:
        raise BundleUnsupportedPlatformError(capability.reason)


@dataclass(frozen=True)
class DeltaBundleMetadata:
    """Integrity and filesystem identity recorded for a sealed bundle."""

    schema_version: int
    invocation_id: str
    repository_path: Path
    repository_device: int
    repository_inode: int
    invocation_root: Path
    invocation_root_device: int
    invocation_root_inode: int
    bundle_path: Path
    source_snapshot_hash: str
    worker_delta_hash: str
    projected_task_patch_hash: str
    bundle_sha256: str
    bundle_device: int
    bundle_inode: int
    bundle_size: int


@dataclass(frozen=True)
class SealedDeltaBundle:
    """A validated source snapshot and worker delta loaded from private storage."""

    metadata: DeltaBundleMetadata
    snapshot: SourceSnapshot
    delta: WorkerDelta
    authority_mode: str = "lite"
    gates: tuple[SealedGateEvidence, ...] = ()


@dataclass(frozen=True)
class SealedGateEvidence:
    """Trusted gate result persisted with the exact worker delta."""

    argv: tuple[str, ...]
    cwd: str
    status: str
    exit_code: int
    stdout_hash: str
    stderr_hash: str
    duration_milliseconds: int

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if (
            not argv
            or len(argv) > 128
            or not all(
                isinstance(member, str)
                and member
                and "\0" not in member
                and len(member) <= 16_384
                for member in argv
            )
        ):
            raise ValueError("sealed gate argv is invalid")
        if not isinstance(self.cwd, str) or not self.cwd or "\0" in self.cwd:
            raise ValueError("sealed gate cwd is invalid")
        cwd = Path(self.cwd)
        if cwd.is_absolute() or ".." in cwd.parts:
            raise ValueError("sealed gate cwd must be repository-relative")
        if self.status != "ok" or type(self.exit_code) is not int or self.exit_code != 0:
            raise ValueError("only successful trusted gates may be sealed")
        for field_name in ("stdout_hash", "stderr_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lower-case SHA-256 digest")
        if (
            type(self.duration_milliseconds) is not int
            or not 0 <= self.duration_milliseconds <= 3_600_000
        ):
            raise ValueError("sealed gate duration is invalid")
        object.__setattr__(self, "argv", argv)


@dataclass(frozen=True)
class FinalReviewReceipt:
    """One approval bound to an invocation, packet, bundle, and reviewer proof."""

    schema_version: int
    authority_mode: str
    invocation_id: str
    bundle_sha256: str
    review_packet_sha256: str
    source_snapshot_hash: str
    worker_delta_hash: str
    projected_task_patch_hash: str
    approval_binding_hash: str
    decision: str
    reviewer_route_id: str
    reviewer_fingerprint: str
    fingerprint_evidence_source: str
    reviewer_read_only_enforced: bool
    main_fingerprint: str
    message: str
    requested_changes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported final review receipt version")
        for field_name in (
            "bundle_sha256",
            "review_packet_sha256",
            "source_snapshot_hash",
            "worker_delta_hash",
            "projected_task_patch_hash",
            "approval_binding_hash",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lower-case SHA-256 digest")
        binding = ApprovalBinding(
            source_snapshot_hash=self.source_snapshot_hash,
            worker_delta_hash=self.worker_delta_hash,
            projected_task_patch_hash=self.projected_task_patch_hash,
        )
        if binding.canonical_hash != self.approval_binding_hash:
            raise ValueError("final review receipt binding hash is inconsistent")
        if self.authority_mode not in {"lite", "max"}:
            raise ValueError("final review receipt authority mode is invalid")
        if self.decision != "approve" or self.requested_changes:
            raise ValueError("only an approval without requested changes can be sealed")
        for field_name in (
            "invocation_id",
            "reviewer_route_id",
            "fingerprint_evidence_source",
            "message",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise ValueError(f"{field_name} must be non-empty safe text")
        for field_name in ("reviewer_fingerprint", "main_fingerprint"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or value != value.lower()
                or len(value.split(":")) != 3
                or any(not part for part in value.split(":"))
            ):
                raise ValueError(f"{field_name} must be a canonical fingerprint")
        if self.authority_mode == "max":
            if self.reviewer_fingerprint == self.main_fingerprint:
                raise ValueError("Max reviewer fingerprint must differ from the main loop")
            if self.reviewer_read_only_enforced is not True:
                raise ValueError("Max approval requires enforced read-only review")
        else:
            if (
                self.reviewer_route_id != "inline-main-loop"
                or self.reviewer_fingerprint != self.main_fingerprint
                or self.fingerprint_evidence_source != "host-metadata"
                or self.reviewer_read_only_enforced is not False
            ):
                raise ValueError("Lite approval must remain inline in the main loop")
        changes = tuple(self.requested_changes)
        if not all(isinstance(change, str) and change.strip() for change in changes):
            raise ValueError("requested_changes must contain non-empty text")
        object.__setattr__(self, "requested_changes", changes)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BundleError("bundle could not be encoded as canonical JSON") from exc


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise BundleError(f"bundle JSON contains duplicate key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise BundleError(f"bundle JSON contains a non-finite number: {value}")


def _decode_json(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("ascii")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except BundleError:
        raise
    except (
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise BundleError("sealed bundle is not valid strict ASCII JSON") from exc
    if type(value) is not dict:
        raise BundleError("sealed bundle must be a JSON object")
    if _canonical_json(value) != raw:
        raise BundleError("sealed bundle JSON is not canonical")
    return value


def _expect_object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise BundleError(f"{label} must be a JSON object")
    return value


def _expect_keys(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    mapping = _expect_object(value, label)
    actual = frozenset(mapping)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        details: list[str] = []
        if unknown:
            details.append(f"unknown keys {unknown!r}")
        if missing:
            details.append(f"missing keys {missing!r}")
        raise BundleError(f"{label} has invalid keys: {', '.join(details)}")
    return mapping


def _expect_list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise BundleError(f"{label} must be a JSON array")
    return value


def _expect_string(value: object, label: str) -> str:
    if type(value) is not str:
        raise BundleError(f"{label} must be a JSON string")
    return value


def _expect_integer(
    value: object,
    label: str,
    *,
    minimum: int = 0,
) -> int:
    if type(value) is not int or value < minimum:
        raise BundleError(f"{label} must be an integer of at least {minimum}")
    return value


def _expect_sha256(value: object, label: str) -> str:
    digest = _expect_string(value, label)
    if _SHA256_RE.fullmatch(digest) is None:
        raise BundleError(f"{label} must be a lower-case SHA-256 digest")
    return digest


def _encode_bytes(value: bytes) -> str:
    if type(value) is not bytes:
        raise BundleError("bundle raw fields must be bytes")
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: object, label: str) -> bytes:
    encoded = _expect_string(value, label)
    try:
        encoded_bytes = encoded.encode("ascii")
        decoded = base64.b64decode(encoded_bytes, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise BundleError(f"{label} is not canonical base64") from exc
    if base64.b64encode(decoded) != encoded_bytes:
        raise BundleError(f"{label} is not canonical base64")
    return decoded


def _encode_record(record: EvidenceRecord) -> dict[str, object]:
    return {
        "canonical_diff_b64": _encode_bytes(record.canonical_diff),
        "content_b64": (
            None if record.content is None else _encode_bytes(record.content)
        ),
        "new_mode": int(record.new_mode),
        "old_mode": int(record.old_mode),
        "path_b64": _encode_bytes(record.path),
        "status": int(record.status),
        "tag": int(record.tag),
    }


_RECORD_KEYS = frozenset(
    {
        "canonical_diff_b64",
        "content_b64",
        "new_mode",
        "old_mode",
        "path_b64",
        "status",
        "tag",
    }
)


def _decode_record(value: object, label: str) -> EvidenceRecord:
    record = _expect_keys(value, _RECORD_KEYS, label)
    content_value = record["content_b64"]
    if content_value is None:
        content = None
    else:
        content = _decode_bytes(content_value, f"{label}.content_b64")
    try:
        return EvidenceRecord(
            tag=_expect_integer(record["tag"], f"{label}.tag"),
            path=_decode_bytes(record["path_b64"], f"{label}.path_b64"),
            status=_expect_integer(record["status"], f"{label}.status"),
            old_mode=_expect_integer(record["old_mode"], f"{label}.old_mode"),
            new_mode=_expect_integer(record["new_mode"], f"{label}.new_mode"),
            canonical_diff=_decode_bytes(
                record["canonical_diff_b64"],
                f"{label}.canonical_diff_b64",
            ),
            content=content,
        )
    except ValueError as exc:
        raise BundleError(f"{label} is not valid evidence: {exc}") from exc


def _encode_private_record(record: PrivateRecord) -> dict[str, object]:
    return {
        "digest": record.digest,
        "digest_kind": int(record.digest_kind),
        "mode": int(record.mode),
        "path_b64": _encode_bytes(record.path),
        "size": record.size,
        "status": int(record.status),
    }


_PRIVATE_RECORD_KEYS = frozenset(
    {"digest", "digest_kind", "mode", "path_b64", "size", "status"}
)


def _decode_private_record(value: object, label: str) -> PrivateRecord:
    record = _expect_keys(value, _PRIVATE_RECORD_KEYS, label)
    try:
        return PrivateRecord(
            digest_kind=_expect_integer(
                record["digest_kind"], f"{label}.digest_kind"
            ),
            path=_decode_bytes(record["path_b64"], f"{label}.path_b64"),
            status=_expect_integer(record["status"], f"{label}.status"),
            mode=_expect_integer(record["mode"], f"{label}.mode"),
            size=_expect_integer(record["size"], f"{label}.size"),
            digest=_expect_sha256(record["digest"], f"{label}.digest"),
        )
    except ValueError as exc:
        raise BundleError(f"{label} is not valid private evidence: {exc}") from exc


def _encode_record_list(records: tuple[EvidenceRecord, ...]) -> list[object]:
    return [_encode_record(record) for record in records]


def _decode_record_list(value: object, label: str) -> tuple[EvidenceRecord, ...]:
    return tuple(
        _decode_record(record, f"{label}[{index}]")
        for index, record in enumerate(_expect_list(value, label))
    )


_SNAPSHOT_KEYS = frozenset(
    {
        "allowed_paths_b64",
        "baseline_oid_b64",
        "private",
        "staged",
        "unstaged",
        "untracked",
    }
)


def _encode_snapshot(snapshot: SourceSnapshot) -> dict[str, object]:
    if not isinstance(snapshot, SourceSnapshot):
        raise BundleError("snapshot must be SourceSnapshot")
    return {
        "allowed_paths_b64": [
            _encode_bytes(path) for path in snapshot.allowed_paths
        ],
        "baseline_oid_b64": _encode_bytes(snapshot.baseline_oid),
        "private": [_encode_private_record(record) for record in snapshot.private],
        "staged": _encode_record_list(snapshot.staged),
        "unstaged": _encode_record_list(snapshot.unstaged),
        "untracked": _encode_record_list(snapshot.untracked),
    }


def _decode_snapshot(value: object, label: str) -> SourceSnapshot:
    snapshot = _expect_keys(value, _SNAPSHOT_KEYS, label)
    allowed_values = _expect_list(
        snapshot["allowed_paths_b64"], f"{label}.allowed_paths_b64"
    )
    private_values = _expect_list(snapshot["private"], f"{label}.private")
    try:
        return SourceSnapshot(
            baseline_oid=_decode_bytes(
                snapshot["baseline_oid_b64"], f"{label}.baseline_oid_b64"
            ),
            allowed_paths=tuple(
                _decode_bytes(item, f"{label}.allowed_paths_b64[{index}]")
                for index, item in enumerate(allowed_values)
            ),
            staged=_decode_record_list(snapshot["staged"], f"{label}.staged"),
            unstaged=_decode_record_list(
                snapshot["unstaged"], f"{label}.unstaged"
            ),
            untracked=_decode_record_list(
                snapshot["untracked"], f"{label}.untracked"
            ),
            private=tuple(
                _decode_private_record(item, f"{label}.private[{index}]")
                for index, item in enumerate(private_values)
            ),
        )
    except ValueError as exc:
        raise BundleError(f"{label} is not a valid source snapshot: {exc}") from exc


_DELTA_KEYS = frozenset({"projected_snapshot", "records"})


def _encode_delta(delta: WorkerDelta) -> dict[str, object]:
    if not isinstance(delta, WorkerDelta):
        raise BundleError("delta must be WorkerDelta")
    return {
        "projected_snapshot": (
            None
            if delta.projected_snapshot is None
            else _encode_snapshot(delta.projected_snapshot)
        ),
        "records": _encode_record_list(delta.records),
    }


def _decode_delta(value: object, label: str) -> WorkerDelta:
    delta = _expect_keys(value, _DELTA_KEYS, label)
    projected_value = delta["projected_snapshot"]
    projected = (
        None
        if projected_value is None
        else _decode_snapshot(projected_value, f"{label}.projected_snapshot")
    )
    try:
        return WorkerDelta(
            records=_decode_record_list(delta["records"], f"{label}.records"),
            projected_snapshot=projected,
        )
    except ValueError as exc:
        raise BundleError(f"{label} is not a valid worker delta: {exc}") from exc


def _canonical_evidence(
    snapshot: SourceSnapshot,
    delta: WorkerDelta,
) -> tuple[SourceSnapshot, WorkerDelta]:
    """Clone through the strict bundle representation to reject mutated objects."""

    try:
        canonical_snapshot = _decode_snapshot(
            _encode_snapshot(snapshot), "source_snapshot"
        )
        canonical_delta = _decode_delta(_encode_delta(delta), "worker_delta")
    except BundleError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise BundleError("bundle evidence contract is invalid") from exc
    return canonical_snapshot, canonical_delta


def _validate_delta_projection_contract(
    snapshot: SourceSnapshot,
    delta: WorkerDelta,
) -> None:
    allowed = frozenset(snapshot.allowed_paths)
    if any(record.path not in allowed for record in delta.records):
        raise BundleError("worker delta contains a path outside the source allowlist")
    projected = delta.projected_snapshot
    if delta.records and projected is None:
        raise BundleError("non-empty worker delta requires a projected snapshot")
    if projected is None:
        return
    if projected.baseline_oid != snapshot.baseline_oid:
        raise BundleError("projected snapshot changed the source baseline")
    if projected.allowed_paths != snapshot.allowed_paths:
        raise BundleError("projected snapshot changed the source allowlist")
    if projected.private:
        raise BundleError("projected snapshot must not carry private records")
    if projected.staged != snapshot.staged:
        raise BundleError("projected snapshot changed the staged source state")


def _evidence_hashes(
    snapshot: SourceSnapshot,
    delta: WorkerDelta,
) -> tuple[str, str, str]:
    _validate_delta_projection_contract(snapshot, delta)
    try:
        source_hash = hashlib.sha256(encode_source_snapshot(snapshot)).hexdigest()
        delta_hash = hashlib.sha256(encode_worker_delta(delta)).hexdigest()
        projected_hash = hashlib.sha256(
            encode_canonical_patch(project_task_patch(snapshot, delta))
        ).hexdigest()
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise BundleError("bundle projected evidence is invalid") from exc
    return source_hash, delta_hash, projected_hash


_BINDING_KEYS = frozenset(
    {
        "delta_bundle_path_b64",
        "invocation_id",
        "invocation_root_device",
        "invocation_root_inode",
        "invocation_root_path_b64",
        "repository_device",
        "repository_inode",
        "repository_path_b64",
    }
)
_HASH_KEYS = frozenset(
    {
        "projected_task_patch_sha256",
        "source_snapshot_sha256",
        "worker_delta_sha256",
    }
)
_ENVELOPE_KEYS = frozenset(
    {
        "authority_mode",
        "binding",
        "gates",
        "hashes",
        "schema_version",
        "source_snapshot",
        "worker_delta",
    }
)
_GATE_KEYS = frozenset(
    {
        "argv",
        "cwd",
        "duration_milliseconds",
        "exit_code",
        "status",
        "stderr_hash",
        "stdout_hash",
    }
)


def _canonical_gates(
    gates: tuple[SealedGateEvidence, ...] | list[SealedGateEvidence],
) -> tuple[SealedGateEvidence, ...]:
    if not isinstance(gates, (tuple, list)) or not all(
        isinstance(gate, SealedGateEvidence) for gate in gates
    ):
        raise BundleError("bundle gates must contain SealedGateEvidence values")
    if len(gates) > 128:
        raise BundleError("bundle contains too many gates")
    try:
        return tuple(
            SealedGateEvidence(
                argv=gate.argv,
                cwd=gate.cwd,
                status=gate.status,
                exit_code=gate.exit_code,
                stdout_hash=gate.stdout_hash,
                stderr_hash=gate.stderr_hash,
                duration_milliseconds=gate.duration_milliseconds,
            )
            for gate in gates
        )
    except (TypeError, ValueError) as exc:
        raise BundleError(f"bundle gate evidence is invalid: {exc}") from exc


def _encode_gate(gate: SealedGateEvidence) -> dict[str, object]:
    return {
        "argv": list(gate.argv),
        "cwd": gate.cwd,
        "duration_milliseconds": gate.duration_milliseconds,
        "exit_code": gate.exit_code,
        "status": gate.status,
        "stderr_hash": gate.stderr_hash,
        "stdout_hash": gate.stdout_hash,
    }


def _decode_gate(value: object, label: str) -> SealedGateEvidence:
    gate = _expect_keys(value, _GATE_KEYS, label)
    argv_values = _expect_list(gate["argv"], f"{label}.argv")
    if not all(type(member) is str for member in argv_values):
        raise BundleError(f"{label}.argv must contain only strings")
    try:
        return SealedGateEvidence(
            argv=tuple(argv_values),  # type: ignore[arg-type]
            cwd=_expect_string(gate["cwd"], f"{label}.cwd"),
            status=_expect_string(gate["status"], f"{label}.status"),
            exit_code=_expect_integer(gate["exit_code"], f"{label}.exit_code"),
            stdout_hash=_expect_sha256(
                gate["stdout_hash"], f"{label}.stdout_hash"
            ),
            stderr_hash=_expect_sha256(
                gate["stderr_hash"], f"{label}.stderr_hash"
            ),
            duration_milliseconds=_expect_integer(
                gate["duration_milliseconds"],
                f"{label}.duration_milliseconds",
            ),
        )
    except ValueError as exc:
        raise BundleError(f"{label} is not valid green gate evidence: {exc}") from exc


def _decode_gates(value: object) -> tuple[SealedGateEvidence, ...]:
    values = _expect_list(value, "gates")
    if len(values) > 128:
        raise BundleError("bundle contains too many gates")
    return tuple(
        _decode_gate(gate, f"gates[{index}]")
        for index, gate in enumerate(values)
    )


def _encode_binding(resources: InvocationResources) -> dict[str, object]:
    return {
        "delta_bundle_path_b64": _encode_bytes(
            os.fsencode(resources.delta_bundle_path)
        ),
        "invocation_id": resources.invocation_id,
        "invocation_root_device": resources.invocation_root_device,
        "invocation_root_inode": resources.invocation_root_inode,
        "invocation_root_path_b64": _encode_bytes(
            os.fsencode(resources.invocation_root)
        ),
        "repository_device": resources.repository_device,
        "repository_inode": resources.repository_inode,
        "repository_path_b64": _encode_bytes(os.fsencode(resources.repository_path)),
    }


def _validate_binding(value: object, resources: InvocationResources) -> None:
    binding = _expect_keys(value, _BINDING_KEYS, "binding")
    expected_bytes = {
        "repository_path_b64": os.fsencode(resources.repository_path),
        "invocation_root_path_b64": os.fsencode(resources.invocation_root),
        "delta_bundle_path_b64": os.fsencode(resources.delta_bundle_path),
    }
    for field_name, expected in expected_bytes.items():
        if _decode_bytes(binding[field_name], f"binding.{field_name}") != expected:
            raise BundleError(f"bundle binding does not match {field_name}")
    expected_values: dict[str, object] = {
        "invocation_id": resources.invocation_id,
        "repository_device": resources.repository_device,
        "repository_inode": resources.repository_inode,
        "invocation_root_device": resources.invocation_root_device,
        "invocation_root_inode": resources.invocation_root_inode,
    }
    for field_name, expected in expected_values.items():
        actual = binding[field_name]
        if field_name != "invocation_id":
            actual = _expect_integer(actual, f"binding.{field_name}")
        elif type(actual) is not str:
            raise BundleError("binding.invocation_id must be a string")
        if actual != expected:
            raise BundleError(f"bundle binding does not match {field_name}")


def _build_envelope(
    resources: InvocationResources,
    snapshot: SourceSnapshot,
    delta: WorkerDelta,
    gates: tuple[SealedGateEvidence, ...] = (),
    authority_mode: str = "lite",
) -> tuple[dict[str, object], tuple[str, str, str]]:
    hashes = _evidence_hashes(snapshot, delta)
    canonical_gates = _canonical_gates(gates)
    if authority_mode not in {"lite", "max"}:
        raise BundleError("bundle authority_mode must be lite or max")
    envelope: dict[str, object] = {
        "authority_mode": authority_mode,
        "binding": _encode_binding(resources),
        "gates": [_encode_gate(gate) for gate in canonical_gates],
        "hashes": {
            "projected_task_patch_sha256": hashes[2],
            "source_snapshot_sha256": hashes[0],
            "worker_delta_sha256": hashes[1],
        },
        "schema_version": SCHEMA_VERSION,
        "source_snapshot": _encode_snapshot(snapshot),
        "worker_delta": _encode_delta(delta),
    }
    return envelope, hashes


def _validate_exact_layout(resources: InvocationResources, anchor: object) -> None:
    """Validate the complete inode-bound private invocation layout."""

    resources_module._validate_active_resources(resources, anchor)


@contextmanager
def _validated_root(resources: InvocationResources) -> Iterator[object]:
    if not isinstance(resources, InvocationResources):
        raise BundleError("resources must be InvocationResources")
    parent_fd: int | None = None
    anchor: object | None = None
    try:
        resources_module._validate_base_identity(resources)
        parent_fd = resources_module._open_parent_anchor(resources)
        anchor = resources_module._open_root_anchor(
            resources,
            parent_fd,
            allow_missing=False,
        )
        if anchor is None:
            raise BundleError("invocation root is missing")
        _validate_exact_layout(resources, anchor)
        yield anchor
    except BundleError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise BundleError(f"invalid invocation resources: {exc}") from exc
    finally:
        if anchor is not None:
            resources_module._close_root_anchor(anchor)
        if parent_fd is not None:
            os.close(parent_fd)


def _current_uid() -> int:
    getter = getattr(os, "geteuid", None)
    if getter is None:
        raise BundleError("sealed bundle ownership checks are unavailable")
    return getter()


def _require_nofollow() -> int:
    flag = getattr(os, "O_NOFOLLOW", None)
    if flag is None:
        raise BundleError("secure no-follow file access is unavailable")
    return flag


def _validate_bundle_stat(metadata: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise BundleError(f"{label} must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) != 0o400:
        raise BundleError(f"{label} mode must be 0400")
    if metadata.st_nlink != 1:
        raise BundleError(f"{label} must have exactly one hard link")
    if metadata.st_uid != _current_uid():
        raise BundleError(f"{label} owner does not match the current user")
    if metadata.st_size > MAX_BUNDLE_BYTES:
        raise BundleTooLargeError(
            f"{label} exceeds the {MAX_BUNDLE_BYTES}-byte size limit"
        )


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _same_file_state(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_inode(first, second)
        and first.st_mode == second.st_mode
        and first.st_nlink == second.st_nlink
        and first.st_uid == second.st_uid
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("sealed bundle write made no progress")
        written += count


def _unlink_created_file(
    root_fd: int,
    name: str,
    created: os.stat_result | None,
) -> None:
    if created is None:
        return
    try:
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _same_inode(created, current):
        try:
            os.unlink(name, dir_fd=root_fd)
        except FileNotFoundError:
            pass


_RECEIPT_KEYS = frozenset(
    {
        "active_manifest",
        "binding_sha256",
        "envelope_sha256",
        "invocation_id",
        "schema_version",
        "state",
    }
)
_MAX_RECEIPT_BYTES = 1024 * 1024


def _seal_receipt_path(resources: InvocationResources) -> Path:
    return resources_module._seal_receipt_path(resources)


def _seal_receipt_final_path(resources: InvocationResources) -> Path:
    return resources_module._seal_receipt_final_path(resources)


def _receipt_payload(
    resources: InvocationResources,
    raw: bytes,
    *,
    state: str,
) -> dict[str, object]:
    return {
        "active_manifest": resources_module._expected_manifest(resources),
        "binding_sha256": hashlib.sha256(
            _canonical_json(_encode_binding(resources))
        ).hexdigest(),
        "envelope_sha256": hashlib.sha256(raw).hexdigest(),
        "invocation_id": resources.invocation_id,
        "schema_version": SCHEMA_VERSION,
        "state": state,
    }


def _validate_receipt_payload(
    value: object,
    resources: InvocationResources,
    *,
    expected_state: str | None,
) -> dict[str, object]:
    receipt = _expect_keys(value, _RECEIPT_KEYS, "seal receipt")
    version = _expect_integer(receipt["schema_version"], "seal receipt version")
    if version != SCHEMA_VERSION:
        raise BundleError("seal receipt has an unsupported schema version")
    if receipt["invocation_id"] != resources.invocation_id:
        raise BundleError("seal receipt is bound to a different invocation")
    if receipt["active_manifest"] != resources_module._expected_manifest(resources):
        raise BundleError("seal receipt does not match the invocation manifest")
    state = _expect_string(receipt["state"], "seal receipt state")
    if state not in {"sealing", "sealed"}:
        raise BundleError("seal receipt has an unsupported state")
    if expected_state is not None and state != expected_state:
        raise BundleError(f"seal receipt is not in {expected_state!r} state")
    _expect_sha256(receipt["binding_sha256"], "seal receipt binding hash")
    _expect_sha256(receipt["envelope_sha256"], "seal receipt envelope hash")
    return receipt


def _validate_receipt_stat(
    metadata: os.stat_result,
    *,
    allowed_modes: frozenset[int],
) -> None:
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise BundleError("seal receipt must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) not in allowed_modes:
        raise BundleError("seal receipt mode is invalid")
    if metadata.st_nlink != 1:
        raise BundleError("seal receipt must have exactly one hard link")
    if metadata.st_uid != _current_uid():
        raise BundleError("seal receipt owner does not match the current user")
    if metadata.st_size > _MAX_RECEIPT_BYTES:
        raise BundleError("seal receipt exceeds its size limit")


def _read_receipt_descriptor(
    descriptor: int,
    *,
    allowed_modes: frozenset[int],
) -> tuple[dict[str, object], os.stat_result]:
    metadata = os.fstat(descriptor)
    _validate_receipt_stat(metadata, allowed_modes=allowed_modes)
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = _read_bounded(descriptor, metadata.st_size)
    after = os.fstat(descriptor)
    if not _same_file_state(metadata, after):
        raise BundleError("seal receipt changed while being read")
    return _decode_json(raw), after


def _open_receipt_at(
    parent_fd: int,
    name: str,
    *,
    allowed_modes: frozenset[int],
) -> tuple[int, dict[str, object], os.stat_result]:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _validate_receipt_stat(before, allowed_modes=allowed_modes)
    descriptor = os.open(
        name,
        os.O_RDONLY | _require_nofollow() | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        value, opened = _read_receipt_descriptor(
            descriptor,
            allowed_modes=allowed_modes,
        )
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_file_state(before, opened) or not _same_file_state(
            opened, current
        ):
            raise BundleError("seal receipt inode changed while being opened")
        return descriptor, value, opened
    except BaseException:
        os.close(descriptor)
        raise


def _create_receipt_at(
    parent_fd: int,
    name: str,
    raw: bytes,
    *,
    final_mode: int,
) -> tuple[int, os.stat_result]:
    descriptor = os.open(
        name,
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | _require_nofollow()
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        created = os.fstat(descriptor)
        if (
            not stat.S_ISREG(created.st_mode)
            or created.st_nlink != 1
            or created.st_uid != _current_uid()
        ):
            raise BundleError("new seal receipt is not a private regular file")
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        os.fchmod(descriptor, final_mode)
        os.fsync(descriptor)
        final = os.fstat(descriptor)
        _validate_receipt_stat(final, allowed_modes=frozenset({final_mode}))
        os.fsync(parent_fd)
        return descriptor, final
    except BaseException:
        try:
            metadata = os.fstat(descriptor)
            _unlink_created_file(parent_fd, name, metadata)
        finally:
            os.close(descriptor)
        raise


def _remove_recoverable_file_at(
    parent_fd: int,
    name: str,
    *,
    label: str,
) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        or metadata.st_nlink != 1
        or metadata.st_uid != _current_uid()
    ):
        raise BundleError(f"stale {label} is not safely recoverable")
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not _same_file_state(metadata, current):
        raise BundleError(f"stale {label} changed before recovery")
    os.unlink(name, dir_fd=parent_fd)


def _acquire_seal_claim(
    anchor: object,
    resources: InvocationResources,
    raw: bytes,
) -> tuple[int, os.stat_result]:
    assert fcntl is not None
    parent_fd = anchor.parent_fd  # type: ignore[attr-defined]
    root_fd = anchor.root_fd  # type: ignore[attr-defined]
    name = _seal_receipt_path(resources).name
    final_name = _seal_receipt_final_path(resources).name
    expected_claim = _receipt_payload(resources, raw, state="sealing")
    expected_claim_raw = _canonical_json(expected_claim)
    try:
        descriptor, value, metadata = _open_receipt_at(
            parent_fd,
            name,
            allowed_modes=frozenset({0o400, 0o600}),
        )
    except FileNotFoundError:
        descriptor = -1
    if descriptor >= 0:
        mode = stat.S_IMODE(metadata.st_mode)
        if mode == 0o400:
            try:
                _validate_receipt_payload(
                    value,
                    resources,
                    expected_state="sealed",
                )
            finally:
                os.close(descriptor)
            raise BundleAlreadySealedError(
                "invocation was already sealed; refusing to seal it again"
            )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BundleAlreadySealedError(
                    "invocation sealing is already in progress"
                ) from exc
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_file_state(metadata, current):
                raise BundleError("seal claim changed before recovery")
            claim = _validate_receipt_payload(
                value,
                resources,
                expected_state="sealing",
            )
            if claim != expected_claim:
                raise BundleAlreadySealedError(
                    "invocation is already sealing different evidence"
                )
            _remove_recoverable_file_at(
                root_fd,
                resources.delta_bundle_path.name,
                label="delta bundle",
            )
            _remove_recoverable_file_at(
                parent_fd,
                final_name,
                label="final seal receipt",
            )
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(descriptor)

    claim_fd, claim_metadata = _create_receipt_at(
        parent_fd,
        name,
        expected_claim_raw,
        final_mode=0o600,
    )
    try:
        fcntl.flock(claim_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        try:
            _unlink_created_file(parent_fd, name, claim_metadata)
            os.fsync(parent_fd)
        finally:
            os.close(claim_fd)
        raise BundleError("new seal claim could not be locked") from exc
    return claim_fd, claim_metadata


def _rollback_seal_claim(
    anchor: object,
    resources: InvocationResources,
    claim_metadata: os.stat_result | None,
) -> None:
    parent_fd = anchor.parent_fd  # type: ignore[attr-defined]
    _remove_recoverable_file_at(
        parent_fd,
        _seal_receipt_final_path(resources).name,
        label="final seal receipt",
    )
    if claim_metadata is not None:
        name = _seal_receipt_path(resources).name
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if _same_inode(claim_metadata, current):
            os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _finalize_seal_receipt(
    anchor: object,
    resources: InvocationResources,
    raw: bytes,
    claim_metadata: os.stat_result,
) -> None:
    parent_fd = anchor.parent_fd  # type: ignore[attr-defined]
    name = _seal_receipt_path(resources).name
    final_name = _seal_receipt_final_path(resources).name
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not _same_file_state(claim_metadata, current):
        raise BundleError("seal claim changed before finalization")
    final_raw = _canonical_json(_receipt_payload(resources, raw, state="sealed"))
    final_fd: int | None = None
    try:
        final_fd, _ = _create_receipt_at(
            parent_fd,
            final_name,
            final_raw,
            final_mode=0o400,
        )
        os.close(final_fd)
        final_fd = None
        os.replace(
            final_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        try:
            os.fsync(parent_fd)
        except OSError:
            # The atomic replacement is already the irreversible one-shot trust
            # anchor.  Revalidate it and report a sealed result instead of
            # deleting the bundle while leaving a final receipt behind.
            _validate_sealed_receipt(parent_fd, resources, raw)
    finally:
        if final_fd is not None:
            os.close(final_fd)


def _validate_sealed_receipt(
    parent_fd: int,
    resources: InvocationResources,
    raw: bytes,
) -> None:
    try:
        descriptor, value, _ = _open_receipt_at(
            parent_fd,
            _seal_receipt_path(resources).name,
            allowed_modes=frozenset({0o400}),
        )
    except FileNotFoundError as exc:
        raise BundleError("sealed bundle has no external trust receipt") from exc
    try:
        receipt = _validate_receipt_payload(
            value,
            resources,
            expected_state="sealed",
        )
        expected = _receipt_payload(resources, raw, state="sealed")
        if receipt != expected:
            raise BundleError("external seal receipt does not bind this bundle")
    finally:
        os.close(descriptor)


def _metadata(
    resources: InvocationResources,
    hashes: tuple[str, str, str],
    raw: bytes,
    file_metadata: os.stat_result,
) -> DeltaBundleMetadata:
    return DeltaBundleMetadata(
        schema_version=SCHEMA_VERSION,
        invocation_id=resources.invocation_id,
        repository_path=resources.repository_path,
        repository_device=resources.repository_device,
        repository_inode=resources.repository_inode,
        invocation_root=resources.invocation_root,
        invocation_root_device=resources.invocation_root_device,
        invocation_root_inode=resources.invocation_root_inode,
        bundle_path=resources.delta_bundle_path,
        source_snapshot_hash=hashes[0],
        worker_delta_hash=hashes[1],
        projected_task_patch_hash=hashes[2],
        bundle_sha256=hashlib.sha256(raw).hexdigest(),
        bundle_device=file_metadata.st_dev,
        bundle_inode=file_metadata.st_ino,
        bundle_size=file_metadata.st_size,
    )


def seal_delta_bundle(
    resources: InvocationResources,
    snapshot: SourceSnapshot,
    delta: WorkerDelta,
    *,
    gates: tuple[SealedGateEvidence, ...] | list[SealedGateEvidence] = (),
    authority_mode: str = "lite",
) -> DeltaBundleMetadata:
    """Persist a worker result exactly once as a private immutable JSON bundle."""

    _require_supported_platform()
    canonical_snapshot, canonical_delta = _canonical_evidence(snapshot, delta)
    canonical_gates = _canonical_gates(gates)
    if authority_mode not in {"lite", "max"}:
        raise BundleError("bundle authority_mode must be lite or max")
    if not isinstance(resources, InvocationResources):
        raise BundleError("resources must be InvocationResources")
    envelope, hashes = _build_envelope(
        resources,
        canonical_snapshot,
        canonical_delta,
        canonical_gates,
        authority_mode,
    )
    raw = _canonical_json(envelope)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise BundleTooLargeError(
            f"sealed bundle exceeds the {MAX_BUNDLE_BYTES}-byte size limit"
        )

    name = resources.delta_bundle_path.name
    with _validated_root(resources) as anchor:
        root_fd = anchor.root_fd  # type: ignore[attr-defined]
        claim_fd: int | None = None
        claim_metadata: os.stat_result | None = None
        descriptor: int | None = None
        created: os.stat_result | None = None
        finalized = False
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _require_nofollow()
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            try:
                replay_worker_delta_projection(
                    resources.repository_path,
                    canonical_snapshot,
                    canonical_delta,
                )
            except (OSError, RepositoryError, ValueError) as exc:
                raise BundleError(
                    "worker delta replay did not reproduce its projected snapshot"
                ) from exc
            claim_fd, claim_metadata = _acquire_seal_claim(
                anchor,
                resources,
                raw,
            )
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=root_fd)
            except FileExistsError as exc:
                raise BundleAlreadySealedError(
                    "sealed delta bundle already exists; refusing to overwrite it"
                ) from exc
            created = os.fstat(descriptor)
            if (
                not stat.S_ISREG(created.st_mode)
                or created.st_nlink != 1
                or created.st_uid != _current_uid()
            ):
                raise BundleError("new sealed bundle is not a private regular file")
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, raw)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            final_metadata = os.fstat(descriptor)
            _validate_bundle_stat(final_metadata, "sealed bundle")
            if not _same_inode(created, final_metadata):
                raise BundleError("sealed bundle changed inode while being written")
            os.fsync(root_fd)
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not _same_file_state(final_metadata, current):
                raise BundleError("sealed bundle changed before sealing completed")
            resources_module._require_anchor_current(anchor)
            resources_module._validate_base_identity(resources)
            _validate_exact_layout(resources, anchor)
            assert claim_metadata is not None
            _finalize_seal_receipt(
                anchor,
                resources,
                raw,
                claim_metadata,
            )
            finalized = True
            result = _metadata(resources, hashes, raw, final_metadata)
        except BundleAlreadySealedError:
            if claim_metadata is not None and not finalized:
                _unlink_created_file(root_fd, name, created)
                _rollback_seal_claim(anchor, resources, claim_metadata)
            raise
        except BundleError:
            _unlink_created_file(root_fd, name, created)
            if claim_metadata is not None and not finalized:
                _rollback_seal_claim(anchor, resources, claim_metadata)
            raise
        except OSError as exc:
            _unlink_created_file(root_fd, name, created)
            if claim_metadata is not None and not finalized:
                _rollback_seal_claim(anchor, resources, claim_metadata)
            raise BundleError(f"sealed bundle could not be written: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if claim_fd is not None:
                os.close(claim_fd)
        return result


def _read_bounded(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            raise BundleError("sealed bundle was truncated while being read")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise BundleTooLargeError("sealed bundle grew while being read")
    return b"".join(chunks)


def _decode_envelope(
    raw: bytes,
    resources: InvocationResources,
) -> tuple[
    SourceSnapshot,
    WorkerDelta,
    str,
    tuple[SealedGateEvidence, ...],
    tuple[str, str, str],
]:
    envelope = _expect_keys(_decode_json(raw), _ENVELOPE_KEYS, "bundle")
    version = _expect_integer(envelope["schema_version"], "schema_version")
    if version != SCHEMA_VERSION:
        raise BundleError(f"unsupported sealed bundle schema version: {version}")
    _validate_binding(envelope["binding"], resources)
    authority_mode = _expect_string(envelope["authority_mode"], "authority_mode")
    if authority_mode not in {"lite", "max"}:
        raise BundleError("sealed bundle authority_mode is invalid")
    hashes_value = _expect_keys(envelope["hashes"], _HASH_KEYS, "hashes")
    expected_hashes = (
        _expect_sha256(
            hashes_value["source_snapshot_sha256"],
            "source snapshot hash",
        ),
        _expect_sha256(
            hashes_value["worker_delta_sha256"],
            "worker delta hash",
        ),
        _expect_sha256(
            hashes_value["projected_task_patch_sha256"],
            "projected task patch hash",
        ),
    )
    snapshot = _decode_snapshot(envelope["source_snapshot"], "source_snapshot")
    delta = _decode_delta(envelope["worker_delta"], "worker_delta")
    gates = _decode_gates(envelope["gates"])
    actual_hashes = _evidence_hashes(snapshot, delta)
    labels = (
        "source snapshot hash",
        "worker delta hash",
        "projected task patch hash",
    )
    for label, expected, actual in zip(
        labels, expected_hashes, actual_hashes, strict=True
    ):
        if expected != actual:
            raise BundleError(f"{label} does not match reconstructed evidence")
    return snapshot, delta, authority_mode, gates, actual_hashes


def read_sealed_delta_bundle(resources: InvocationResources) -> SealedDeltaBundle:
    """Load and fully validate the exact one-shot bundle for an invocation."""

    _require_supported_platform()
    if not isinstance(resources, InvocationResources):
        raise BundleError("resources must be InvocationResources")
    name = resources.delta_bundle_path.name
    raw: bytes
    file_metadata: os.stat_result
    descriptor: int | None = None
    with _validated_root(resources) as anchor:
        root_fd = anchor.root_fd  # type: ignore[attr-defined]
        try:
            before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise BundleError("sealed delta bundle does not exist") from exc
        _validate_bundle_stat(before, "sealed bundle")
        flags = os.O_RDONLY | _require_nofollow() | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=root_fd)
            opened = os.fstat(descriptor)
            _validate_bundle_stat(opened, "sealed bundle")
            if not _same_file_state(before, opened):
                raise BundleError("sealed bundle inode changed while being opened")
            raw = _read_bounded(descriptor, opened.st_size)
            after_read = os.fstat(descriptor)
            if not _same_file_state(opened, after_read):
                raise BundleError("sealed bundle changed while being read")
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not _same_file_state(after_read, current):
                raise BundleError("sealed bundle inode changed after reading")
            resources_module._require_anchor_current(anchor)
            file_metadata = after_read
        except BundleError:
            raise
        except OSError as exc:
            raise BundleError(f"sealed bundle could not be read safely: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        snapshot, delta, authority_mode, gates, hashes = _decode_envelope(raw, resources)
        _validate_sealed_receipt(
            anchor.parent_fd,  # type: ignore[attr-defined]
            resources,
            raw,
        )
        resources_module._require_anchor_current(anchor)
        resources_module._validate_base_identity(resources)
        _validate_exact_layout(resources, anchor)
    return SealedDeltaBundle(
        metadata=_metadata(resources, hashes, raw, file_metadata),
        snapshot=snapshot,
        delta=delta,
        authority_mode=authority_mode,
        gates=gates,
    )


_FINAL_CONTEXT_KEYS = frozenset(
    {"acceptance_criteria", "approved_plan", "goal", "main_loop_verdict", "version"}
)
_FINAL_RECEIPT_KEYS = frozenset(
    {
        "approval_binding_hash",
        "authority_mode",
        "bundle_sha256",
        "decision",
        "fingerprint_evidence_source",
        "invocation_id",
        "main_fingerprint",
        "message",
        "projected_task_patch_hash",
        "requested_changes",
        "review_packet_sha256",
        "reviewer_fingerprint",
        "reviewer_read_only_enforced",
        "reviewer_route_id",
        "schema_version",
        "source_snapshot_hash",
        "worker_delta_hash",
    }
)


def _final_context(value: object) -> dict[str, object]:
    context = _expect_keys(value, _FINAL_CONTEXT_KEYS, "final review context")
    if _expect_integer(context["version"], "final review context version") != 1:
        raise BundleError("final review context version is unsupported")
    for field_name in ("goal", "approved_plan"):
        text = _expect_string(context[field_name], f"final context.{field_name}")
        if not text.strip() or "\0" in text or len(text) > 65_536:
            raise BundleError(f"final context.{field_name} is invalid")
    verdict = _expect_string(
        context["main_loop_verdict"], "final context.main_loop_verdict"
    )
    if verdict != "approve":
        raise BundleError("main loop must approve before authority review")
    criteria = _expect_list(
        context["acceptance_criteria"], "final context.acceptance_criteria"
    )
    if (
        not criteria
        or len(criteria) > 256
        or not all(
            type(item) is str
            and bool(item.strip())
            and "\0" not in item
            and len(item) <= 16_384
            for item in criteria
        )
    ):
        raise BundleError("final context acceptance criteria are invalid")
    return {
        "acceptance_criteria": list(criteria),
        "approved_plan": context["approved_plan"],
        "goal": context["goal"],
        "main_loop_verdict": verdict,
        "version": 1,
    }


def _review_file_manifest(section: str, records: tuple[EvidenceRecord, ...]) -> list[object]:
    result: list[object] = []
    for index, record in enumerate(records):
        payload = record.content if record.content is not None else record.canonical_diff
        result.append(
            {
                "index": index,
                "new_mode": int(record.new_mode),
                "old_mode": int(record.old_mode),
                "path_b64": _encode_bytes(record.path),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "section": section,
                "status": int(record.status),
                "tag": int(record.tag),
            }
        )
    return result


def build_final_review_packet(
    bundle: SealedDeltaBundle,
    context: object,
) -> bytes:
    """Build the complete reviewer-visible packet from one validated sealed bundle."""

    if not isinstance(bundle, SealedDeltaBundle):
        raise BundleError("final review requires a sealed delta bundle")
    if not bundle.gates:
        raise BundleError("final review requires persisted trusted gate evidence")
    canonical_context = _final_context(context)
    patch = project_task_patch(bundle.snapshot, bundle.delta)
    binding = ApprovalBinding(
        source_snapshot_hash=bundle.metadata.source_snapshot_hash,
        worker_delta_hash=bundle.metadata.worker_delta_hash,
        projected_task_patch_hash=bundle.metadata.projected_task_patch_hash,
    )
    sections = (
        ("records", patch.records),
        ("staged", patch.staged),
        ("unstaged", patch.unstaged),
        ("untracked", patch.untracked),
    )
    file_manifest = [
        entry
        for section, records in sections
        for entry in _review_file_manifest(section, records)
    ]
    packet = {
        "acceptance_and_plan": canonical_context,
        "allowed_paths_b64": [
            _encode_bytes(path) for path in bundle.snapshot.allowed_paths
        ],
        "approval_binding_hash": binding.canonical_hash,
        "authority_mode": bundle.authority_mode,
        "bundle_sha256": bundle.metadata.bundle_sha256,
        "canonical_patch_b64": _encode_bytes(encode_canonical_patch(patch)),
        "file_manifest": file_manifest,
        "gates": [_encode_gate(gate) for gate in bundle.gates],
        "invocation_id": bundle.metadata.invocation_id,
        "patch_sections": {
            section: [_encode_record(record) for record in records]
            for section, records in sections
        },
        "private_scope_summary": {
            "aggregate_hash": patch.private_summary.aggregate_hash,
            "status_counts": [
                [int(status), count]
                for status, count in patch.private_summary.status_counts
            ],
        },
        "projected_task_patch_hash": bundle.metadata.projected_task_patch_hash,
        "purpose": "final-review",
        "source_snapshot_hash": bundle.metadata.source_snapshot_hash,
        "version": 1,
        "worker_delta_hash": bundle.metadata.worker_delta_hash,
    }
    return _canonical_json(packet)


def _validate_final_review_packet(
    packet: bytes,
    bundle: SealedDeltaBundle,
) -> str:
    if type(packet) is not bytes or not packet or len(packet) > MAX_BUNDLE_BYTES * 3:
        raise BundleError("final review packet is invalid or too large")
    value = _decode_json(packet)
    expected = {
        "approval_binding_hash": ApprovalBinding(
            source_snapshot_hash=bundle.metadata.source_snapshot_hash,
            worker_delta_hash=bundle.metadata.worker_delta_hash,
            projected_task_patch_hash=bundle.metadata.projected_task_patch_hash,
        ).canonical_hash,
        "authority_mode": bundle.authority_mode,
        "bundle_sha256": bundle.metadata.bundle_sha256,
        "invocation_id": bundle.metadata.invocation_id,
        "projected_task_patch_hash": bundle.metadata.projected_task_patch_hash,
        "purpose": "final-review",
        "source_snapshot_hash": bundle.metadata.source_snapshot_hash,
        "version": 1,
        "worker_delta_hash": bundle.metadata.worker_delta_hash,
    }
    for field_name, expected_value in expected.items():
        if value.get(field_name) != expected_value:
            raise BundleError(f"final review packet does not match {field_name}")
    return expected["approval_binding_hash"]  # type: ignore[return-value]


def _open_evidence_anchor(anchor: object, resources: InvocationResources) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | _require_nofollow()
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(
        resources.evidence_path.name,
        flags,
        dir_fd=anchor.root_fd,  # type: ignore[attr-defined]
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_dev != resources.evidence_device
            or metadata.st_ino != resources.evidence_inode
            or metadata.st_uid != _current_uid()
        ):
            raise BundleError("review evidence directory changed identity")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _receipt_from_value(value: object) -> FinalReviewReceipt:
    receipt = _expect_keys(value, _FINAL_RECEIPT_KEYS, "final review receipt")
    changes = _expect_list(receipt["requested_changes"], "requested_changes")
    if not all(type(change) is str for change in changes):
        raise BundleError("requested_changes must contain only strings")
    try:
        return FinalReviewReceipt(
            schema_version=_expect_integer(receipt["schema_version"], "schema_version"),
            authority_mode=_expect_string(
                receipt["authority_mode"], "authority_mode"
            ),
            invocation_id=_expect_string(receipt["invocation_id"], "invocation_id"),
            bundle_sha256=_expect_sha256(receipt["bundle_sha256"], "bundle_sha256"),
            review_packet_sha256=_expect_sha256(
                receipt["review_packet_sha256"], "review_packet_sha256"
            ),
            source_snapshot_hash=_expect_sha256(
                receipt["source_snapshot_hash"], "source_snapshot_hash"
            ),
            worker_delta_hash=_expect_sha256(
                receipt["worker_delta_hash"], "worker_delta_hash"
            ),
            projected_task_patch_hash=_expect_sha256(
                receipt["projected_task_patch_hash"],
                "projected_task_patch_hash",
            ),
            approval_binding_hash=_expect_sha256(
                receipt["approval_binding_hash"], "approval_binding_hash"
            ),
            decision=_expect_string(receipt["decision"], "decision"),
            reviewer_route_id=_expect_string(
                receipt["reviewer_route_id"], "reviewer_route_id"
            ),
            reviewer_fingerprint=_expect_string(
                receipt["reviewer_fingerprint"], "reviewer_fingerprint"
            ),
            fingerprint_evidence_source=_expect_string(
                receipt["fingerprint_evidence_source"],
                "fingerprint_evidence_source",
            ),
            reviewer_read_only_enforced=receipt["reviewer_read_only_enforced"],
            main_fingerprint=_expect_string(
                receipt["main_fingerprint"], "main_fingerprint"
            ),
            message=_expect_string(receipt["message"], "message"),
            requested_changes=tuple(changes),  # type: ignore[arg-type]
        )
    except ValueError as exc:
        raise BundleError(f"final review receipt is invalid: {exc}") from exc


def _review_receipt_payload(receipt: FinalReviewReceipt) -> dict[str, object]:
    return {
        "approval_binding_hash": receipt.approval_binding_hash,
        "authority_mode": receipt.authority_mode,
        "bundle_sha256": receipt.bundle_sha256,
        "decision": receipt.decision,
        "fingerprint_evidence_source": receipt.fingerprint_evidence_source,
        "invocation_id": receipt.invocation_id,
        "main_fingerprint": receipt.main_fingerprint,
        "message": receipt.message,
        "projected_task_patch_hash": receipt.projected_task_patch_hash,
        "requested_changes": list(receipt.requested_changes),
        "review_packet_sha256": receipt.review_packet_sha256,
        "reviewer_fingerprint": receipt.reviewer_fingerprint,
        "reviewer_read_only_enforced": receipt.reviewer_read_only_enforced,
        "reviewer_route_id": receipt.reviewer_route_id,
        "schema_version": receipt.schema_version,
        "source_snapshot_hash": receipt.source_snapshot_hash,
        "worker_delta_hash": receipt.worker_delta_hash,
    }


def seal_final_review_receipt(
    resources: InvocationResources,
    *,
    packet: bytes,
    decision: str,
    approval_binding_hash: str,
    reviewer_route_id: str,
    reviewer_fingerprint: str,
    fingerprint_evidence_source: str,
    reviewer_read_only_enforced: bool,
    main_fingerprint: str,
    message: str,
    requested_changes: tuple[str, ...] | list[str],
) -> FinalReviewReceipt:
    """Persist the only public-CLI integration authority for one invocation."""

    bundle = read_sealed_delta_bundle(resources)
    expected_binding = _validate_final_review_packet(packet, bundle)
    if approval_binding_hash != expected_binding:
        raise BundleError("review verdict does not bind the exact final packet")
    try:
        receipt = FinalReviewReceipt(
            schema_version=1,
            authority_mode=bundle.authority_mode,
            invocation_id=resources.invocation_id,
            bundle_sha256=bundle.metadata.bundle_sha256,
            review_packet_sha256=hashlib.sha256(packet).hexdigest(),
            source_snapshot_hash=bundle.metadata.source_snapshot_hash,
            worker_delta_hash=bundle.metadata.worker_delta_hash,
            projected_task_patch_hash=bundle.metadata.projected_task_patch_hash,
            approval_binding_hash=approval_binding_hash,
            decision=decision,
            reviewer_route_id=reviewer_route_id,
            reviewer_fingerprint=reviewer_fingerprint,
            fingerprint_evidence_source=fingerprint_evidence_source,
            reviewer_read_only_enforced=reviewer_read_only_enforced,
            main_fingerprint=main_fingerprint,
            message=message,
            requested_changes=tuple(requested_changes),
        )
    except (TypeError, ValueError) as exc:
        raise BundleError(f"final review approval cannot be sealed: {exc}") from exc
    raw = _canonical_json(_review_receipt_payload(receipt))
    descriptor: int | None = None
    created: os.stat_result | None = None
    evidence_fd: int | None = None
    with _validated_root(resources) as anchor:
        try:
            evidence_fd = _open_evidence_anchor(anchor, resources)
            descriptor, created = _create_receipt_at(
                evidence_fd,
                resources.final_evidence_path.name,
                raw,
                final_mode=0o400,
            )
            os.close(descriptor)
            descriptor = None
            os.fsync(evidence_fd)
            _validate_exact_layout(resources, anchor)
        except FileExistsError as exc:
            raise BundleAlreadySealedError(
                "final review receipt already exists; refusing to overwrite it"
            ) from exc
        except BundleError:
            if evidence_fd is not None:
                _unlink_created_file(
                    evidence_fd,
                    resources.final_evidence_path.name,
                    created,
                )
            raise
        except OSError as exc:
            if evidence_fd is not None:
                _unlink_created_file(
                    evidence_fd,
                    resources.final_evidence_path.name,
                    created,
                )
            raise BundleError(f"final review receipt could not be sealed: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if evidence_fd is not None:
                os.close(evidence_fd)
    return receipt


def read_final_review_receipt(resources: InvocationResources) -> FinalReviewReceipt:
    """Read and re-bind the exact final approval used by public integration."""

    bundle = read_sealed_delta_bundle(resources)
    evidence_fd: int | None = None
    descriptor: int | None = None
    with _validated_root(resources) as anchor:
        try:
            evidence_fd = _open_evidence_anchor(anchor, resources)
            descriptor, value, _ = _open_receipt_at(
                evidence_fd,
                resources.final_evidence_path.name,
                allowed_modes=frozenset({0o400}),
            )
            receipt = _receipt_from_value(value)
            if (
                receipt.authority_mode != bundle.authority_mode
                or receipt.invocation_id != resources.invocation_id
                or receipt.bundle_sha256 != bundle.metadata.bundle_sha256
                or receipt.source_snapshot_hash
                != bundle.metadata.source_snapshot_hash
                or receipt.worker_delta_hash != bundle.metadata.worker_delta_hash
                or receipt.projected_task_patch_hash
                != bundle.metadata.projected_task_patch_hash
            ):
                raise BundleError("final review receipt does not bind this sealed bundle")
            _validate_exact_layout(resources, anchor)
            return receipt
        except FileNotFoundError as exc:
            raise BundleError("sealed bundle has no final review receipt") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if evidence_fd is not None:
                os.close(evidence_fd)


__all__ = (
    "BundleAlreadySealedError",
    "BundleCapability",
    "BundleError",
    "BundleTooLargeError",
    "BundleUnsupportedPlatformError",
    "DeltaBundleMetadata",
    "FinalReviewReceipt",
    "MAX_BUNDLE_BYTES",
    "SCHEMA_VERSION",
    "SealedDeltaBundle",
    "SealedGateEvidence",
    "build_final_review_packet",
    "probe_bundle_capability",
    "read_final_review_receipt",
    "read_sealed_delta_bundle",
    "seal_delta_bundle",
    "seal_final_review_receipt",
)
