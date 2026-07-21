from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from runtime.token_saver.config import (
    ConfigError,
    discover_project_config_path,
    discover_user_config_path,
    load_config,
    load_config_layers,
    load_profile_data,
)
from runtime.token_saver.models import (
    CapabilityBand,
    CredentialBinding,
    MainLoop,
    Mode,
    ModelFingerprint,
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
                self.assertIn("routes.worker.command", str(error))

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
                )
                self.assertIn("credential", str(error).lower())

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
                self.assertIn(f"credential_env[0].{field}", str(error))

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
        project_path = Path("/repo/.token-saver.json")
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
            Path("/xdg/config/token-saver/config.json"),
        )
        self.assertEqual(
            discover_user_config_path({"HOME": "/chosen/home"}),
            Path("/chosen/home/.config/token-saver/config.json"),
        )
        self.assertEqual(os.environ.get("HOME"), process_home)

    def test_project_path_is_root_dot_token_saver_json(self) -> None:
        self.assertEqual(
            discover_project_config_path(Path("/work/repository")),
            Path("/work/repository/.token-saver.json"),
        )

    def test_load_config_discovers_xdg_and_project_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xdg = root / "xdg"
            repo = root / "repo"
            repo.mkdir()
            self.write_json(
                xdg / "token-saver" / "config.json",
                _layer(mode="lite", preferences={"workers": ["worker"]}),
            )
            self.write_json(
                repo / ".token-saver.json",
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
            "kimi": {"kimi-k3": "authority"},
        }
        for profile_name, routes in expected.items():
            data = load_profile_data(profile_name)
            with self.subTest(profile=profile_name):
                self.assertEqual(
                    {route_id: data["routes"][route_id]["band"] for route_id in routes},
                    routes,
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
