"""Credential-free configuration loading for Model Boss routes."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    CapabilityBand,
    CredentialBinding,
    LoadedConfig,
    Mode,
    PreferenceProvenance,
    Preferences,
    Provenance,
    RetryPolicy,
    Role,
    Route,
    RunOverrides,
    Transport,
)


SCHEMA_VERSION = 1
_DEFAULT_PROFILES_ROOT = Path(__file__).resolve().parents[2] / "references" / "profiles"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PREFERENCE_FIELDS = ("reviewers", "workers", "scouts", "mechanics")
_PREFERENCE_ROLES = {
    "reviewers": Role.REVIEWER,
    "workers": Role.WORKER,
    "scouts": Role.SCOUT,
    "mechanics": Role.MECHANIC,
}
_OVERRIDE_FIELDS = {
    "reviewer": "reviewers",
    "worker": "workers",
    "scout": "scouts",
    "mechanic": "mechanics",
}
_SCAN_FIELDS = {
    "layer": {"schema_version", "mode", "routes", "preferences", "capabilities"},
    "route": {
        "transport",
        "band",
        "roles",
        "read_only",
        "model",
        "provider_family",
        "variant",
        "command",
        "timeout_seconds",
        "retry_policy",
        "credential_env",
    },
    "preferences": set(_PREFERENCE_FIELDS),
    "retry_policy": {"worker_attempts", "review_revisions"},
    "credential_binding": {"child_name", "source_name"},
}
_CREDENTIAL_KEYS = {
    "apikey",
    "token",
    "accesstoken",
    "refreshtoken",
    "secret",
    "clientsecret",
    "password",
    "authorization",
    "credential",
    "credentials",
}
_NAMED_VALUE = re.compile(
    r"(?<![A-Za-z0-9])(?P<name>[A-Za-z][A-Za-z0-9_-]{0,127})\s*[:=]"
)
_NAMED_OPTION = re.compile(
    r"^-{1,2}(?P<name>[A-Za-z][A-Za-z0-9_-]{0,127})(?:=|$)"
)
_JWT_VALUE = re.compile(
    r"^[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}$"
)
_AWS_ACCESS_KEY = re.compile(r"^(?:AKIA|ASIA)[A-Z0-9]{16}$")
_GOOGLE_API_KEY = re.compile(r"^AIza[A-Za-z0-9_-]{20,}$")
_URL_USERINFO = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#@\s]+@"
)
_SENSITIVE_VALUE_OPTIONS = {"--key", "--api-key", "--token", "--password"}
_USERINFO_OPTIONS = {"--user", "-u"}


class ConfigError(ValueError):
    """A safe configuration error containing only a short path and reason."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class _DuplicateKeyError(ValueError):
    pass


@dataclass(frozen=True)
class _ParsedLayer:
    mode: Mode | None
    routes: dict[str, Route]
    preferences: dict[str, tuple[str, ...]]


LayerData = Mapping[str, object]
LayerInput = LayerData | Path


def _compact_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _name_parts(name: str) -> tuple[str, ...]:
    with_camel_boundaries = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return tuple(
        part
        for part in re.split(r"[^a-z0-9]+", with_camel_boundaries.lower())
        if part
    )


def _is_credential_name(name: str, *, embedded_single_terms: bool) -> bool:
    compact = _compact_key(name)
    if compact in _CREDENTIAL_KEYS:
        return True
    parts = _name_parts(name)
    pairs = tuple(zip(parts, parts[1:]))
    if any(
        pair in {
            ("api", "key"),
            ("access", "token"),
            ("refresh", "token"),
            ("client", "secret"),
        }
        for pair in pairs
    ):
        return True
    sensitive_single_terms = {
        "token",
        "secret",
        "password",
        "authorization",
        "credential",
        "credentials",
    }
    if parts and parts[-1] in sensitive_single_terms:
        return True
    return embedded_single_terms and any(
        part in sensitive_single_terms for part in parts
    )


def _is_credential_key(key: str) -> bool:
    return _is_credential_name(key, embedded_single_terms=True)


def _looks_like_credential_value(value: str) -> bool:
    stripped = value.strip()
    lowered = stripped.lower()
    named_values = _NAMED_VALUE.finditer(stripped)
    named_option = _NAMED_OPTION.match(stripped)
    return (
        lowered.startswith("bearer ")
        or lowered.startswith("sk-")
        or lowered.startswith(
            ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "xoxb-", "xoxp-")
        )
        or "-----begin private key-----" in lowered
        or any(
            _is_credential_name(
                named_value.group("name"),
                embedded_single_terms=False,
            )
            for named_value in named_values
        )
        or (
            named_option is not None
            and _is_credential_name(
                named_option.group("name"),
                embedded_single_terms=False,
            )
        )
        or _JWT_VALUE.fullmatch(stripped) is not None
        or _AWS_ACCESS_KEY.fullmatch(stripped) is not None
        or _GOOGLE_API_KEY.fullmatch(stripped) is not None
        or _URL_USERINFO.match(stripped) is not None
    )


def _child_path(parent: str, child: str) -> str:
    if not parent:
        return child
    return f"{parent}.{child}"


def _safe_path_component(key: str, context: str) -> str:
    if context == "routes":
        return "<route>"
    if key in _SCAN_FIELDS.get(context, set()):
        return key
    return "<field>"


def _child_scan_context(key: str, context: str) -> str:
    if context == "layer" and key == "routes":
        return "routes"
    if context == "layer" and key == "preferences":
        return "preferences"
    if context == "routes":
        return "route"
    if context == "route" and key == "retry_policy":
        return "retry_policy"
    if context == "route" and key == "credential_env":
        return "credential_binding"
    return "opaque"


def _scan_for_forbidden(
    value: object,
    path: str,
    *,
    context: str = "layer",
) -> None:
    """Reject immutable-main-loop and credential material before shape errors."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ConfigError(path or "config", "object keys must be strings")
            compact = _compact_key(key)
            if compact == "mainloop":
                raise ConfigError(
                    _child_path(path, "main_loop"),
                    "is host-controlled and cannot be configured",
                )
            approved_binding = context == "route" and key == "credential_env"
            if (compact == "credentialenv" and not approved_binding) or (
                not approved_binding and _is_credential_key(key)
            ):
                raise ConfigError(
                    _child_path(path, "<credential>"),
                    "credential material is not allowed",
                )
            child_path = _child_path(path, _safe_path_component(key, context))
            _scan_for_forbidden(
                child,
                child_path,
                context=_child_scan_context(key, context),
            )
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _scan_for_forbidden(
                child,
                f"{path}[{index}]",
                context=context,
            )
        return
    if isinstance(value, str) and _looks_like_credential_value(value):
        raise ConfigError(path or "config", "credential material is not allowed")


def _expect_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigError(path, "must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ConfigError(path, "object keys must be strings")
    return value  # type: ignore[return-value]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _reject_unknown_fields(
    value: Mapping[str, object],
    allowed: set[str],
    path: str,
) -> None:
    if any(key not in allowed for key in value):
        raise ConfigError(_child_path(path, "<field>"), "unsupported field")


def _validate_version(value: Mapping[str, object], source: str) -> None:
    if "schema_version" not in value:
        raise ConfigError(f"{source}.schema_version", "is required")
    version = value["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ConfigError(f"{source}.schema_version", "must be 1")


def _parse_enum(enum_type: type[Any], value: object, path: str, label: str) -> Any:
    if not isinstance(value, str):
        raise ConfigError(path, f"must be a supported {label}")
    try:
        return enum_type(value)
    except ValueError:
        raise ConfigError(path, f"must be a supported {label}") from None


def _parse_retry_policy(value: object, path: str) -> RetryPolicy:
    mapping = _expect_mapping(value, path)
    _reject_unknown_fields(mapping, {"worker_attempts", "review_revisions"}, path)
    values: dict[str, int] = {}
    for field_name, default in (
        ("worker_attempts", 3),
        ("review_revisions", 2),
    ):
        candidate = mapping.get(field_name, default)
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or not 0 <= candidate <= 10
        ):
            raise ConfigError(
                f"{path}.{field_name}",
                "must be an integer from 0 to 10",
            )
        values[field_name] = candidate
    return RetryPolicy(**values)


def _parse_credential_env(value: object, path: str) -> tuple[CredentialBinding, ...]:
    if not isinstance(value, list):
        raise ConfigError(path, "must be an array of environment-name bindings")
    bindings: list[CredentialBinding] = []
    child_names: set[str] = set()
    for index, raw_binding in enumerate(value):
        binding_path = f"{path}[{index}]"
        mapping = _expect_mapping(raw_binding, binding_path)
        _reject_unknown_fields(mapping, {"child_name", "source_name"}, binding_path)
        for field_name in ("child_name", "source_name"):
            candidate = mapping.get(field_name)
            if not isinstance(candidate, str) or _ENV_NAME.fullmatch(candidate) is None:
                raise ConfigError(
                    f"{binding_path}.{field_name}",
                    "must be an environment variable name",
                )
        child_name = mapping["child_name"]
        if child_name in child_names:
            raise ConfigError(
                f"{binding_path}.child_name",
                "must be unique within a route",
            )
        child_names.add(child_name)  # type: ignore[arg-type]
        bindings.append(
            CredentialBinding(
                child_name=mapping["child_name"],  # type: ignore[arg-type]
                source_name=mapping["source_name"],  # type: ignore[arg-type]
            )
        )
    return tuple(bindings)


def _validate_command_structure(command: list[str], path: str) -> None:
    for index, member in enumerate(command):
        option, separator, attached_value = member.partition("=")
        normalized_option = option.lower()
        if normalized_option in _SENSITIVE_VALUE_OPTIONS:
            raise ConfigError(
                f"{path}[{index}]",
                "credential-bearing command options are not allowed",
            )
        if normalized_option in _USERINFO_OPTIONS:
            candidate = attached_value if separator else None
            candidate_index = index
            if candidate is None and index + 1 < len(command):
                candidate = command[index + 1]
                candidate_index = index + 1
            if candidate is not None and ":" in candidate:
                raise ConfigError(
                    f"{path}[{candidate_index}]",
                    "credential-bearing command options are not allowed",
                )


def _parse_route(route_id: str, value: object) -> Route:
    path = "routes.<route>"
    if not isinstance(route_id, str) or not route_id.strip():
        raise ConfigError("routes.<route>", "route identifiers must be non-empty strings")
    mapping = _expect_mapping(value, path)
    allowed = {
        "transport",
        "band",
        "roles",
        "read_only",
        "model",
        "provider_family",
        "variant",
        "command",
        "timeout_seconds",
        "retry_policy",
        "credential_env",
    }
    _reject_unknown_fields(mapping, allowed, path)
    for required in ("transport", "band", "roles", "read_only"):
        if required not in mapping:
            raise ConfigError(f"{path}.{required}", "is required")

    transport = _parse_enum(
        Transport,
        mapping["transport"],
        f"{path}.transport",
        "transport",
    )
    band = _parse_enum(
        CapabilityBand,
        mapping["band"],
        f"{path}.band",
        "capability band",
    )

    raw_roles = mapping["roles"]
    if not isinstance(raw_roles, list) or not raw_roles:
        raise ConfigError(f"{path}.roles", "must be a non-empty role array")
    roles = tuple(
        _parse_enum(Role, role, f"{path}.roles[{index}]", "role")
        for index, role in enumerate(raw_roles)
    )
    if len(set(roles)) != len(roles):
        raise ConfigError(f"{path}.roles", "must not contain duplicates")

    read_only = mapping["read_only"]
    if not isinstance(read_only, bool):
        raise ConfigError(f"{path}.read_only", "must be a boolean")
    if Role.REVIEWER in roles and not read_only:
        raise ConfigError(f"{path}.read_only", "reviewer-capable routes must be read-only")

    text_fields: dict[str, str | None] = {}
    for field_name in ("model", "provider_family", "variant"):
        candidate = mapping.get(field_name)
        if candidate is not None and (
            not isinstance(candidate, str) or not candidate.strip()
        ):
            raise ConfigError(f"{path}.{field_name}", "must be a non-empty string or null")
        text_fields[field_name] = candidate

    command_present = "command" in mapping
    raw_command = mapping.get("command", [])
    if not isinstance(raw_command, list) or not all(
        isinstance(member, str) for member in raw_command
    ):
        raise ConfigError(f"{path}.command", "must be a JSON string argument array")
    for index, member in enumerate(raw_command):
        if "\0" in member:
            raise ConfigError(
                f"{path}.command[{index}]",
                "must not contain NUL",
            )
    _validate_command_structure(raw_command, f"{path}.command")
    if transport is Transport.EXTERNAL_CLI:
        if not raw_command or not raw_command[0].strip():
            raise ConfigError(f"{path}.command", "must contain a non-empty executable")
    elif command_present:
        raise ConfigError(f"{path}.command", "is only valid for external-cli routes")

    timeout_seconds = mapping.get("timeout_seconds", 600)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise ConfigError(f"{path}.timeout_seconds", "must be a positive integer")

    retry_policy = _parse_retry_policy(
        mapping.get("retry_policy", {}),
        f"{path}.retry_policy",
    )
    credential_env = _parse_credential_env(
        mapping.get("credential_env", []),
        f"{path}.credential_env",
    )
    try:
        return Route(
            route_id=route_id,
            transport=transport,
            band=band,
            roles=frozenset(roles),
            read_only=read_only,
            model=text_fields["model"],
            provider_family=text_fields["provider_family"],
            variant=text_fields["variant"],
            command=tuple(raw_command),
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            credential_env=credential_env,
        )
    except ValueError:
        raise ConfigError(path, "route contract is invalid") from None


def _parse_preferences(value: object, path: str) -> dict[str, tuple[str, ...]]:
    mapping = _expect_mapping(value, path)
    _reject_unknown_fields(mapping, set(_PREFERENCE_FIELDS), path)
    preferences: dict[str, tuple[str, ...]] = {}
    for field_name, raw_values in mapping.items():
        field_path = f"{path}.{field_name}"
        if not isinstance(raw_values, list) or not all(
            isinstance(route_id, str) and route_id.strip() for route_id in raw_values
        ):
            raise ConfigError(field_path, "must be an array of route identifiers")
        values = tuple(raw_values)
        if len(set(values)) != len(values):
            raise ConfigError(field_path, "must not contain duplicates")
        preferences[field_name] = values
    return preferences


def _parse_layer(value: LayerData, source: str) -> _ParsedLayer:
    path = source
    mapping = _expect_mapping(value, path)
    _scan_for_forbidden(mapping, path)
    _validate_version(mapping, source)
    allowed = {"schema_version", "mode", "routes", "preferences"}
    if source == "profile":
        allowed.add("capabilities")
    _reject_unknown_fields(mapping, allowed, path)

    mode: Mode | None = None
    if "mode" in mapping:
        mode = _parse_enum(Mode, mapping["mode"], f"{path}.mode", "mode")

    routes: dict[str, Route] = {}
    if "routes" in mapping:
        raw_routes = _expect_mapping(mapping["routes"], f"{path}.routes")
        routes = {
            route_id: _parse_route(route_id, route)
            for route_id, route in raw_routes.items()
        }

    preferences: dict[str, tuple[str, ...]] = {}
    if "preferences" in mapping:
        preferences = _parse_preferences(mapping["preferences"], f"{path}.preferences")

    if "capabilities" in mapping:
        _expect_mapping(mapping["capabilities"], f"{path}.capabilities")
    return _ParsedLayer(mode=mode, routes=routes, preferences=preferences)


def _layer_provenance(
    source: str,
    paths: Mapping[str, Path | str | None],
) -> Provenance:
    raw_path = paths.get(source)
    return Provenance(source, None if raw_path is None else Path(raw_path))


def _validate_preference_routes(
    preferences: Mapping[str, tuple[str, ...]],
    routes: Mapping[str, Route],
) -> None:
    for field_name, route_ids in preferences.items():
        required_role = _PREFERENCE_ROLES[field_name]
        for index, route_id in enumerate(route_ids):
            route = routes.get(route_id)
            path = f"preferences.{field_name}[{index}]"
            if route is None:
                raise ConfigError(path, "references an unknown route")
            if required_role not in route.roles:
                raise ConfigError(path, "route does not support this role")


def load_config_layers(
    *,
    profile: LayerData,
    user: LayerData | None = None,
    project: LayerData | None = None,
    explicit: LayerData | None = None,
    overrides: RunOverrides | None = None,
    paths: Mapping[str, Path | str | None] | None = None,
) -> LoadedConfig:
    """Merge validated layers while retaining per-field provenance.

    Routes are atomic: a route in a higher layer replaces the complete lower
    route.  Preference lists are independent atomic values.
    """

    source_paths = {} if paths is None else dict(paths)
    raw_layers = (
        ("profile", profile),
        ("user", user),
        ("project", project),
        ("explicit", explicit),
    )
    parsed_layers = [
        (source, _parse_layer(layer, source))
        for source, layer in raw_layers
        if layer is not None
    ]

    mode: Mode | None = None
    mode_provenance: Provenance | None = None
    routes: dict[str, Route] = {}
    route_provenance: dict[str, Provenance] = {}
    preference_values = {field_name: () for field_name in _PREFERENCE_FIELDS}
    default_provenance = _layer_provenance("profile", source_paths)
    preference_provenance = {
        field_name: default_provenance for field_name in _PREFERENCE_FIELDS
    }

    for source, layer in parsed_layers:
        provenance = _layer_provenance(source, source_paths)
        if layer.mode is not None:
            mode = layer.mode
            mode_provenance = provenance
        for route_id, route in layer.routes.items():
            routes[route_id] = route
            route_provenance[route_id] = provenance
        for field_name, values in layer.preferences.items():
            preference_values[field_name] = values
            preference_provenance[field_name] = provenance

    if mode is None or mode_provenance is None:
        raise ConfigError("mode", "must be supplied by a configuration layer")

    if overrides is not None and not isinstance(overrides, RunOverrides):
        raise ConfigError("overrides", "must be RunOverrides")
    effective_overrides = overrides or RunOverrides()
    explicit_provenance = Provenance("explicit")
    if effective_overrides.mode is not None:
        mode = effective_overrides.mode
        mode_provenance = explicit_provenance
    for override_field, preference_field in _OVERRIDE_FIELDS.items():
        route_id = getattr(effective_overrides, override_field)
        if route_id is not None:
            preference_values[preference_field] = (route_id,)
            preference_provenance[preference_field] = explicit_provenance

    _validate_preference_routes(preference_values, routes)
    preferences = Preferences(**preference_values)
    provenance = PreferenceProvenance(**preference_provenance)
    return LoadedConfig(
        mode=mode,
        routes=routes,
        preferences=preferences,
        mode_provenance=mode_provenance,
        route_provenance=route_provenance,
        preference_provenance=provenance,
    )


def discover_user_config_path(
    environ: Mapping[str, str] | None = None,
    *,
    config_root: Path | str | None = None,
) -> Path:
    """Return the user-config path without changing process environment."""

    if config_root is not None:
        root = Path(config_root)
        if not root.is_absolute():
            raise ConfigError(
                "environment.config_root",
                "must be absolute",
            )
    else:
        source = os.environ if environ is None else environ
        xdg_root = source.get("XDG_CONFIG_HOME")
        xdg_path = Path(xdg_root) if isinstance(xdg_root, str) and xdg_root else None
        if xdg_path is not None and xdg_path.is_absolute():
            root = xdg_path
        else:
            home = source.get("HOME")
            home_path = Path(home) if isinstance(home, str) and home else None
            if home_path is not None:
                if not home_path.is_absolute():
                    raise ConfigError(
                        "environment.config_root",
                        "requires an absolute XDG_CONFIG_HOME, HOME, or USERPROFILE",
                    )
                root = home_path / ".config"
            else:
                userprofile = source.get("USERPROFILE")
                userprofile_path = (
                    Path(userprofile)
                    if isinstance(userprofile, str) and userprofile
                    else None
                )
                if userprofile_path is None or not userprofile_path.is_absolute():
                    raise ConfigError(
                        "environment.config_root",
                        "requires an absolute XDG_CONFIG_HOME, HOME, or USERPROFILE",
                    )
                root = userprofile_path / ".config"
    return root / "model-boss" / "config.json"


def discover_project_config_path(repo_root: Path | str) -> Path:
    return Path(repo_root) / ".model-boss.json"


def _read_json_file(path: Path, source: str) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ConfigError(source, "configuration file could not be read") from None
    try:
        value = json.loads(text, object_pairs_hook=_unique_json_object)
    except _DuplicateKeyError:
        raise ConfigError(
            f"{source}.<duplicate>",
            "duplicate object keys are not allowed",
        ) from None
    except (json.JSONDecodeError, UnicodeError):
        raise ConfigError(source, "configuration file is not valid JSON") from None
    mapping = _expect_mapping(value, source)
    return dict(mapping)


def load_profile_data(
    profile_name: str,
    *,
    profiles_root: Path | str | None = None,
) -> dict[str, object]:
    """Load a named built-in profile as validated credential-free JSON data."""

    if not isinstance(profile_name, str) or _PROFILE_NAME.fullmatch(profile_name) is None:
        raise ConfigError("profile", "profile name is invalid")
    root = _DEFAULT_PROFILES_ROOT if profiles_root is None else Path(profiles_root)
    path = root / f"{profile_name}.json"
    data = _read_json_file(path, "profile")
    _scan_for_forbidden(data, "profile")
    _validate_version(data, "profile")
    _parse_layer(data, "profile")
    return data


def _resolve_layer_input(
    value: LayerInput | None,
    source: str,
    *,
    discovered_path: Path | None = None,
) -> tuple[dict[str, object] | None, Path | None]:
    if value is None:
        if discovered_path is None or not discovered_path.is_file():
            return None, None
        return _read_json_file(discovered_path, source), discovered_path
    if isinstance(value, Path):
        return _read_json_file(value, source), value
    if isinstance(value, Mapping):
        return dict(value), None
    raise ConfigError(source, "must be an object or a JSON file path")


def load_config(
    profile: str | Path | LayerData = "anthropic",
    *,
    repo_root: Path | str | None = None,
    project_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    config_root: Path | str | None = None,
    profiles_root: Path | str | None = None,
    user_config: LayerInput | None = None,
    project_config: LayerInput | None = None,
    explicit_config: LayerInput | None = None,
    overrides: RunOverrides | None = None,
    discover: bool = True,
) -> LoadedConfig:
    """Load profile, user, project, and optional explicit run configuration."""

    if repo_root is not None and project_root is not None:
        raise ConfigError("project_root", "specify only one project root")
    selected_project_root = project_root if project_root is not None else repo_root

    profile_path: Path | None = None
    if isinstance(profile, Path):
        profile_data = _read_json_file(profile, "profile")
        profile_path = profile
    elif isinstance(profile, str):
        profile_data = load_profile_data(profile, profiles_root=profiles_root)
        root = _DEFAULT_PROFILES_ROOT if profiles_root is None else Path(profiles_root)
        profile_path = root / f"{profile}.json"
    elif isinstance(profile, Mapping):
        profile_data = dict(profile)
    else:
        raise ConfigError("profile", "must be a profile name, object, or JSON file path")

    discovered_user = None
    discovered_project = None
    if discover:
        if user_config is None:
            discovered_user = discover_user_config_path(environ, config_root=config_root)
        if project_config is None and selected_project_root is not None:
            discovered_project = discover_project_config_path(selected_project_root)

    user_data, user_path = _resolve_layer_input(
        user_config,
        "user",
        discovered_path=discovered_user,
    )
    project_data, project_path = _resolve_layer_input(
        project_config,
        "project",
        discovered_path=discovered_project,
    )
    explicit_data, explicit_path = _resolve_layer_input(explicit_config, "explicit")
    return load_config_layers(
        profile=profile_data,
        user=user_data,
        project=project_data,
        explicit=explicit_data,
        overrides=overrides,
        paths={
            "profile": profile_path,
            "user": user_path,
            "project": project_path,
            "explicit": explicit_path,
        },
    )


# Straightforward aliases for hosts that prefer noun-first discovery naming.
user_config_path = discover_user_config_path
project_config_path = discover_project_config_path
