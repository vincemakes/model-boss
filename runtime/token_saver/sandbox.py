"""Verified, default-deny filesystem sandboxes for external workers.

The module deliberately separates policy construction, backend conformance, and
route launch preparation.  A route command is never executed while a backend is
being verified, and an unavailable or stale verification never yields a command
that could accidentally run without its sandbox wrapper.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .models import Route, Status, WorkerSandboxIdentity


_SHA256_LENGTH = 64
_PROBE_PREFIX = "TOKEN-SAVER-SANDBOX-PROBE"
_PROBE_KEYS = (
    "allowed_read",
    "protected_read_denied",
    "worktree_write",
    "outside_write_denied",
)
_PROBE_CODE = """\
import os
import sys

allowed_path, protected_path, protected_kind, inside_path, outside_path = sys.argv[1:6]
expected = b"token-saver-allowed-read-v1"
inside_payload = b"token-saver-inside-write-v1"

try:
    with open(allowed_path, "rb") as stream:
        allowed_read = stream.read() == expected
except OSError:
    allowed_read = False

try:
    if protected_kind == "directory":
        os.listdir(protected_path)
    else:
        with open(protected_path, "rb") as stream:
            stream.read(1)
except OSError:
    protected_read_denied = True
else:
    protected_read_denied = False

try:
    with open(inside_path, "xb") as stream:
        stream.write(inside_payload)
except OSError:
    worktree_write = False
else:
    worktree_write = True

try:
    with open(outside_path, "xb") as stream:
        stream.write(b"sandbox-escape")
except OSError:
    outside_write_denied = True
else:
    outside_write_denied = False

values = (
    allowed_read,
    protected_read_denied,
    worktree_write,
    outside_write_denied,
)
fields = ["TOKEN-SAVER-SANDBOX-PROBE"]
fields.extend(
    key + "=" + ("1" if value else "0")
    for key, value in zip(
        (
            "allowed_read",
            "protected_read_denied",
            "worktree_write",
            "outside_write_denied",
        ),
        values,
    )
)
sys.stdout.write("\\t".join(fields) + "\\n")
sys.stdout.flush()
raise SystemExit(0 if all(values) else 97)
"""


class SandboxPolicyError(ValueError):
    """Raised when filesystem roots cannot form a narrow sandbox policy."""


@dataclass(frozen=True)
class _PathIdentity:
    path: Path
    device: int
    inode: int
    file_type: int
    content_hash: str | None

    @classmethod
    def capture(cls, path: Path) -> _PathIdentity:
        metadata = os.stat(path, follow_symlinks=False)
        content_hash = None
        if stat.S_ISREG(metadata.st_mode):
            try:
                content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                after = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise SandboxPolicyError("sandbox file identity could not be captured") from exc
            if (
                after.st_dev != metadata.st_dev
                or after.st_ino != metadata.st_ino
                or after.st_size != metadata.st_size
                or after.st_mtime_ns != metadata.st_mtime_ns
                or after.st_ctime_ns != metadata.st_ctime_ns
            ):
                raise SandboxPolicyError("sandbox file changed during identity capture")
        return cls(
            path=path,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            file_type=stat.S_IFMT(metadata.st_mode),
            content_hash=content_hash,
        )

    def is_current(self) -> bool:
        try:
            metadata = os.stat(self.path, follow_symlinks=False)
        except OSError:
            return False
        unchanged = (
            metadata.st_dev == self.device
            and metadata.st_ino == self.inode
            and stat.S_IFMT(metadata.st_mode) == self.file_type
        )
        if not unchanged or self.content_hash is None:
            return unchanged
        try:
            digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
            after = os.stat(self.path, follow_symlinks=False)
        except OSError:
            return False
        return (
            digest == self.content_hash
            and after.st_dev == self.device
            and after.st_ino == self.inode
            and stat.S_IFMT(after.st_mode) == self.file_type
        )


def _validate_path_text(path: Path, field_name: str) -> None:
    try:
        encoded = os.fspath(path).encode("utf-8", "strict")
    except UnicodeError:
        raise SandboxPolicyError(
            f"{field_name} must be representable as strict UTF-8"
        ) from None
    if b"\0" in encoded or any(byte < 0x20 or byte == 0x7F for byte in encoded):
        raise SandboxPolicyError(f"{field_name} contains unsafe control characters")


def _resolve_existing_path(value: object, field_name: str) -> Path:
    if isinstance(value, bytes):
        raise SandboxPolicyError(f"{field_name} must be a text path")
    try:
        unresolved = Path(value)  # type: ignore[arg-type]
        resolved = unresolved.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise SandboxPolicyError(f"{field_name} must resolve to an existing path") from None
    _validate_path_text(resolved, field_name)
    try:
        mode = os.stat(resolved, follow_symlinks=False).st_mode
    except OSError:
        raise SandboxPolicyError(f"{field_name} must resolve to an existing path") from None
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise SandboxPolicyError(f"{field_name} must be a directory or regular file")
    return resolved


def _resolve_directory(value: object, field_name: str) -> Path:
    resolved = _resolve_existing_path(value, field_name)
    if not resolved.is_dir():
        raise SandboxPolicyError(f"{field_name} must resolve to a directory")
    return resolved


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _require_narrow_allow_root(path: Path, field_name: str) -> None:
    filesystem_root = Path(path.anchor)
    try:
        real_home = Path.home().resolve(strict=True)
    except OSError:
        real_home = Path.home().resolve(strict=False)
    if path == filesystem_root:
        raise SandboxPolicyError(f"{field_name} cannot allow the filesystem root")
    if path == real_home or path in real_home.parents:
        raise SandboxPolicyError(f"{field_name} cannot allow the real home directory")


def _canonical_roots(
    values: object,
    field_name: str,
    *,
    writable: bool,
) -> tuple[Path, ...]:
    if isinstance(values, (str, bytes, os.PathLike)):
        raise SandboxPolicyError(f"{field_name} must be a path sequence")
    try:
        members = tuple(values)  # type: ignore[arg-type]
    except TypeError:
        raise SandboxPolicyError(f"{field_name} must be a path sequence") from None
    resolved = tuple(
        (
            _resolve_directory(value, field_name)
            if writable
            else _resolve_existing_path(value, field_name)
        )
        for value in members
    )
    if len(set(resolved)) != len(resolved):
        raise SandboxPolicyError(f"{field_name} must not contain duplicate roots")
    return resolved


@dataclass(frozen=True)
class SandboxPolicy:
    """Resolved filesystem roots and network policy for one external route."""

    worktree_root: Path
    route_state_root: Path
    readable_roots: tuple[Path, ...] = ()
    protected_roots: tuple[Path, ...] = ()
    network_required: bool = False
    _identities: tuple[_PathIdentity, ...] = field(
        init=False, repr=False, compare=True
    )

    def __post_init__(self) -> None:
        if not isinstance(self.network_required, bool):
            raise SandboxPolicyError("network_required must be a boolean")
        worktree = _resolve_directory(self.worktree_root, "worktree_root")
        route_state = _resolve_directory(self.route_state_root, "route_state_root")
        readable = _canonical_roots(
            self.readable_roots, "readable_roots", writable=False
        )
        protected = _canonical_roots(
            self.protected_roots, "protected_roots", writable=False
        )
        if not protected:
            raise SandboxPolicyError("protected_roots must not be empty")

        for field_name, root in (
            ("worktree_root", worktree),
            ("route_state_root", route_state),
            *(("readable_roots", root) for root in readable),
        ):
            _require_narrow_allow_root(root, field_name)
        if _paths_overlap(worktree, route_state):
            raise SandboxPolicyError("writable roots overlap")

        writable_roots = (worktree, route_state)
        for writable_root in writable_roots:
            for readable_root in readable:
                if (
                    writable_root == readable_root
                    or readable_root in writable_root.parents
                ):
                    raise SandboxPolicyError("writable and readable roots overlap")
            for protected_root in protected:
                if _paths_overlap(writable_root, protected_root):
                    raise SandboxPolicyError("writable and protected roots overlap")
        for readable_root in readable:
            for protected_root in protected:
                if _paths_overlap(readable_root, protected_root):
                    raise SandboxPolicyError("readable and protected roots overlap")

        canonical_roots = (*writable_roots, *readable, *protected)
        identities = tuple(_PathIdentity.capture(root) for root in canonical_roots)
        object.__setattr__(self, "worktree_root", worktree)
        object.__setattr__(self, "route_state_root", route_state)
        object.__setattr__(self, "readable_roots", readable)
        object.__setattr__(self, "protected_roots", protected)
        object.__setattr__(self, "_identities", identities)

    @property
    def writable_roots(self) -> tuple[Path, Path]:
        return (self.worktree_root, self.route_state_root)

    @property
    def read_only_nested_roots(self) -> tuple[Path, ...]:
        return tuple(
            readable
            for readable in self.readable_roots
            if any(
                readable != writable and readable.is_relative_to(writable)
                for writable in self.writable_roots
            )
        )

    @property
    def current(self) -> bool:
        return all(identity.is_current() for identity in self._identities)

    @property
    def binding_hash(self) -> str:
        fields: list[bytes] = [
            b"1" if self.network_required else b"0",
        ]
        for category, roots in (
            (b"write", self.writable_roots),
            (b"read", self.readable_roots),
            (b"protect", self.protected_roots),
        ):
            fields.append(category)
            fields.append(len(roots).to_bytes(8, "big"))
            for root in roots:
                identity = next(
                    member for member in self._identities if member.path == root
                )
                fields.extend(
                    (
                        os.fsencode(root),
                        identity.device.to_bytes(16, "big", signed=False),
                        identity.inode.to_bytes(16, "big", signed=False),
                        identity.file_type.to_bytes(8, "big", signed=False),
                        (identity.content_hash or "").encode("ascii"),
                    )
                )
        return _framed_hash(b"TOKEN-SAVER-SANDBOX-POLICY\0", fields)


@dataclass(frozen=True)
class ConformanceProbe:
    allowed_read: bool
    protected_read_denied: bool
    worktree_write: bool
    outside_write_denied: bool
    complete: bool

    def __post_init__(self) -> None:
        for field_name in (
            "allowed_read",
            "protected_read_denied",
            "worktree_write",
            "outside_write_denied",
            "complete",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be a boolean")

    @property
    def passed(self) -> bool:
        return self.complete and all(
            (
                self.allowed_read,
                self.protected_read_denied,
                self.worktree_write,
                self.outside_write_denied,
            )
        )


@dataclass(frozen=True)
class SandboxLaunch:
    status: Status
    argv: tuple[str, ...] = ()
    cwd: Path | None = None
    profile_hash: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        try:
            normalized_status = Status(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("status must be a supported status") from exc
        argv = tuple(self.argv)
        if not all(isinstance(member, str) and "\0" not in member for member in argv):
            raise ValueError("argv must be a direct text argument tuple")
        if normalized_status is Status.OK:
            if not argv or self.cwd is None or self.profile_hash is None:
                raise ValueError("available launches require argv, cwd, and profile_hash")
            _require_sha256(self.profile_hash, "profile_hash")
        elif argv or self.cwd is not None:
            raise ValueError("unavailable launches cannot contain an executable command")
        object.__setattr__(self, "status", normalized_status)
        object.__setattr__(self, "argv", argv)

    @property
    def available(self) -> bool:
        return self.status is Status.OK


@dataclass(frozen=True)
class UnavailableSandbox:
    """Fail-closed result for a missing or non-conforming sandbox backend."""

    message: str
    status: Status = field(default=Status.SANDBOX_UNAVAILABLE, init=False)

    @property
    def available(self) -> bool:
        return False

    def prepare(
        self,
        *,
        route_id: str,
        argv: Sequence[str],
        policy: SandboxPolicy,
        cwd: str | os.PathLike[str],
    ) -> SandboxLaunch:
        del route_id, argv, policy, cwd
        return SandboxLaunch(status=Status.SANDBOX_UNAVAILABLE, message=self.message)


def _require_sha256(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _framed_hash(domain: bytes, fields: Sequence[bytes]) -> str:
    encoded = bytearray(domain)
    for value in fields:
        encoded.extend(len(value).to_bytes(8, "big"))
        encoded.extend(value)
    return hashlib.sha256(encoded).hexdigest()


def _root_binding_hash(domain: bytes, identity: _PathIdentity) -> str:
    return _framed_hash(
        domain,
        (
            os.fsencode(identity.path),
            identity.device.to_bytes(16, "big", signed=False),
            identity.inode.to_bytes(16, "big", signed=False),
            identity.file_type.to_bytes(8, "big", signed=False),
            (identity.content_hash or "").encode("ascii"),
        ),
    )


def _normalize_command(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)):
        raise SandboxPolicyError("argv must be an argument array")
    command = tuple(argv)
    if not command or not all(isinstance(member, str) for member in command):
        raise SandboxPolicyError("argv must be a non-empty text argument array")
    if any("\0" in member for member in command):
        raise SandboxPolicyError("argv members must not contain NUL")
    executable_name = command[0]
    if not executable_name.strip():
        raise SandboxPolicyError("argv requires an executable")
    if os.sep in executable_name or (os.altsep and os.altsep in executable_name):
        executable_path = Path(executable_name)
        if not executable_path.is_absolute():
            raise SandboxPolicyError("route executable must be absolute or discoverable")
        try:
            executable = executable_path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise SandboxPolicyError("route executable does not exist") from None
    else:
        discovered = shutil.which(executable_name)
        if discovered is None:
            raise SandboxPolicyError("route executable is unavailable")
        executable = Path(discovered).resolve(strict=True)
    _validate_path_text(executable, "route executable")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise SandboxPolicyError("route executable must be an executable regular file")
    return (os.fspath(executable), *command[1:])


def _command_binding_hash(
    *,
    backend: str,
    policy: SandboxPolicy,
    route_id: str,
    route_argv: tuple[str, ...],
    launcher_prefix: tuple[str, ...],
    profile_hash: str,
) -> str:
    return _framed_hash(
        b"TOKEN-SAVER-VERIFIED-SANDBOX\0",
        (
            backend.encode("utf-8"),
            policy.binding_hash.encode("ascii"),
            route_id.encode("utf-8"),
            len(route_argv).to_bytes(8, "big"),
            *(member.encode("utf-8") for member in route_argv),
            len(launcher_prefix).to_bytes(8, "big"),
            *(member.encode("utf-8") for member in launcher_prefix),
            profile_hash.encode("ascii"),
        ),
    )


@dataclass(frozen=True, init=False)
class VerifiedSandbox:
    """A backend proof bound to one route command and one exact root policy."""

    backend: str
    policy: SandboxPolicy
    route_id: str
    route_argv: tuple[str, ...]
    launcher_prefix: tuple[str, ...]
    profile_hash: str
    binding_hash: str
    probe: ConformanceProbe
    _route_executable_identity: _PathIdentity = field(repr=False)
    _launcher_identity: _PathIdentity = field(repr=False)

    @classmethod
    def _from_successful_probe(
        cls,
        *,
        backend: str,
        policy: SandboxPolicy,
        route_id: str,
        route_argv: Sequence[str],
        launcher_prefix: Sequence[str],
        profile_hash: str,
        probe: ConformanceProbe,
    ) -> VerifiedSandbox:
        if not isinstance(probe, ConformanceProbe) or not probe.passed:
            raise ValueError("a complete successful conformance probe is required")
        if not isinstance(policy, SandboxPolicy) or not policy.current:
            raise ValueError("policy must be a current SandboxPolicy")
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("backend must be a non-empty string")
        if not isinstance(route_id, str) or not route_id.strip() or "\0" in route_id:
            raise ValueError("route_id must be a non-empty safe string")
        normalized_command = _normalize_command(route_argv)
        prefix = tuple(launcher_prefix)
        if not prefix or not all(
            isinstance(member, str) and member and "\0" not in member
            for member in prefix
        ):
            raise ValueError("launcher_prefix must be a direct argument tuple")
        normalized_profile_hash = _require_sha256(profile_hash, "profile_hash")
        try:
            launcher = Path(prefix[0]).resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("sandbox launcher must resolve to an executable") from None
        if not launcher.is_file() or not os.access(launcher, os.X_OK):
            raise ValueError("sandbox launcher must resolve to an executable")
        if os.fspath(launcher) != prefix[0]:
            raise ValueError("sandbox launcher must already be canonical")
        binding_hash = _command_binding_hash(
            backend=backend,
            policy=policy,
            route_id=route_id,
            route_argv=normalized_command,
            launcher_prefix=prefix,
            profile_hash=normalized_profile_hash,
        )
        instance = object.__new__(cls)
        for field_name, value in (
            ("backend", backend),
            ("policy", policy),
            ("route_id", route_id),
            ("route_argv", normalized_command),
            ("launcher_prefix", prefix),
            ("profile_hash", normalized_profile_hash),
            ("binding_hash", binding_hash),
            ("probe", probe),
            (
                "_route_executable_identity",
                _PathIdentity.capture(Path(normalized_command[0])),
            ),
            ("_launcher_identity", _PathIdentity.capture(launcher)),
        ):
            object.__setattr__(instance, field_name, value)
        return instance

    @property
    def status(self) -> Status:
        return Status.OK

    @property
    def available(self) -> bool:
        return True

    @property
    def worktree_identity(self) -> str:
        return _root_binding_hash(
            b"TOKEN-SAVER-WORKTREE-IDENTITY\0",
            self.policy._identities[0],
        )

    @property
    def route_state_identity(self) -> str:
        return _root_binding_hash(
            b"TOKEN-SAVER-ROUTE-STATE-IDENTITY\0",
            self.policy._identities[1],
        )

    def worker_identity(self, route: Route) -> WorkerSandboxIdentity:
        """Bridge this live proof into the routing module's sealed identity."""

        if not isinstance(route, Route):
            raise ValueError("route must be a Route")
        try:
            route_command = _normalize_command(route.command)
        except SandboxPolicyError:
            route_command = ()
        if route.route_id != self.route_id or route_command != self.route_argv:
            raise ValueError("verified sandbox is not bound to the exact route")
        if not self.is_bound_to(
            route_id=route.route_id,
            argv=route.command,
            policy=self.policy,
            cwd=self.policy.worktree_root,
        ):
            raise ValueError("verified sandbox binding is no longer current")
        return WorkerSandboxIdentity.issue(
            route=route,
            worktree_identity=self.worktree_identity,
            route_state_identity=self.route_state_identity,
            profile_hash=self.profile_hash,
        )

    def is_bound_to(
        self,
        *,
        route_id: str,
        argv: Sequence[str],
        policy: SandboxPolicy,
        cwd: str | os.PathLike[str],
    ) -> bool:
        try:
            normalized_command = _normalize_command(argv)
            resolved_cwd = _resolve_directory(cwd, "cwd")
        except (SandboxPolicyError, TypeError, ValueError):
            return False
        expected_binding = _command_binding_hash(
            backend=self.backend,
            policy=self.policy,
            route_id=self.route_id,
            route_argv=self.route_argv,
            launcher_prefix=self.launcher_prefix,
            profile_hash=self.profile_hash,
        )
        return (
            route_id == self.route_id
            and normalized_command == self.route_argv
            and policy == self.policy
            and policy.current
            and self.policy.current
            and resolved_cwd == self.policy.worktree_root
            and self.binding_hash == expected_binding
            and self.probe.passed
            and self._route_executable_identity.is_current()
            and self._launcher_identity.is_current()
        )

    def prepare(
        self,
        *,
        route_id: str,
        argv: Sequence[str],
        policy: SandboxPolicy,
        cwd: str | os.PathLike[str],
    ) -> SandboxLaunch:
        if not self.is_bound_to(
            route_id=route_id,
            argv=argv,
            policy=policy,
            cwd=cwd,
        ):
            return SandboxLaunch(
                status=Status.SANDBOX_UNAVAILABLE,
                message="verified sandbox binding does not match this launch",
            )
        return SandboxLaunch(
            status=Status.OK,
            argv=(*self.launcher_prefix, *self.route_argv),
            cwd=self.policy.worktree_root,
            profile_hash=self.profile_hash,
            message="verified sandbox binding is current",
        )


def _deduplicate_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return tuple(result)


def _existing_runtime_roots(candidates: Sequence[str]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = Path(candidate).resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() or resolved.is_file():
            roots.append(resolved)
    return _deduplicate_paths(roots)


def _python_runtime_roots() -> tuple[Path, ...]:
    executable = Path(sys.executable).resolve(strict=True)
    try:
        standard_library = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
        common = Path(os.path.commonpath((executable, standard_library)))
    except (OSError, ValueError):
        common = executable.parent
    if common.is_file():
        common = common.parent
    return (common,)


def _macos_system_roots() -> tuple[Path, ...]:
    return _existing_runtime_roots(
        (
            "/System",
            "/usr",
            "/bin",
            "/sbin",
            "/Library/Apple/System",
            "/Library/Developer/CommandLineTools",
            "/opt/homebrew",
            "/private/var/db/dyld",
            "/private/var/select",
        )
    )


def _linux_system_layout(
    network_required: bool,
) -> tuple[tuple[Path, ...], tuple[tuple[str, Path], ...]]:
    candidates = [
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/etc/ld.so.cache",
        "/etc/ssl/certs",
    ]
    if network_required:
        candidates.extend(
            ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf")
        )
    roots = _existing_runtime_roots(candidates)
    aliases: list[tuple[str, Path]] = []
    for candidate in candidates:
        unresolved = Path(candidate)
        if not unresolved.is_symlink():
            continue
        try:
            target = os.readlink(unresolved)
        except OSError:
            continue
        aliases.append((target, unresolved))
    return roots, tuple(aliases)


def _validate_effective_reads(
    policy: SandboxPolicy,
    roots: Sequence[Path],
    executable: Path,
) -> None:
    for root in (*roots, executable):
        _require_narrow_allow_root(root, "effective readable root")
        for protected in policy.protected_roots:
            if _paths_overlap(root, protected):
                raise SandboxPolicyError(
                    "effective readable and protected roots overlap"
                )
        for writable in policy.writable_roots:
            if (
                root not in policy.writable_roots
                and _paths_overlap(root, writable)
                and not (
                    root != writable
                    and root.is_relative_to(writable)
                    and root in policy.read_only_nested_roots
                )
            ):
                raise SandboxPolicyError(
                    "provider executable/runtime overlaps a writable root"
                )


def _scheme_string(value: str) -> str:
    _validate_path_text(Path(value), "sandbox profile path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _profile_filter(path: Path) -> str:
    operation = "subpath" if path.is_dir() else "literal"
    return f"({operation} {_scheme_string(os.fspath(path))})"


def _metadata_ancestors(paths: Sequence[Path]) -> tuple[Path, ...]:
    ancestors: set[Path] = {Path("/")}
    for path in paths:
        cursor = path if path.is_dir() else path.parent
        while cursor != Path("/"):
            ancestors.add(cursor)
            cursor = cursor.parent
    return tuple(sorted(ancestors, key=lambda member: (len(member.parts), os.fspath(member))))


def render_macos_profile(
    policy: SandboxPolicy,
    executable: str | os.PathLike[str],
) -> str:
    """Render a deterministic sandbox-exec default-deny profile."""

    if not isinstance(policy, SandboxPolicy) or not policy.current:
        raise SandboxPolicyError("policy must be a current SandboxPolicy")
    resolved_executable = _resolve_existing_path(executable, "route executable")
    if not resolved_executable.is_file() or not os.access(resolved_executable, os.X_OK):
        raise SandboxPolicyError("route executable must be executable")
    read_roots = _deduplicate_paths(
        (
            *_macos_system_roots(),
            *_python_runtime_roots(),
            *policy.readable_roots,
            *policy.writable_roots,
            resolved_executable,
        )
    )
    _validate_effective_reads(policy, read_roots, resolved_executable)
    metadata_roots = _metadata_ancestors((*read_roots, *policy.writable_roots))
    read_filters = " ".join(_profile_filter(root) for root in read_roots)
    metadata_filters = " ".join(
        f"(literal {_scheme_string(os.fspath(root))})" for root in metadata_roots
    )
    write_filters = " ".join(
        f"(subpath {_scheme_string(os.fspath(root))})"
        for root in policy.writable_roots
    )
    lines = [
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process*)",
        f"(allow file-read-metadata {metadata_filters})",
        f"(allow file-read* {read_filters})",
        f"(allow file-write* {write_filters})",
    ]
    if policy.read_only_nested_roots:
        read_only_filters = " ".join(
            _profile_filter(root) for root in policy.read_only_nested_roots
        )
        lines.append(f"(deny file-write* {read_only_filters})")
    if policy.network_required:
        lines.append("(allow network*)")
    return "\n".join(lines) + "\n"


def _bwrap_parent_directories(paths: Sequence[Path]) -> tuple[Path, ...]:
    directories: set[Path] = set()
    for path in paths:
        cursor = path.parent
        while cursor != Path("/"):
            directories.add(cursor)
            cursor = cursor.parent
    return tuple(
        sorted(directories, key=lambda member: (len(member.parts), os.fspath(member)))
    )


def _bwrap_prefix(
    policy: SandboxPolicy,
    executable: Path,
    bwrap_executable: str | os.PathLike[str],
) -> tuple[str, ...]:
    system_roots, system_aliases = _linux_system_layout(policy.network_required)
    read_roots = _deduplicate_paths(
        (
            *system_roots,
            *_python_runtime_roots(),
            *policy.readable_roots,
            executable,
        )
    )
    _validate_effective_reads(policy, read_roots, executable)
    bwrap = os.fspath(bwrap_executable)
    argv: list[str] = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if policy.network_required:
        argv.append("--share-net")
    destinations = (
        *read_roots,
        *policy.writable_roots,
        *(destination for _, destination in system_aliases),
    )
    for directory in _bwrap_parent_directories(destinations):
        if directory not in (Path("/proc"), Path("/dev")):
            argv.extend(("--dir", os.fspath(directory)))
    argv.extend(("--proc", "/proc", "--dev", "/dev"))
    for target, destination in system_aliases:
        argv.extend(("--symlink", target, os.fspath(destination)))
    nested_read_only = frozenset(policy.read_only_nested_roots)
    for root in sorted(
        (root for root in read_roots if root not in nested_read_only),
        key=lambda member: (len(member.parts), os.fspath(member)),
    ):
        argv.extend(("--ro-bind", os.fspath(root), os.fspath(root)))
    for root in policy.writable_roots:
        argv.extend(("--bind", os.fspath(root), os.fspath(root)))
    for root in sorted(
        nested_read_only,
        key=lambda member: (len(member.parts), os.fspath(member)),
    ):
        argv.extend(("--ro-bind", os.fspath(root), os.fspath(root)))
    argv.extend(
        (
            "--remount-ro",
            "/",
            "--setenv",
            "HOME",
            os.fspath(policy.route_state_root),
            "--setenv",
            "TMPDIR",
            os.fspath(policy.route_state_root),
            "--chdir",
            os.fspath(policy.worktree_root),
            "--",
        )
    )
    return tuple(argv)


def build_bwrap_argv(
    policy: SandboxPolicy,
    executable: str | os.PathLike[str],
    command: Sequence[str],
    *,
    bwrap_executable: str | os.PathLike[str] = "bwrap",
) -> tuple[str, ...]:
    """Build Bubblewrap argv without invoking a shell."""

    if not isinstance(policy, SandboxPolicy) or not policy.current:
        raise SandboxPolicyError("policy must be a current SandboxPolicy")
    resolved_executable = _resolve_existing_path(executable, "route executable")
    normalized_command = _normalize_command(command)
    if normalized_command[0] != os.fspath(resolved_executable):
        raise SandboxPolicyError("command must start with the resolved route executable")
    return (
        *_bwrap_prefix(policy, resolved_executable, bwrap_executable),
        *normalized_command,
    )


def _parse_probe(stdout: bytes, returncode: int) -> ConformanceProbe:
    try:
        text = stdout.decode("utf-8", "strict")
    except UnicodeError:
        text = ""
    parts = text.removesuffix("\n").split("\t") if text.endswith("\n") else []
    complete = len(parts) == 5 and parts[0] == _PROBE_PREFIX and returncode == 0
    values: dict[str, bool] = {}
    if complete:
        for expected_key, member in zip(_PROBE_KEYS, parts[1:], strict=True):
            key, separator, value = member.partition("=")
            if key != expected_key or separator != "=" or value not in {"0", "1"}:
                complete = False
                break
            values[key] = value == "1"
    return ConformanceProbe(
        allowed_read=values.get("allowed_read", False),
        protected_read_denied=values.get("protected_read_denied", False),
        worktree_write=values.get("worktree_write", False),
        outside_write_denied=values.get("outside_write_denied", False),
        complete=complete,
    )


def _write_probe_fixture(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, payload)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _unlink_probe_file(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode):
        return
    path.unlink()


def _run_conformance_probe(
    *,
    launcher_prefix: tuple[str, ...],
    policy: SandboxPolicy,
    probe_parent: Path | None = None,
) -> ConformanceProbe:
    nonce = uuid.uuid4().hex
    allowed_path = policy.worktree_root / f".token-saver-probe-{nonce}.read"
    inside_path = policy.worktree_root / f".token-saver-probe-{nonce}.write"
    allowed_payload = b"token-saver-allowed-read-v1"
    inside_payload = b"token-saver-inside-write-v1"
    _write_probe_fixture(allowed_path, allowed_payload)
    try:
        resolved_probe_parent = None
        if probe_parent is not None:
            try:
                resolved_probe_parent = _resolve_directory(
                    probe_parent,
                    "probe_parent",
                )
            except SandboxPolicyError:
                return ConformanceProbe(False, False, False, False, False)
            if any(
                _paths_overlap(resolved_probe_parent, governed)
                for governed in (
                    *policy.writable_roots,
                    *policy.readable_roots,
                    *policy.protected_roots,
                )
            ):
                return ConformanceProbe(False, False, False, False, False)
        with tempfile.TemporaryDirectory(
            prefix="token-saver-outside-probe-",
            dir=resolved_probe_parent,
        ) as root:
            outside_root = Path(root).resolve(strict=True)
            if any(
                _paths_overlap(outside_root, allowed)
                for allowed in (
                    *policy.writable_roots,
                    *policy.readable_roots,
                )
            ):
                return ConformanceProbe(False, False, False, False, False)
            outside_path = outside_root / "outside.write"
            protected_path = policy.protected_roots[0]
            protected_kind = "directory" if protected_path.is_dir() else "file"
            probe_argv = (
                os.fspath(Path(sys.executable).resolve(strict=True)),
                "-I",
                "-S",
                "-B",
                "-c",
                _PROBE_CODE,
                os.fspath(allowed_path),
                os.fspath(protected_path),
                protected_kind,
                os.fspath(inside_path),
                os.fspath(outside_path),
            )
            environment = {
                "HOME": os.fspath(policy.route_state_root),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "TMPDIR": os.fspath(policy.route_state_root),
            }
            try:
                completed = subprocess.run(
                    (*launcher_prefix, *probe_argv),
                    cwd=policy.worktree_root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.SubprocessError):
                return ConformanceProbe(False, False, False, False, False)
            parsed = _parse_probe(completed.stdout, completed.returncode)
            try:
                allowed_unchanged = allowed_path.read_bytes() == allowed_payload
                inside_written = inside_path.read_bytes() == inside_payload
            except OSError:
                allowed_unchanged = False
                inside_written = False
            outside_absent = not outside_path.exists()
            return ConformanceProbe(
                allowed_read=parsed.allowed_read and allowed_unchanged,
                protected_read_denied=(
                    parsed.protected_read_denied and policy.current
                ),
                worktree_write=parsed.worktree_write and inside_written,
                outside_write_denied=(
                    parsed.outside_write_denied and outside_absent
                ),
                complete=parsed.complete,
            )
    finally:
        _unlink_probe_file(inside_path)
        _unlink_probe_file(allowed_path)


def _resolve_backend_executable(
    backend_name: str,
    override: str | os.PathLike[str] | None,
) -> Path | None:
    if backend_name == "macos-sandbox-exec":
        expected = Path("/usr/bin/sandbox-exec")
        candidate = Path(override) if override is not None else expected
        try:
            resolved_candidate = candidate.resolve(strict=True)
            resolved_expected = expected.resolve(strict=True)
        except OSError:
            return None
        if resolved_candidate != resolved_expected:
            return None
    else:
        discovered = shutil.which("bwrap")
        if discovered is None:
            return None
        expected = Path(discovered).resolve(strict=True)
        candidate = Path(override) if override is not None else expected
        try:
            resolved_candidate = candidate.resolve(strict=True)
        except OSError:
            return None
        if resolved_candidate != expected:
            return None
    if not resolved_candidate.is_file() or not os.access(resolved_candidate, os.X_OK):
        return None
    return resolved_candidate


def select_verified_backend(
    policy: SandboxPolicy,
    *,
    route_id: str,
    argv: Sequence[str],
    backend_executable: str | os.PathLike[str] | None = None,
    probe_parent: str | os.PathLike[str] | None = None,
) -> VerifiedSandbox | UnavailableSandbox:
    """Run a real conformance probe and bind its backend to one exact route.

    The supplied route argv is only normalized and hashed here.  The probe uses a
    fixed Token Saver helper, so the external worker cannot start before all four
    filesystem checks have passed.
    """

    if not isinstance(policy, SandboxPolicy) or not policy.current:
        raise SandboxPolicyError("policy must be a current SandboxPolicy")
    if not isinstance(route_id, str) or not route_id.strip() or "\0" in route_id:
        raise SandboxPolicyError("route_id must be a non-empty safe string")
    route_argv = _normalize_command(argv)
    route_executable = Path(route_argv[0])
    system = platform.system()
    if system == "Darwin":
        backend = "macos-sandbox-exec"
    elif system == "Linux":
        backend = "linux-bwrap"
    else:
        return UnavailableSandbox(f"no verified sandbox backend for {system or 'host'}")
    launcher = _resolve_backend_executable(backend, backend_executable)
    if launcher is None:
        return UnavailableSandbox(f"{backend} executable is unavailable")

    try:
        if backend == "macos-sandbox-exec":
            profile = render_macos_profile(policy, route_executable)
            launcher_prefix = (
                os.fspath(launcher),
                "-p",
                profile,
            )
            profile_hash = hashlib.sha256(profile.encode("utf-8")).hexdigest()
        else:
            launcher_prefix = _bwrap_prefix(policy, route_executable, launcher)
            profile_hash = _framed_hash(
                b"TOKEN-SAVER-BWRAP-PROFILE\0",
                tuple(member.encode("utf-8") for member in launcher_prefix),
            )
    except SandboxPolicyError:
        raise

    probe = _run_conformance_probe(
        launcher_prefix=launcher_prefix,
        policy=policy,
        probe_parent=Path(probe_parent) if probe_parent is not None else None,
    )
    if not probe.passed:
        return UnavailableSandbox(f"{backend} failed its filesystem conformance probe")
    return VerifiedSandbox._from_successful_probe(
        backend=backend,
        policy=policy,
        route_id=route_id,
        route_argv=route_argv,
        launcher_prefix=launcher_prefix,
        profile_hash=profile_hash,
        probe=probe,
    )


__all__ = [
    "ConformanceProbe",
    "SandboxLaunch",
    "SandboxPolicy",
    "SandboxPolicyError",
    "UnavailableSandbox",
    "VerifiedSandbox",
    "build_bwrap_argv",
    "render_macos_profile",
    "select_verified_backend",
]
