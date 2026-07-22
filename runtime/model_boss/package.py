"""Deterministic, allowlisted Token Saver skill packaging."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import stat
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PACKAGE_ROOT = "token-saver"
PACKAGE_MANIFEST = (
    "SKILL.md",
    "agents/openai.yaml",
    "README.md",
    "README.zh-CN.md",
    "BENCHMARKS.md",
    "BENCHMARKS.zh-CN.md",
    "LICENSE",
    "references/protocol.md",
    "references/routing.md",
    "references/adapters/claude-code.md",
    "references/adapters/codex.md",
    "references/adapters/external-cli.md",
    "references/profiles/anthropic.json",
    "references/profiles/openai.json",
    "references/profiles/kimi.json",
    "assets/agents/prompts/reviewer.md",
    "assets/agents/prompts/implementer.md",
    "assets/agents/prompts/mechanic.md",
    "assets/agents/prompts/scout.md",
    "assets/agents/claude-code/reviewer.md",
    "assets/agents/claude-code/implementer.md",
    "assets/agents/claude-code/mechanic.md",
    "assets/agents/claude-code/scout.md",
    "assets/agents/codex/reviewer.toml",
    "assets/agents/codex/implementer.toml",
    "assets/agents/codex/mechanic.toml",
    "assets/agents/codex/scout.toml",
    "config/model-boss.schema.json",
    "config/model-boss.example.json",
    "runtime/model_boss/__init__.py",
    "runtime/model_boss/models.py",
    "runtime/model_boss/config.py",
    "runtime/model_boss/routing.py",
    "runtime/model_boss/evidence.py",
    "runtime/model_boss/repository.py",
    "runtime/model_boss/bundle.py",
    "runtime/model_boss/integration.py",
    "runtime/model_boss/resources.py",
    "runtime/model_boss/sandbox.py",
    "runtime/model_boss/process.py",
    "runtime/model_boss/transport.py",
    "runtime/model_boss/cli.py",
    "runtime/model_boss/setup.py",
    "runtime/model_boss/package.py",
    "scripts/model-boss.py",
    "scripts/setup-model-providers.sh",
    "scripts/package-skill.sh",
    "scripts/validate.sh",
    "media/og.png",
)
EXECUTABLE_PATHS = frozenset(
    {
        "scripts/model-boss.py",
        "scripts/setup-model-providers.sh",
        "scripts/package-skill.sh",
        "scripts/validate.sh",
    }
)
_CONTROLLED_PREFIXES = (
    "agents",
    "assets/agents",
    "config",
    "media",
    "references",
    "runtime/model_boss",
    "scripts",
)
_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


class PackageError(ValueError):
    """The source tree or archive violates the distribution contract."""


@dataclass(frozen=True)
class PackageResult:
    output_path: Path
    sha256: str
    skill_sha256: str
    entries: tuple[str, ...]


def _ordered_manifest() -> tuple[str, ...]:
    return tuple(sorted(PACKAGE_MANIFEST, key=lambda value: value.encode("utf-8")))


def _is_cache_path(relative: Path) -> bool:
    return "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}


def _validate_controlled_tree(root: Path) -> None:
    expected = set(PACKAGE_MANIFEST)
    for prefix_text in _CONTROLLED_PREFIXES:
        prefix = root / prefix_text
        if not prefix.exists():
            continue
        for directory, directory_names, filenames in os.walk(prefix, followlinks=False):
            directory_path = Path(directory)
            for name in tuple(directory_names):
                candidate = directory_path / name
                relative = candidate.relative_to(root)
                if candidate.is_symlink():
                    raise PackageError(f"unlisted symlink in packaged area: {relative.as_posix()}")
                if _is_cache_path(relative):
                    directory_names.remove(name)
            for name in filenames:
                candidate = directory_path / name
                relative_path = candidate.relative_to(root)
                if _is_cache_path(relative_path):
                    continue
                relative = relative_path.as_posix()
                if candidate.is_symlink():
                    raise PackageError(f"unlisted symlink in packaged area: {relative}")
                if relative not in expected:
                    raise PackageError(f"unlisted file in packaged area: {relative}")


def _validate_skill_frontmatter(payload: bytes) -> None:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeError:
        raise PackageError("SKILL.md must be UTF-8") from None
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if match is None:
        raise PackageError("SKILL.md frontmatter is missing")
    frontmatter = match.group(1)
    if re.search(r"(?m)^name:\s*token-saver\s*$", frontmatter) is None:
        raise PackageError("SKILL.md must use the token-saver name")
    description_match = re.search(
        r"(?ms)^description:\s*>-\s*\n((?:[ \t]+.*(?:\n|\Z))+)",
        frontmatter,
    )
    if description_match is None:
        raise PackageError("SKILL.md description is invalid")
    description = " ".join(
        line.strip() for line in description_match.group(1).splitlines()
    ).strip()
    if not description.startswith("Use when") or len(description) > 1024:
        raise PackageError("SKILL.md description violates the trigger contract")


def _link_resolves(target: str, manifest: set[str]) -> bool:
    if not target or target.startswith("#") or _SCHEME.match(target):
        return True
    raw = target.split("#", 1)[0]
    if not raw:
        return True
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or "\\" in raw:
        return False
    normalized = path.as_posix().rstrip("/")
    return normalized in manifest or any(
        member.startswith(normalized + "/") for member in manifest
    )


def _validate_readme_links(root: Path) -> None:
    manifest = set(PACKAGE_MANIFEST)
    for name in ("README.md", "README.zh-CN.md"):
        text = (root / name).read_text(encoding="utf-8")
        for target in _LINK.findall(text):
            if not _link_resolves(target, manifest):
                raise PackageError(f"README link is outside the package manifest: {target}")


def _validate_sources(repo_root: str | os.PathLike[str]) -> tuple[Path, dict[str, bytes]]:
    try:
        root = Path(repo_root).resolve(strict=True)
    except OSError:
        raise PackageError("repository root is unavailable") from None
    if not root.is_dir():
        raise PackageError("repository root must be a directory")
    if len(PACKAGE_MANIFEST) != len(set(PACKAGE_MANIFEST)):
        raise PackageError("package manifest contains duplicates")
    _validate_controlled_tree(root)
    payloads: dict[str, bytes] = {}
    for relative in PACKAGE_MANIFEST:
        path = root / relative
        try:
            lexical = os.lstat(path)
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            raise PackageError(f"package source is unavailable or outside root: {relative}") from None
        if stat.S_ISLNK(lexical.st_mode) or not stat.S_ISREG(lexical.st_mode):
            raise PackageError(f"package source must be a non-symlink regular file: {relative}")
        payloads[relative] = path.read_bytes()
    _validate_skill_frontmatter(payloads["SKILL.md"])
    _validate_readme_links(root)
    return root, payloads


def _zip_info(relative: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(
        filename=f"{PACKAGE_ROOT}/{relative}",
        date_time=(1980, 1, 1, 0, 0, 0),
    )
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.flag_bits = 0
    info.internal_attr = 0
    info.external_attr = (stat.S_IFREG | (0o755 if relative in EXECUTABLE_PATHS else 0o644)) << 16
    info.extra = b""
    info.comment = b""
    return info


def _result(path: Path, entries: tuple[str, ...], skill_bytes: bytes) -> PackageResult:
    return PackageResult(
        output_path=path.resolve(strict=True),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
        entries=entries,
    )


def _validate_archive(
    root: Path,
    archive_path: Path,
    payloads: dict[str, bytes],
) -> PackageResult:
    ordered = _ordered_manifest()
    expected_names = [f"{PACKAGE_ROOT}/{relative}" for relative in ordered]
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            if archive.comment != b"" or [info.filename for info in infos] != expected_names:
                raise PackageError("archive manifest or root is invalid")
            if archive.testzip() is not None:
                raise PackageError("archive integrity check failed")
            for info, relative in zip(infos, ordered, strict=True):
                mode = 0o755 if relative in EXECUTABLE_PATHS else 0o644
                if (
                    info.is_dir()
                    or info.compress_type != zipfile.ZIP_STORED
                    or info.date_time != (1980, 1, 1, 0, 0, 0)
                    or info.create_system != 3
                    or info.create_version != 20
                    or info.extract_version != 20
                    or info.extra != b""
                    or info.comment != b""
                    or ((info.external_attr >> 16) & 0o777) != mode
                ):
                    raise PackageError(f"archive metadata is unstable: {relative}")
                if archive.read(info) != payloads[relative]:
                    raise PackageError(f"archive payload differs from source: {relative}")
    except (OSError, zipfile.BadZipFile):
        raise PackageError("archive is unreadable") from None
    return _result(archive_path, ordered, payloads["SKILL.md"])


def validate_package(
    repo_root: str | os.PathLike[str],
    archive_path: str | os.PathLike[str],
) -> PackageResult:
    root, payloads = _validate_sources(repo_root)
    path = Path(archive_path)
    if path.name == "fable-token-saver.skill":
        raise PackageError("obsolete artifact name is rejected")
    return _validate_archive(root, path.resolve(strict=True), payloads)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_package(
    repo_root: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
) -> PackageResult:
    root, payloads = _validate_sources(repo_root)
    requested = Path(output_path)
    if requested.name == "fable-token-saver.skill":
        raise PackageError("obsolete artifact name is rejected")
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError:
        raise PackageError("package output parent is unavailable") from None
    if not parent.is_dir():
        raise PackageError("package output parent must be a directory")
    destination = parent / requested.name
    if os.path.lexists(destination):
        metadata = os.lstat(destination)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PackageError("existing package output must be a non-symlink regular file")
    temporary = parent / f".{destination.name}.token-saver-{os.getpid()}-{secrets.token_hex(12)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w+b") as stream:
            descriptor = -1
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.comment = b""
                for relative in _ordered_manifest():
                    archive.writestr(_zip_info(relative), payloads[relative])
            stream.flush()
            os.fsync(stream.fileno())
        _validate_archive(root, temporary, payloads)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        _fsync_directory(parent)
        return _result(destination, _ordered_manifest(), payloads["SKILL.md"])
    except PackageError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageError(f"package build failed: {type(exc).__name__}") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m runtime.token_saver.package")
    parser.add_argument("--repo-root", default=os.curdir)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output")
    group.add_argument("--validate")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.output:
            result = build_package(arguments.repo_root, arguments.output)
        else:
            result = validate_package(arguments.repo_root, arguments.validate)
    except PackageError as error:
        print(f"token-saver package error: {error}", file=sys.stderr)
        return 2
    print(result.sha256)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "EXECUTABLE_PATHS",
    "PACKAGE_MANIFEST",
    "PackageError",
    "PackageResult",
    "build_package",
    "validate_package",
)
