from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from runtime.model_boss.config import (
    ConfigError,
    discover_project_config_path,
    discover_user_config_path,
    load_config,
    load_config_layers,
    load_profile_data,
)
from runtime.model_boss.models import (
    CapabilityBand,
    CredentialBinding,
    LoadedConfig,
    MainLoop,
    Mode,
    ModelFingerprint,
    PreferenceProvenance,
    Preferences,
    Provenance,
    RetryPolicy,
    Role,
    Route,
    RunOverrides,
    Status,
    Transport,
)


def _review_route(model: str = "review-v1") -> dict[str, object]:
    return {
        "transport": "host-subagent",
        "band": "authority",
        "roles": ["reviewer"],
        "read_only": True,
        "model": model,
        "provider_family": "test",
    }


def _worker_route(model: str = "worker-v1") -> dict[str, object]:
    return {
        "transport": "host-subagent",
        "band": "balanced",
        "roles": ["worker"],
        "read_only": False,
        "model": model,
        "provider_family": "test",
    }


def _scout_route(model: str = "scout-v1") -> dict[str, object]:
    return {
        "transport": "host-subagent",
        "band": "fast",
        "roles": ["scout"],
        "read_only": True,
        "model": model,
        "provider_family": "test",
    }


def _mechanic_route(model: str = "mechanic-v1") -> dict[str, object]:
    return {
        "transport": "host-subagent",
        "band": "fast",
        "roles": ["mechanic"],
        "read_only": False,
        "model": model,
        "provider_family": "test",
    }


def _base_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "auto",
        "routes": {
            "review": _review_route(),
            "worker": _worker_route(),
            "scout": _scout_route(),
            "mechanic": _mechanic_route(),
        },
        "preferences": {
            "reviewers": ["review"],
            "workers": ["worker"],
            "scouts": ["scout"],
            "mechanics": ["mechanic"],
        },
    }


def _layer(**values: object) -> dict[str, object]:
    return {"schema_version": 1, **values}


class ModelContractTests(unittest.TestCase):
    def test_string_enums_contain_the_approved_values(self) -> None:
        self.assertEqual({mode.value for mode in Mode}, {"auto", "lite", "max"})
        self.assertEqual(
            {band.value for band in CapabilityBand},
            {"authority", "balanced", "fast"},
        )
        self.assertEqual(
            {role.value for role in Role},
            {"reviewer", "worker", "scout", "mechanic"},
        )
        self.assertEqual(
            {transport.value for transport in Transport},
            {"host-subagent", "external-cli"},
        )
        self.assertEqual(
            {status.value for status in Status},
            {
                "ok",
                "needs_context",
                "gate_failed",
                "provider_unavailable",
                "reviewer_unavailable",
                "timeout",
                "scope_violation",
                "transport_error",
                "review_revise",
                "approval_stale",
                "destination_changed",
                "sandbox_unavailable",
            },
        )

    def test_fingerprint_is_frozen_and_has_lower_case_canonical_form(self) -> None:
        fingerprint = ModelFingerprint("Anthropic", "Claude-OPUS", "Thinking")
        self.assertEqual(
            fingerprint.canonical,
            "anthropic:claude-opus:thinking",
        )
        with self.assertRaises(FrozenInstanceError):
            fingerprint.variant = "changed"  # type: ignore[misc]

    def test_retry_policy_rejects_values_outside_zero_to_ten(self) -> None:
        self.assertEqual(RetryPolicy().worker_attempts, 3)
        self.assertEqual(RetryPolicy().review_revisions, 2)
        for value in (-1, 11):
            with self.subTest(value=value, field="worker_attempts"):
                with self.assertRaises(ValueError):
                    RetryPolicy(worker_attempts=value)
            with self.subTest(value=value, field="review_revisions"):
                with self.assertRaises(ValueError):
                    RetryPolicy(review_revisions=value)

    def test_credential_binding_accepts_names_not_values(self) -> None:
        binding = CredentialBinding("CHILD_API_KEY", "SOURCE_API_KEY")
        self.assertEqual(binding.child_name, "CHILD_API_KEY")
        for name in ("lowercase", "1STARTS_WITH_DIGIT", "$EXPANSION", "HAS-DASH"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    CredentialBinding(name, "SOURCE_API_KEY")

    def test_main_loop_is_frozen_host_input(self) -> None:
        main_loop = MainLoop(
            route_id="host",
            fingerprint=ModelFingerprint("test", "authority"),
            band=CapabilityBand.AUTHORITY,
            host="unit-test-host",
        )
        with self.assertRaises(FrozenInstanceError):
            main_loop.route_id = "other"  # type: ignore[misc]

    def test_route_roles_are_an_order_independent_immutable_set(self) -> None:
        route = Route(
            route_id="multi-role",
            transport=Transport.HOST_SUBAGENT,
            band=CapabilityBand.FAST,
            roles=(Role.MECHANIC, Role.SCOUT),
            read_only=False,
        )
        self.assertIsInstance(route.roles, frozenset)
        self.assertEqual(route.roles, frozenset({Role.SCOUT, Role.MECHANIC}))

    def test_route_variant_does_not_break_legacy_positional_command(self) -> None:
        route = Route(
            "legacy-worker",
            Transport.EXTERNAL_CLI,
            CapabilityBand.BALANCED,
            frozenset({Role.WORKER}),
            False,
            "worker-model",
            "test-provider",
            ("worker-bin", "--json"),
        )
        self.assertEqual(route.command, ("worker-bin", "--json"))
        self.assertIsNone(route.variant)

    def test_route_rejects_duplicate_credential_child_names(self) -> None:
        duplicates = (
            (
                CredentialBinding("CHILD_KEY", "SOURCE_ONE"),
                CredentialBinding("CHILD_KEY", "SOURCE_ONE"),
            ),
            (
                CredentialBinding("CHILD_KEY", "SOURCE_ONE"),
                CredentialBinding("CHILD_KEY", "SOURCE_TWO"),
            ),
        )
        for credential_env in duplicates:
            with self.subTest(sources=tuple(b.source_name for b in credential_env)):
                with self.assertRaisesRegex(ValueError, "child_name"):
                    Route(
                        route_id="worker",
                        transport=Transport.HOST_SUBAGENT,
                        band=CapabilityBand.BALANCED,
                        roles=frozenset({Role.WORKER}),
                        read_only=False,
                        credential_env=credential_env,
                    )

    def test_route_rejects_nul_in_every_command_element(self) -> None:
        for command in (("worker\0bin",), ("worker-bin", "bad\0argument")):
            with self.subTest(command_index=0 if "\0" in command[0] else 1):
                with self.assertRaisesRegex(ValueError, "command"):
                    Route(
                        route_id="worker",
                        transport=Transport.EXTERNAL_CLI,
                        band=CapabilityBand.BALANCED,
                        roles=frozenset({Role.WORKER}),
                        read_only=False,
                        command=command,
                    )

    def test_loaded_config_requires_route_map_keys_to_match_route_ids(self) -> None:
        route = Route(
            route_id="worker",
            transport=Transport.HOST_SUBAGENT,
            band=CapabilityBand.BALANCED,
            roles=frozenset({Role.WORKER}),
            read_only=False,
        )
        provenance = Provenance("profile")
        preference_provenance = PreferenceProvenance(
            reviewers=provenance,
            workers=provenance,
            scouts=provenance,
            mechanics=provenance,
        )
        with self.assertRaisesRegex(ValueError, "route identifiers"):
            LoadedConfig(
                mode=Mode.AUTO,
                routes={"mismatched-alias": route},
                preferences=Preferences(),
                mode_provenance=provenance,
                route_provenance={"mismatched-alias": provenance},
                preference_provenance=preference_provenance,
            )


class ConfigValidationTests(unittest.TestCase):
    def assert_config_error_redacts(
        self,
        callable_obj: object,
        *forbidden_values: str,
    ) -> ConfigError:
        with self.assertRaises(ConfigError) as raised:
            callable_obj()  # type: ignore[operator]
        message = str(raised.exception)
        for value in forbidden_values:
            self.assertNotIn(value, message)
        self.assertLessEqual(len(message), 180)
        return raised.exception

    def test_rejects_main_loop_recursively_at_every_layer(self) -> None:
        cases = {
            "profile": {"profile": _layer(nested={"main_loop": "immutable"})},
            "user": {"profile": _base_profile(), "user": _layer(main_loop={})},
            "project": {
                "profile": _base_profile(),
                "project": _layer(nested={"main_loop": {}}),
            },
            "explicit": {
                "profile": _base_profile(),
                "explicit": _layer(main_loop={"model": "must-not-leak"}),
            },
        }
        for source, kwargs in cases.items():
            with self.subTest(source=source):
                error = self.assert_config_error_redacts(
                    lambda kwargs=kwargs: load_config_layers(**kwargs),
                    "immutable",
                    "must-not-leak",
                )
                self.assertIn("main_loop", str(error))

    def test_accepts_only_schema_version_one(self) -> None:
        for version in (0, 2):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ConfigError, "schema_version"):
                    load_config_layers(profile={**_base_profile(), "schema_version": version})

    def test_rejects_invalid_route_enums_without_echoing_values(self) -> None:
        cases = (
            ("band", "classified-band"),
            ("roles", ["classified-role"]),
            ("transport", "classified-transport"),
        )
        for field, rejected in cases:
            profile = _base_profile()
            profile["routes"]["worker"][field] = rejected  # type: ignore[index]
            secret_text = rejected[0] if isinstance(rejected, list) else rejected
            with self.subTest(field=field):
                error = self.assert_config_error_redacts(
                    lambda profile=profile: load_config_layers(profile=profile),
                    secret_text,
                )
                self.assertIn(field, str(error))
                self.assertIsNone(error.__cause__)

    def test_reviewer_capable_routes_must_be_read_only(self) -> None:
        profile = _base_profile()
        profile["routes"]["review"]["read_only"] = False  # type: ignore[index]
        with self.assertRaisesRegex(ConfigError, "read_only"):
            load_config_layers(profile=profile)

    def test_external_command_is_a_non_empty_string_array(self) -> None:
        invalid_commands: tuple[object, ...] = (
            "worker --run",
            [],
            [""],
            ["ultra-secret-command", 42],
        )
        for command in invalid_commands:
            profile = _base_profile()
            profile["routes"]["worker"] = {
                "transport": "external-cli",
                "band": "balanced",
                "roles": ["worker"],
                "read_only": False,
                "command": command,
            }
            with self.subTest(command_type=type(command).__name__):
                error = self.assert_config_error_redacts(
                    lambda profile=profile: load_config_layers(profile=profile),
                    "worker --run",
                    "ultra-secret-command",
                )
                self.assertIn("routes.<route>.command", str(error))

        profile = _base_profile()
        profile["routes"]["worker"] = {
            "transport": "external-cli",
            "band": "balanced",
            "roles": ["worker"],
            "read_only": False,
            "command": ["worker-bin", ""],
        }
        loaded = load_config_layers(profile=profile)
        self.assertEqual(loaded.routes["worker"].command, ("worker-bin", ""))

    def test_external_command_rejects_credential_flags_and_redacts_argv(self) -> None:
        secret = "plain-never-echo-command-secret"
        commands = (
            ["worker-bin", "--api-key", secret],
            ["worker-bin", f"--api-key={secret}"],
            ["worker-bin", "--access_token", secret],
            ["worker-bin", f"OPENAI_API_KEY={secret}"],
            ["worker-bin", f"X-API-Key: {secret}"],
            ["worker-bin", f"--header=X-API-Key: {secret}"],
            ["worker-bin", f"--header=Authorization: Basic {secret}"],
            ["worker-bin", f"--env=OPENAI_API_KEY={secret}"],
        )
        for command in commands:
            profile = _base_profile()
            profile["routes"]["worker"] = {
                "transport": "external-cli",
                "band": "balanced",
                "roles": ["worker"],
                "read_only": False,
                "command": command,
            }
            with self.subTest(command_index=commands.index(command)):
                error = self.assert_config_error_redacts(
                    lambda profile=profile: load_config_layers(profile=profile),
                    secret,
                    "--api-key",
                    "--access_token",
                    "OPENAI_API_KEY",
                    "X-API-Key",
                    "Authorization",
                )
                self.assertIn("credential", str(error).lower())

    def test_external_command_rejects_structural_credentials_and_userinfo(self) -> None:
        secret = "structural-never-echo-password"
        commands = (
            ["worker-bin", "--key", secret],
            ["worker-bin", "--api-key", secret],
            ["worker-bin", "--token", secret],
            ["worker-bin", "--password", secret],
            ["worker-bin", "--user", f"user:{secret}"],
            ["worker-bin", "-u", f"user:{secret}"],
            ["worker-bin", f"https://user:{secret}@example.test/path"],
        )
        for index, command in enumerate(commands):
            profile = _base_profile()
            profile["routes"]["worker"] = {
                "transport": "external-cli",
                "band": "balanced",
                "roles": ["worker"],
                "read_only": False,
                "command": command,
            }
            with self.subTest(command_index=index):
                error = self.assert_config_error_redacts(
                    lambda profile=profile: load_config_layers(profile=profile),
                    secret,
                    f"user:{secret}",
                    f"https://user:{secret}@example.test/path",
                )
                self.assertIn("credential", str(error).lower())

    def test_external_command_rejects_every_colon_bearing_user_value(self) -> None:
        opaque_username = "qzv471"
        opaque_password = "mnb842"
        values = (
            ("leading-colon", f":{opaque_password}"),
            ("trailing-colon", f"{opaque_username}:"),
            ("both-sides", f"{opaque_username}:{opaque_password}"),
        )
        for option in ("--user", "-u"):
            for value_shape, value in values:
                profile = _base_profile()
                profile["routes"]["worker"] = {
                    "transport": "external-cli",
                    "band": "balanced",
                    "roles": ["worker"],
                    "read_only": False,
                    "command": ["worker-bin", option, value],
                }
                with self.subTest(option=option, value_shape=value_shape):
                    error = self.assert_config_error_redacts(
                        lambda profile=profile: load_config_layers(profile=profile),
                        opaque_username,
                        opaque_password,
                        value,
                    )
                    self.assertIn("credential", str(error).lower())

    def test_external_command_rejects_opaque_url_authority_userinfo(self) -> None:
        opaque_userinfo = "qzv471mnb842"
        credential_url = f"https://{opaque_userinfo}@example.test/"
        profile = _base_profile()
        profile["routes"]["worker"] = {
            "transport": "external-cli",
            "band": "balanced",
            "roles": ["worker"],
            "read_only": False,
            "command": ["worker-bin", credential_url],
        }
        error = self.assert_config_error_redacts(
            lambda: load_config_layers(profile=profile),
            opaque_userinfo,
            credential_url,
        )
        self.assertIn("credential", str(error).lower())

    def test_credential_detection_does_not_reject_benign_similar_words(self) -> None:
        profile = _base_profile()
        profile["routes"]["worker"] = {
            "transport": "external-cli",
            "band": "balanced",
            "roles": ["worker"],
            "read_only": False,
            "model": "tokenizer-v2",
            "command": [
                "worker-bin",
                "--token-budget",
                "100",
                "OPENAI_API_KEYSTONE=value",
                "X-API-Keynote: chapter",
                "--env=OPENAI_API_KEYSTONE=value",
                "--header=X-API-Keynote: chapter",
                "--header=Authorization-Mode: basic",
                "--keyboard",
                "--user",
                "alice",
                "-u",
                "alice",
                "https://example.test/path",
                "https://example.test/path/@opaque-user",
                "https://example.test/search?q=opaque-user@example.test",
            ],
        }
        loaded = load_config_layers(profile=profile)
        self.assertEqual(loaded.routes["worker"].model, "tokenizer-v2")

    def test_retry_values_are_validated_in_config(self) -> None:
        for field in ("worker_attempts", "review_revisions"):
            for value in (-1, 11):
                profile = _base_profile()
                profile["routes"]["worker"]["retry_policy"] = {field: value}  # type: ignore[index]
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ConfigError, f"retry_policy.{field}"):
                        load_config_layers(profile=profile)

    def test_credential_environment_references_are_validated(self) -> None:
        valid = _base_profile()
        valid["routes"]["worker"]["credential_env"] = [  # type: ignore[index]
            {"child_name": "CHILD_API_KEY", "source_name": "SOURCE_API_KEY"}
        ]
        loaded = load_config_layers(profile=valid)
        self.assertEqual(
            loaded.routes["worker"].credential_env,
            (CredentialBinding("CHILD_API_KEY", "SOURCE_API_KEY"),),
        )

        for field, name in (
            ("child_name", "child_api_key"),
            ("source_name", "$SOURCE_API_KEY"),
        ):
            profile = _base_profile()
            binding = {"child_name": "CHILD_API_KEY", "source_name": "SOURCE_API_KEY"}
            binding[field] = name
            profile["routes"]["worker"]["credential_env"] = [binding]  # type: ignore[index]
            with self.subTest(field=field):
                error = self.assert_config_error_redacts(
                    lambda profile=profile: load_config_layers(profile=profile),
                    name,
                )
                self.assertIn(f"routes.<route>.credential_env[0].{field}", str(error))

    def test_config_rejects_duplicate_credential_child_names(self) -> None:
        for sources in (("SOURCE_ONE", "SOURCE_ONE"), ("SOURCE_ONE", "SOURCE_TWO")):
            profile = _base_profile()
            profile["routes"]["worker"]["credential_env"] = [  # type: ignore[index]
                {"child_name": "CHILD_KEY", "source_name": sources[0]},
                {"child_name": "CHILD_KEY", "source_name": sources[1]},
            ]
            with self.subTest(sources=sources):
                with self.assertRaisesRegex(
                    ConfigError,
                    r"routes\.<route>\.credential_env\[1\]\.child_name",
                ):
                    load_config_layers(profile=profile)

    def test_config_rejects_nul_in_every_command_element(self) -> None:
        for command in (["worker\0bin"], ["worker-bin", "bad\0argument"]):
            profile = _base_profile()
            profile["routes"]["worker"] = {
                "transport": "external-cli",
                "band": "balanced",
                "roles": ["worker"],
                "read_only": False,
                "command": command,
            }
            nul_index = 0 if "\0" in command[0] else 1
            with self.subTest(command_index=nul_index):
                error = self.assert_config_error_redacts(
                    lambda profile=profile: load_config_layers(profile=profile),
                    command[nul_index],
                )
                self.assertIn(f"command[{nul_index}]", error.path)

    def test_host_route_rejects_command_property_even_when_empty(self) -> None:
        profile = _base_profile()
        profile["routes"]["worker"]["command"] = []  # type: ignore[index]
        with self.assertRaisesRegex(ConfigError, r"routes\.<route>\.command"):
            load_config_layers(profile=profile)

    def test_nested_duplicate_json_keys_are_rejected_before_shadowing(self) -> None:
        secret = "duplicate-shadow-never-echo"
        safe_command = "duplicate-safe-executable"
        raw_profile = f"""{{
          "schema_version": 1,
          "mode": "auto",
          "routes": {{
            "worker": {{
              "transport": "external-cli",
              "band": "balanced",
              "roles": ["worker"],
              "read_only": false,
              "command": ["credential-command", "--api-key", "{secret}"],
              "command": ["{safe_command}"]
            }}
          }},
          "preferences": {{"workers": ["worker"]}}
        }}"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "duplicate.json"
            path.write_text(raw_profile, encoding="utf-8")
            error = self.assert_config_error_redacts(
                lambda: load_config(path, discover=False),
                secret,
                safe_command,
                "credential-command",
            )
        self.assertIn("duplicate", str(error).lower())
        self.assertIsNone(error.__cause__)

    def test_rejects_credential_like_keys_and_values_recursively(self) -> None:
        secret = "never-echo-this-secret-material"
        cases = (
            {"nested": {"api_key": secret}},
            {"nested": [{"token": secret}]},
            {"nested": {"secret": secret}},
            {"nested": {"password": secret}},
            {"nested": {"authorization": secret}},
            {"nested": {"header": f"Bearer {secret}"}},
            {"nested": {"header": f"sk-{secret}"}},
            {"nested": {"header": f"ghp_{secret}"}},
            {"nested": {"header": "AKIAIOSFODNN7EXAMPLE"}},
        )
        for payload in cases:
            with self.subTest(payload_key=next(iter(payload))):
                error = self.assert_config_error_redacts(
                    lambda payload=payload: load_config_layers(
                        profile=_base_profile(),
                        user=_layer(**payload),
                    ),
                    secret,
                    f"Bearer {secret}",
                    f"sk-{secret}",
                    f"ghp_{secret}",
                    "AKIAIOSFODNN7EXAMPLE",
                )
                self.assertIn("credential", str(error).lower())

    def test_credential_key_path_does_not_echo_secret_key_text(self) -> None:
        secret_key = "token-never-echo-key-material"
        error = self.assert_config_error_redacts(
            lambda: load_config_layers(
                profile=_base_profile(),
                user=_layer(nested={secret_key: "value"}),
            ),
            secret_key,
            "never-echo-key-material",
        )
        self.assertIn("<credential>", error.path)

    def test_rejects_provider_prefixed_credential_keys(self) -> None:
        secret = "provider-prefixed-never-echo"
        cases = (
            {"openai_api_key": secret},
            {"provider": {"anthropic_access_token": secret}},
            {"provider": {"customClientSecret": secret}},
        )
        for payload in cases:
            with self.subTest(payload=tuple(payload)):
                error = self.assert_config_error_redacts(
                    lambda payload=payload: load_config_layers(
                        profile=_base_profile(),
                        user=_layer(**payload),
                    ),
                    secret,
                    "openai_api_key",
                    "anthropic_access_token",
                    "customClientSecret",
                )
                self.assertIn("<credential>", error.path)

    def test_credential_env_exemption_is_limited_to_route_bindings(self) -> None:
        secret = "credential-env-scope-never-echo"
        profile = _base_profile()
        profile["capabilities"] = {
            "metadata": {
                "credential_env": {
                    "header": f"Bearer {secret}",
                }
            }
        }
        error = self.assert_config_error_redacts(
            lambda: load_config_layers(profile=profile),
            secret,
            f"Bearer {secret}",
        )
        self.assertIn("credential", str(error).lower())

        error = self.assert_config_error_redacts(
            lambda: load_config_layers(
                profile=_base_profile(),
                user=_layer(credential_env={"value": secret}),
            ),
            secret,
        )
        self.assertIn("credential", str(error).lower())

    def test_error_paths_redact_all_arbitrary_mapping_keys(self) -> None:
        secret_key = "attacker-controlled-never-echo-key"
        profile = _base_profile()
        profile["capabilities"] = {
            secret_key: {
                "header": "Bearer credential-material",
            }
        }
        error = self.assert_config_error_redacts(
            lambda: load_config_layers(profile=profile),
            secret_key,
            "attacker-controlled-never-echo-key",
            "credential-material",
        )
        self.assertIn("profile.capabilities.<field>.<field>", error.path)

        route_key = "route-id-never-echo-key"
        profile = _base_profile()
        profile["routes"][route_key] = {**_worker_route(), "transport": "bogus"}  # type: ignore[index]
        error = self.assert_config_error_redacts(
            lambda: load_config_layers(profile=profile),
            route_key,
            "route-id-never-echo-key",
            "bogus",
        )
        self.assertIn("routes.<route>.transport", error.path)


class ConfigMergeTests(unittest.TestCase):
    def test_project_preferences_override_user_and_profile(self) -> None:
        profile = _base_profile()
        profile["routes"]["user-worker"] = _worker_route("user")  # type: ignore[index]
        profile["routes"]["project-worker"] = _worker_route("project")  # type: ignore[index]
        loaded = load_config_layers(
            profile=profile,
            user=_layer(preferences={"workers": ["user-worker"]}),
            project=_layer(preferences={"workers": ["project-worker"]}),
        )
        self.assertEqual(loaded.preferences.workers, ("project-worker",))
        self.assertEqual(loaded.preference_provenance.workers.source, "project")

    def test_route_override_replaces_the_whole_route(self) -> None:
        profile = _base_profile()
        profile["routes"]["worker"] = {
            "transport": "external-cli",
            "band": "balanced",
            "roles": ["worker"],
            "read_only": False,
            "command": ["worker-bin", "--json"],
            "timeout_seconds": 42,
            "retry_policy": {"worker_attempts": 9, "review_revisions": 7},
            "credential_env": [
                {"child_name": "CHILD_KEY", "source_name": "SOURCE_KEY"}
            ],
        }
        loaded = load_config_layers(
            profile=profile,
            project=_layer(routes={"worker": _worker_route("replacement")}),
        )
        route = loaded.routes["worker"]
        self.assertEqual(route.transport, Transport.HOST_SUBAGENT)
        self.assertEqual(route.command, ())
        self.assertEqual(route.timeout_seconds, 600)
        self.assertEqual(route.retry_policy, RetryPolicy())
        self.assertEqual(route.credential_env, ())
        self.assertEqual(route.model, "replacement")
        self.assertEqual(loaded.route_provenance["worker"].source, "project")

    def test_each_preference_list_is_replaced_independently(self) -> None:
        profile = _base_profile()
        profile["routes"]["worker-two"] = _worker_route("worker-two")  # type: ignore[index]
        profile["routes"]["scout-two"] = _scout_route("scout-two")  # type: ignore[index]
        loaded = load_config_layers(
            profile=profile,
            user=_layer(preferences={"workers": ["worker-two"]}),
            project=_layer(preferences={"scouts": ["scout-two"]}),
        )
        self.assertEqual(loaded.preferences.reviewers, ("review",))
        self.assertEqual(loaded.preferences.workers, ("worker-two",))
        self.assertEqual(loaded.preferences.scouts, ("scout-two",))
        self.assertEqual(loaded.preferences.mechanics, ("mechanic",))
        self.assertEqual(loaded.preference_provenance.reviewers.source, "profile")
        self.assertEqual(loaded.preference_provenance.workers.source, "user")
        self.assertEqual(loaded.preference_provenance.scouts.source, "project")

    def test_mode_provenance_tracks_config_and_run_override(self) -> None:
        project_path = Path("/repo/.model-boss.json")
        configured = load_config_layers(
            profile=_base_profile(),
            project=_layer(mode="max"),
            paths={"project": project_path},
        )
        self.assertEqual(configured.mode, Mode.MAX)
        self.assertEqual(configured.mode_provenance.source, "project")
        self.assertEqual(configured.mode_provenance.path, project_path)

        overridden = load_config_layers(
            profile=_base_profile(),
            project=_layer(mode="max"),
            overrides=RunOverrides(mode=Mode.LITE, worker="worker"),
        )
        self.assertEqual(overridden.mode, Mode.LITE)
        self.assertEqual(overridden.mode_provenance.source, "explicit")
        self.assertEqual(overridden.preferences.workers, ("worker",))
        self.assertEqual(overridden.preference_provenance.workers.source, "explicit")


class ConfigDiscoveryTests(unittest.TestCase):
    def write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_user_path_uses_xdg_then_home_fallback_without_mutating_environment(self) -> None:
        process_home = os.environ.get("HOME")
        self.assertEqual(
            discover_user_config_path(
                {"XDG_CONFIG_HOME": "/xdg/config", "HOME": "/ignored/home"}
            ),
            Path("/xdg/config/model-boss/config.json"),
        )
        self.assertEqual(
            discover_user_config_path({"HOME": "/chosen/home"}),
            Path("/chosen/home/.config/model-boss/config.json"),
        )
        self.assertEqual(os.environ.get("HOME"), process_home)

    def test_relative_xdg_root_falls_back_to_absolute_home(self) -> None:
        self.assertEqual(
            discover_user_config_path(
                {"XDG_CONFIG_HOME": "relative/xdg", "HOME": "/absolute/home"}
            ),
            Path("/absolute/home/.config/model-boss/config.json"),
        )

    def test_userprofile_is_used_only_when_home_is_absent(self) -> None:
        try:
            userprofile_path = discover_user_config_path(
                {"USERPROFILE": "/windows/user"}
            )
        except ConfigError as exc:
            self.fail(f"absolute USERPROFILE was rejected: {exc}")
        self.assertEqual(
            userprofile_path,
            Path("/windows/user/.config/model-boss/config.json"),
        )
        self.assertEqual(
            discover_user_config_path(
                {"HOME": "/preferred/home", "USERPROFILE": "/windows/user"}
            ),
            Path("/preferred/home/.config/model-boss/config.json"),
        )
        with self.assertRaises(ConfigError):
            discover_user_config_path(
                {"HOME": "relative/home", "USERPROFILE": "/windows/user"}
            )

    def test_discovery_rejects_relative_or_missing_environment_roots(self) -> None:
        invalid_environments = (
            {"XDG_CONFIG_HOME": "relative/xdg", "HOME": "relative/home"},
            {"XDG_CONFIG_HOME": "relative/xdg"},
        )
        for environ in invalid_environments:
            with self.subTest(environ=tuple(environ)):
                with self.assertRaises(ConfigError) as raised:
                    discover_user_config_path(environ)
                message = str(raised.exception)
                for value in environ.values():
                    self.assertNotIn(value, message)
                self.assertIsNone(raised.exception.__cause__)

        with self.assertRaises(ConfigError):
            discover_user_config_path({}, config_root="relative/config")

    def test_project_path_is_root_dot_model_boss_json(self) -> None:
        self.assertEqual(
            discover_project_config_path(Path("/work/repository")),
            Path("/work/repository/.model-boss.json"),
        )

    def test_load_config_discovers_xdg_and_project_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xdg = root / "xdg"
            repo = root / "repo"
            repo.mkdir()
            self.write_json(
                xdg / "model-boss" / "config.json",
                _layer(mode="lite", preferences={"workers": ["worker"]}),
            )
            self.write_json(
                repo / ".model-boss.json",
                _layer(mode="max", preferences={"reviewers": ["review"]}),
            )
            loaded = load_config(
                profile=_base_profile(),
                repo_root=repo,
                environ={"XDG_CONFIG_HOME": str(xdg), "HOME": str(root / "home")},
            )
            self.assertEqual(loaded.mode, Mode.MAX)
            self.assertEqual(loaded.mode_provenance.source, "project")
            self.assertEqual(loaded.preferences.workers, ("worker",))
            self.assertEqual(loaded.preference_provenance.workers.source, "user")
            self.assertEqual(loaded.preference_provenance.reviewers.source, "project")

    def test_discovery_ignores_legacy_json_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xdg = root / "xdg"
            repo = root / "repo"
            repo.mkdir()
            self.write_json(
                xdg / "token-saver" / "config.json",
                _layer(mode="lite"),
            )
            self.write_json(repo / ".token-saver.json", _layer(mode="max"))

            loaded = load_config(
                profile=_base_profile(),
                repo_root=repo,
                environ={"XDG_CONFIG_HOME": str(xdg), "HOME": str(root / "home")},
            )

            self.assertEqual(loaded.mode, Mode.AUTO)
            self.assertEqual(loaded.mode_provenance.source, "profile")

    def test_explicit_layer_inputs_do_not_require_discovery_environment(self) -> None:
        loaded = load_config(
            profile=_base_profile(),
            user_config=_layer(mode="lite"),
            environ={},
        )
        self.assertEqual(loaded.mode, Mode.LITE)
        self.assertEqual(loaded.mode_provenance.source, "user")

    def test_invalid_utf8_file_is_wrapped_in_safe_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.json"
            path.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(ConfigError, "profile") as raised:
                load_config(path, discover=False)
            self.assertIsNone(raised.exception.__cause__)


class BuiltInProfileTests(unittest.TestCase):
    def test_builtin_profile_aliases_have_the_required_capability_bands(self) -> None:
        expected = {
            "anthropic": {
                "fable": "authority",
                "opus": "authority",
                "sonnet": "balanced",
                "haiku": "fast",
            },
            "openai": {
                "gpt-5.6-sol": "authority",
                "gpt-5.6-terra": "balanced",
                "gpt-5.6-luna": "fast",
            },
            "kimi": {
                "kimi-k3": "authority",
                "claude-kimi": "balanced",
            },
        }
        for profile_name, routes in expected.items():
            data = load_profile_data(profile_name)
            with self.subTest(profile=profile_name):
                self.assertEqual(set(data["routes"]), set(routes))
                self.assertEqual(
                    {route_id: data["routes"][route_id]["band"] for route_id in routes},
                    routes,
                )

    def test_builtin_pinned_routes_declare_canonical_variants(self) -> None:
        expected = {
            "anthropic": {
                "fable": "default",
                "opus": "default",
                "sonnet": "default",
                "haiku": "default",
            },
            "openai": {
                "gpt-5.6-sol": "high",
                "gpt-5.6-terra": "medium",
                "gpt-5.6-luna": "low",
            },
            "kimi": {"kimi-k3": "default"},
        }
        for profile_name, variants in expected.items():
            data = load_profile_data(profile_name)
            with self.subTest(profile=profile_name):
                self.assertEqual(
                    {
                        route_id: data["routes"][route_id]["variant"]
                        for route_id in variants
                    },
                    variants,
                )

    def test_kimi_authority_is_exact_and_unpinned_cli_is_only_a_custom_worker(self) -> None:
        data = load_profile_data("kimi")
        exact = data["capabilities"]["kimi-k3"]
        self.assertTrue(exact["requires_exact_host_identity"])
        self.assertTrue(exact["requires_preflight_identity"])
        self.assertEqual(exact["resolved_model_id"], "kimi-k3")

        custom = data["routes"]["claude-kimi"]
        self.assertEqual(custom["transport"], "external-cli")
        self.assertEqual(custom["roles"], ["worker"])
        self.assertNotEqual(custom["band"], "authority")
        self.assertFalse(data["capabilities"]["claude-kimi"]["proves_authority"])

    def test_profile_data_loader_validates_route_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid = _base_profile()
            invalid["routes"]["worker"]["transport"] = "bogus"  # type: ignore[index]
            (root / "invalid.json").write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "transport"):
                load_profile_data("invalid", profiles_root=root)


if __name__ == "__main__":
    unittest.main()
