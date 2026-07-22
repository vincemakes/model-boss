from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.token_saver.models import (
    CapabilityBand,
    Role,
    Route,
    Status,
    Transport,
    WorkerSandboxIdentity,
)
from runtime.token_saver.sandbox import (
    ConformanceProbe,
    SandboxPolicy,
    UnavailableSandbox,
    VerifiedSandbox,
    build_bwrap_argv,
    render_macos_profile,
)


class SandboxPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="token-saver-sandbox-unit-")
        self.root = Path(self.temporary.name).resolve()
        self.worktree = self._directory("worktree")
        self.route_state = self._directory("route-state")
        self.readable = self._directory("provider-runtime")
        self.protected = self._directory("source-repository")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _directory(self, relative: str) -> Path:
        path = self.root / relative
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _policy(self, **overrides: object) -> SandboxPolicy:
        values: dict[str, object] = {
            "worktree_root": self.worktree,
            "route_state_root": self.route_state,
            "readable_roots": (self.readable,),
            "protected_roots": (self.protected,),
            "network_required": False,
        }
        values.update(overrides)
        return SandboxPolicy(**values)

    def test_resolves_symlinked_read_roots_before_binding(self) -> None:
        alias = self.root / "runtime-alias"
        alias.symlink_to(self.readable, target_is_directory=True)

        policy = self._policy(readable_roots=(alias,))

        self.assertEqual(policy.readable_roots, (self.readable,))
        self.assertEqual(policy.writable_roots, (self.worktree, self.route_state))

    def test_rejects_unsafe_overlap_but_allows_a_nested_read_only_worktree_file(self) -> None:
        parent = self._directory("overlap")
        child = self._directory("overlap/child")
        separate_worktree = self._directory("separate-worktree")
        separate_state = self._directory("separate-state")
        separate_read = self._directory("separate-read")
        separate_protected = self._directory("separate-protected")
        cases = (
            {
                "worktree_root": parent,
                "route_state_root": separate_state,
                "readable_roots": (parent,),
                "protected_roots": (separate_protected,),
            },
            {
                "worktree_root": child,
                "route_state_root": separate_state,
                "readable_roots": (parent,),
                "protected_roots": (separate_protected,),
            },
            {
                "worktree_root": parent,
                "route_state_root": separate_state,
                "readable_roots": (separate_read,),
                "protected_roots": (parent,),
            },
            {
                "worktree_root": parent,
                "route_state_root": separate_state,
                "readable_roots": (separate_read,),
                "protected_roots": (child,),
            },
            {
                "worktree_root": child,
                "route_state_root": separate_state,
                "readable_roots": (separate_read,),
                "protected_roots": (parent,),
            },
            {
                "worktree_root": separate_worktree,
                "route_state_root": separate_state,
                "readable_roots": (parent,),
                "protected_roots": (parent,),
            },
            {
                "worktree_root": separate_worktree,
                "route_state_root": separate_state,
                "readable_roots": (parent,),
                "protected_roots": (child,),
            },
            {
                "worktree_root": separate_worktree,
                "route_state_root": separate_state,
                "readable_roots": (child,),
                "protected_roots": (parent,),
            },
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "overlap"):
                    SandboxPolicy(network_required=False, **values)

        nested_read_only = SandboxPolicy(
            worktree_root=parent,
            route_state_root=separate_state,
            readable_roots=(child,),
            protected_roots=(separate_protected,),
            network_required=False,
        )
        self.assertEqual(nested_read_only.read_only_nested_roots, (child,))

    def test_rejects_overlapping_writable_roots(self) -> None:
        nested_state = self._directory("worktree/state")

        with self.assertRaisesRegex(ValueError, "writable roots overlap"):
            self._policy(route_state_root=nested_state)

    def test_rejects_filesystem_root_and_real_home_as_allow_roots(self) -> None:
        for field_name, path in (
            ("worktree_root", Path("/")),
            ("route_state_root", Path("/")),
            ("readable_roots", (Path("/"),)),
            ("worktree_root", Path.home()),
            ("route_state_root", Path.home()),
            ("readable_roots", (Path.home(),)),
        ):
            with self.subTest(field_name=field_name, path=path):
                with self.assertRaises(ValueError):
                    self._policy(**{field_name: path})

    def test_accepts_a_narrow_provider_runtime_below_home(self) -> None:
        provider_runtime = Path(__file__).resolve().parent
        try:
            provider_runtime.relative_to(Path.home().resolve(strict=True))
        except ValueError:
            self.skipTest("test checkout is not below the real home directory")

        policy = self._policy(readable_roots=(provider_runtime,))

        self.assertEqual(policy.readable_roots, (provider_runtime,))

    def test_rejects_broken_symlinks_without_falling_back_to_lexical_paths(self) -> None:
        broken = self.root / "broken-runtime"
        broken.symlink_to(self.root / "missing-target", target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "resolve"):
            self._policy(readable_roots=(broken,))

    def test_rejects_source_repository_disguised_as_writable_symlink(self) -> None:
        alias = self.root / "source-alias"
        alias.symlink_to(self.protected, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "overlap"):
            self._policy(worktree_root=alias)

    def test_rejects_non_directory_writable_roots_and_non_boolean_network_flag(self) -> None:
        regular_file = self.root / "not-a-directory"
        regular_file.write_text("data", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "directory"):
            self._policy(worktree_root=regular_file)
        with self.assertRaisesRegex(ValueError, "boolean"):
            self._policy(network_required=1)

    def test_requires_at_least_one_explicit_protected_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "protected_roots"):
            self._policy(protected_roots=())


class SandboxRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="token-saver-render-")
        root = Path(self.temporary.name).resolve()
        self.worktree = root / "worktree"
        self.state = root / "route-state"
        self.runtime = root / "provider-runtime"
        self.protected = root / "source"
        for path in (self.worktree, self.state, self.runtime, self.protected):
            path.mkdir()
        self.policy = SandboxPolicy(
            worktree_root=self.worktree,
            route_state_root=self.state,
            readable_roots=(self.runtime,),
            protected_roots=(self.protected,),
            network_required=False,
        )
        self.executable = Path(sys.executable).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_macos_profile_is_default_deny_with_only_two_explicit_write_grants(self) -> None:
        profile = render_macos_profile(self.policy, self.executable)

        self.assertIn("(deny default)", profile)
        self.assertIn('(import "system.sb")', profile)
        self.assertIn("(allow process*)", profile)
        self.assertIn("(allow file-read*", profile)
        read_line = next(
            line for line in profile.splitlines() if line.startswith("(allow file-read*")
        )
        self.assertIn(os.fspath(self.worktree), read_line)
        self.assertIn(os.fspath(self.state), read_line)
        write_line = next(
            line for line in profile.splitlines() if line.startswith("(allow file-write*")
        )
        self.assertEqual(write_line.count("(subpath "), 2)
        self.assertIn(os.fspath(self.worktree), write_line)
        self.assertIn(os.fspath(self.state), write_line)
        self.assertNotIn(os.fspath(self.protected), profile)
        self.assertNotIn("(allow network*)", profile)

    def test_nested_readable_git_pointer_is_explicitly_write_denied(self) -> None:
        git_pointer = self.worktree / ".git"
        git_pointer.write_text("gitdir: isolated\n", encoding="utf-8")
        policy = SandboxPolicy(
            worktree_root=self.worktree,
            route_state_root=self.state,
            readable_roots=(git_pointer,),
            protected_roots=(self.protected,),
            network_required=False,
        )

        profile = render_macos_profile(policy, self.executable)
        deny_line = next(
            line for line in profile.splitlines() if line.startswith("(deny file-write*")
        )
        self.assertIn(os.fspath(git_pointer), deny_line)

        argv = build_bwrap_argv(
            policy,
            self.executable,
            (os.fspath(self.executable), "--version"),
            bwrap_executable="/usr/bin/bwrap",
        )
        worktree_bind = argv.index(os.fspath(self.worktree))
        final_git_bind = max(
            index
            for index, member in enumerate(argv)
            if member == os.fspath(git_pointer)
        )
        self.assertGreater(final_git_bind, worktree_bind)
        self.assertEqual(argv[final_git_bind - 2], "--ro-bind")

    def test_macos_profile_adds_network_only_for_a_network_route(self) -> None:
        network_policy = SandboxPolicy(
            worktree_root=self.worktree,
            route_state_root=self.state,
            readable_roots=(self.runtime,),
            protected_roots=(self.protected,),
            network_required=True,
        )

        self.assertIn(
            "(allow network*)", render_macos_profile(network_policy, self.executable)
        )

    def test_bwrap_argv_has_exactly_two_writable_binds_and_direct_command_argv(self) -> None:
        command = (
            os.fspath(self.executable),
            "--literal",
            "$(touch should-not-run)",
        )

        argv = build_bwrap_argv(
            self.policy,
            self.executable,
            command,
            bwrap_executable="/usr/bin/bwrap",
        )

        self.assertEqual(argv[0], "/usr/bin/bwrap")
        self.assertIn("--die-with-parent", argv)
        self.assertIn("--new-session", argv)
        self.assertIn("--unshare-all", argv)
        self.assertIn("--remount-ro", argv)
        self.assertNotIn("--share-net", argv)
        self.assertEqual(argv.count("--bind"), 2)
        writable_pairs = tuple(
            argv[index + 1 : index + 3]
            for index, value in enumerate(argv)
            if value == "--bind"
        )
        self.assertEqual(
            writable_pairs,
            (
                (os.fspath(self.worktree), os.fspath(self.worktree)),
                (os.fspath(self.state), os.fspath(self.state)),
            ),
        )
        self.assertIn("--ro-bind", argv)
        chdir_index = argv.index("--chdir")
        self.assertEqual(argv[chdir_index + 1], os.fspath(self.worktree))
        separator = len(argv) - len(command) - 1
        self.assertEqual(argv[separator], "--")
        self.assertEqual(argv[separator + 1 :], command)
        self.assertNotIn("sh", argv[: separator + 1])
        self.assertNotIn("-c", argv[: separator + 1])

    def test_bwrap_shares_network_only_when_required(self) -> None:
        network_policy = SandboxPolicy(
            worktree_root=self.worktree,
            route_state_root=self.state,
            readable_roots=(self.runtime,),
            protected_roots=(self.protected,),
            network_required=True,
        )

        argv = build_bwrap_argv(
            network_policy,
            self.executable,
            (os.fspath(self.executable), "--version"),
            bwrap_executable="/usr/bin/bwrap",
        )

        self.assertEqual(argv.count("--share-net"), 1)

    @unittest.skipUnless(
        Path("/etc/resolv.conf").is_symlink(),
        "/etc/resolv.conf is not a system symlink",
    )
    def test_bwrap_recreates_required_system_symlinks_in_its_empty_root(self) -> None:
        network_policy = SandboxPolicy(
            worktree_root=self.worktree,
            route_state_root=self.state,
            readable_roots=(self.runtime,),
            protected_roots=(self.protected,),
            network_required=True,
        )
        argv = build_bwrap_argv(
            network_policy,
            self.executable,
            (os.fspath(self.executable), "--version"),
            bwrap_executable="/usr/bin/bwrap",
        )

        alias_index = next(
            index
            for index, member in enumerate(argv)
            if member == "--symlink" and argv[index + 2] == "/etc/resolv.conf"
        )
        self.assertEqual(argv[alias_index + 1], os.readlink("/etc/resolv.conf"))


class VerifiedSandboxBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="token-saver-binding-")
        root = Path(self.temporary.name).resolve()
        self.worktree = root / "worktree"
        self.state = root / "state"
        self.runtime = root / "runtime"
        self.protected = root / "source"
        for path in (self.worktree, self.state, self.runtime, self.protected):
            path.mkdir()
        self.policy = SandboxPolicy(
            worktree_root=self.worktree,
            route_state_root=self.state,
            readable_roots=(self.runtime,),
            protected_roots=(self.protected,),
            network_required=False,
        )
        self.argv = (os.fspath(Path(sys.executable).resolve()), "--version")
        self.launcher_prefix = (
            os.fspath(Path(sys.executable).resolve()),
            "--verified-wrapper",
            "--",
        )
        self.profile_hash = hashlib.sha256(b"test-profile").hexdigest()
        self.verified = VerifiedSandbox._from_successful_probe(
            backend="test",
            policy=self.policy,
            route_id="worker-route",
            route_argv=self.argv,
            launcher_prefix=self.launcher_prefix,
            profile_hash=self.profile_hash,
            probe=ConformanceProbe(
                allowed_read=True,
                protected_read_denied=True,
                worktree_write=True,
                outside_write_denied=True,
                complete=True,
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_binding_prepares_only_the_wrapped_direct_argv(self) -> None:
        launch = self.verified.prepare(
            route_id="worker-route",
            argv=self.argv,
            policy=self.policy,
            cwd=self.worktree,
        )

        self.assertEqual(launch.status, Status.OK)
        self.assertTrue(launch.available)
        self.assertEqual(launch.argv, (*self.launcher_prefix, *self.argv))
        self.assertEqual(launch.cwd, self.worktree)
        self.assertEqual(launch.profile_hash, self.profile_hash)

    def test_route_argv_policy_or_cwd_mismatch_never_returns_a_launch_argv(self) -> None:
        other_root = Path(self.temporary.name).resolve() / "other"
        other_worktree = other_root / "worktree"
        other_state = other_root / "state"
        other_runtime = other_root / "runtime"
        for path in (other_worktree, other_state, other_runtime):
            path.mkdir(parents=True, exist_ok=True)
        other_policy = SandboxPolicy(
            worktree_root=other_worktree,
            route_state_root=other_state,
            readable_roots=(other_runtime,),
            protected_roots=(self.protected,),
            network_required=False,
        )
        cases = (
            {
                "route_id": "other-route",
                "argv": self.argv,
                "policy": self.policy,
                "cwd": self.worktree,
            },
            {
                "route_id": "worker-route",
                "argv": (*self.argv, "extra"),
                "policy": self.policy,
                "cwd": self.worktree,
            },
            {
                "route_id": "worker-route",
                "argv": self.argv,
                "policy": other_policy,
                "cwd": other_worktree,
            },
            {
                "route_id": "worker-route",
                "argv": self.argv,
                "policy": self.policy,
                "cwd": self.state,
            },
        )
        for values in cases:
            with self.subTest(values=values):
                launch = self.verified.prepare(**values)
                self.assertEqual(launch.status, Status.SANDBOX_UNAVAILABLE)
                self.assertFalse(launch.available)
                self.assertEqual(launch.argv, ())
                self.assertIsNone(launch.cwd)

    def test_root_replacement_invalidates_a_previously_verified_object(self) -> None:
        old_worktree = self.worktree.with_name("old-worktree")
        self.worktree.rename(old_worktree)
        self.worktree.mkdir()

        launch = self.verified.prepare(
            route_id="worker-route",
            argv=self.argv,
            policy=self.policy,
            cwd=self.worktree,
        )

        self.assertEqual(launch.status, Status.SANDBOX_UNAVAILABLE)
        self.assertEqual(launch.argv, ())

    def test_incomplete_probe_cannot_issue_a_verified_sandbox(self) -> None:
        with self.assertRaisesRegex(ValueError, "probe"):
            VerifiedSandbox._from_successful_probe(
                backend="test",
                policy=self.policy,
                route_id="worker-route",
                route_argv=self.argv,
                launcher_prefix=self.launcher_prefix,
                profile_hash=self.profile_hash,
                probe=ConformanceProbe(
                    allowed_read=True,
                    protected_read_denied=True,
                    worktree_write=True,
                    outside_write_denied=False,
                    complete=True,
                ),
            )

    def test_issues_the_existing_routing_identity_from_exact_verified_roots(self) -> None:
        route = Route(
            route_id="worker-route",
            transport=Transport.EXTERNAL_CLI,
            band=CapabilityBand.FAST,
            roles=frozenset({Role.WORKER}),
            read_only=False,
            command=self.argv,
        )

        identity = self.verified.worker_identity(route)

        self.assertIsInstance(identity, WorkerSandboxIdentity)
        self.assertTrue(identity.is_bound_to(route))
        self.assertEqual(identity.worktree_identity, self.verified.worktree_identity)
        self.assertEqual(
            identity.route_state_identity, self.verified.route_state_identity
        )
        self.assertEqual(identity.profile_hash, self.verified.profile_hash)

        wrong_route = Route(
            route_id="wrong-route",
            transport=Transport.EXTERNAL_CLI,
            band=CapabilityBand.FAST,
            roles=frozenset({Role.WORKER}),
            read_only=False,
            command=self.argv,
        )
        with self.assertRaisesRegex(ValueError, "exact route"):
            self.verified.worker_identity(wrong_route)

    def test_cannot_issue_a_routing_identity_from_a_tampered_binding(self) -> None:
        route = Route(
            route_id="worker-route",
            transport=Transport.EXTERNAL_CLI,
            band=CapabilityBand.FAST,
            roles=frozenset({Role.WORKER}),
            read_only=False,
            command=self.argv,
        )
        object.__setattr__(self.verified, "binding_hash", "0" * 64)

        with self.assertRaisesRegex(ValueError, "current"):
            self.verified.worker_identity(route)

    def test_tampered_probe_invalidates_launch_preparation(self) -> None:
        object.__setattr__(
            self.verified,
            "probe",
            ConformanceProbe(True, True, True, False, True),
        )

        launch = self.verified.prepare(
            route_id="worker-route",
            argv=self.argv,
            policy=self.policy,
            cwd=self.worktree,
        )

        self.assertEqual(launch.status, Status.SANDBOX_UNAVAILABLE)
        self.assertEqual(launch.argv, ())

    def test_unavailable_backend_never_returns_an_unsandboxed_command(self) -> None:
        unavailable = UnavailableSandbox("backend missing")

        launch = unavailable.prepare(
            route_id="worker-route",
            argv=self.argv,
            policy=self.policy,
            cwd=self.worktree,
        )

        self.assertEqual(unavailable.status, Status.SANDBOX_UNAVAILABLE)
        self.assertEqual(launch.status, Status.SANDBOX_UNAVAILABLE)
        self.assertEqual(launch.argv, ())
        self.assertIsNone(launch.cwd)


if __name__ == "__main__":
    unittest.main()
