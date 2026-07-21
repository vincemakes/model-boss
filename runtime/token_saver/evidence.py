"""Canonical, versioned evidence encoding for patch-bound reviews.

The serializer deliberately has no decoder. Runtime objects are validated before every
encoding, and display rendering of raw Git paths is kept outside all hashed bytes.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass, field
from enum import IntEnum


MAGIC = b"TOKEN-SAVER-EVIDENCE\0"
FORMAT_VERSION = 1
_MAX_U64 = (1 << 64) - 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)

MODE_ABSENT = 0
MODE_REGULAR = 0o100644
MODE_EXECUTABLE = 0o100755
MODE_SYMLINK = 0o120000
_SUPPORTED_PRESENT_MODES = frozenset(
    {MODE_REGULAR, MODE_EXECUTABLE, MODE_SYMLINK}
)
_REGULAR_MODES = frozenset({MODE_REGULAR, MODE_EXECUTABLE})


class RecordTag(IntEnum):
    """Closed record domains used by public task evidence."""

    TEXT_DIFF = 1
    BINARY = 2
    UNTRACKED = 3
    SYMLINK = 4
    MODE_ONLY = 5


class RecordStatus(IntEnum):
    """Normalized Git change categories; renames are represented as delete plus add."""

    ADDED = 1
    MODIFIED = 2
    DELETED = 3
    TYPE_CHANGED = 4
    UNTRACKED = 5


class PrivateDigestKind(IntEnum):
    """Identifies whether a private digest covers content or canonical diff bytes."""

    CONTENT = 1
    CANONICAL_DIFF = 2


class _DocumentTag(IntEnum):
    SOURCE_SNAPSHOT = 1
    WORKER_DELTA = 2
    CANONICAL_PATCH = 3
    APPROVAL_BINDING = 4
    PRIVATE_MANIFEST = 5


class _SectionTag(IntEnum):
    STAGED = 1
    UNSTAGED = 2
    UNTRACKED = 3
    PRIVATE = 4
    WORKER = 5
    PATCH = 6
    PRIVATE_SUMMARY = 7
    STATUS_COUNTS = 8
    ALLOWED_PATHS = 9


def _coerce_enum(enum_type: type[IntEnum], value: object, label: str) -> IntEnum:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a known integer tag")
    try:
        return enum_type(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a known integer tag") from exc


def _require_u64(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an unsigned 64-bit integer")
    if value < 0 or value > _MAX_U64:
        raise ValueError(f"{label} must be an unsigned 64-bit integer")
    return value


def _u64(value: object, label: str) -> bytes:
    return struct.pack(">Q", _require_u64(value, label))


def _require_bytes(value: object, label: str) -> bytes:
    if type(value) is not bytes:
        raise ValueError(f"{label} must be raw bytes")
    return value


def _byte_field(value: object, label: str) -> bytes:
    raw = _require_bytes(value, label)
    return _u64(len(raw), f"{label} length") + raw


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lower-case SHA-256 hex digest")
    return value


def _require_git_path(value: object) -> bytes:
    path = _require_bytes(value, "raw Git path")
    if not path or b"\0" in path:
        raise ValueError("raw Git path is empty or contains NUL")
    if path.startswith(b"/") or path.startswith(b"\\\\"):
        raise ValueError("raw Git path must be repository-relative")
    if (
        len(path) >= 3
        and path[0:1].lower() in b"abcdefghijklmnopqrstuvwxyz"
        and path[1:2] == b":"
        and path[2:3] in {b"/", b"\\"}
    ):
        raise ValueError("raw Git path must be repository-relative")
    components = path.split(b"/")
    if any(component in {b"", b".", b"..", b".git"} for component in components):
        raise ValueError("raw Git path contains a noncanonical component")
    return path


def _require_mode(value: object, label: str, *, absent: bool = True) -> int:
    mode = _require_u64(value, label)
    allowed = _SUPPORTED_PRESENT_MODES | ({MODE_ABSENT} if absent else set())
    if mode not in allowed:
        raise ValueError(f"{label} is not a supported Git mode")
    return mode


def _validate_transition(
    status: RecordStatus,
    old_mode: int,
    new_mode: int,
) -> None:
    valid = {
        RecordStatus.ADDED: old_mode == MODE_ABSENT and new_mode != MODE_ABSENT,
        RecordStatus.MODIFIED: old_mode != MODE_ABSENT and old_mode == new_mode,
        RecordStatus.DELETED: old_mode != MODE_ABSENT and new_mode == MODE_ABSENT,
        RecordStatus.TYPE_CHANGED: (
            old_mode != MODE_ABSENT
            and new_mode != MODE_ABSENT
            and old_mode != new_mode
        ),
        RecordStatus.UNTRACKED: old_mode == MODE_ABSENT and new_mode != MODE_ABSENT,
    }[status]
    if not valid:
        raise ValueError("record status and Git modes describe different transitions")


@dataclass(frozen=True)
class EvidenceRecord:
    """One complete public path record in a source, delta, or projected patch."""

    tag: RecordTag
    path: bytes
    status: RecordStatus
    old_mode: int
    new_mode: int
    canonical_diff: bytes = b""
    content: bytes | None = None

    def __post_init__(self) -> None:
        tag = _coerce_enum(RecordTag, self.tag, "record tag")
        status = _coerce_enum(RecordStatus, self.status, "record status")
        path = _require_git_path(self.path)
        old_mode = _require_mode(self.old_mode, "old mode")
        new_mode = _require_mode(self.new_mode, "new mode")
        canonical_diff = _require_bytes(self.canonical_diff, "canonical diff")
        if self.content is not None:
            content = _require_bytes(self.content, "record content")
        else:
            content = None

        if tag is RecordTag.MODE_ONLY:
            if (
                status is not RecordStatus.MODIFIED
                or old_mode not in _REGULAR_MODES
                or new_mode not in _REGULAR_MODES
                or old_mode == new_mode
                or canonical_diff
                or content is not None
            ):
                raise ValueError("mode-only record has an invalid shape")
        else:
            _validate_transition(status, old_mode, new_mode)

        if tag is RecordTag.TEXT_DIFF:
            present_modes = {mode for mode in (old_mode, new_mode) if mode}
            if (
                status is RecordStatus.UNTRACKED
                or not canonical_diff
                or content is not None
                or not present_modes.issubset(_REGULAR_MODES)
            ):
                raise ValueError("text-diff record has an invalid shape")
        elif tag is RecordTag.BINARY:
            present_modes = {mode for mode in (old_mode, new_mode) if mode}
            if (
                status is RecordStatus.UNTRACKED
                or not canonical_diff
                or not present_modes.issubset(_REGULAR_MODES)
                or (new_mode and content is None)
                or (not new_mode and content is not None)
            ):
                raise ValueError("binary record has an invalid shape")
        elif tag is RecordTag.UNTRACKED:
            if (
                status is not RecordStatus.UNTRACKED
                or old_mode != MODE_ABSENT
                or new_mode not in _REGULAR_MODES
                or canonical_diff
                or content is None
            ):
                raise ValueError("untracked record has an invalid shape")
        elif tag is RecordTag.SYMLINK:
            present_modes = {mode for mode in (old_mode, new_mode) if mode}
            if status is RecordStatus.TYPE_CHANGED:
                valid_modes = (
                    MODE_SYMLINK in present_modes
                    and len(present_modes) == 2
                    and bool(present_modes.intersection(_REGULAR_MODES))
                )
            else:
                valid_modes = present_modes == {MODE_SYMLINK}
            if (
                not present_modes
                or not valid_modes
                or (new_mode and content is None)
                or (not new_mode and content is not None)
                or (status is RecordStatus.UNTRACKED and canonical_diff)
                or (status is not RecordStatus.UNTRACKED and not canonical_diff)
            ):
                raise ValueError("symlink record has an invalid shape")

        object.__setattr__(self, "tag", tag)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "old_mode", old_mode)
        object.__setattr__(self, "new_mode", new_mode)
        object.__setattr__(self, "canonical_diff", canonical_diff)
        object.__setattr__(self, "content", content)


@dataclass(frozen=True)
class PrivateRecord:
    """Local-only fingerprint of one out-of-scope dirty path."""

    digest_kind: PrivateDigestKind
    path: bytes = field(repr=False)
    status: RecordStatus
    mode: int = field(repr=False)
    size: int = field(repr=False)
    digest: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "digest_kind",
            _coerce_enum(PrivateDigestKind, self.digest_kind, "digest kind"),
        )
        object.__setattr__(self, "path", _require_git_path(self.path))
        object.__setattr__(
            self,
            "status",
            _coerce_enum(RecordStatus, self.status, "record status"),
        )
        object.__setattr__(self, "mode", _require_mode(self.mode, "private mode"))
        object.__setattr__(self, "size", _require_u64(self.size, "private size"))
        object.__setattr__(
            self,
            "digest",
            _require_sha256(self.digest, "private digest"),
        )


def _validated_public_record(value: object) -> EvidenceRecord:
    if not isinstance(value, EvidenceRecord):
        raise ValueError("public evidence lists must contain EvidenceRecord values")
    return EvidenceRecord(
        tag=value.tag,
        path=value.path,
        status=value.status,
        old_mode=value.old_mode,
        new_mode=value.new_mode,
        canonical_diff=value.canonical_diff,
        content=value.content,
    )


def _validated_private_record(value: object) -> PrivateRecord:
    if not isinstance(value, PrivateRecord):
        raise ValueError("private evidence lists must contain PrivateRecord values")
    return PrivateRecord(
        digest_kind=value.digest_kind,
        path=value.path,
        status=value.status,
        mode=value.mode,
        size=value.size,
        digest=value.digest,
    )


def _normalize_public_records(values: object, label: str) -> tuple[EvidenceRecord, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{label} must be a record sequence")
    records = tuple(_validated_public_record(value) for value in values)
    paths = tuple(record.path for record in records)
    if len(paths) != len(set(paths)):
        raise ValueError(f"{label} contains a duplicate raw Git path")
    return tuple(sorted(records, key=lambda record: record.path))


def _normalize_private_records(values: object) -> tuple[PrivateRecord, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError("private records must be a record sequence")
    records = tuple(_validated_private_record(value) for value in values)
    paths = tuple(record.path for record in records)
    if len(paths) != len(set(paths)):
        raise ValueError("private records contain a duplicate raw Git path")
    return tuple(sorted(records, key=lambda record: record.path))


def _normalize_paths(values: object, label: str) -> tuple[bytes, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{label} must be a raw-path sequence")
    paths = tuple(_require_git_path(value) for value in values)
    if len(paths) != len(set(paths)):
        raise ValueError(f"{label} contains a duplicate raw Git path")
    return tuple(sorted(paths))


def _require_baseline_oid(value: object) -> bytes:
    oid = _require_bytes(value, "baseline object ID")
    if len(oid) not in {40, 64} or any(
        byte not in b"0123456789abcdef" for byte in oid
    ):
        raise ValueError("baseline object ID must be lower-case hexadecimal Git OID")
    return oid


@dataclass(frozen=True)
class SourceSnapshot:
    """Complete source state, including local-only out-of-scope fingerprints."""

    baseline_oid: bytes
    allowed_paths: tuple[bytes, ...] = ()
    staged: tuple[EvidenceRecord, ...] = ()
    unstaged: tuple[EvidenceRecord, ...] = ()
    untracked: tuple[EvidenceRecord, ...] = ()
    private: tuple[PrivateRecord, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        baseline = _require_baseline_oid(self.baseline_oid)
        allowed_paths = _normalize_paths(self.allowed_paths, "source allowlist")
        staged = _normalize_public_records(self.staged, "staged records")
        unstaged = _normalize_public_records(self.unstaged, "unstaged records")
        untracked = _normalize_public_records(self.untracked, "untracked records")
        private = _normalize_private_records(self.private)

        if any(record.status is RecordStatus.UNTRACKED for record in staged + unstaged):
            raise ValueError("tracked source sections cannot contain untracked records")
        if any(record.status is not RecordStatus.UNTRACKED for record in untracked):
            raise ValueError("untracked source section contains a tracked record")
        tracked_paths = {record.path for record in staged + unstaged}
        untracked_paths = {record.path for record in untracked}
        private_paths = {record.path for record in private}
        public_paths = tracked_paths | untracked_paths
        allowed_path_set = set(allowed_paths)
        if not public_paths.issubset(allowed_path_set):
            raise ValueError("public source path is outside the source allowlist")
        if private_paths.intersection(allowed_path_set):
            raise ValueError("private source path overlaps the source allowlist")
        staged_by_path = {record.path: record for record in staged}
        unstaged_paths = {record.path for record in unstaged}
        for path in tracked_paths.intersection(untracked_paths):
            staged_record = staged_by_path.get(path)
            if (
                staged_record is None
                or staged_record.status is not RecordStatus.DELETED
                or path in unstaged_paths
            ):
                raise ValueError("tracked and untracked source paths overlap")
        if private_paths.intersection(tracked_paths | untracked_paths):
            raise ValueError("public and private source paths overlap")

        object.__setattr__(self, "baseline_oid", baseline)
        object.__setattr__(self, "allowed_paths", allowed_paths)
        object.__setattr__(self, "staged", staged)
        object.__setattr__(self, "unstaged", unstaged)
        object.__setattr__(self, "untracked", untracked)
        object.__setattr__(self, "private", private)

    @property
    def private_summary(self) -> PrivateSummary:
        return summarize_private_records(self.private)


@dataclass(frozen=True)
class WorkerDelta:
    """Worker-only records plus a non-hashed projected destination snapshot."""

    records: tuple[EvidenceRecord, ...]
    projected_snapshot: SourceSnapshot | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "records",
            _normalize_public_records(self.records, "worker records"),
        )
        if self.projected_snapshot is not None:
            if not isinstance(self.projected_snapshot, SourceSnapshot):
                raise ValueError("projected snapshot must be SourceSnapshot")
            object.__setattr__(
                self,
                "projected_snapshot",
                SourceSnapshot(
                    baseline_oid=self.projected_snapshot.baseline_oid,
                    allowed_paths=self.projected_snapshot.allowed_paths,
                    staged=self.projected_snapshot.staged,
                    unstaged=self.projected_snapshot.unstaged,
                    untracked=self.projected_snapshot.untracked,
                    private=self.projected_snapshot.private,
                ),
            )


@dataclass(frozen=True)
class PrivateSummary:
    """Reviewer-safe aggregate and redacted private status counts."""

    aggregate_hash: str
    status_counts: tuple[tuple[RecordStatus, int], ...]

    def __post_init__(self) -> None:
        aggregate_hash = _require_sha256(self.aggregate_hash, "private aggregate")
        if not isinstance(self.status_counts, (tuple, list)):
            raise ValueError("private status counts must be a sequence")
        normalized: list[tuple[RecordStatus, int]] = []
        seen: set[RecordStatus] = set()
        for item in self.status_counts:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError("private status counts must contain status/count pairs")
            status = _coerce_enum(RecordStatus, item[0], "record status")
            count = _require_u64(item[1], "private status count")
            if count == 0:
                raise ValueError("private status counts must omit zero values")
            if status in seen:
                raise ValueError("private status counts contain a duplicate status")
            seen.add(status)
            normalized.append((status, count))  # type: ignore[arg-type]
        normalized.sort(key=lambda item: int(item[0]))
        object.__setattr__(self, "aggregate_hash", aggregate_hash)
        object.__setattr__(self, "status_counts", tuple(normalized))


@dataclass(frozen=True)
class CanonicalPatch:
    """Complete public task patch plus a reviewer-safe private-state summary."""

    records: tuple[EvidenceRecord, ...]
    private_summary: PrivateSummary
    staged: tuple[EvidenceRecord, ...] = ()
    unstaged: tuple[EvidenceRecord, ...] = ()
    untracked: tuple[EvidenceRecord, ...] = ()

    def __post_init__(self) -> None:
        records = _normalize_public_records(self.records, "canonical patch records")
        staged = _normalize_public_records(self.staged, "canonical staged records")
        unstaged = _normalize_public_records(
            self.unstaged,
            "canonical unstaged records",
        )
        untracked = _normalize_public_records(
            self.untracked,
            "canonical untracked records",
        )
        if any(record.status is RecordStatus.UNTRACKED for record in staged + unstaged):
            raise ValueError("canonical tracked sections contain an untracked record")
        if any(record.status is not RecordStatus.UNTRACKED for record in untracked):
            raise ValueError("canonical untracked section contains a tracked record")
        if not isinstance(self.private_summary, PrivateSummary):
            raise ValueError("canonical patch requires a private summary")
        summary = PrivateSummary(
            aggregate_hash=self.private_summary.aggregate_hash,
            status_counts=self.private_summary.status_counts,
        )
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "staged", staged)
        object.__setattr__(self, "unstaged", unstaged)
        object.__setattr__(self, "untracked", untracked)
        object.__setattr__(self, "private_summary", summary)


@dataclass(frozen=True)
class ApprovalBinding:
    """The exact destination-specific tuple authorized by an approval."""

    source_snapshot_hash: str
    worker_delta_hash: str
    projected_task_patch_hash: str

    def __post_init__(self) -> None:
        for field_name in (
            "source_snapshot_hash",
            "worker_delta_hash",
            "projected_task_patch_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_sha256(getattr(self, field_name), field_name),
            )

    @property
    def canonical_hash(self) -> str:
        return hashlib.sha256(encode_approval_binding(self)).hexdigest()


def _header(document_tag: _DocumentTag) -> bytes:
    return MAGIC + _u64(FORMAT_VERSION, "format version") + _u64(
        int(document_tag), "document tag"
    )


def _encode_public_record(record: EvidenceRecord) -> bytes:
    record = _validated_public_record(record)
    chunks = [
        _u64(int(record.tag), "record tag"),
        _byte_field(record.path, "raw Git path"),
        _u64(int(record.status), "record status"),
        _u64(record.old_mode, "old mode"),
        _u64(record.new_mode, "new mode"),
        _byte_field(record.canonical_diff, "canonical diff"),
        _u64(1 if record.content is not None else 0, "content presence"),
    ]
    if record.content is not None:
        chunks.extend(
            (
                _u64(len(record.content), "content size"),
                _byte_field(hashlib.sha256(record.content).digest(), "content digest"),
                _byte_field(record.content, "record content"),
            )
        )
    return b"".join(chunks)


def _encode_private_record(record: PrivateRecord) -> bytes:
    record = _validated_private_record(record)
    return b"".join(
        (
            _u64(int(record.digest_kind), "private record tag"),
            _byte_field(record.path, "raw Git path"),
            _u64(int(record.status), "record status"),
            _u64(record.mode, "private mode"),
            _u64(record.size, "private size"),
            _byte_field(bytes.fromhex(record.digest), "private digest"),
        )
    )


def _encode_public_section(
    section: _SectionTag,
    records: tuple[EvidenceRecord, ...],
) -> bytes:
    return b"".join(
        (
            _u64(int(section), "section tag"),
            _u64(len(records), "record count"),
            *(_encode_public_record(record) for record in records),
        )
    )


def _encode_private_manifest(records: tuple[PrivateRecord, ...]) -> bytes:
    records = _normalize_private_records(records)
    return b"".join(
        (
            _header(_DocumentTag.PRIVATE_MANIFEST),
            _u64(int(_SectionTag.PRIVATE), "section tag"),
            _u64(len(records), "record count"),
            *(_encode_private_record(record) for record in records),
        )
    )


def summarize_private_records(records: object) -> PrivateSummary:
    """Create the only private-state projection suitable for reviewer evidence."""

    normalized = _normalize_private_records(records)
    counts: dict[RecordStatus, int] = {}
    for record in normalized:
        counts[record.status] = counts.get(record.status, 0) + 1
    return PrivateSummary(
        aggregate_hash=hashlib.sha256(_encode_private_manifest(normalized)).hexdigest(),
        status_counts=tuple(sorted(counts.items(), key=lambda item: int(item[0]))),
    )


def encode_source_snapshot(snapshot: SourceSnapshot) -> bytes:
    """Encode a complete local source snapshot for destination-change detection."""

    if not isinstance(snapshot, SourceSnapshot):
        raise ValueError("snapshot must be SourceSnapshot")
    snapshot = SourceSnapshot(
        baseline_oid=snapshot.baseline_oid,
        allowed_paths=snapshot.allowed_paths,
        staged=snapshot.staged,
        unstaged=snapshot.unstaged,
        untracked=snapshot.untracked,
        private=snapshot.private,
    )
    return b"".join(
        (
            _header(_DocumentTag.SOURCE_SNAPSHOT),
            _byte_field(snapshot.baseline_oid, "baseline object ID"),
            _u64(int(_SectionTag.ALLOWED_PATHS), "section tag"),
            _u64(len(snapshot.allowed_paths), "allowed path count"),
            *(
                _byte_field(path, "allowed raw Git path")
                for path in snapshot.allowed_paths
            ),
            _encode_public_section(_SectionTag.STAGED, snapshot.staged),
            _encode_public_section(_SectionTag.UNSTAGED, snapshot.unstaged),
            _encode_public_section(_SectionTag.UNTRACKED, snapshot.untracked),
            _u64(int(_SectionTag.PRIVATE), "section tag"),
            _u64(len(snapshot.private), "record count"),
            *(_encode_private_record(record) for record in snapshot.private),
        )
    )


def encode_worker_delta(delta: WorkerDelta) -> bytes:
    """Encode worker-created records without preexisting source bytes."""

    if not isinstance(delta, WorkerDelta):
        raise ValueError("delta must be WorkerDelta")
    delta = WorkerDelta(records=delta.records)
    return _header(_DocumentTag.WORKER_DELTA) + _encode_public_section(
        _SectionTag.WORKER, delta.records
    )


def encode_canonical_patch(patch: CanonicalPatch) -> bytes:
    """Encode reviewer-visible task evidence without private paths or per-file data."""

    if not isinstance(patch, CanonicalPatch):
        raise ValueError("patch must be CanonicalPatch")
    patch = CanonicalPatch(
        records=patch.records,
        private_summary=patch.private_summary,
        staged=patch.staged,
        unstaged=patch.unstaged,
        untracked=patch.untracked,
    )
    summary = patch.private_summary
    status_chunks: list[bytes] = []
    for status, count in summary.status_counts:
        status_chunks.extend(
            (
                _u64(int(status), "record status"),
                _u64(count, "private status count"),
            )
        )
    return b"".join(
        (
            _header(_DocumentTag.CANONICAL_PATCH),
            _encode_public_section(_SectionTag.STAGED, patch.staged),
            _encode_public_section(_SectionTag.UNSTAGED, patch.unstaged),
            _encode_public_section(_SectionTag.UNTRACKED, patch.untracked),
            _encode_public_section(_SectionTag.PATCH, patch.records),
            _u64(int(_SectionTag.PRIVATE_SUMMARY), "section tag"),
            _byte_field(bytes.fromhex(summary.aggregate_hash), "private aggregate"),
            _u64(int(_SectionTag.STATUS_COUNTS), "section tag"),
            _u64(len(summary.status_counts), "private status count length"),
            *status_chunks,
        )
    )


def encode_approval_binding(binding: ApprovalBinding) -> bytes:
    """Encode the three exact hashes authorized by an approval."""

    if not isinstance(binding, ApprovalBinding):
        raise ValueError("binding must be ApprovalBinding")
    binding = ApprovalBinding(
        source_snapshot_hash=binding.source_snapshot_hash,
        worker_delta_hash=binding.worker_delta_hash,
        projected_task_patch_hash=binding.projected_task_patch_hash,
    )
    return b"".join(
        (
            _header(_DocumentTag.APPROVAL_BINDING),
            _byte_field(
                bytes.fromhex(binding.source_snapshot_hash),
                "source snapshot hash",
            ),
            _byte_field(bytes.fromhex(binding.worker_delta_hash), "worker delta hash"),
            _byte_field(
                bytes.fromhex(binding.projected_task_patch_hash),
                "projected task patch hash",
            ),
        )
    )


def display_git_path(path: bytes) -> str:
    """Return a control-safe diagnostic rendering that never enters an evidence hash."""

    raw = _require_git_path(path)
    rendered = ascii(raw)
    return rendered[2:-1]


__all__ = (
    "ApprovalBinding",
    "CanonicalPatch",
    "EvidenceRecord",
    "FORMAT_VERSION",
    "MAGIC",
    "MODE_ABSENT",
    "MODE_EXECUTABLE",
    "MODE_REGULAR",
    "MODE_SYMLINK",
    "PrivateDigestKind",
    "PrivateRecord",
    "PrivateSummary",
    "RecordStatus",
    "RecordTag",
    "SourceSnapshot",
    "WorkerDelta",
    "display_git_path",
    "encode_approval_binding",
    "encode_canonical_patch",
    "encode_source_snapshot",
    "encode_worker_delta",
    "summarize_private_records",
)
