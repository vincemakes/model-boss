"""Deterministic Git capture and isolated worktree materialization."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .evidence import (
    CanonicalPatch,
    EvidenceRecord,
    MODE_EXECUTABLE,
    MODE_REGULAR,
    MODE_SYMLINK,
    PrivateDigestKind,
    PrivateRecord,
    RecordStatus,
    RecordTag,
    SourceSnapshot,
    WorkerDelta,
    display_git_path,
    encode_source_snapshot,
)


class RepositoryError(RuntimeError):
    """A repository could not be captured or materialized safely."""


class ScopeViolationError(RepositoryError):
    """A worker changed a path outside its exact source allowlist."""


@dataclass
class WorktreeHandle:
    """Exact disposable worktree state owned by one invocation."""

    source_repo: Path
    path: Path
    baseline_oid: bytes
    allowed_paths: tuple[bytes, ...]
    object_directory: Path
    source_object_directory: Path
    materialized_tree_oid: bytes | None = None


@dataclass(frozen=True)
class _StatusEntry:
    path: bytes
    index_status: bytes
    worktree_status: bytes
    head_mode: int
    index_mode: int
    worktree_mode: int
    head_oid: bytes | None
    index_oid: bytes | None
    untracked: bool = False


_ZERO_OIDS = {b"0" * 40, b"0" * 64}
_PRIVATE_HASH_CHUNK_SIZE = 1024 * 1024
_STATUS_MAP = {
    b"A": RecordStatus.ADDED,
    b"M": RecordStatus.MODIFIED,
    b"D": RecordStatus.DELETED,
    b"T": RecordStatus.TYPE_CHANGED,
}


def _git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if "PATH" in os.environ:
        environment["PATH"] = os.environ["PATH"]
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    if extra:
        environment.update(extra)
    return environment


def _run_git(
    repo: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    extra_env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            (
                "git",
                "--no-pager",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "core.quotePath=true",
                *arguments,
            ),
            cwd=repo,
            env=_git_environment(extra_env),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise RepositoryError("Git executable is unavailable") from exc
    if check and result.returncode != 0:
        operation = arguments[0] if arguments else "command"
        raise RepositoryError(f"Git {operation} operation failed")
    return result


def _resolve_repository(repo: object) -> Path:
    if not isinstance(repo, (str, os.PathLike)):
        raise ValueError("repo must be a filesystem path")
    candidate = Path(repo)
    if candidate.is_symlink():
        raise RepositoryError("source repository path must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RepositoryError("source repository is unavailable") from exc
    result = _run_git(resolved, "rev-parse", "--show-toplevel")
    try:
        top_level = Path(os.fsdecode(result.stdout.strip())).resolve(strict=True)
    except OSError as exc:
        raise RepositoryError("repository root cannot be resolved") from exc
    if top_level != resolved:
        raise RepositoryError("repo must be the exact repository root")
    return resolved


def _reject_repository_content_filters(repo: Path) -> None:
    """Fail before Git can execute a repository-controlled content filter."""

    result = _run_git(
        repo,
        "config",
        "--includes",
        "--null",
        "--name-only",
        "--list",
        check=False,
    )
    if result.returncode != 0:
        raise RepositoryError("Git repository filter inspection failed")
    for raw_name in result.stdout.split(b"\0"):
        name = raw_name.decode("utf-8", "surrogateescape").casefold()
        if name.startswith("filter.") and name.rsplit(".", 1)[-1] in {
            "clean",
            "smudge",
            "process",
        }:
            raise RepositoryError("repository content filters are not supported")


def _normalize_allowed_paths(values: object) -> tuple[bytes, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError("allowed_paths must be a raw-path sequence")
    paths: list[bytes] = []
    aliases: set[str] = set()
    for value in values:
        if type(value) is not bytes:
            raise ValueError("allowed paths must be raw Git path bytes")
        _validate_platform_path(value)
        alias = unicodedata.normalize("NFC", os.fsdecode(value)).casefold()
        if value in paths or alias in aliases:
            raise ValueError("allowed paths contain a duplicate or path alias")
        aliases.add(alias)
        paths.append(value)
    return tuple(sorted(paths))


def _path_alias(path: bytes) -> str:
    return unicodedata.normalize("NFC", os.fsdecode(path)).casefold()


def _validate_platform_path(path: bytes, *, windows: bool | None = None) -> None:
    """Reject raw paths that Windows would reinterpret or alias."""

    display_git_path(path)
    components = path.split(b"/")
    if any(component.lower() == b".git" for component in components):
        raise ValueError("raw Git path aliases Git administrative data")
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return
    if b"\\" in path:
        raise ValueError("raw Git path contains a Windows path separator")
    if (
        len(path) >= 2
        and path[0:1].lower() in b"abcdefghijklmnopqrstuvwxyz"
        and path[1:2] == b":"
    ):
        raise ValueError("raw Git path contains a Windows drive prefix")
    if any(b":" in component for component in components):
        raise ValueError("raw Git path contains a Windows alternate data stream")
    if any(component.endswith((b".", b" ")) for component in components):
        raise ValueError("raw Git path has a Windows-ambiguous suffix")
    reserved = {b"CON", b"PRN", b"AUX", b"NUL"}
    reserved.update(f"COM{number}".encode("ascii") for number in range(1, 10))
    reserved.update(f"LPT{number}".encode("ascii") for number in range(1, 10))
    if any(component.split(b".", 1)[0].upper() in reserved for component in components):
        raise ValueError("raw Git path aliases a Windows device name")


def _path_argument(path: bytes) -> str:
    return os.fsdecode(path)


def _filesystem_path(repo: Path, path: bytes) -> bytes:
    return os.fsencode(repo) + b"/" + path


def _ensure_safe_path(repo: Path, path: bytes, *, inspect_leaf: bool = True) -> None:
    _validate_platform_path(path)
    current = os.fsencode(repo)
    components = path.split(b"/")
    for index, component in enumerate(components):
        try:
            names = os.listdir(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise RepositoryError("raw Git path parent cannot be inspected") from exc
        aliases = [name for name in names if _path_alias(name) == _path_alias(component)]
        if aliases and (len(aliases) != 1 or aliases[0] != component):
            raise RepositoryError("raw Git path has a filesystem path alias")
        if index == len(components) - 1:
            break
        current += b"/" + component
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise RepositoryError("raw Git path traverses a symlink")
        if not stat.S_ISDIR(mode):
            raise RepositoryError("raw Git path parent is not a directory")
    if not inspect_leaf:
        return
    leaf = _filesystem_path(repo, path)
    try:
        mode = os.lstat(leaf).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        target = os.readlink(leaf)
        if isinstance(target, str):
            target = os.fsencode(target)
        if target.startswith((b"/", b"\\\\")):
            raise RepositoryError("symlink target escapes the repository")
        resolved_target = (Path(os.fsdecode(leaf)).parent / os.fsdecode(target)).resolve(
            strict=False
        )
        if not resolved_target.is_relative_to(repo):
            raise RepositoryError("symlink target escapes the repository")
    elif not stat.S_ISREG(mode):
        raise RepositoryError("repository contains an unsupported special file")


def _parse_mode(value: bytes) -> int:
    try:
        mode = int(value, 8)
    except ValueError as exc:
        raise RepositoryError("Git reported an invalid file mode") from exc
    if mode == 0o160000:
        raise RepositoryError("Git submodule entries are not supported")
    if mode not in {0, MODE_REGULAR, MODE_EXECUTABLE, MODE_SYMLINK}:
        raise RepositoryError("Git reported an unsupported file mode")
    return mode


def _parse_status(output: bytes) -> tuple[_StatusEntry, ...]:
    fields = output.split(b"\0")
    entries: list[_StatusEntry] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if record.startswith(b"1 "):
            parts = record.split(b" ", 8)
            if len(parts) != 9 or len(parts[1]) != 2:
                raise RepositoryError("Git status record is malformed")
            entries.append(
                _StatusEntry(
                    path=parts[8],
                    index_status=parts[1][0:1],
                    worktree_status=parts[1][1:2],
                    head_mode=_parse_mode(parts[3]),
                    index_mode=_parse_mode(parts[4]),
                    worktree_mode=_parse_mode(parts[5]),
                    head_oid=parts[6],
                    index_oid=parts[7],
                )
            )
        elif record.startswith(b"? "):
            entries.append(
                _StatusEntry(
                    path=record[2:],
                    index_status=b".",
                    worktree_status=b"?",
                    head_mode=0,
                    index_mode=0,
                    worktree_mode=0,
                    head_oid=None,
                    index_oid=None,
                    untracked=True,
                )
            )
        elif record.startswith((b"2 ", b"u ")):
            raise RepositoryError("rename, copy, or unmerged status is unsupported")
        elif record.startswith(b"! "):
            continue
        else:
            raise RepositoryError("Git returned an unknown status record")
    return tuple(entries)


def _status_entries(repo: Path) -> tuple[_StatusEntry, ...]:
    result = _run_git(
        repo,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignored=no",
        "--no-renames",
    )
    if result.stderr:
        raise RepositoryError(
            "Git status emitted diagnostics and may have omitted repository state"
        )
    return _parse_status(result.stdout)


def _validate_full_index(
    repo: Path,
    allowed_paths: tuple[bytes, ...],
) -> None:
    result = _run_git(repo, "ls-files", "--stage", "-z")
    allowed_by_alias = {_path_alias(path): path for path in allowed_paths}
    indexed_by_alias: dict[str, bytes] = {}
    for raw_record in result.stdout.split(b"\0"):
        if not raw_record:
            continue
        try:
            raw_metadata, path = raw_record.split(b"\t", 1)
        except ValueError as exc:
            raise RepositoryError("Git index record is malformed") from exc
        metadata = raw_metadata.split()
        if len(metadata) != 3:
            raise RepositoryError("Git index metadata is malformed")
        _parse_mode(metadata[0])
        if metadata[2] != b"0":
            raise RepositoryError("unmerged Git index entries are not supported")
        _validate_platform_path(path)
        alias = _path_alias(path)
        prior = indexed_by_alias.get(alias)
        if prior is not None and prior != path:
            raise RepositoryError("Git index contains a raw path alias")
        indexed_by_alias[alias] = path
        allowed_path = allowed_by_alias.get(alias)
        if allowed_path is not None and allowed_path != path:
            raise RepositoryError("source allowlist aliases a Git index path")

    visible = _run_git(repo, "ls-files", "-v", "-z")
    for raw_record in visible.stdout.split(b"\0"):
        if not raw_record:
            continue
        if not raw_record.startswith(b"H "):
            raise RepositoryError(
                "Git index contains a concealment or unsupported visibility flag"
            )
        path = raw_record[2:]
        _validate_platform_path(path)
        if path not in indexed_by_alias.values():
            raise RepositoryError("Git index visibility manifest is inconsistent")


def _working_mode_and_content(repo: Path, path: bytes) -> tuple[int, bytes] | None:
    _ensure_safe_path(repo, path)
    filesystem_path = _filesystem_path(repo, path)
    try:
        metadata = os.lstat(filesystem_path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(filesystem_path)
        return MODE_SYMLINK, os.fsencode(target) if isinstance(target, str) else target
    if not stat.S_ISREG(metadata.st_mode):
        raise RepositoryError("repository contains an unsupported special file")
    mode = MODE_EXECUTABLE if metadata.st_mode & stat.S_IXUSR else MODE_REGULAR
    try:
        with open(filesystem_path, "rb") as stream:
            return mode, stream.read()
    except OSError as exc:
        raise RepositoryError("repository file cannot be read") from exc


def _blob_content(
    repo: Path,
    oid: bytes | None,
    *,
    extra_env: Mapping[str, str] | None = None,
) -> bytes | None:
    if oid is None or oid in _ZERO_OIDS:
        return None
    result = _run_git(
        repo,
        "cat-file",
        "blob",
        oid.decode("ascii"),
        extra_env=extra_env,
    )
    return result.stdout


def _diff_bytes(
    repo: Path,
    path: bytes,
    *,
    staged: bool,
    base_oid: bytes | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> bytes:
    arguments = ["diff"]
    if staged:
        arguments.append("--cached")
    if base_oid is not None:
        arguments.append(base_oid.decode("ascii"))
    arguments.extend(
        (
            "--binary",
            "--diff-algorithm=myers",
            "--full-index",
            "--inter-hunk-context=0",
            "--no-indent-heuristic",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--unified=3",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--",
            _path_argument(path),
        )
    )
    return _run_git(repo, *arguments, extra_env=extra_env).stdout


def _status_value(code: bytes) -> RecordStatus:
    try:
        return _STATUS_MAP[code]
    except KeyError as exc:
        raise RepositoryError("Git reported an unsupported change category") from exc


def _record_from_change(
    repo: Path,
    path: bytes,
    status_code: bytes,
    old_mode: int,
    new_mode: int,
    old_oid: bytes | None,
    new_oid: bytes | None,
    *,
    staged: bool,
    base_oid: bytes | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> EvidenceRecord:
    diff = _diff_bytes(
        repo,
        path,
        staged=staged,
        base_oid=base_oid,
        extra_env=extra_env,
    )
    old_content = _blob_content(repo, old_oid, extra_env=extra_env)
    if new_mode == 0:
        new_content = None
    elif staged:
        new_content = _blob_content(repo, new_oid, extra_env=extra_env)
    else:
        working = _working_mode_and_content(repo, path)
        if working is None:
            new_content = None
        else:
            actual_mode, new_content = working
            if actual_mode != new_mode:
                raise RepositoryError("working-tree mode changed during capture")

    status = _status_value(status_code)
    if old_mode and new_mode and old_mode != new_mode:
        status = RecordStatus.TYPE_CHANGED
    if (
        old_mode in {MODE_REGULAR, MODE_EXECUTABLE}
        and new_mode in {MODE_REGULAR, MODE_EXECUTABLE}
        and old_mode != new_mode
        and old_content == new_content
    ):
        return EvidenceRecord(
            tag=RecordTag.MODE_ONLY,
            path=path,
            status=RecordStatus.MODIFIED,
            old_mode=old_mode,
            new_mode=new_mode,
        )
    if MODE_SYMLINK in {old_mode, new_mode}:
        tag = RecordTag.SYMLINK
    elif b"\0" in (old_content or b"") or b"\0" in (new_content or b""):
        tag = RecordTag.BINARY
    elif b"GIT binary patch" in diff:
        tag = RecordTag.BINARY
    else:
        tag = RecordTag.TEXT_DIFF
    return EvidenceRecord(
        tag=tag,
        path=path,
        status=status,
        old_mode=old_mode,
        new_mode=new_mode,
        canonical_diff=diff,
        content=new_content if tag in {RecordTag.BINARY, RecordTag.SYMLINK} else None,
    )


def _untracked_record(repo: Path, path: bytes) -> EvidenceRecord:
    working = _working_mode_and_content(repo, path)
    if working is None:
        raise RepositoryError("untracked file disappeared during capture")
    mode, content = working
    tag = RecordTag.SYMLINK if mode == MODE_SYMLINK else RecordTag.UNTRACKED
    return EvidenceRecord(
        tag=tag,
        path=path,
        status=RecordStatus.UNTRACKED,
        old_mode=0,
        new_mode=mode,
        content=content,
    )


def _private_untracked_digest(repo: Path, path: bytes) -> tuple[int, int, str]:
    _ensure_safe_path(repo, path)
    filesystem_path = _filesystem_path(repo, path)
    try:
        before = os.lstat(filesystem_path)
    except FileNotFoundError as exc:
        raise RepositoryError("private file disappeared during capture") from exc
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(filesystem_path)
        content = os.fsencode(target) if isinstance(target, str) else target
        return MODE_SYMLINK, len(content), hashlib.sha256(content).hexdigest()
    if not stat.S_ISREG(before.st_mode):
        raise RepositoryError("repository contains an unsupported special file")
    digest = hashlib.sha256()
    size = 0
    try:
        with open(filesystem_path, "rb") as stream:
            while True:
                chunk = stream.read(_PRIVATE_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise RepositoryError("private file cannot be read") from exc
    try:
        after = os.lstat(filesystem_path)
    except FileNotFoundError as exc:
        raise RepositoryError("private file changed during capture") from exc
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    ) or size != before.st_size:
        raise RepositoryError("private file changed during capture")
    mode = MODE_EXECUTABLE if before.st_mode & stat.S_IXUSR else MODE_REGULAR
    return mode, size, digest.hexdigest()


def _private_working_mode(repo: Path, path: bytes) -> int | None:
    _ensure_safe_path(repo, path)
    try:
        mode = os.lstat(_filesystem_path(repo, path)).st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        return MODE_SYMLINK
    if not stat.S_ISREG(mode):
        raise RepositoryError("repository contains an unsupported special file")
    return MODE_EXECUTABLE if mode & stat.S_IXUSR else MODE_REGULAR


def _private_record(
    repo: Path,
    entries: Sequence[_StatusEntry],
) -> PrivateRecord:
    if not entries or len({entry.path for entry in entries}) != 1:
        raise RepositoryError("private status grouping is malformed")
    path = entries[0].path
    untracked_entries = [entry for entry in entries if entry.untracked]
    tracked_entries = [entry for entry in entries if not entry.untracked]
    if len(untracked_entries) > 1 or len(tracked_entries) > 1:
        raise RepositoryError("private path has duplicate status records")
    if untracked_entries and not tracked_entries:
        mode, size, digest = _private_untracked_digest(repo, path)
        return PrivateRecord(
            digest_kind=PrivateDigestKind.CONTENT,
            path=path,
            status=RecordStatus.UNTRACKED,
            mode=mode,
            size=size,
            digest=digest,
        )
    parts: list[bytes] = []
    entry = tracked_entries[0]
    if entry.index_status != b".":
        diff = _diff_bytes(repo, path, staged=True)
        parts.extend((b"STAGED\0", len(diff).to_bytes(8, "big"), diff))
    if entry.worktree_status != b".":
        diff = _diff_bytes(repo, path, staged=False)
        parts.extend((b"UNSTAGED\0", len(diff).to_bytes(8, "big"), diff))
    replacement_mode: int | None = None
    if untracked_entries:
        replacement_mode, replacement_size, replacement_digest = (
            _private_untracked_digest(repo, path)
        )
        parts.extend(
            (
                b"UNTRACKED\0",
                replacement_mode.to_bytes(4, "big"),
                replacement_size.to_bytes(8, "big"),
                bytes.fromhex(replacement_digest),
            )
        )
    canonical = b"".join(parts)
    if not canonical:
        raise RepositoryError("private change has no canonical evidence")
    mode = (
        replacement_mode
        or _private_working_mode(repo, path)
        or entry.index_mode
        or entry.head_mode
    )
    code = (
        entry.worktree_status
        if entry.worktree_status != b"."
        else entry.index_status
    )
    return PrivateRecord(
        digest_kind=PrivateDigestKind.CANONICAL_DIFF,
        path=path,
        status=(RecordStatus.MODIFIED if untracked_entries else _status_value(code)),
        mode=mode,
        size=len(canonical),
        digest=hashlib.sha256(canonical).hexdigest(),
    )


def _tracked_in_index(
    repo: Path,
    path: bytes,
    *,
    extra_env: Mapping[str, str] | None = None,
) -> bool:
    result = _run_git(
        repo,
        "ls-files",
        "-z",
        "--",
        _path_argument(path),
        extra_env=extra_env,
        check=False,
    )
    matches = tuple(value for value in result.stdout.split(b"\0") if value)
    if any(value != path for value in matches):
        raise RepositoryError("Git path lookup returned a raw path alias")
    return path in matches


def _explicit_untracked_entries(
    repo: Path,
    allowed_paths: tuple[bytes, ...],
    existing_paths: set[bytes],
) -> tuple[_StatusEntry, ...]:
    entries: list[_StatusEntry] = []
    for path in allowed_paths:
        if path in existing_paths or not os.path.lexists(_filesystem_path(repo, path)):
            continue
        if _tracked_in_index(repo, path):
            continue
        ignored = _run_git(
            repo,
            "check-ignore",
            "--stdin",
            "-z",
            input_bytes=path + b"\0",
            extra_env={"GIT_LITERAL_PATHSPECS": "0"},
            check=False,
        )
        if ignored.returncode not in {0, 1}:
            raise RepositoryError("Git check-ignore operation failed")
        entries.append(
            _StatusEntry(
                path=path,
                index_status=b".",
                worktree_status=b"?",
                head_mode=0,
                index_mode=0,
                worktree_mode=0,
                head_oid=None,
                index_oid=None,
                untracked=True,
            )
        )
    return tuple(entries)


def capture_source_snapshot(
    repo: object,
    allowed_paths: object,
) -> SourceSnapshot:
    """Capture staged, unstaged, untracked, and private state without filters."""

    repository = _resolve_repository(repo)
    _reject_repository_content_filters(repository)
    allowed = _normalize_allowed_paths(allowed_paths)
    for path in allowed:
        _ensure_safe_path(repository, path)
    _validate_full_index(repository, allowed)
    baseline = _run_git(repository, "rev-parse", "--verify", "HEAD").stdout.strip()
    entries = list(_status_entries(repository))
    allowed_by_alias = {_path_alias(path): path for path in allowed}
    status_by_alias: dict[str, bytes] = {}
    for entry in entries:
        alias = _path_alias(entry.path)
        prior = status_by_alias.get(alias)
        if prior is not None and prior != entry.path:
            raise RepositoryError("Git status contains a raw path alias")
        status_by_alias[alias] = entry.path
        allowed_path = allowed_by_alias.get(alias)
        if allowed_path is not None and allowed_path != entry.path:
            raise RepositoryError("source allowlist aliases a Git status path")
    entries.extend(
        _explicit_untracked_entries(
            repository,
            allowed,
            {entry.path for entry in entries},
        )
    )
    staged: list[EvidenceRecord] = []
    unstaged: list[EvidenceRecord] = []
    untracked: list[EvidenceRecord] = []
    private_entries: dict[bytes, list[_StatusEntry]] = {}
    allowed_set = set(allowed)

    for entry in sorted(entries, key=lambda value: value.path):
        display_git_path(entry.path)
        _ensure_safe_path(repository, entry.path)
        for mode in (entry.head_mode, entry.index_mode, entry.worktree_mode):
            if mode == 0o160000:
                raise RepositoryError("Git submodule entries are not supported")
        if entry.path not in allowed_set:
            private_entries.setdefault(entry.path, []).append(entry)
            continue
        if entry.untracked:
            untracked.append(_untracked_record(repository, entry.path))
            continue
        if entry.index_status != b".":
            staged.append(
                _record_from_change(
                    repository,
                    entry.path,
                    entry.index_status,
                    entry.head_mode,
                    entry.index_mode,
                    entry.head_oid,
                    entry.index_oid,
                    staged=True,
                )
            )
        if entry.worktree_status != b".":
            unstaged.append(
                _record_from_change(
                    repository,
                    entry.path,
                    entry.worktree_status,
                    entry.index_mode,
                    entry.worktree_mode,
                    entry.index_oid,
                    None,
                    staged=False,
                )
            )
    private = tuple(
        _private_record(repository, private_entries[path])
        for path in sorted(private_entries)
    )
    return SourceSnapshot(
        baseline_oid=baseline,
        allowed_paths=allowed,
        staged=tuple(staged),
        unstaged=tuple(unstaged),
        untracked=tuple(untracked),
        private=private,
    )


def capture_destination(repo: object, allowed_paths: object) -> SourceSnapshot:
    """Recapture the full destination using the same source representation."""

    return capture_source_snapshot(repo, allowed_paths)


def _resolved_git_path(repo: Path, *arguments: str) -> Path:
    result = _run_git(repo, "rev-parse", *arguments)
    candidate = Path(os.fsdecode(result.stdout.strip()))
    if not candidate.is_absolute():
        candidate = repo / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise RepositoryError("Git administrative path cannot be resolved") from exc


def create_worktree(
    repo: object,
    snapshot: SourceSnapshot,
    temp_root: object,
) -> WorktreeHandle:
    """Create an exact detached worktree destination at the captured commit."""

    repository = _resolve_repository(repo)
    if not isinstance(snapshot, SourceSnapshot):
        raise ValueError("snapshot must be SourceSnapshot")
    current_head = _run_git(repository, "rev-parse", "--verify", "HEAD").stdout.strip()
    if current_head != snapshot.baseline_oid:
        raise RepositoryError("source repository baseline changed")
    if not isinstance(temp_root, (str, os.PathLike)):
        raise ValueError("temp_root must be an exact filesystem path")
    destination = Path(temp_root)
    if destination.exists() or destination.is_symlink():
        raise RepositoryError("worktree destination must not already exist")
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if destination == repository or destination.is_relative_to(repository):
        raise RepositoryError("worktree destination must be outside the source repository")
    try:
        _run_git(
            repository,
            "worktree",
            "add",
            "--detach",
            str(destination),
            snapshot.baseline_oid.decode("ascii"),
        )
        worktree_head = _run_git(
            destination,
            "rev-parse",
            "--verify",
            "HEAD",
        ).stdout.strip()
        if worktree_head != snapshot.baseline_oid:
            raise RepositoryError("detached worktree baseline mismatch")
        source_object_directory = _resolved_git_path(
            repository,
            "--git-path",
            "objects",
        )
        route_state = destination.parent / "route-state"
        if route_state.is_symlink() or (
            route_state.exists() and not route_state.is_dir()
        ):
            raise RepositoryError("route-state object storage root is unsafe")
        route_state.mkdir(mode=0o700, exist_ok=True)
        object_directory = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.model-boss-objects-",
                dir=route_state,
            )
        )
    except BaseException:
        _run_git(
            repository,
            "worktree",
            "remove",
            "--force",
            str(destination),
            check=False,
        )
        raise
    return WorktreeHandle(
        source_repo=repository,
        path=destination,
        baseline_oid=snapshot.baseline_oid,
        allowed_paths=snapshot.allowed_paths,
        object_directory=object_directory,
        source_object_directory=source_object_directory,
    )


def _apply_record(repo: Path, record: EvidenceRecord, *, staged: bool) -> None:
    path_argument = _path_argument(record.path)
    if record.tag is RecordTag.MODE_ONLY:
        filesystem_path = _filesystem_path(repo, record.path)
        permissions = 0o755 if record.new_mode == MODE_EXECUTABLE else 0o644
        os.chmod(filesystem_path, permissions)
        if staged:
            _run_git(repo, "add", "--", path_argument)
        return
    arguments = ["apply"]
    if staged:
        arguments.append("--index")
    arguments.extend(("--binary", "--whitespace=nowarn", "-"))
    _run_git(repo, *arguments, input_bytes=record.canonical_diff)


def _materialize_untracked(repo: Path, record: EvidenceRecord) -> None:
    filesystem_path = _filesystem_path(repo, record.path)
    parent = os.path.dirname(filesystem_path)
    os.makedirs(parent, exist_ok=True)
    _ensure_safe_path(repo, record.path, inspect_leaf=False)
    if os.path.lexists(filesystem_path):
        raise RepositoryError("untracked materialization target already exists")
    if record.content is None:
        raise RepositoryError("untracked evidence omitted file bytes")
    if record.tag is RecordTag.SYMLINK:
        os.symlink(record.content, filesystem_path)
    else:
        descriptor = os.open(filesystem_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(record.content)
        except BaseException:
            raise
        os.chmod(
            filesystem_path,
            0o755 if record.new_mode == MODE_EXECUTABLE else 0o644,
        )


def _working_manifest(repo: Path, paths: Sequence[bytes]) -> tuple[tuple[object, ...], ...]:
    manifest: list[tuple[object, ...]] = []
    for path in paths:
        value = _working_mode_and_content(repo, path)
        if value is None:
            manifest.append((path, 0, b""))
        else:
            mode, content = value
            manifest.append((path, mode, hashlib.sha256(content).digest()))
    return tuple(manifest)


def _isolated_object_environment(
    handle: WorktreeHandle,
    *,
    index_file: str | None = None,
) -> dict[str, str]:
    environment = {
        "GIT_OBJECT_DIRECTORY": os.fspath(handle.object_directory),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.fspath(
            handle.source_object_directory
        ),
    }
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = index_file
    return environment


def _temporary_index_tree(
    handle: WorktreeHandle,
    baseline: bytes,
    paths: tuple[bytes, ...],
) -> bytes:
    repo = handle.path
    descriptor, name = tempfile.mkstemp(prefix="model-boss-index-", dir=repo.parent)
    os.close(descriptor)
    os.unlink(name)
    try:
        environment = _isolated_object_environment(handle, index_file=name)
        _run_git(repo, "read-tree", baseline.decode("ascii"), extra_env=environment)
        applicable_paths = tuple(
            path
            for path in paths
            if os.path.lexists(_filesystem_path(repo, path))
            or _tracked_in_index(repo, path, extra_env=environment)
        )
        if applicable_paths:
            _run_git(
                repo,
                "add",
                "-A",
                "-f",
                "--",
                *(_path_argument(path) for path in applicable_paths),
                extra_env=environment,
            )
        return _run_git(repo, "write-tree", extra_env=environment).stdout.strip()
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def materialize_snapshot(handle: WorktreeHandle, snapshot: SourceSnapshot) -> None:
    """Recreate captured task state without touching the original repository index."""

    if not isinstance(handle, WorktreeHandle) or not isinstance(snapshot, SourceSnapshot):
        raise ValueError("materialization requires a worktree handle and source snapshot")
    if (
        handle.baseline_oid != snapshot.baseline_oid
        or handle.allowed_paths != snapshot.allowed_paths
    ):
        raise RepositoryError("worktree handle does not match the source snapshot")
    current_source = capture_destination(handle.source_repo, snapshot.allowed_paths)
    if encode_source_snapshot(current_source) != encode_source_snapshot(snapshot):
        raise RepositoryError("source repository changed before materialization")
    if _run_git(handle.path, "rev-parse", "HEAD").stdout.strip() != snapshot.baseline_oid:
        raise RepositoryError("worktree baseline changed before materialization")

    for record in snapshot.staged:
        _apply_record(handle.path, record, staged=True)
    for record in snapshot.unstaged:
        _apply_record(handle.path, record, staged=False)
    for record in snapshot.untracked:
        _materialize_untracked(handle.path, record)

    projected = capture_source_snapshot(handle.path, snapshot.allowed_paths)
    if (
        projected.staged != snapshot.staged
        or projected.unstaged != snapshot.unstaged
        or projected.untracked != snapshot.untracked
        or projected.private
    ):
        raise RepositoryError("materialized worktree did not reproduce source evidence")
    if _working_manifest(handle.path, snapshot.allowed_paths) != _working_manifest(
        handle.source_repo,
        snapshot.allowed_paths,
    ):
        raise RepositoryError("materialized worktree bytes or modes differ from source")
    handle.materialized_tree_oid = _temporary_index_tree(
        handle,
        snapshot.baseline_oid,
        snapshot.allowed_paths,
    )


def _raw_index_changes(
    repo: Path,
    base_tree: bytes,
    paths: tuple[bytes, ...],
    environment: Mapping[str, str],
) -> tuple[EvidenceRecord, ...]:
    arguments = [
        "diff",
        "--cached",
        "--raw",
        "-z",
        "--no-renames",
        base_tree.decode("ascii"),
        "--",
        *(_path_argument(path) for path in paths),
    ]
    raw = _run_git(repo, *arguments, extra_env=environment).stdout.split(b"\0")
    records: list[EvidenceRecord] = []
    index = 0
    while index < len(raw):
        header = raw[index]
        index += 1
        if not header:
            continue
        if not header.startswith(b":") or index >= len(raw):
            raise RepositoryError("Git raw diff record is malformed")
        path = raw[index]
        index += 1
        parts = header[1:].split()
        if len(parts) != 5 or len(parts[4]) != 1:
            raise RepositoryError("Git raw diff metadata is malformed")
        old_mode = _parse_mode(parts[0])
        new_mode = _parse_mode(parts[1])
        status_code = parts[4]
        old_oid, new_oid = parts[2], parts[3]
        if old_mode == 0:
            content = _blob_content(repo, new_oid, extra_env=environment)
            if content is None:
                raise RepositoryError("new worker file omitted blob content")
            if new_mode == MODE_SYMLINK:
                records.append(
                    EvidenceRecord(
                        tag=RecordTag.SYMLINK,
                        path=path,
                        status=RecordStatus.UNTRACKED,
                        old_mode=0,
                        new_mode=new_mode,
                        content=content,
                    )
                )
            else:
                records.append(
                    EvidenceRecord(
                        tag=RecordTag.UNTRACKED,
                        path=path,
                        status=RecordStatus.UNTRACKED,
                        old_mode=0,
                        new_mode=new_mode,
                        content=content,
                    )
                )
        else:
            records.append(
                _record_from_change(
                    repo,
                    path,
                    status_code,
                    old_mode,
                    new_mode,
                    old_oid,
                    new_oid,
                    staged=True,
                    base_oid=base_tree,
                    extra_env=environment,
                )
            )
    return tuple(records)


def capture_worker_delta(
    handle: WorktreeHandle,
    snapshot: SourceSnapshot,
    allowed_paths: object,
) -> WorkerDelta:
    """Capture a worker-only net delta and its projected destination snapshot."""

    allowed = _normalize_allowed_paths(allowed_paths)
    if (
        not isinstance(handle, WorktreeHandle)
        or handle.materialized_tree_oid is None
        or not isinstance(snapshot, SourceSnapshot)
    ):
        raise RepositoryError("worker delta requires a materialized worktree")
    if allowed != snapshot.allowed_paths or allowed != handle.allowed_paths:
        raise ScopeViolationError("worker delta attempted to change its allowlist")
    projected = capture_source_snapshot(handle.path, allowed)
    if projected.private:
        raise ScopeViolationError("worker changed an out-of-scope path")
    if projected.staged != snapshot.staged:
        raise ScopeViolationError("worker changed the disposable Git index")

    descriptor, name = tempfile.mkstemp(prefix="model-boss-delta-index-", dir=handle.path.parent)
    os.close(descriptor)
    os.unlink(name)
    try:
        environment = _isolated_object_environment(handle, index_file=name)
        _run_git(
            handle.path,
            "read-tree",
            handle.materialized_tree_oid.decode("ascii"),
            extra_env=environment,
        )
        applicable_paths = tuple(
            path
            for path in allowed
            if os.path.lexists(_filesystem_path(handle.path, path))
            or _tracked_in_index(handle.path, path, extra_env=environment)
        )
        if applicable_paths:
            _run_git(
                handle.path,
                "add",
                "-A",
                "-f",
                "--",
                *(_path_argument(path) for path in applicable_paths),
                extra_env=environment,
            )
        records = _raw_index_changes(
            handle.path,
            handle.materialized_tree_oid,
            allowed,
            environment,
        )
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass

    current_source = capture_destination(handle.source_repo, snapshot.allowed_paths)
    if encode_source_snapshot(current_source) != encode_source_snapshot(snapshot):
        raise ScopeViolationError("original destination changed during worker execution")
    return WorkerDelta(records=records, projected_snapshot=projected)


def validate_worker_delta_projection(
    snapshot: SourceSnapshot,
    delta: WorkerDelta,
) -> SourceSnapshot:
    """Validate the worker cache shape without trusting it as replay evidence."""

    if not isinstance(snapshot, SourceSnapshot) or not isinstance(delta, WorkerDelta):
        raise ValueError("projection requires SourceSnapshot and WorkerDelta")
    allowed = frozenset(snapshot.allowed_paths)
    if any(record.path not in allowed for record in delta.records):
        raise ScopeViolationError("worker delta contains a path outside its allowlist")
    if delta.records and delta.projected_snapshot is None:
        raise ScopeViolationError(
            "a non-empty worker delta requires its captured projected snapshot"
        )
    projected = delta.projected_snapshot
    if projected is None:
        return SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            staged=snapshot.staged,
            unstaged=snapshot.unstaged,
            untracked=snapshot.untracked,
        )
    if projected.baseline_oid != snapshot.baseline_oid:
        raise ScopeViolationError("projected snapshot changed baseline")
    if projected.allowed_paths != snapshot.allowed_paths:
        raise ScopeViolationError("projected snapshot changed allowlist")
    if projected.private:
        raise ScopeViolationError("projected snapshot contains private records")
    if projected.staged != snapshot.staged:
        raise ScopeViolationError("projected snapshot changed the staged source state")
    return projected


def replay_worker_delta_projection(
    repo: object,
    snapshot: SourceSnapshot,
    delta: WorkerDelta,
) -> SourceSnapshot:
    """Independently derive ``delta`` on an isolated worktree and match its cache."""

    projected = validate_worker_delta_projection(snapshot, delta)
    repository = _resolve_repository(repo)
    handle: WorktreeHandle | None = None
    primary_error: BaseException | None = None
    with tempfile.TemporaryDirectory(prefix="model-boss-delta-replay-") as root:
        worktree_path = Path(root) / "worktree"
        try:
            handle = create_worktree(repository, snapshot, worktree_path)
            materialize_snapshot(handle, snapshot)
            for record in delta.records:
                if record.old_mode == 0:
                    _materialize_untracked(handle.path, record)
                else:
                    _apply_record(handle.path, record, staged=False)
            replayed = capture_source_snapshot(handle.path, snapshot.allowed_paths)
            if replayed.private:
                raise ScopeViolationError("delta replay produced private changes")
            if replayed.staged != snapshot.staged:
                raise ScopeViolationError("delta replay changed the staged source state")
            if replayed != projected:
                raise ScopeViolationError(
                    "delta replay does not match the projected snapshot"
                )
            return replayed
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if handle is not None:
                cleanup = _run_git(
                    repository,
                    "worktree",
                    "remove",
                    "--force",
                    os.fspath(handle.path),
                    check=False,
                )
                if cleanup.returncode != 0 and primary_error is None:
                    raise RepositoryError("delta replay worktree cleanup failed")


def project_task_patch(snapshot: SourceSnapshot, delta: WorkerDelta) -> CanonicalPatch:
    """Build reviewer-visible final sections while keeping worker hashing independent."""

    projected = validate_worker_delta_projection(snapshot, delta)
    generic_records = () if delta.projected_snapshot is not None else delta.records
    return CanonicalPatch(
        records=generic_records,
        staged=projected.staged,
        unstaged=projected.unstaged,
        untracked=projected.untracked,
        private_summary=snapshot.private_summary,
    )


__all__ = (
    "RepositoryError",
    "ScopeViolationError",
    "WorktreeHandle",
    "capture_destination",
    "capture_source_snapshot",
    "capture_worker_delta",
    "create_worktree",
    "materialize_snapshot",
    "project_task_patch",
    "replay_worker_delta_projection",
    "validate_worker_delta_projection",
)
