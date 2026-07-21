"""Immutable data contracts for model-independent Token Saver routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Mode(_StringEnum):
    AUTO = "auto"
    LITE = "lite"
    MAX = "max"


class CapabilityBand(_StringEnum):
    AUTHORITY = "authority"
    BALANCED = "balanced"
    FAST = "fast"


class Role(_StringEnum):
    REVIEWER = "reviewer"
    WORKER = "worker"
    SCOUT = "scout"
    MECHANIC = "mechanic"


class Transport(_StringEnum):
    HOST_SUBAGENT = "host-subagent"
    EXTERNAL_CLI = "external-cli"


class Status(_StringEnum):
    OK = "ok"
    NEEDS_CONTEXT = "needs_context"
    GATE_FAILED = "gate_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    REVIEWER_UNAVAILABLE = "reviewer_unavailable"
    TIMEOUT = "timeout"
    SCOPE_VIOLATION = "scope_violation"
    TRANSPORT_ERROR = "transport_error"
    REVIEW_REVISE = "review_revise"
    APPROVAL_STALE = "approval_stale"
    DESTINATION_CHANGED = "destination_changed"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"


# Descriptive aliases keep the structured-status contract easy to discover
# without creating additional serialized values.
StructuredStatus = Status
RunStatus = Status


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _require_non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _bounded_retry(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
        raise ValueError(f"{field_name} must be an integer from 0 to 10")
    return value


@dataclass(frozen=True)
class ModelFingerprint:
    provider_family: str
    resolved_model_id: str
    variant: str = "default"

    def __post_init__(self) -> None:
        _require_non_empty_text(self.provider_family, "provider_family")
        _require_non_empty_text(self.resolved_model_id, "resolved_model_id")
        _require_non_empty_text(self.variant, "variant")

    @property
    def canonical(self) -> str:
        """Return the stable lower-case identity used in comparisons."""

        return ":".join(
            part.strip().lower()
            for part in (self.provider_family, self.resolved_model_id, self.variant)
        )


@dataclass(frozen=True)
class RetryPolicy:
    worker_attempts: int = 3
    review_revisions: int = 2

    def __post_init__(self) -> None:
        _bounded_retry(self.worker_attempts, "worker_attempts")
        _bounded_retry(self.review_revisions, "review_revisions")


@dataclass(frozen=True)
class CredentialBinding:
    child_name: str
    source_name: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("child_name", self.child_name),
            ("source_name", self.source_name),
        ):
            if not isinstance(value, str) or _ENV_NAME.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be an environment variable name")


@dataclass(frozen=True)
class Route:
    route_id: str
    transport: Transport
    band: CapabilityBand
    roles: frozenset[Role]
    read_only: bool
    model: str | None = None
    provider_family: str | None = None
    command: tuple[str, ...] = ()
    timeout_seconds: int = 600
    retry_policy: RetryPolicy = RetryPolicy()
    credential_env: tuple[CredentialBinding, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_text(self.route_id, "route_id")

        try:
            transport = Transport(self.transport)
        except (TypeError, ValueError) as exc:
            raise ValueError("transport must be a supported transport") from exc
        try:
            band = CapabilityBand(self.band)
        except (TypeError, ValueError) as exc:
            raise ValueError("band must be a supported capability band") from exc
        try:
            ordered_roles = tuple(Role(role) for role in self.roles)
        except (TypeError, ValueError) as exc:
            raise ValueError("roles must contain supported roles") from exc

        if not ordered_roles or len(set(ordered_roles)) != len(ordered_roles):
            raise ValueError("roles must be non-empty and unique")
        roles = frozenset(ordered_roles)
        if not isinstance(self.read_only, bool):
            raise ValueError("read_only must be a boolean")
        if Role.REVIEWER in roles and not self.read_only:
            raise ValueError("reviewer-capable routes must be read_only")

        for field_name, value in (
            ("model", self.model),
            ("provider_family", self.provider_family),
        ):
            if value is not None:
                _require_non_empty_text(value, field_name)

        if not isinstance(self.command, (tuple, list)) or not all(
            isinstance(member, str) for member in self.command
        ):
            raise ValueError("command must be an argument array")
        command = tuple(self.command)
        if transport is Transport.EXTERNAL_CLI:
            if not command or not command[0].strip():
                raise ValueError("external-cli command requires an executable")
        elif command:
            raise ValueError("host-subagent routes cannot define a command")

        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive integer")
        if not isinstance(self.retry_policy, RetryPolicy):
            raise ValueError("retry_policy must be a RetryPolicy")
        if not isinstance(self.credential_env, (tuple, list)) or not all(
            isinstance(binding, CredentialBinding) for binding in self.credential_env
        ):
            raise ValueError("credential_env must contain CredentialBinding entries")

        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "band", band)
        object.__setattr__(self, "roles", roles)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "credential_env", tuple(self.credential_env))


@dataclass(frozen=True)
class Preferences:
    reviewers: tuple[str, ...] = ()
    workers: tuple[str, ...] = ()
    scouts: tuple[str, ...] = ()
    mechanics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("reviewers", "workers", "scouts", "mechanics"):
            values = getattr(self, field_name)
            if not isinstance(values, (tuple, list)) or not all(
                isinstance(route_id, str) and route_id.strip() for route_id in values
            ):
                raise ValueError(f"{field_name} must contain route identifiers")
            normalized = tuple(values)
            if len(set(normalized)) != len(normalized):
                raise ValueError(f"{field_name} must not contain duplicates")
            object.__setattr__(self, field_name, normalized)


@dataclass(frozen=True)
class RunOverrides:
    mode: Mode | None = None
    reviewer: str | None = None
    worker: str | None = None
    scout: str | None = None
    mechanic: str | None = None

    def __post_init__(self) -> None:
        if self.mode is not None:
            try:
                object.__setattr__(self, "mode", Mode(self.mode))
            except (TypeError, ValueError) as exc:
                raise ValueError("mode must be auto, lite, or max") from exc
        for field_name in ("reviewer", "worker", "scout", "mechanic"):
            value = getattr(self, field_name)
            if value is not None:
                _require_non_empty_text(value, field_name)


@dataclass(frozen=True)
class Provenance:
    source: str
    path: Path | None = None

    def __post_init__(self) -> None:
        if self.source not in {"profile", "user", "project", "explicit"}:
            raise ValueError("source must be profile, user, project, or explicit")
        if self.path is not None and not isinstance(self.path, Path):
            object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True)
class MainLoop:
    route_id: str
    fingerprint: ModelFingerprint
    band: CapabilityBand
    host: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.route_id, "route_id")
        if not isinstance(self.fingerprint, ModelFingerprint):
            raise ValueError("fingerprint must be a ModelFingerprint")
        try:
            object.__setattr__(self, "band", CapabilityBand(self.band))
        except (TypeError, ValueError) as exc:
            raise ValueError("band must be a supported capability band") from exc
        _require_non_empty_text(self.host, "host")


@dataclass(frozen=True)
class PreferenceProvenance:
    reviewers: Provenance
    workers: Provenance
    scouts: Provenance
    mechanics: Provenance


@dataclass(frozen=True)
class LoadedConfig:
    mode: Mode
    routes: Mapping[str, Route]
    preferences: Preferences
    mode_provenance: Provenance
    route_provenance: Mapping[str, Provenance]
    preference_provenance: PreferenceProvenance

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "mode", Mode(self.mode))
        except (TypeError, ValueError) as exc:
            raise ValueError("mode must be auto, lite, or max") from exc
        if not isinstance(self.preferences, Preferences):
            raise ValueError("preferences must be Preferences")
        if not isinstance(self.mode_provenance, Provenance):
            raise ValueError("mode_provenance must be Provenance")
        if not isinstance(self.preference_provenance, PreferenceProvenance):
            raise ValueError("preference_provenance must be PreferenceProvenance")

        routes = dict(self.routes)
        provenance = dict(self.route_provenance)
        if not all(
            isinstance(route_id, str) and isinstance(route, Route)
            for route_id, route in routes.items()
        ):
            raise ValueError("routes must map identifiers to Route values")
        if set(routes) != set(provenance) or not all(
            isinstance(value, Provenance) for value in provenance.values()
        ):
            raise ValueError("route provenance must cover every route")
        object.__setattr__(self, "routes", MappingProxyType(routes))
        object.__setattr__(self, "route_provenance", MappingProxyType(provenance))
