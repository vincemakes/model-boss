"""Immutable data contracts for model-independent Token Saver routing."""

from __future__ import annotations

import hashlib
import re
import unicodedata
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


class FingerprintEvidenceSource(_StringEnum):
    HOST_METADATA = "host-metadata"
    PINNED_ADAPTER = "pinned-adapter"
    PROVIDER_RESPONSE = "provider-response"
    IDENTITY_HANDSHAKE = "identity-handshake"


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
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _require_non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    ):
        raise ValueError(f"{field_name} must not contain control characters")
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
        if any("\0" in member for member in command):
            raise ValueError("command elements must not contain NUL")
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
        credential_env = tuple(self.credential_env)
        child_names = tuple(binding.child_name for binding in credential_env)
        if len(set(child_names)) != len(child_names):
            raise ValueError("credential_env child_name values must be unique")

        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "band", band)
        object.__setattr__(self, "roles", roles)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "credential_env", credential_env)


def _sandbox_binding_hash(
    *,
    route_id: str,
    transport: Transport,
    command: tuple[str, ...],
    worktree_identity: str,
    route_state_identity: str,
    profile_hash: str,
) -> str:
    fields = (
        route_id.encode("utf-8"),
        transport.value.encode("ascii"),
        len(command).to_bytes(8, "big"),
        *(member.encode("utf-8") for member in command),
        worktree_identity.encode("ascii"),
        route_state_identity.encode("ascii"),
        profile_hash.encode("ascii"),
    )
    encoded = bytearray(b"TOKEN-SAVER-SANDBOX-BINDING\0")
    for field in fields:
        encoded.extend(len(field).to_bytes(8, "big"))
        encoded.extend(field)
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class WorkerSandboxIdentity:
    """Proof token bound to one external route and one verified sandbox context."""

    route_id: str
    transport: Transport
    command: tuple[str, ...]
    worktree_identity: str
    route_state_identity: str
    profile_hash: str
    binding_hash: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.route_id, "route_id")
        try:
            transport = Transport(self.transport)
        except (TypeError, ValueError) as exc:
            raise ValueError("transport must be a supported transport") from exc
        if transport is not Transport.EXTERNAL_CLI:
            raise ValueError("worker sandbox identity requires external-cli transport")
        if not isinstance(self.command, (tuple, list)) or not self.command or not all(
            isinstance(member, str) for member in self.command
        ):
            raise ValueError("command must be a non-empty argument tuple")
        command = tuple(self.command)
        for field_name in (
            "worktree_identity",
            "route_state_identity",
            "profile_hash",
            "binding_hash",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256")
        expected = _sandbox_binding_hash(
            route_id=self.route_id,
            transport=transport,
            command=command,
            worktree_identity=self.worktree_identity,
            route_state_identity=self.route_state_identity,
            profile_hash=self.profile_hash,
        )
        if self.binding_hash != expected:
            raise ValueError("binding_hash does not match the exact sandbox context")
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "command", command)

    @classmethod
    def issue(
        cls,
        *,
        route: Route,
        worktree_identity: str,
        route_state_identity: str,
        profile_hash: str,
    ) -> WorkerSandboxIdentity:
        if not isinstance(route, Route):
            raise ValueError("route must be a Route")
        binding_hash = _sandbox_binding_hash(
            route_id=route.route_id,
            transport=route.transport,
            command=route.command,
            worktree_identity=worktree_identity,
            route_state_identity=route_state_identity,
            profile_hash=profile_hash,
        )
        return cls(
            route_id=route.route_id,
            transport=route.transport,
            command=route.command,
            worktree_identity=worktree_identity,
            route_state_identity=route_state_identity,
            profile_hash=profile_hash,
            binding_hash=binding_hash,
        )

    def is_bound_to(self, route: Route) -> bool:
        return (
            isinstance(route, Route)
            and self.route_id == route.route_id
            and self.transport is route.transport
            and self.command == route.command
        )


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
        if any(route_id != route.route_id for route_id, route in routes.items()):
            raise ValueError("route identifiers must match their mapping keys")
        if set(routes) != set(provenance) or not all(
            isinstance(value, Provenance) for value in provenance.values()
        ):
            raise ValueError("route provenance must cover every route")
        object.__setattr__(self, "routes", MappingProxyType(routes))
        object.__setattr__(self, "route_provenance", MappingProxyType(provenance))


@dataclass(frozen=True)
class RouteProbeResult:
    """Credential-free evidence produced by a later transport preflight."""

    route_id: str
    reachable: bool
    resolved_fingerprint: ModelFingerprint | None
    fingerprint_evidence_source: FingerprintEvidenceSource | None
    executable_available: bool
    native_agent_available: bool
    reviewer_read_only_enforced: bool
    verified_worker_sandbox_identity: WorkerSandboxIdentity | None
    configured_credentials: tuple[str, ...] = ()
    missing_credentials: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_text(self.route_id, "route_id")
        for field_name in (
            "reachable",
            "executable_available",
            "native_agent_available",
            "reviewer_read_only_enforced",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be a boolean")
        if self.resolved_fingerprint is not None and not isinstance(
            self.resolved_fingerprint, ModelFingerprint
        ):
            raise ValueError("resolved_fingerprint must be a ModelFingerprint or null")
        if self.fingerprint_evidence_source is not None:
            try:
                evidence_source = FingerprintEvidenceSource(
                    self.fingerprint_evidence_source
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("fingerprint evidence source is unsupported") from exc
            object.__setattr__(
                self,
                "fingerprint_evidence_source",
                evidence_source,
            )
        if (
            self.resolved_fingerprint is None
            and self.fingerprint_evidence_source is not None
        ):
            raise ValueError("fingerprint evidence requires a resolved fingerprint")
        if (
            self.resolved_fingerprint is not None
            and self.fingerprint_evidence_source is None
        ):
            raise ValueError("resolved fingerprint requires an evidence source")
        if self.verified_worker_sandbox_identity is not None and not isinstance(
            self.verified_worker_sandbox_identity,
            WorkerSandboxIdentity,
        ):
            raise ValueError(
                "verified_worker_sandbox_identity must be exact sandbox proof"
            )

        for field_name in ("configured_credentials", "missing_credentials"):
            names = getattr(self, field_name)
            if not isinstance(names, (tuple, list)) or not all(
                isinstance(name, str) and _ENV_NAME.fullmatch(name) is not None
                for name in names
            ):
                raise ValueError(f"{field_name} must contain environment variable names")
            normalized = tuple(names)
            if len(set(normalized)) != len(normalized):
                raise ValueError(f"{field_name} must not contain duplicates")
            object.__setattr__(self, field_name, normalized)
        if set(self.missing_credentials).intersection(self.configured_credentials):
            raise ValueError("credential names cannot be both configured and missing")


@dataclass(frozen=True)
class CandidateTopology:
    """Pure route preferences before any availability claims are considered."""

    main: MainLoop | None
    requested_mode: Mode
    routes: Mapping[str, Route]
    reviewer_route_ids: tuple[str, ...]
    worker_route_ids: tuple[str, ...]
    mode_source: Provenance
    reviewer_source: Provenance
    worker_source: Provenance
    resolution_source: str
    status: Status = Status.OK
    facts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.main is not None and not isinstance(self.main, MainLoop):
            raise ValueError("main must be a MainLoop or null")
        try:
            object.__setattr__(self, "requested_mode", Mode(self.requested_mode))
        except (TypeError, ValueError) as exc:
            raise ValueError("requested_mode must be auto, lite, or max") from exc
        routes = dict(self.routes)
        if not all(
            isinstance(route_id, str)
            and isinstance(route, Route)
            and route_id == route.route_id
            for route_id, route in routes.items()
        ):
            raise ValueError("routes must map matching identifiers to Route values")
        object.__setattr__(self, "routes", MappingProxyType(routes))
        for field_name in ("reviewer_route_ids", "worker_route_ids"):
            route_ids = getattr(self, field_name)
            if not isinstance(route_ids, (tuple, list)) or not all(
                isinstance(route_id, str) and route_id.strip() for route_id in route_ids
            ):
                raise ValueError(f"{field_name} must contain route identifiers")
            normalized = tuple(route_ids)
            if len(set(normalized)) != len(normalized):
                raise ValueError(f"{field_name} must not contain duplicates")
            object.__setattr__(self, field_name, normalized)
        for field_name in ("mode_source", "reviewer_source", "worker_source"):
            if not isinstance(getattr(self, field_name), Provenance):
                raise ValueError(f"{field_name} must be Provenance")
        if self.resolution_source not in {"profile", "user", "project", "explicit"}:
            raise ValueError("resolution_source must be a supported source")
        try:
            status = Status(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("status must be a structured status") from exc
        if status not in {Status.OK, Status.NEEDS_CONTEXT}:
            raise ValueError("candidate status must be ok or needs_context")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "facts", _normalize_facts(self.facts))


@dataclass(frozen=True)
class PreflightReport:
    """Eligibility results derived only from injected probe evidence."""

    candidate: CandidateTopology
    status: Status
    resolved_mode: Mode | None
    selected_reviewer_route_id: str | None = None
    selected_worker_route_id: str | None = None
    eligible_reviewer_route_ids: tuple[str, ...] = ()
    ineligible_reviewer_route_ids: tuple[str, ...] = ()
    eligible_worker_route_ids: tuple[str, ...] = ()
    ineligible_worker_route_ids: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, CandidateTopology):
            raise ValueError("candidate must be CandidateTopology")
        try:
            status = Status(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("status must be a structured status") from exc
        object.__setattr__(self, "status", status)
        if self.resolved_mode is not None:
            try:
                mode = Mode(self.resolved_mode)
            except (TypeError, ValueError) as exc:
                raise ValueError("resolved_mode must be lite, max, or null") from exc
            if mode is Mode.AUTO:
                raise ValueError("resolved_mode cannot remain auto")
            object.__setattr__(self, "resolved_mode", mode)
        if status is Status.OK and self.resolved_mode is None:
            raise ValueError("successful preflight requires a resolved mode")
        if status is not Status.OK and self.resolved_mode is not None:
            raise ValueError("blocked preflight cannot expose a resolved mode")
        for field_name in (
            "selected_reviewer_route_id",
            "selected_worker_route_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_non_empty_text(value, field_name)
        for field_name in (
            "eligible_reviewer_route_ids",
            "ineligible_reviewer_route_ids",
            "eligible_worker_route_ids",
            "ineligible_worker_route_ids",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, (tuple, list)) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ValueError(f"{field_name} must contain route identifiers")
            object.__setattr__(self, field_name, tuple(values))
        if (
            self.selected_reviewer_route_id is not None
            and self.selected_reviewer_route_id
            not in self.eligible_reviewer_route_ids
        ):
            raise ValueError("selected reviewer must be eligible")
        if (
            self.selected_worker_route_id is not None
            and self.selected_worker_route_id not in self.eligible_worker_route_ids
        ):
            raise ValueError("selected worker must be eligible")
        if status is not Status.OK and (
            self.selected_reviewer_route_id is not None
            or self.selected_worker_route_id is not None
        ):
            raise ValueError("blocked preflight cannot select routes")
        if self.resolved_mode is Mode.LITE and self.selected_reviewer_route_id is not None:
            raise ValueError("Lite preflight cannot select an external authority")
        if self.resolved_mode is Mode.MAX and self.selected_reviewer_route_id is None:
            raise ValueError("Max preflight requires a selected reviewer")
        object.__setattr__(self, "facts", _normalize_facts(self.facts))


@dataclass(frozen=True)
class Resolution:
    """Final authority topology, or a fail-closed structured status."""

    status: Status
    main: MainLoop | None
    mode: Mode | None
    authority_route_id: str | None
    worker: str
    resolution_source: str
    facts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            status = Status(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("status must be a structured status") from exc
        object.__setattr__(self, "status", status)
        if self.main is not None and not isinstance(self.main, MainLoop):
            raise ValueError("main must be a MainLoop or null")
        if self.mode is not None:
            try:
                mode = Mode(self.mode)
            except (TypeError, ValueError) as exc:
                raise ValueError("mode must be lite, max, or null") from exc
            if mode is Mode.AUTO:
                raise ValueError("mode cannot remain auto")
            object.__setattr__(self, "mode", mode)
        if status is Status.OK and (self.main is None or self.mode is None):
            raise ValueError("successful resolution requires a main loop and mode")
        if status is not Status.OK and self.mode is not None:
            raise ValueError("blocked resolution cannot expose a successful mode")
        if self.authority_route_id is not None:
            _require_non_empty_text(self.authority_route_id, "authority_route_id")
        _require_non_empty_text(self.worker, "worker")
        if status is Status.OK and self.mode is Mode.LITE and self.authority_route_id:
            raise ValueError("Lite authority must remain inline")
        if status is Status.OK and self.mode is Mode.MAX and not self.authority_route_id:
            raise ValueError("Max authority requires a reviewer route")
        if status is not Status.OK and (
            self.authority_route_id is not None or self.worker != "none"
        ):
            raise ValueError("blocked resolution cannot expose a runnable topology")
        if self.resolution_source not in {"profile", "user", "project", "explicit"}:
            raise ValueError("resolution_source must be a supported source")
        object.__setattr__(self, "facts", _normalize_facts(self.facts))

    def startup_verdict(self) -> str:
        """Serialize the five-line startup verdict only for valid topologies."""

        if self.status is not Status.OK or self.main is None or self.mode is None:
            raise ValueError("blocked resolutions do not have a startup verdict")
        authority = self.authority_route_id or "inline main loop"
        return "\n".join(
            (
                "Main loop: "
                f"{self.main.route_id}/{self.main.fingerprint.resolved_model_id}",
                f"Resolved mode: {self.mode.value.title()}",
                f"Authority: {authority}",
                f"Worker: {self.worker}",
                f"Resolution source: {self.resolution_source}",
            )
        )


def _normalize_facts(values: object) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or not all(
        isinstance(value, str) and value.strip() for value in values
    ):
        raise ValueError("facts must contain non-empty strings")
    for value in values:
        _require_non_empty_text(value, "fact")
    return tuple(values)
