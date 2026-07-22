"""Bounded direct-argv process execution for Model Boss transports."""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .models import Status


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
_AUTHORIZATION = re.compile(
    br"(?im)^authorization\s*:\s*[^\r\n]*",
)


@dataclass(frozen=True)
class ProcessSpec:
    """One exact child process contract with no ambient environment inheritance."""

    argv: tuple[str, ...]
    cwd: Path
    stdin: bytes = b""
    env: Mapping[str, str] = None  # type: ignore[assignment]
    timeout_seconds: float = 600.0
    stdout_limit: int = 1_048_576
    stderr_limit: int = 1_048_576
    terminate_grace_seconds: float = 1.0
    redact_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.argv, (tuple, list)) or not self.argv or not all(
            isinstance(member, str) and "\0" not in member for member in self.argv
        ):
            raise ValueError("argv must be a non-empty NUL-free argument tuple")
        argv = tuple(self.argv)
        executable = Path(argv[0])
        if not executable.is_absolute():
            raise ValueError("argv executable must be an absolute preflight result")
        try:
            resolved_executable = executable.resolve(strict=True)
            metadata = resolved_executable.stat()
        except OSError:
            raise ValueError("argv executable must resolve to an existing file") from None
        if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved_executable, os.X_OK):
            raise ValueError("argv executable must be an executable regular file")
        argv = (os.fspath(resolved_executable), *argv[1:])

        try:
            cwd = Path(self.cwd).resolve(strict=True)
        except (OSError, TypeError, ValueError):
            raise ValueError("cwd must resolve to an existing directory") from None
        if not cwd.is_dir():
            raise ValueError("cwd must resolve to a directory")
        if type(self.stdin) is not bytes:
            raise ValueError("stdin must be bytes")
        environment = {} if self.env is None else dict(self.env)
        if not all(
            isinstance(name, str)
            and _ENV_NAME.fullmatch(name) is not None
            and isinstance(value, str)
            and "\0" not in value
            for name, value in environment.items()
        ):
            raise ValueError("env must contain safe string names and values")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(self.terminate_grace_seconds, bool)
            or not isinstance(self.terminate_grace_seconds, (int, float))
            or self.terminate_grace_seconds < 0
        ):
            raise ValueError("terminate_grace_seconds must be non-negative")
        for name in ("stdout_limit", "stderr_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.redact_values, (tuple, list)) or not all(
            isinstance(value, str) for value in self.redact_values
        ):
            raise ValueError("redact_values must be a string tuple")

        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "env", MappingProxyType(environment))
        object.__setattr__(self, "redact_values", tuple(self.redact_values))


@dataclass(frozen=True)
class ProcessResult:
    status: Status
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_seconds: float

    def __post_init__(self) -> None:
        try:
            status = Status(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("status must be a Model Boss status") from exc
        if type(self.stdout) is not bytes or type(self.stderr) is not bytes:
            raise ValueError("process output must be bytes")
        if self.returncode is not None and (
            isinstance(self.returncode, bool) or not isinstance(self.returncode, int)
        ):
            raise ValueError("returncode must be an integer or null")
        for field_name in ("stdout_truncated", "stderr_truncated", "timed_out"):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be boolean")
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")
        object.__setattr__(self, "status", status)


class _BoundedCollector:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = bytearray()
        self.truncated = False

    def drain(self, stream: object) -> None:
        reader = stream
        try:
            while True:
                chunk = reader.read(65536)  # type: ignore[attr-defined]
                if not chunk:
                    return
                remaining = self.limit - len(self.buffer)
                if remaining > 0:
                    self.buffer.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    self.truncated = True
        finally:
            try:
                reader.close()  # type: ignore[attr-defined]
            except OSError:
                pass


def _terminate_process_group(process: subprocess.Popen[bytes], grace: float) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=max(grace, 0.1))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _redact(data: bytes, values: tuple[str, ...], limit: int) -> bytes:
    redacted = _AUTHORIZATION.sub(b"Authorization: [REDACTED]", data)
    for value in values:
        if not value:
            continue
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            continue
        redacted = redacted.replace(encoded, b"[REDACTED]")
    return redacted[:limit]


def run_process(spec: ProcessSpec) -> ProcessResult:
    """Execute and reap the complete child process group under ``spec``."""

    if not isinstance(spec, ProcessSpec):
        raise ValueError("spec must be ProcessSpec")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            spec.argv,
            cwd=spec.cwd,
            env=dict(spec.env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError:
        return ProcessResult(
            status=Status.TRANSPORT_ERROR,
            returncode=None,
            stdout=b"",
            stderr=b"process launch failed",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            duration_seconds=time.monotonic() - started,
        )

    stdout = _BoundedCollector(spec.stdout_limit)
    stderr = _BoundedCollector(spec.stderr_limit)
    threads = (
        threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
    )
    for thread in threads:
        thread.start()
    try:
        assert process.stdin is not None
        try:
            process.stdin.write(spec.stdin)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()
        try:
            process.wait(timeout=spec.timeout_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(process, spec.terminate_grace_seconds)
    finally:
        for thread in threads:
            thread.join(timeout=max(spec.terminate_grace_seconds, 1.0))
        if process.poll() is None:
            _terminate_process_group(process, spec.terminate_grace_seconds)

    if timed_out:
        status = Status.TIMEOUT
    elif process.returncode == 0:
        status = Status.OK
    else:
        status = Status.TRANSPORT_ERROR
    return ProcessResult(
        status=status,
        returncode=process.returncode,
        stdout=_redact(bytes(stdout.buffer), spec.redact_values, spec.stdout_limit),
        stderr=_redact(bytes(stderr.buffer), spec.redact_values, spec.stderr_limit),
        stdout_truncated=stdout.truncated,
        stderr_truncated=stderr.truncated,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
    )


__all__ = ("ProcessResult", "ProcessSpec", "run_process")
