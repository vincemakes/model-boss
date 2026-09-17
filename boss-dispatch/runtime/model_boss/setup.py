"""Data-only credential migration and provider wrapper setup."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


CREDENTIALS_VERSION = 1
LEGACY_KEYS = frozenset(
    {
        "KIMI_BASE_URL",
        "KIMI_AUTH_TOKEN",
        "GLM_BASE_URL",
        "GLM_AUTH_TOKEN",
        "GLM_MODEL",
        "GLM_SMALL_FAST_MODEL",
    }
)
WRAPPER_SPECS = {
    "claude-kimi": ("kimi", "safe"),
    "claude-kimi-bypass": ("kimi", "sandboxed-worker"),
    "claude-glm": ("glm", "safe"),
    "claude-glm-bypass": ("glm", "sandboxed-worker"),
    "claude-glm-turbo": ("glm-turbo", "safe"),
    "claude-glm-turbo-bypass": ("glm-turbo", "sandboxed-worker"),
}


class SetupError(ValueError):
    """A setup failure whose message never contains credential values."""


@dataclass(frozen=True)
class SetupResult:
    status: str
    destination: Path

    def __post_init__(self) -> None:
        if self.status not in {"migrated", "already_configured", "configured"}:
            raise ValueError("unsupported setup status")


def parse_legacy_env(raw: bytes) -> dict[str, str]:
    """Parse the narrow legacy file without shell syntax or interpolation."""

    if type(raw) is not bytes:
        raise SetupError("legacy credential source must be bytes")
    if b"\0" in raw:
        raise SetupError("legacy credential source contains NUL")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeError:
        raise SetupError("legacy credential source is not valid UTF-8") from None
    result: dict[str, str] = {}
    for line_number, physical in enumerate(text.splitlines(keepends=True), start=1):
        if physical.endswith("\r\n"):
            line = physical[:-2]
        elif physical.endswith("\n"):
            line = physical[:-1]
        elif physical.endswith("\r"):
            line = physical[:-1]
        else:
            line = physical
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.endswith("\\"):
            raise SetupError(f"legacy line {line_number} uses a continuation")
        if "=" not in line:
            raise SetupError(f"legacy line {line_number} is not NAME=VALUE")
        name, value = line.split("=", 1)
        if name not in LEGACY_KEYS:
            raise SetupError(f"legacy line {line_number} has an unknown name")
        if name in result:
            raise SetupError(f"legacy line {line_number} duplicates a name")
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or not value.endswith(quote):
                raise SetupError(f"legacy line {line_number} has unmatched quotes")
            value = value[1:-1]
        elif value.endswith(("'", '"')):
            raise SetupError(f"legacy line {line_number} has unmatched quotes")
        result[name] = value
    return result


def _path_exists(path: Path) -> bool:
    return os.path.lexists(os.fsencode(path))


def _require_regular_source(path: Path) -> bytes:
    try:
        metadata = os.lstat(path)
    except OSError:
        raise SetupError("legacy credential source is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SetupError("legacy credential source must be a non-symlink regular file")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise SetupError("legacy credential source could not be opened safely") from None
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise SetupError("legacy credential source changed during open")
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > 1_048_576:
                raise SetupError("legacy credential source is too large")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _prepare_parent(destination: Path) -> Path:
    parent = destination.parent
    if not _path_exists(parent):
        grandparent = parent.parent
        try:
            metadata = os.lstat(grandparent)
        except OSError:
            raise SetupError("configuration parent is unavailable") from None
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise SetupError("configuration parent must be a non-symlink directory")
        try:
            parent.mkdir(mode=0o700)
        except OSError:
            raise SetupError("configuration directory could not be created") from None
    try:
        metadata = os.lstat(parent)
    except OSError:
        raise SetupError("configuration directory is unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SetupError("configuration directory must be a non-symlink directory")
    try:
        parent.chmod(0o700)
    except OSError:
        raise SetupError("configuration directory permissions could not be secured") from None
    return parent


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _credential_document(credentials: Mapping[str, str]) -> bytes:
    if set(credentials) - LEGACY_KEYS or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in credentials.items()
    ):
        raise SetupError("credential document contains unsupported fields")
    return (
        json.dumps(
            {
                "version": CREDENTIALS_VERSION,
                "credentials": dict(sorted(credentials.items())),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        + b"\n"
    )


def migrate_legacy_credentials(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> SetupResult:
    """Install credentials once without evaluating or replacing any existing object."""

    source_path = Path(source)
    destination_path = Path(destination)
    if _path_exists(destination_path):
        return SetupResult("already_configured", destination_path)
    parent = _prepare_parent(destination_path)
    raw = _require_regular_source(source_path)
    payload = _credential_document(parse_legacy_env(raw))
    temporary = parent / (
        f".{destination_path.name}.model-boss-{os.getpid()}-{secrets.token_hex(12)}.tmp"
    )
    descriptor = -1
    installed = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination_path, follow_symlinks=False)
        except FileExistsError:
            return SetupResult("already_configured", destination_path)
        installed = True
        _fsync_directory(parent)
        return SetupResult("migrated", destination_path)
    except SetupError:
        raise
    except OSError:
        if installed:
            raise SetupError("credential destination durability could not be confirmed") from None
        raise SetupError("credential destination could not be installed atomically") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_credentials(path: str | os.PathLike[str]) -> dict[str, str]:
    """Read the versioned credential document as strict JSON data."""

    source = Path(path)
    raw = _require_regular_source(source)
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeError, ValueError):
        raise SetupError("credential document is invalid") from None
    if not isinstance(value, dict) or set(value) != {"version", "credentials"}:
        raise SetupError("credential document has an invalid schema")
    if value["version"] != CREDENTIALS_VERSION or not isinstance(
        value["credentials"], dict
    ):
        raise SetupError("credential document has an invalid schema")
    credentials = value["credentials"]
    if set(credentials) - LEGACY_KEYS or not all(
        isinstance(name, str) and isinstance(secret, str)
        for name, secret in credentials.items()
    ):
        raise SetupError("credential document has unsupported fields")
    return dict(credentials)


def _validated_runner(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise SetupError("runner path must be absolute")
    try:
        lexical = os.lstat(candidate)
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        raise SetupError("runner is unavailable") from None
    if (
        stat.S_ISLNK(lexical.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(resolved, os.X_OK)
    ):
        raise SetupError("runner must be a non-symlink executable regular file")
    return resolved


def render_wrapper(
    runner: str | os.PathLike[str],
    route: str,
    policy: str,
) -> str:
    """Render a two-line wrapper with static policy and byte-preserving argv."""

    if (route, policy) not in set(WRAPPER_SPECS.values()):
        raise SetupError("wrapper route or policy is unsupported")
    executable = _validated_runner(runner)
    return (
        "#!/bin/sh\n"
        f"exec {shlex.quote(os.fspath(executable))} provider-exec "
        f"--route {shlex.quote(route)} --policy {shlex.quote(policy)} -- \"$@\"\n"
    )


def install_provider_wrappers(
    runner: str | os.PathLike[str],
    install_path: str | os.PathLike[str],
) -> SetupResult:
    """Install the exact compatibility wrapper set without embedding credentials."""

    executable = _validated_runner(runner)
    directory = Path(install_path)
    if _path_exists(directory):
        metadata = os.lstat(directory)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise SetupError("wrapper install path must be a non-symlink directory")
    else:
        try:
            directory.mkdir(mode=0o700)
        except OSError:
            raise SetupError("wrapper install directory could not be created") from None
    for name, (route, policy) in WRAPPER_SPECS.items():
        destination = directory / name
        if _path_exists(destination):
            metadata = os.lstat(destination)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise SetupError("existing wrapper is not a regular file")
        payload = render_wrapper(executable, route, policy).encode("utf-8")
        temporary = directory / f".{name}.{os.getpid()}-{secrets.token_hex(8)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o755,
            )
            os.fchmod(descriptor, 0o755)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except OSError:
            raise SetupError("provider wrapper could not be installed atomically") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    _fsync_directory(directory)
    return SetupResult("configured", directory)


def provider_child_environment(
    route: str,
    credentials: Mapping[str, str],
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Map credential JSON into the narrow Anthropic-compatible child contract."""

    if route not in {"kimi", "glm", "glm-turbo"}:
        raise SetupError("provider route is unsupported")
    source = dict(base_environment or {})
    environment = {
        name: source[name]
        for name in (
            "PATH",
            "HOME",
            "TERM",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
        )
        if isinstance(source.get(name), str)
    }
    if "PATH" not in environment:
        environment["PATH"] = os.defpath
    environment.setdefault("LANG", "C")
    environment.setdefault("LC_ALL", "C")
    if route == "kimi":
        required = ("KIMI_BASE_URL", "KIMI_AUTH_TOKEN")
        if any(name not in credentials for name in required):
            raise SetupError("Kimi credentials are incomplete")
        environment["ANTHROPIC_BASE_URL"] = credentials["KIMI_BASE_URL"]
        environment["ANTHROPIC_AUTH_TOKEN"] = credentials["KIMI_AUTH_TOKEN"]
    else:
        required = (
            "GLM_BASE_URL",
            "GLM_AUTH_TOKEN",
            "GLM_MODEL",
            "GLM_SMALL_FAST_MODEL",
        )
        if any(name not in credentials for name in required):
            raise SetupError("GLM credentials are incomplete")
        environment["ANTHROPIC_BASE_URL"] = credentials["GLM_BASE_URL"]
        environment["ANTHROPIC_AUTH_TOKEN"] = credentials["GLM_AUTH_TOKEN"]
        environment["ANTHROPIC_MODEL"] = (
            credentials["GLM_MODEL"]
            if route == "glm"
            else credentials["GLM_SMALL_FAST_MODEL"]
        )
        environment["ANTHROPIC_SMALL_FAST_MODEL"] = credentials[
            "GLM_SMALL_FAST_MODEL"
        ]
    return environment


__all__ = (
    "CREDENTIALS_VERSION",
    "LEGACY_KEYS",
    "WRAPPER_SPECS",
    "SetupError",
    "SetupResult",
    "load_credentials",
    "install_provider_wrappers",
    "migrate_legacy_credentials",
    "parse_legacy_env",
    "provider_child_environment",
    "render_wrapper",
)
