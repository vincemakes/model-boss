"""Invocation-scoped filesystem resources and conservative cleanup."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - sandboxed workers require POSIX/WSL.
    fcntl = None  # type: ignore[assignment]


_MARKER_NAMES = (
    "invocation-root",
    "worktree-path",
    "worktree-registration",
    "route-state",
    "evidence",
    "plan-evidence",
    "final-evidence",
    "delta-bundle",
)


@dataclass(frozen=True)
class InvocationResources:
    """Exact paths owned by one Model Boss invocation."""

    invocation_id: str
    repository_path: Path
    temp_parent: Path
    invocation_root: Path
    worktree_path: Path
    worktree_registration_path: Path
    route_state_path: Path
    evidence_path: Path
    plan_evidence_path: Path
    final_evidence_path: Path
    delta_bundle_path: Path
    manifest_path: Path
    marker_paths: tuple[Path, ...]
    repository_device: int = 0
    repository_inode: int = 0
    temp_parent_device: int = 0
    temp_parent_inode: int = 0
    invocation_root_device: int = 0
    invocation_root_inode: int = 0
    route_state_device: int = 0
    route_state_inode: int = 0
    evidence_device: int = 0
    evidence_inode: int = 0
    manifest_device: int = 0
    manifest_inode: int = 0
    marker_identities: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class CleanupResult:
    """Structured outcome from one cleanup attempt."""

    status: str
    invocation_id: str
    removed_paths: tuple[Path, ...] = ()
    worktree_removed: bool = False
    message: str = ""

    @property
    def cleaned(self) -> bool:
        return self.status == "cleaned"

    @property
    def rejected(self) -> bool:
        return self.status in {"rejected", "already_consumed"}


@dataclass(frozen=True)
class _RootAnchor:
    parent_fd: int
    parent_path: Path
    parent_device: int
    parent_inode: int
    root_fd: int
    root_name: str
    device: int
    inode: int


def _write_private_json(path: Path, value: dict[str, object]) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _paths_overlap(first: Path, second: Path) -> bool:
    for possible_parent, possible_child in (
        (first, second),
        (second, first),
    ):
        try:
            possible_child.relative_to(possible_parent)
        except ValueError:
            continue
        return True
    return False


def create_invocation_resources(
    repo: str | os.PathLike[str],
    temp_parent: str | os.PathLike[str],
) -> InvocationResources:
    """Create the private path layout for one invocation."""

    repository_path = Path(repo).resolve(strict=True)
    resolved_parent = Path(temp_parent).resolve(strict=True)
    if not repository_path.is_dir():
        raise ValueError("repo must resolve to a directory")
    if not resolved_parent.is_dir():
        raise ValueError("temp_parent must resolve to a directory")
    if _paths_overlap(repository_path, resolved_parent):
        raise ValueError("temp_parent must be outside the source repository")

    repository_metadata = os.stat(repository_path, follow_symlinks=False)
    parent_metadata = os.stat(resolved_parent, follow_symlinks=False)

    invocation_id = str(uuid.uuid4())
    invocation_root = resolved_parent / f"model-boss-invocation-{invocation_id}"
    root_created = False
    try:
        invocation_root.mkdir(mode=0o700)
        root_created = True
        invocation_root.chmod(0o700)
        route_state_path = invocation_root / "route-state"
        route_state_path.mkdir(mode=0o700)
        route_state_path.chmod(0o700)
        evidence_path = invocation_root / "evidence"
        evidence_path.mkdir(mode=0o700)
        evidence_path.chmod(0o700)
        root_metadata = os.stat(invocation_root, follow_symlinks=False)
        route_state_metadata = os.stat(route_state_path, follow_symlinks=False)
        evidence_metadata = os.stat(evidence_path, follow_symlinks=False)

        marker_paths = tuple(
            invocation_root / f".{name}.owner.json" for name in _MARKER_NAMES
        )

        resources = InvocationResources(
            invocation_id=invocation_id,
            repository_path=repository_path,
            temp_parent=resolved_parent,
            invocation_root=invocation_root,
            worktree_path=invocation_root / "worktree",
            worktree_registration_path=invocation_root / "worktree",
            route_state_path=route_state_path,
            evidence_path=evidence_path,
            plan_evidence_path=evidence_path / "plan.packet",
            final_evidence_path=evidence_path / "final.packet",
            delta_bundle_path=invocation_root / "worker.delta",
            manifest_path=invocation_root / "manifest.json",
            marker_paths=marker_paths,
            repository_device=repository_metadata.st_dev,
            repository_inode=repository_metadata.st_ino,
            temp_parent_device=parent_metadata.st_dev,
            temp_parent_inode=parent_metadata.st_ino,
            invocation_root_device=root_metadata.st_dev,
            invocation_root_inode=root_metadata.st_ino,
            route_state_device=route_state_metadata.st_dev,
            route_state_inode=route_state_metadata.st_ino,
            evidence_device=evidence_metadata.st_dev,
            evidence_inode=evidence_metadata.st_ino,
        )
        marker_targets = _marker_targets(resources)
        for marker_path, marker_name, target_path in zip(
            resources.marker_paths, _MARKER_NAMES, marker_targets, strict=True
        ):
            _write_private_json(
                marker_path,
                _expected_marker(resources, marker_name, target_path),
            )
        marker_identities = tuple(
            (metadata.st_dev, metadata.st_ino)
            for metadata in (
                os.stat(path, follow_symlinks=False)
                for path in resources.marker_paths
            )
        )
        resources = replace(
            resources,
            marker_identities=marker_identities,
        )
        _write_private_json(resources.manifest_path, _expected_manifest(resources))
        manifest_metadata = os.stat(resources.manifest_path, follow_symlinks=False)
        resources = replace(
            resources,
            manifest_device=manifest_metadata.st_dev,
            manifest_inode=manifest_metadata.st_ino,
        )
        return resources
    except BaseException:
        if root_created:
            try:
                metadata = os.lstat(invocation_root)
                if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
                    metadata.st_mode
                ):
                    invocation_root.chmod(0o700)
                    shutil.rmtree(invocation_root)
            except FileNotFoundError:
                pass
        raise


def _expected_manifest(resources: InvocationResources) -> dict[str, object]:
    return {
        "schema_version": 1,
        "invocation_id": resources.invocation_id,
        "repository_path": os.fspath(resources.repository_path),
        "repository_identity": {
            "device": resources.repository_device,
            "inode": resources.repository_inode,
        },
        "temp_parent": os.fspath(resources.temp_parent),
        "temp_parent_identity": {
            "device": resources.temp_parent_device,
            "inode": resources.temp_parent_inode,
        },
        "invocation_root": os.fspath(resources.invocation_root),
        "invocation_root_identity": {
            "device": resources.invocation_root_device,
            "inode": resources.invocation_root_inode,
        },
        "route_state_identity": {
            "device": resources.route_state_device,
            "inode": resources.route_state_inode,
        },
        "evidence_identity": {
            "device": resources.evidence_device,
            "inode": resources.evidence_inode,
        },
        "worktree_path": os.fspath(resources.worktree_path),
        "worktree_registration_path": os.fspath(
            resources.worktree_registration_path
        ),
        "route_state_path": os.fspath(resources.route_state_path),
        "evidence_path": os.fspath(resources.evidence_path),
        "plan_evidence_path": os.fspath(resources.plan_evidence_path),
        "final_evidence_path": os.fspath(resources.final_evidence_path),
        "delta_bundle_path": os.fspath(resources.delta_bundle_path),
        "manifest_path": os.fspath(resources.manifest_path),
        "markers": [os.fspath(path) for path in resources.marker_paths],
        "marker_identities": [
            {"device": device, "inode": inode}
            for device, inode in resources.marker_identities
        ],
        "state": "active",
    }


def _read_private_json_descriptor(
    descriptor: int, display_path: str | os.PathLike[str]
) -> dict[str, object]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"expected a regular private file: {display_path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError(f"private file mode changed: {display_path}")
    if metadata.st_nlink != 1:
        raise ValueError(f"private file hard-link count changed: {display_path}")
    getter = getattr(os, "geteuid", None)
    if getter is None or metadata.st_uid != getter():
        raise ValueError(f"private file owner changed: {display_path}")
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {display_path}")
    return value


def _read_private_json(path: Path) -> dict[str, object]:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        return _read_private_json_descriptor(descriptor, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


_ACTIVE_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "invocation_id",
        "repository_path",
        "repository_identity",
        "temp_parent",
        "temp_parent_identity",
        "invocation_root",
        "invocation_root_identity",
        "route_state_identity",
        "evidence_identity",
        "worktree_path",
        "worktree_registration_path",
        "route_state_path",
        "evidence_path",
        "plan_evidence_path",
        "final_evidence_path",
        "delta_bundle_path",
        "manifest_path",
        "markers",
        "marker_identities",
        "state",
    }
)


def _manifest_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be non-empty text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        raise ValueError(f"{field_name} must be strict UTF-8") from None
    if b"\0" in encoded or any(byte < 0x20 or byte == 0x7F for byte in encoded):
        raise ValueError(f"{field_name} contains unsafe control characters")
    return value


def _manifest_path_value(value: object, field_name: str) -> Path:
    path = Path(_manifest_text(value, field_name))
    if not path.is_absolute() or path.resolve(strict=False) != path:
        raise ValueError(f"{field_name} is not a canonical absolute path")
    return path


def _manifest_identity(
    value: object,
    field_name: str,
) -> tuple[int, int]:
    if not isinstance(value, dict) or set(value) != {"device", "inode"}:
        raise ValueError(f"{field_name} has an invalid identity schema")
    device = value["device"]
    inode = value["inode"]
    if (
        type(device) is not int
        or type(inode) is not int
        or device < 0
        or inode <= 0
    ):
        raise ValueError(f"{field_name} has an invalid filesystem identity")
    return device, inode


def load_invocation_resources(
    manifest_path: str | os.PathLike[str],
    *,
    require_registered_worktree: bool = False,
) -> InvocationResources:
    """Reconstruct and revalidate one exact active invocation manifest.

    The caller supplies only the manifest path.  Every path and filesystem
    identity is recovered from the private document, then rebound through
    directory descriptors before the value is returned.  No resource is
    consumed by this operation.
    """

    if type(require_registered_worktree) is not bool:
        raise ValueError("require_registered_worktree must be a boolean")
    supplied_path = Path(manifest_path)
    if not supplied_path.is_absolute():
        raise ValueError("manifest_path must be absolute")
    try:
        lexical = os.lstat(supplied_path)
        resolved = supplied_path.resolve(strict=True)
    except OSError:
        raise ValueError("manifest_path is unavailable") from None
    if (
        resolved != supplied_path
        or stat.S_ISLNK(lexical.st_mode)
        or not stat.S_ISREG(lexical.st_mode)
    ):
        raise ValueError("manifest_path must be a canonical regular file")

    try:
        manifest = _read_private_json(supplied_path)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        raise ValueError("manifest_path is not a valid private manifest") from error
    if set(manifest) != _ACTIVE_MANIFEST_KEYS:
        raise ValueError("manifest has an invalid schema")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("manifest has an unsupported schema version")
    if manifest["state"] != "active":
        raise ValueError("manifest is not active")

    repository_identity = _manifest_identity(
        manifest["repository_identity"], "repository_identity"
    )
    temp_parent_identity = _manifest_identity(
        manifest["temp_parent_identity"], "temp_parent_identity"
    )
    invocation_root_identity = _manifest_identity(
        manifest["invocation_root_identity"], "invocation_root_identity"
    )
    route_state_identity = _manifest_identity(
        manifest["route_state_identity"], "route_state_identity"
    )
    evidence_identity = _manifest_identity(
        manifest["evidence_identity"], "evidence_identity"
    )

    marker_values = manifest["markers"]
    identity_values = manifest["marker_identities"]
    if not isinstance(marker_values, list) or not isinstance(identity_values, list):
        raise ValueError("manifest markers have an invalid schema")
    marker_paths = tuple(
        _manifest_path_value(value, "marker_path") for value in marker_values
    )
    marker_identities = tuple(
        _manifest_identity(value, "marker_identity") for value in identity_values
    )

    resources = InvocationResources(
        invocation_id=_manifest_text(manifest["invocation_id"], "invocation_id"),
        repository_path=_manifest_path_value(
            manifest["repository_path"], "repository_path"
        ),
        temp_parent=_manifest_path_value(manifest["temp_parent"], "temp_parent"),
        invocation_root=_manifest_path_value(
            manifest["invocation_root"], "invocation_root"
        ),
        worktree_path=_manifest_path_value(
            manifest["worktree_path"], "worktree_path"
        ),
        worktree_registration_path=_manifest_path_value(
            manifest["worktree_registration_path"],
            "worktree_registration_path",
        ),
        route_state_path=_manifest_path_value(
            manifest["route_state_path"], "route_state_path"
        ),
        evidence_path=_manifest_path_value(
            manifest["evidence_path"], "evidence_path"
        ),
        plan_evidence_path=_manifest_path_value(
            manifest["plan_evidence_path"], "plan_evidence_path"
        ),
        final_evidence_path=_manifest_path_value(
            manifest["final_evidence_path"], "final_evidence_path"
        ),
        delta_bundle_path=_manifest_path_value(
            manifest["delta_bundle_path"], "delta_bundle_path"
        ),
        manifest_path=_manifest_path_value(
            manifest["manifest_path"], "manifest_path"
        ),
        marker_paths=marker_paths,
        repository_device=repository_identity[0],
        repository_inode=repository_identity[1],
        temp_parent_device=temp_parent_identity[0],
        temp_parent_inode=temp_parent_identity[1],
        invocation_root_device=invocation_root_identity[0],
        invocation_root_inode=invocation_root_identity[1],
        route_state_device=route_state_identity[0],
        route_state_inode=route_state_identity[1],
        evidence_device=evidence_identity[0],
        evidence_inode=evidence_identity[1],
        manifest_device=lexical.st_dev,
        manifest_inode=lexical.st_ino,
        marker_identities=marker_identities,
    )
    if resources.manifest_path != supplied_path:
        raise ValueError("supplied manifest does not match its recorded path")

    anchor: _RootAnchor | None = None
    parent_fd: int | None = None
    try:
        _validate_base_identity(resources)
        parent_fd = _open_parent_anchor(resources)
        anchor = _open_root_anchor(resources, parent_fd, allow_missing=False)
        if anchor is None:  # pragma: no cover - allow_missing is false.
            raise ValueError("invocation_root is unavailable")
        _validate_active_resources(resources, anchor)
        if require_registered_worktree:
            registered = _registered_worktrees(resources.repository_path)
            _validate_active_resources(resources, anchor)
            if resources.worktree_registration_path not in registered:
                raise ValueError("invocation worktree is not registered")
        return resources
    except (
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        TypeError,
        ValueError,
    ) as error:
        raise ValueError("invocation manifest validation failed") from error
    finally:
        _close_root_anchor(anchor)
        if parent_fd is not None:
            os.close(parent_fd)


def _require_direct_child_name(name: str) -> None:
    if name in {"", ".", ".."} or Path(name).name != name:
        raise ValueError("private file is not an exact directory child")


def _write_private_json_at(
    parent_fd: int, name: str, value: dict[str, object]
) -> None:
    _require_direct_child_name(name)
    descriptor = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise


def _read_private_json_at(parent_fd: int, name: str) -> dict[str, object]:
    _require_direct_child_name(name)
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        return _read_private_json_descriptor(descriptor, name)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _read_bound_private_json_at(
    parent_fd: int,
    name: str,
    expected_device: int,
    expected_inode: int,
    field_name: str,
) -> dict[str, object]:
    _require_direct_child_name(name)
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        before = os.fstat(descriptor)
        getter = getattr(os, "geteuid", None)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or getter is None
            or before.st_uid != getter()
            or before.st_dev != expected_device
            or before.st_ino != expected_inode
        ):
            raise ValueError(f"{field_name} changed filesystem identity")
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError(f"expected a JSON object: {field_name}")
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        for candidate in (after, current):
            if (
                candidate.st_dev != before.st_dev
                or candidate.st_ino != before.st_ino
                or candidate.st_mode != before.st_mode
                or candidate.st_nlink != before.st_nlink
                or candidate.st_uid != before.st_uid
                or candidate.st_size != before.st_size
                or candidate.st_mtime_ns != before.st_mtime_ns
                or candidate.st_ctime_ns != before.st_ctime_ns
            ):
                raise ValueError(f"{field_name} changed while being read")
        return value
    finally:
        os.close(descriptor)


def _require_contained(path: Path, parent: Path, field_name: str) -> None:
    if not path.is_absolute() or path.resolve(strict=False) != path:
        raise ValueError(f"{field_name} is not a canonical recorded path")
    try:
        path.relative_to(parent)
    except ValueError:
        raise ValueError(f"{field_name} escapes its recorded parent") from None


def _require_real_directory(path: Path, field_name: str) -> os.stat_result:
    metadata = os.lstat(path)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{field_name} is not the recorded directory")
    if path.resolve(strict=True) != path:
        raise ValueError(f"{field_name} changed identity")
    return metadata


def _require_directory_identity(
    path: Path,
    expected_device: int,
    expected_inode: int,
    field_name: str,
) -> None:
    if (
        not isinstance(expected_device, int)
        or isinstance(expected_device, bool)
        or not isinstance(expected_inode, int)
        or isinstance(expected_inode, bool)
        or expected_device < 0
        or expected_inode <= 0
    ):
        raise ValueError(f"{field_name} has no recorded filesystem identity")
    metadata = _require_real_directory(path, field_name)
    if (
        metadata.st_dev != expected_device
        or metadata.st_ino != expected_inode
    ):
        raise ValueError(f"{field_name} changed filesystem identity")


def _require_private_directory_identity(
    path: Path,
    expected_device: int,
    expected_inode: int,
    field_name: str,
) -> None:
    _require_directory_identity(path, expected_device, expected_inode, field_name)
    metadata = os.lstat(path)
    getter = getattr(os, "geteuid", None)
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError(f"{field_name} private mode changed")
    if metadata.st_nlink < 1:
        raise ValueError(f"{field_name} link count changed")
    if getter is None or metadata.st_uid != getter():
        raise ValueError(f"{field_name} owner changed")


def _require_private_file_identity_at(
    parent_fd: int,
    name: str,
    expected_device: int,
    expected_inode: int,
    field_name: str,
) -> None:
    if (
        not isinstance(expected_device, int)
        or isinstance(expected_device, bool)
        or not isinstance(expected_inode, int)
        or isinstance(expected_inode, bool)
        or expected_device < 0
        or expected_inode <= 0
    ):
        raise ValueError(f"{field_name} has no recorded filesystem identity")
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    getter = getattr(os, "geteuid", None)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{field_name} is not the recorded private file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError(f"{field_name} private mode changed")
    if metadata.st_nlink != 1:
        raise ValueError(f"{field_name} hard-link count changed")
    if getter is None or metadata.st_uid != getter():
        raise ValueError(f"{field_name} owner changed")
    if metadata.st_dev != expected_device or metadata.st_ino != expected_inode:
        raise ValueError(f"{field_name} changed filesystem identity")


def _git_environment() -> dict[str, str]:
    return {
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git executable is unavailable")
    return subprocess.run(
        [
            executable,
            "--no-pager",
            "-C",
            os.fspath(repository),
            *arguments,
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
    )


def _registered_worktrees(repository: Path) -> set[Path]:
    output = _run_git(
        repository, "worktree", "list", "--porcelain", "-z"
    ).stdout
    registered: set[Path] = set()
    for field in output.split(b"\0"):
        if field.startswith(b"worktree "):
            registered.add(Path(os.fsdecode(field[len(b"worktree ") :])))
    return registered


def _marker_targets(resources: InvocationResources) -> tuple[Path, ...]:
    return (
        resources.invocation_root,
        resources.worktree_path,
        resources.worktree_registration_path,
        resources.route_state_path,
        resources.evidence_path,
        resources.plan_evidence_path,
        resources.final_evidence_path,
        resources.delta_bundle_path,
    )


def _expected_marker(
    resources: InvocationResources, marker_name: str, target_path: Path
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "invocation_id": resources.invocation_id,
        "kind": marker_name,
        "target_path": os.fspath(target_path),
    }


def _validate_base_identity(resources: InvocationResources) -> None:
    try:
        invocation_uuid = uuid.UUID(resources.invocation_id)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("invocation_id is not a UUID") from None
    if invocation_uuid.version != 4 or str(invocation_uuid) != resources.invocation_id:
        raise ValueError("invocation_id is not the recorded UUID4")

    _require_directory_identity(
        resources.repository_path,
        resources.repository_device,
        resources.repository_inode,
        "repository_path",
    )
    _require_directory_identity(
        resources.temp_parent,
        resources.temp_parent_device,
        resources.temp_parent_inode,
        "temp_parent",
    )
    _require_contained(
        resources.invocation_root, resources.temp_parent, "invocation_root"
    )
    if _paths_overlap(resources.repository_path, resources.temp_parent):
        raise ValueError("owned temporary paths overlap the source repository")

    for owned_root in (resources.invocation_root, resources.worktree_path):
        if _paths_overlap(resources.repository_path, owned_root):
            raise ValueError("owned paths overlap the source repository")


def _validate_recorded_paths(resources: InvocationResources) -> None:
    if resources.worktree_registration_path != resources.worktree_path:
        raise ValueError("worktree registration does not match its exact path")
    if resources.worktree_path == resources.repository_path:
        raise ValueError("the source repository is never an owned worktree")

    expected_root = resources.temp_parent / (
        f"model-boss-invocation-{resources.invocation_id}"
    )
    expected_paths = {
        "invocation_root": expected_root,
        "worktree_path": expected_root / "worktree",
        "worktree_registration_path": expected_root / "worktree",
        "route_state_path": expected_root / "route-state",
        "evidence_path": expected_root / "evidence",
        "plan_evidence_path": expected_root / "evidence" / "plan.packet",
        "final_evidence_path": expected_root / "evidence" / "final.packet",
        "delta_bundle_path": expected_root / "worker.delta",
        "manifest_path": expected_root / "manifest.json",
    }
    for field_name, expected_path in expected_paths.items():
        if getattr(resources, field_name) != expected_path:
            raise ValueError(f"{field_name} does not match the invocation layout")
    expected_markers = tuple(
        expected_root / f".{name}.owner.json" for name in _MARKER_NAMES
    )
    if resources.marker_paths != expected_markers:
        raise ValueError("marker paths do not match the invocation layout")

    if resources.route_state_path.exists():
        _require_real_directory(resources.route_state_path, "route_state_path")
    if resources.evidence_path.exists():
        _require_real_directory(resources.evidence_path, "evidence_path")
    if resources.worktree_path.exists():
        _require_real_directory(resources.worktree_path, "worktree_path")


def _require_anchored_directory(
    anchor: _RootAnchor,
    resources: InvocationResources,
    path: Path,
    field_name: str,
    *,
    required: bool,
    expected_device: int | None = None,
    expected_inode: int | None = None,
    private: bool = False,
) -> None:
    name = _owned_leaf(resources, path)
    try:
        metadata = os.stat(name, dir_fd=anchor.root_fd, follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise ValueError(f"{field_name} disappeared before cleanup") from None
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{field_name} is not the recorded directory")
    if expected_device is not None and expected_inode is not None and (
        metadata.st_dev != expected_device or metadata.st_ino != expected_inode
    ):
        raise ValueError(f"{field_name} changed filesystem identity")
    if private:
        getter = getattr(os, "geteuid", None)
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError(f"{field_name} private mode changed")
        if metadata.st_nlink < 1:
            raise ValueError(f"{field_name} link count changed")
        if getter is None or metadata.st_uid != getter():
            raise ValueError(f"{field_name} owner changed")


def _validate_active_resources(
    resources: InvocationResources, anchor: _RootAnchor
) -> None:
    _validate_base_identity(resources)
    _require_anchor_current(anchor)
    _validate_recorded_paths(resources)
    _require_anchored_directory(
        anchor,
        resources,
        resources.route_state_path,
        "route_state_path",
        required=True,
        expected_device=resources.route_state_device,
        expected_inode=resources.route_state_inode,
        private=True,
    )
    _require_anchored_directory(
        anchor,
        resources,
        resources.evidence_path,
        "evidence_path",
        required=True,
        expected_device=resources.evidence_device,
        expected_inode=resources.evidence_inode,
        private=True,
    )
    _require_anchored_directory(
        anchor,
        resources,
        resources.worktree_path,
        "worktree_path",
        required=False,
    )

    _require_private_file_identity_at(
        anchor.root_fd,
        resources.manifest_path.name,
        resources.manifest_device,
        resources.manifest_inode,
        "manifest_path",
    )
    manifest = _read_bound_private_json_at(
        anchor.root_fd,
        resources.manifest_path.name,
        resources.manifest_device,
        resources.manifest_inode,
        "manifest_path",
    )
    if manifest != _expected_manifest(resources):
        raise ValueError("manifest does not match the invocation resources")

    marker_targets = _marker_targets(resources)
    if not len(resources.marker_paths) == len(_MARKER_NAMES) == len(marker_targets):
        raise ValueError("marker count does not match the owned paths")
    if len(resources.marker_identities) != len(resources.marker_paths):
        raise ValueError("marker identities do not match the marker paths")
    for marker_path, marker_name, target_path, identity in zip(
        resources.marker_paths,
        _MARKER_NAMES,
        marker_targets,
        resources.marker_identities,
        strict=True,
    ):
        _require_contained(marker_path, resources.invocation_root, "marker_path")
        _require_private_file_identity_at(
            anchor.root_fd,
            marker_path.name,
            identity[0],
            identity[1],
            "marker_path",
        )
        marker = _read_bound_private_json_at(
            anchor.root_fd,
            marker_path.name,
            identity[0],
            identity[1],
            "marker_path",
        )
        if marker != _expected_marker(resources, marker_name, target_path):
            raise ValueError("marker does not match its exact owned path")


def _consumed_manifest_path(resources: InvocationResources) -> Path:
    return resources.temp_parent / (
        f".model-boss-consumed-{resources.invocation_id}.json"
    )


def _seal_receipt_path(resources: InvocationResources) -> Path:
    return resources.temp_parent / (
        f".model-boss-sealed-{resources.invocation_id}.json"
    )


def _seal_receipt_final_path(resources: InvocationResources) -> Path:
    return resources.temp_parent / (
        f".model-boss-sealed-final-{resources.invocation_id}.json"
    )


def _remove_external_seal_files(
    resources: InvocationResources,
    parent_fd: int,
    removed_paths: list[Path],
) -> None:
    getter = getattr(os, "geteuid", None)
    if getter is None:
        raise ValueError("seal receipt ownership checks are unavailable")
    for path in (
        _seal_receipt_path(resources),
        _seal_receipt_final_path(resources),
    ):
        name = path.name
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
            or metadata.st_nlink != 1
            or metadata.st_uid != getter()
        ):
            raise ValueError("external seal receipt is not a private regular file")
        if fcntl is None:
            raise ValueError("external seal receipt locking is unavailable")
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_mode != metadata.st_mode
                or opened.st_nlink != metadata.st_nlink
            ):
                raise ValueError("external seal receipt changed before cleanup")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("external seal receipt is still active") from None
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                current.st_dev != metadata.st_dev
                or current.st_ino != metadata.st_ino
                or current.st_mode != metadata.st_mode
                or current.st_nlink != metadata.st_nlink
            ):
                raise ValueError("external seal receipt changed before cleanup")
            os.unlink(name, dir_fd=parent_fd)
            removed_paths.append(path)
        finally:
            os.close(descriptor)
    os.fsync(parent_fd)


def _consumed_manifest_name(resources: InvocationResources) -> str:
    return _consumed_manifest_path(resources).name


def _consumption_receipt(
    resources: InvocationResources, state: str, phase: str
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "invocation_id": resources.invocation_id,
        "active_manifest": _expected_manifest(resources),
        "phase": phase,
        "state": state,
    }


def _existing_consumption_phase(
    resources: InvocationResources, parent_fd: int
) -> str | None:
    _require_parent_current(parent_fd, resources)
    try:
        receipt = _read_private_json_at(
            parent_fd, _consumed_manifest_name(resources)
        )
    except FileNotFoundError:
        return None
    states_and_phases = (
        ("consuming", "claimed"),
        ("consuming", "contents_removed"),
        ("consumed", "complete"),
    )
    for state, phase in states_and_phases:
        if receipt == _consumption_receipt(resources, state, phase):
            return phase
    raise ValueError("consumption receipt does not match the invocation")


def _replace_consumption_receipt(
    resources: InvocationResources,
    parent_fd: int,
    *,
    state: str,
    phase: str,
    temporary_label: str,
) -> None:
    _require_parent_current(parent_fd, resources)
    consumed_name = _consumed_manifest_name(resources)
    completed_name = (
        f".model-boss-consumed-{temporary_label}-{resources.invocation_id}.json"
    )
    completed_receipt = _consumption_receipt(resources, state, phase)
    try:
        _write_private_json_at(parent_fd, completed_name, completed_receipt)
    except FileExistsError:
        if _read_private_json_at(parent_fd, completed_name) != completed_receipt:
            raise ValueError("completed consumption receipt does not match") from None
    _require_parent_current(parent_fd, resources)
    os.replace(
        completed_name,
        consumed_name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )


def _mark_contents_removed(
    resources: InvocationResources, parent_fd: int
) -> None:
    _require_parent_current(parent_fd, resources)
    active_receipt = _read_private_json_at(
        parent_fd, _consumed_manifest_name(resources)
    )
    if active_receipt != _consumption_receipt(resources, "consuming", "claimed"):
        raise ValueError("consumption claim changed before progress update")
    _replace_consumption_receipt(
        resources,
        parent_fd,
        state="consuming",
        phase="contents_removed",
        temporary_label="contents",
    )


def _finish_consumption_receipt(
    resources: InvocationResources, parent_fd: int
) -> None:
    _require_parent_current(parent_fd, resources)
    active_receipt = _read_private_json_at(
        parent_fd, _consumed_manifest_name(resources)
    )
    if active_receipt != _consumption_receipt(
        resources, "consuming", "contents_removed"
    ):
        raise ValueError("consumption progress is not ready to finish")
    _replace_consumption_receipt(
        resources,
        parent_fd,
        state="consumed",
        phase="complete",
        temporary_label="complete",
    )


def _require_parent_anchor_current(
    parent_fd: int,
    parent_path: Path,
    expected_device: int,
    expected_inode: int,
) -> None:
    anchored = os.fstat(parent_fd)
    if (
        not stat.S_ISDIR(anchored.st_mode)
        or anchored.st_dev != expected_device
        or anchored.st_ino != expected_inode
    ):
        raise ValueError("temp_parent changed during cleanup")
    try:
        current = os.lstat(parent_path)
    except FileNotFoundError:
        raise ValueError("temp_parent changed during cleanup") from None
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or current.st_dev != expected_device
        or current.st_ino != expected_inode
        or parent_path.resolve(strict=True) != parent_path
    ):
        raise ValueError("temp_parent changed during cleanup")


def _require_parent_current(
    parent_fd: int, resources: InvocationResources
) -> None:
    _require_parent_anchor_current(
        parent_fd,
        resources.temp_parent,
        resources.temp_parent_device,
        resources.temp_parent_inode,
    )


def _open_parent_anchor(resources: InvocationResources) -> int:
    open_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(
        resources.temp_parent,
        open_flags | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        _require_parent_current(descriptor, resources)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_repository_current(resources: InvocationResources) -> None:
    _require_directory_identity(
        resources.repository_path,
        resources.repository_device,
        resources.repository_inode,
        "repository_path",
    )


def _open_root_anchor(
    resources: InvocationResources,
    parent_fd: int,
    *,
    allow_missing: bool,
) -> _RootAnchor | None:
    if resources.invocation_root.parent != resources.temp_parent:
        raise ValueError("invocation_root must be an immediate temporary child")
    _require_parent_current(parent_fd, resources)
    open_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(
            resources.invocation_root.name,
            open_flags | no_follow,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ValueError("invocation_root disappeared before cleanup") from None

    try:
        root_metadata = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_dev != resources.invocation_root_device
            or root_metadata.st_ino != resources.invocation_root_inode
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
            or root_metadata.st_nlink < 1
            or getattr(os, "geteuid", lambda: -1)() != root_metadata.st_uid
        ):
            raise ValueError("invocation_root changed filesystem identity")
        anchor = _RootAnchor(
            parent_fd=parent_fd,
            parent_path=resources.temp_parent,
            parent_device=resources.temp_parent_device,
            parent_inode=resources.temp_parent_inode,
            root_fd=root_fd,
            root_name=resources.invocation_root.name,
            device=root_metadata.st_dev,
            inode=root_metadata.st_ino,
        )
        _require_anchor_current(anchor)
        return anchor
    except BaseException:
        os.close(root_fd)
        raise


def _require_anchor_current(anchor: _RootAnchor) -> None:
    _require_parent_anchor_current(
        anchor.parent_fd,
        anchor.parent_path,
        anchor.parent_device,
        anchor.parent_inode,
    )
    try:
        current = os.stat(
            anchor.root_name,
            dir_fd=anchor.parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        raise ValueError("invocation_root changed during cleanup") from None
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != anchor.device
        or current.st_ino != anchor.inode
        or stat.S_IMODE(current.st_mode) != 0o700
        or current.st_nlink < 1
        or getattr(os, "geteuid", lambda: -1)() != current.st_uid
    ):
        raise ValueError("invocation_root changed during cleanup")


def _close_root_anchor(anchor: _RootAnchor | None) -> None:
    if anchor is None:
        return
    os.close(anchor.root_fd)


def _owned_leaf(resources: InvocationResources, path: Path) -> str:
    if path.parent != resources.invocation_root or path.name in {"", ".", ".."}:
        raise ValueError("owned path is not an exact invocation-root child")
    return path.name


def _require_registered_worktree_leaf(
    anchor: _RootAnchor, resources: InvocationResources
) -> None:
    name = _owned_leaf(resources, resources.worktree_registration_path)
    try:
        metadata = os.stat(name, dir_fd=anchor.root_fd, follow_symlinks=False)
    except FileNotFoundError:
        # Git can retain a valid worktree registration after the directory was
        # removed out-of-band.  There is no leaf to authenticate in that case,
        # but cleanup must still remove the exact recorded registration.
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("registered worktree path changed identity")


def _remove_owned_directory(
    anchor: _RootAnchor,
    resources: InvocationResources,
    path: Path,
    removed_paths: list[Path],
) -> None:
    _require_anchor_current(anchor)
    name = _owned_leaf(resources, path)
    try:
        metadata = os.stat(name, dir_fd=anchor.root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"refusing to remove a replaced directory: {path}")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("safe directory removal is unavailable")
    shutil.rmtree(name, dir_fd=anchor.root_fd)
    removed_paths.append(path)


def _unlink_owned_file(
    anchor: _RootAnchor,
    resources: InvocationResources,
    path: Path,
    removed_paths: list[Path],
) -> None:
    _require_anchor_current(anchor)
    name = _owned_leaf(resources, path)
    try:
        os.unlink(name, dir_fd=anchor.root_fd)
    except FileNotFoundError:
        return
    removed_paths.append(path)


def _remove_empty_anchored_root(
    anchor: _RootAnchor,
    resources: InvocationResources,
    removed_paths: list[Path],
) -> None:
    _require_anchor_current(anchor)
    try:
        os.rmdir(anchor.root_name, dir_fd=anchor.parent_fd)
    except OSError as error:
        if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            return
        raise
    removed_paths.append(resources.invocation_root)


def _require_owned_contents_absent(
    anchor: _RootAnchor, resources: InvocationResources
) -> None:
    _require_anchor_current(anchor)
    owned_paths = (
        resources.worktree_path,
        resources.route_state_path,
        resources.evidence_path,
        resources.delta_bundle_path,
        *resources.marker_paths,
        resources.manifest_path,
    )
    for path in owned_paths:
        name = _owned_leaf(resources, path)
        try:
            os.stat(name, dir_fd=anchor.root_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise ValueError(f"owned path reappeared after removal: {path}")


def cleanup_invocation(resources: InvocationResources) -> CleanupResult:
    """Consume an invocation manifest and remove only its recorded resources."""

    if not isinstance(resources, InvocationResources):
        return CleanupResult(
            status="rejected",
            invocation_id="",
            message="resources must be InvocationResources",
        )
    anchor: _RootAnchor | None = None
    parent_fd: int | None = None
    try:
        _validate_base_identity(resources)
        parent_fd = _open_parent_anchor(resources)
        consumption_phase = _existing_consumption_phase(resources, parent_fd)
        if consumption_phase == "complete":
            return CleanupResult(
                status="already_consumed",
                invocation_id=resources.invocation_id,
                message="invocation manifest was already consumed",
            )
        if consumption_phase is None:
            anchor = _open_root_anchor(
                resources, parent_fd, allow_missing=False
            )
            if anchor is None:
                raise ValueError("invocation_root disappeared before cleanup")
            _validate_active_resources(resources, anchor)
            _require_repository_current(resources)
            _require_parent_current(parent_fd, resources)
            registered_worktrees = _registered_worktrees(resources.repository_path)
            # The Git probe is an external scheduling point. Revalidate every
            # ownership proof before claiming or deleting anything.
            _require_repository_current(resources)
            _validate_active_resources(resources, anchor)
            _write_private_json_at(
                parent_fd,
                _consumed_manifest_name(resources),
                _consumption_receipt(resources, "consuming", "claimed"),
            )
            consumption_phase = "claimed"
        else:
            _validate_recorded_paths(resources)
            anchor = _open_root_anchor(
                resources,
                parent_fd,
                allow_missing=consumption_phase == "contents_removed",
            )
            _require_repository_current(resources)
            _require_parent_current(parent_fd, resources)
            registered_worktrees = _registered_worktrees(resources.repository_path)
            _require_repository_current(resources)
            _require_parent_current(parent_fd, resources)
            if anchor is not None:
                _require_anchor_current(anchor)

        worktree_removed = resources.worktree_registration_path in registered_worktrees
        if consumption_phase == "contents_removed" and worktree_removed:
            raise ValueError("worktree registration reappeared after removal")
        if worktree_removed:
            if anchor is None:
                raise ValueError("registered worktree lost its anchored root")
            _require_repository_current(resources)
            _require_anchor_current(anchor)
            _require_registered_worktree_leaf(anchor, resources)
            _run_git(
                resources.repository_path,
                "worktree",
                "remove",
                "--force",
                os.fspath(resources.worktree_registration_path),
            )
            _require_repository_current(resources)
            _require_anchor_current(anchor)

        removed_paths: list[Path] = []
        if anchor is not None and consumption_phase == "claimed":
            _require_anchor_current(anchor)
            _remove_owned_directory(
                anchor, resources, resources.worktree_path, removed_paths
            )
            _remove_owned_directory(
                anchor, resources, resources.route_state_path, removed_paths
            )
            _remove_owned_directory(
                anchor, resources, resources.evidence_path, removed_paths
            )
            _unlink_owned_file(
                anchor, resources, resources.delta_bundle_path, removed_paths
            )
            for marker_path in resources.marker_paths:
                _unlink_owned_file(anchor, resources, marker_path, removed_paths)

            _unlink_owned_file(
                anchor, resources, resources.manifest_path, removed_paths
            )
            _mark_contents_removed(resources, parent_fd)
            consumption_phase = "contents_removed"

        if anchor is not None and consumption_phase == "contents_removed":
            _remove_external_seal_files(resources, parent_fd, removed_paths)
            _require_owned_contents_absent(anchor, resources)
            _remove_empty_anchored_root(anchor, resources, removed_paths)
        _finish_consumption_receipt(resources, parent_fd)
        return CleanupResult(
            status="cleaned",
            invocation_id=resources.invocation_id,
            removed_paths=tuple(removed_paths),
            worktree_removed=worktree_removed,
        )
    except (
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        TypeError,
        ValueError,
    ) as error:
        return CleanupResult(
            status="rejected",
            invocation_id=resources.invocation_id,
            message=str(error),
        )
    finally:
        _close_root_anchor(anchor)
        if parent_fd is not None:
            os.close(parent_fd)
