from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

import runtime.token_saver.resources as resources_module
from runtime.token_saver.resources import (
    cleanup_invocation,
    create_invocation_resources,
    load_invocation_resources,
)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", os.fspath(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _create_repository(base: Path) -> Path:
    repository = base / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Token Saver Tests")
    _git(repository, "config", "user.email", "token-saver@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-q", "-m", "baseline")
    return repository.resolve()


def _registered_worktrees(repository: Path) -> set[Path]:
    output = _git(repository, "worktree", "list", "--porcelain", "-z").stdout
    return {
        Path(os.fsdecode(field.removeprefix(b"worktree "))).resolve()
        for field in output.split(b"\0")
        if field.startswith(b"worktree ")
    }


class InvocationResourceCreationTests(unittest.TestCase):
    def test_creation_records_and_secures_every_private_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repository"
            repository.mkdir()
            temp_parent = base / "temporary"
            temp_parent.mkdir()

            resources = create_invocation_resources(repository, temp_parent)
            directories = (
                (
                    resources.invocation_root,
                    resources.invocation_root_device,
                    resources.invocation_root_inode,
                ),
                (
                    resources.route_state_path,
                    resources.route_state_device,
                    resources.route_state_inode,
                ),
                (
                    resources.evidence_path,
                    resources.evidence_device,
                    resources.evidence_inode,
                ),
            )
            for path, device, inode in directories:
                metadata = os.lstat(path)
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o700)
                self.assertEqual(metadata.st_uid, os.geteuid())
                self.assertGreaterEqual(metadata.st_nlink, 1)
                self.assertEqual((metadata.st_dev, metadata.st_ino), (device, inode))

            private_files = (
                (
                    resources.manifest_path,
                    resources.manifest_device,
                    resources.manifest_inode,
                ),
                *(
                    (path, device, inode)
                    for path, (device, inode) in zip(
                        resources.marker_paths,
                        resources.marker_identities,
                        strict=True,
                    )
                ),
            )
            for path, device, inode in private_files:
                metadata = os.lstat(path)
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                self.assertEqual(metadata.st_uid, os.geteuid())
                self.assertEqual(metadata.st_nlink, 1)
                self.assertEqual((metadata.st_dev, metadata.st_ino), (device, inode))

    def test_creation_uses_random_identity_resolved_parent_and_private_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repository"
            repository.mkdir()
            real_parent = base / "real-parent"
            real_parent.mkdir()
            parent_alias = base / "parent-alias"
            parent_alias.symlink_to(real_parent, target_is_directory=True)

            first = create_invocation_resources(repository, parent_alias)
            second = create_invocation_resources(repository, parent_alias)

            self.assertNotEqual(first.invocation_id, second.invocation_id)
            self.assertEqual(uuid.UUID(first.invocation_id).version, 4)
            self.assertEqual(first.temp_parent, real_parent.resolve())
            self.assertEqual(first.repository_path, repository.resolve())
            self.assertEqual(
                (first.repository_device, first.repository_inode),
                (
                    os.stat(first.repository_path).st_dev,
                    os.stat(first.repository_path).st_ino,
                ),
            )
            self.assertEqual(
                (first.temp_parent_device, first.temp_parent_inode),
                (
                    os.stat(first.temp_parent).st_dev,
                    os.stat(first.temp_parent).st_ino,
                ),
            )
            self.assertEqual(
                (first.invocation_root_device, first.invocation_root_inode),
                (
                    os.stat(first.invocation_root).st_dev,
                    os.stat(first.invocation_root).st_ino,
                ),
            )
            self.assertTrue(first.manifest_path.is_file())
            self.assertEqual(
                stat.S_IMODE(os.lstat(first.manifest_path).st_mode),
                0o600,
            )

    def test_manifest_pins_every_exact_path_and_matching_private_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repository"
            repository.mkdir()
            temp_parent = base / "temporary"
            temp_parent.mkdir()

            resources = create_invocation_resources(repository, temp_parent)

            self.assertEqual(
                resources.worktree_registration_path,
                resources.worktree_path,
            )
            self.assertFalse(resources.worktree_path.exists())
            expected_paths = {
                "invocation_root": resources.invocation_root,
                "worktree_path": resources.worktree_path,
                "worktree_registration_path": resources.worktree_registration_path,
                "route_state_path": resources.route_state_path,
                "evidence_path": resources.evidence_path,
                "plan_evidence_path": resources.plan_evidence_path,
                "final_evidence_path": resources.final_evidence_path,
                "delta_bundle_path": resources.delta_bundle_path,
                "manifest_path": resources.manifest_path,
            }
            manifest = json.loads(resources.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["state"], "active")
            self.assertEqual(manifest["invocation_id"], resources.invocation_id)
            self.assertEqual(
                manifest["repository_path"], os.fspath(repository.resolve())
            )
            self.assertEqual(manifest["temp_parent"], os.fspath(temp_parent.resolve()))
            self.assertEqual(
                manifest["repository_identity"],
                {
                    "device": resources.repository_device,
                    "inode": resources.repository_inode,
                },
            )
            self.assertEqual(
                manifest["temp_parent_identity"],
                {
                    "device": resources.temp_parent_device,
                    "inode": resources.temp_parent_inode,
                },
            )
            self.assertEqual(
                manifest["invocation_root_identity"],
                {
                    "device": resources.invocation_root_device,
                    "inode": resources.invocation_root_inode,
                },
            )
            for field_name, expected in expected_paths.items():
                self.assertEqual(manifest[field_name], os.fspath(expected))

            self.assertEqual(len(resources.marker_paths), len(expected_paths) - 1)
            marker_targets = set()
            for marker_path in resources.marker_paths:
                self.assertTrue(marker_path.is_file())
                self.assertEqual(stat.S_IMODE(os.lstat(marker_path).st_mode), 0o600)
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                self.assertEqual(marker["schema_version"], 1)
                self.assertEqual(marker["invocation_id"], resources.invocation_id)
                marker_targets.add(Path(marker["target_path"]))
            self.assertEqual(
                marker_targets,
                set(expected_paths.values()) - {resources.manifest_path},
            )
            self.assertEqual(
                manifest["markers"],
                [os.fspath(path) for path in resources.marker_paths],
            )

    def test_creation_rolls_back_its_layout_when_manifest_setup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repository"
            repository.mkdir()
            temp_parent = base / "temporary"
            temp_parent.mkdir()

            with mock.patch(
                "runtime.token_saver.resources._write_private_json",
                side_effect=OSError("injected manifest failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected manifest failure"):
                    create_invocation_resources(repository, temp_parent)

            self.assertEqual(list(temp_parent.iterdir()), [])

    def test_creation_rejects_the_source_repository_as_a_temporary_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            repository.mkdir()
            nested_parent = repository / "nested-temporary"
            nested_parent.mkdir()

            for unsafe_parent in (repository, nested_parent):
                with self.subTest(unsafe_parent=unsafe_parent):
                    with self.assertRaisesRegex(ValueError, "source repository"):
                        create_invocation_resources(repository, unsafe_parent)

            self.assertEqual(
                list(repository.glob("token-saver-invocation-*")),
                [],
            )
            self.assertEqual(
                list(nested_parent.glob("token-saver-invocation-*")),
                [],
            )

    def test_creation_rejects_a_temporary_parent_containing_the_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp_parent = Path(temporary) / "temporary"
            temp_parent.mkdir()
            repository = temp_parent / "repository"
            repository.mkdir()

            with self.assertRaisesRegex(ValueError, "source repository"):
                create_invocation_resources(repository, temp_parent)

            self.assertEqual(
                list(temp_parent.glob("token-saver-invocation-*")),
                [],
            )

    def test_creation_enforces_private_modes_independently_of_umask(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repository"
            repository.mkdir()
            temp_parent = base / "temporary"
            temp_parent.mkdir()

            previous_umask = os.umask(0o777)
            try:
                resources = create_invocation_resources(repository, temp_parent)
            finally:
                os.umask(previous_umask)

            for directory in (
                resources.invocation_root,
                resources.route_state_path,
                resources.evidence_path,
            ):
                self.assertEqual(stat.S_IMODE(os.lstat(directory).st_mode), 0o700)
            for private_file in (
                resources.manifest_path,
                *resources.marker_paths,
            ):
                self.assertEqual(
                    stat.S_IMODE(os.lstat(private_file).st_mode),
                    0o600,
                )


class InvocationResourceLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="token-saver-resource-load-test-"
        )
        self.base = Path(self.temporary.name)
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self.temp_parent = self.base / "temporary"
        self.temp_parent.mkdir()
        self.resources = create_invocation_resources(
            self.repository,
            self.temp_parent,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_load_reconstructs_and_revalidates_the_exact_active_manifest(self) -> None:
        loaded = load_invocation_resources(self.resources.manifest_path)

        self.assertEqual(loaded, self.resources)

    def test_load_rejects_tampered_manifest_content(self) -> None:
        manifest = json.loads(
            self.resources.manifest_path.read_text(encoding="utf-8")
        )
        manifest["state"] = "consumed"
        self.resources.manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self.resources.manifest_path.chmod(0o600)

        with self.assertRaises(ValueError):
            load_invocation_resources(self.resources.manifest_path)

    def test_load_rejects_a_manifest_symlink_or_external_copy(self) -> None:
        external = self.base / "manifest-copy.json"
        external.write_bytes(self.resources.manifest_path.read_bytes())
        external.chmod(0o600)
        alias = self.base / "manifest-link.json"
        alias.symlink_to(self.resources.manifest_path)

        for candidate in (external, alias):
            with self.subTest(candidate=candidate):
                with self.assertRaises((OSError, ValueError)):
                    load_invocation_resources(candidate)

    def test_load_rejects_a_replaced_invocation_root(self) -> None:
        original = self.temp_parent / "original-root"
        self.resources.invocation_root.rename(original)
        self.resources.invocation_root.mkdir(mode=0o700)

        with self.assertRaises(ValueError):
            load_invocation_resources(original / "manifest.json")

    def test_load_rejects_a_manifest_path_with_a_symlink_loop(self) -> None:
        loop = self.base / "loop"
        loop.symlink_to(loop)
        manifest = json.loads(
            self.resources.manifest_path.read_text(encoding="utf-8")
        )
        manifest["worktree_path"] = os.fspath(loop / "worktree")
        self.resources.manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self.resources.manifest_path.chmod(0o600)

        with self.assertRaises(ValueError):
            load_invocation_resources(self.resources.manifest_path)

class InvocationResourceCleanupTests(unittest.TestCase):
    def _exercise_cleanup(self, lifecycle: str, *, register_worktree: bool) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = _create_repository(base)
            temp_parent = base / "temporary"
            temp_parent.mkdir()
            resources = create_invocation_resources(repository, temp_parent)

            if register_worktree:
                _git(
                    repository,
                    "worktree",
                    "add",
                    "--detach",
                    os.fspath(resources.worktree_path),
                    "HEAD",
                )
                (resources.worktree_path / "partial-worker-output").write_text(
                    lifecycle, encoding="utf-8"
                )
            (resources.route_state_path / "status").write_text(
                lifecycle, encoding="utf-8"
            )
            (resources.evidence_path / "result").write_text(
                lifecycle, encoding="utf-8"
            )
            resources.delta_bundle_path.write_bytes(lifecycle.encode("ascii"))

            owned_directories = (
                resources.invocation_root,
                resources.worktree_path,
                resources.route_state_path,
                resources.evidence_path,
            )
            sentinels = []
            for index, owned_directory in enumerate(owned_directories):
                sentinel = owned_directory.parent / f"sibling-sentinel-{index}"
                sentinel.write_text("must survive", encoding="utf-8")
                sentinels.append(sentinel)

            result = cleanup_invocation(resources)

            self.assertEqual(result.status, "cleaned")
            self.assertTrue(result.cleaned)
            self.assertEqual(result.invocation_id, resources.invocation_id)
            self.assertEqual(result.worktree_removed, register_worktree)
            self.assertNotIn(resources.worktree_path, _registered_worktrees(repository))
            self.assertFalse(resources.worktree_path.exists())
            self.assertFalse(resources.route_state_path.exists())
            self.assertFalse(resources.evidence_path.exists())
            self.assertFalse(resources.delta_bundle_path.exists())
            for sentinel in sentinels:
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "must survive")

    def test_cleanup_after_worktree_creation_failure(self) -> None:
        self._exercise_cleanup("worktree-creation-failure", register_worktree=False)

    def test_cleanup_after_materialization_failure(self) -> None:
        self._exercise_cleanup("materialization-failure", register_worktree=True)

    def test_cleanup_after_worker_timeout(self) -> None:
        self._exercise_cleanup("timeout", register_worktree=True)

    def test_cleanup_after_review_rejection(self) -> None:
        self._exercise_cleanup("rejection", register_worktree=True)

    def test_cleanup_after_successful_integration(self) -> None:
        self._exercise_cleanup("successful-integration", register_worktree=True)

    def test_cleanup_after_explicit_abandonment(self) -> None:
        self._exercise_cleanup("abandonment", register_worktree=True)

    def test_cleanup_removes_external_seal_receipt_and_recovery_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = _create_repository(base)
            temp_parent = base / "temporary"
            temp_parent.mkdir()
            resources = create_invocation_resources(repository, temp_parent)
            receipt_paths = (
                resources_module._seal_receipt_path(resources),
                resources_module._seal_receipt_final_path(resources),
            )
            for path in receipt_paths:
                path.write_bytes(b"{}")
                path.chmod(0o400)

            result = cleanup_invocation(resources)

            self.assertEqual(result.status, "cleaned")
            for path in receipt_paths:
                self.assertFalse(path.exists())

    def test_cleanup_rejects_an_active_seal_claim_then_resumes(self) -> None:
        import fcntl

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = _create_repository(base)
            temp_parent = base / "temporary"
            temp_parent.mkdir()
            resources = create_invocation_resources(repository, temp_parent)
            receipt = resources_module._seal_receipt_path(resources)
            receipt.write_bytes(b"{}")
            receipt.chmod(0o600)
            descriptor = os.open(receipt, os.O_RDONLY)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                first = cleanup_invocation(resources)
                self.assertEqual(first.status, "rejected")
                self.assertIn("still active", first.message)
                self.assertTrue(receipt.exists())
            finally:
                os.close(descriptor)

            resumed = cleanup_invocation(resources)
            self.assertEqual(resumed.status, "cleaned")
            self.assertFalse(receipt.exists())


class InvocationResourceRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repository = _create_repository(self.base)
        self.temp_parent = self.base / "temporary"
        self.temp_parent.mkdir()
        self.resources = create_invocation_resources(
            self.repository, self.temp_parent
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_rejected_without_removing_owned_directories(
        self, resources=None
    ) -> None:
        result = cleanup_invocation(resources or self.resources)
        self.assertEqual(result.status, "rejected")
        self.assertTrue(result.rejected)
        self.assertTrue(self.resources.invocation_root.exists())
        self.assertTrue(self.resources.route_state_path.exists())
        self.assertTrue(self.resources.evidence_path.exists())

    def test_cleanup_rejects_a_missing_marker(self) -> None:
        self.resources.marker_paths[3].unlink()

        self._assert_rejected_without_removing_owned_directories()

    def test_cleanup_rejects_private_mode_and_inode_replacements(self) -> None:
        cases = ("root-mode", "route-inode", "evidence-mode", "manifest-inode", "marker-link")
        for case in cases:
            with self.subTest(case), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                repository = _create_repository(base)
                temp_parent = base / "temporary"
                temp_parent.mkdir()
                resources = create_invocation_resources(repository, temp_parent)
                if case == "root-mode":
                    resources.invocation_root.chmod(0o755)
                elif case == "route-inode":
                    resources.route_state_path.rmdir()
                    resources.route_state_path.mkdir(mode=0o700)
                elif case == "evidence-mode":
                    resources.evidence_path.chmod(0o755)
                elif case == "manifest-inode":
                    raw = resources.manifest_path.read_bytes()
                    resources.manifest_path.unlink()
                    resources.manifest_path.write_bytes(raw)
                    resources.manifest_path.chmod(0o600)
                else:
                    os.link(resources.marker_paths[0], base / "marker-hardlink")

                result = cleanup_invocation(resources)

                self.assertEqual(result.status, "rejected")
                self.assertTrue(resources.invocation_root.exists())

    def test_cleanup_rejects_a_marker_for_the_wrong_owned_kind(self) -> None:
        marker_path = self.resources.marker_paths[3]
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["kind"] = "evidence"
        marker_path.write_text(
            json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        self._assert_rejected_without_removing_owned_directories()

    def test_cleanup_rejects_a_wrong_invocation_uuid(self) -> None:
        manifest = json.loads(
            self.resources.manifest_path.read_text(encoding="utf-8")
        )
        manifest["invocation_id"] = str(uuid.uuid4())
        self.resources.manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        self._assert_rejected_without_removing_owned_directories()

    def test_cleanup_rejects_a_manifest_outside_the_recorded_parent(self) -> None:
        forged_manifest = self.base / "outside-manifest.json"
        forged_manifest.write_bytes(self.resources.manifest_path.read_bytes())
        forged_manifest.chmod(0o600)
        forged = replace(self.resources, manifest_path=forged_manifest)

        self._assert_rejected_without_removing_owned_directories(forged)
        self.assertTrue(forged_manifest.exists())

    def test_cleanup_rejects_a_traversal_alias_before_unlinking_it(self) -> None:
        outside_delta = self.temp_parent / "outside-delta"
        outside_delta.write_text("must survive", encoding="utf-8")
        traversal_alias = self.resources.invocation_root / ".." / outside_delta.name
        manifest = json.loads(
            self.resources.manifest_path.read_text(encoding="utf-8")
        )
        manifest["delta_bundle_path"] = os.fspath(traversal_alias)
        self.resources.manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        delta_marker = self.resources.marker_paths[7]
        marker = json.loads(delta_marker.read_text(encoding="utf-8"))
        marker["target_path"] = os.fspath(traversal_alias)
        delta_marker.write_text(
            json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        forged = replace(self.resources, delta_bundle_path=traversal_alias)

        self._assert_rejected_without_removing_owned_directories(forged)
        self.assertEqual(outside_delta.read_text(encoding="utf-8"), "must survive")

    def test_cleanup_rejects_a_symlink_swapped_invocation_root(self) -> None:
        original_root = self.temp_parent / "original-root"
        self.resources.invocation_root.rename(original_root)
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "must-survive"
        sentinel.write_text("outside", encoding="utf-8")
        self.resources.invocation_root.symlink_to(outside, target_is_directory=True)

        result = cleanup_invocation(self.resources)

        self.assertEqual(result.status, "rejected")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside")
        self.assertTrue(original_root.is_dir())

    def test_cleanup_revalidates_root_after_the_git_probe(self) -> None:
        original_root = self.temp_parent / "original-root"
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "must-survive"
        sentinel.write_text("outside", encoding="utf-8")

        def swap_root(_repository: Path) -> set[Path]:
            self.resources.invocation_root.rename(original_root)
            self.resources.invocation_root.symlink_to(
                outside, target_is_directory=True
            )
            return set()

        with mock.patch(
            "runtime.token_saver.resources._registered_worktrees",
            side_effect=swap_root,
        ):
            result = cleanup_invocation(self.resources)

        self.assertEqual(result.status, "rejected")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside")
        self.assertTrue((original_root / "manifest.json").is_file())
        self.assertEqual(
            list(self.temp_parent.glob(".token-saver-consumed-*.json")),
            [],
        )

    def test_cleanup_does_not_follow_a_root_swapped_after_claim(self) -> None:
        original_root = self.temp_parent / "original-root"
        outside = self.base / "outside"
        outside.mkdir()
        outside_route = outside / "route-state"
        outside_route.mkdir()
        route_sentinel = outside_route / "must-survive"
        route_sentinel.write_text("route", encoding="utf-8")
        outside_evidence = outside / "evidence"
        outside_evidence.mkdir()
        evidence_sentinel = outside_evidence / "must-survive"
        evidence_sentinel.write_text("evidence", encoding="utf-8")
        outside_delta = outside / "worker.delta"
        outside_delta.write_text("delta", encoding="utf-8")
        original_writer = resources_module._write_private_json_at
        swapped = False

        def write_then_swap(
            parent_fd: int, name: str, value: dict[str, object]
        ) -> None:
            nonlocal swapped
            original_writer(parent_fd, name, value)
            if (
                not swapped
                and name.startswith(".token-saver-consumed-")
            ):
                swapped = True
                self.resources.invocation_root.rename(original_root)
                self.resources.invocation_root.symlink_to(
                    outside, target_is_directory=True
                )

        with mock.patch(
            "runtime.token_saver.resources._write_private_json_at",
            side_effect=write_then_swap,
        ):
            result = cleanup_invocation(self.resources)

        self.assertEqual(result.status, "rejected")
        self.assertEqual(route_sentinel.read_text(encoding="utf-8"), "route")
        self.assertEqual(
            evidence_sentinel.read_text(encoding="utf-8"), "evidence"
        )
        self.assertEqual(outside_delta.read_text(encoding="utf-8"), "delta")
        self.assertTrue((original_root / "manifest.json").is_file())

    def test_cleanup_rejects_an_ancestor_swap_before_opening_parent(self) -> None:
        anchor_parent = self.base / "anchor-parent"
        anchor_parent.mkdir()
        nested_temp_parent = anchor_parent / "temporary"
        nested_temp_parent.mkdir()
        resources = create_invocation_resources(
            self.repository, nested_temp_parent
        )

        outside_anchor = self.base / "outside-anchor"
        outside_temp_parent = outside_anchor / "temporary"
        outside_temp_parent.mkdir(parents=True)
        outside_root = outside_temp_parent / resources.invocation_root.name
        shutil.copytree(resources.invocation_root, outside_root)
        route_sentinel = outside_root / "route-state" / "must-survive"
        route_sentinel.write_text("route", encoding="utf-8")
        evidence_sentinel = outside_root / "evidence" / "must-survive"
        evidence_sentinel.write_text("evidence", encoding="utf-8")

        original_anchor_parent = self.base / "original-anchor-parent"
        original_open = resources_module._open_root_anchor
        swapped = False

        def swap_ancestor_then_open(
            resources_arg, parent_fd: int, *, allow_missing: bool
        ):
            nonlocal swapped
            if not swapped:
                swapped = True
                anchor_parent.rename(original_anchor_parent)
                anchor_parent.symlink_to(
                    outside_anchor, target_is_directory=True
                )
            return original_open(
                resources_arg, parent_fd, allow_missing=allow_missing
            )

        with mock.patch(
            "runtime.token_saver.resources._open_root_anchor",
            side_effect=swap_ancestor_then_open,
        ):
            result = cleanup_invocation(resources)

        self.assertEqual(result.status, "rejected")
        self.assertEqual(route_sentinel.read_text(encoding="utf-8"), "route")
        self.assertEqual(
            evidence_sentinel.read_text(encoding="utf-8"), "evidence"
        )
        self.assertTrue(
            (
                original_anchor_parent
                / "temporary"
                / resources.invocation_root.name
                / "manifest.json"
            ).is_file()
        )

    def test_cleanup_rejects_worktree_symlink_before_git_remove(self) -> None:
        _git(
            self.repository,
            "worktree",
            "add",
            "--detach",
            os.fspath(self.resources.worktree_path),
            "HEAD",
        )
        original_worktree = self.resources.invocation_root / "original-worktree"
        outside = self.base / "outside-worktree"
        outside.mkdir()
        sentinel = outside / "must-survive"
        sentinel.write_text("outside", encoding="utf-8")
        original_writer = resources_module._write_private_json_at
        original_run_git = resources_module._run_git
        remove_calls = 0
        swapped = False

        def write_then_swap(
            parent_fd: int, name: str, value: dict[str, object]
        ) -> None:
            nonlocal swapped
            original_writer(parent_fd, name, value)
            if (
                not swapped
                and name.startswith(".token-saver-consumed-")
            ):
                swapped = True
                self.resources.worktree_path.rename(original_worktree)
                self.resources.worktree_path.symlink_to(
                    outside, target_is_directory=True
                )

        def observe_git(repository: Path, *arguments: str):
            nonlocal remove_calls
            if arguments[:2] == ("worktree", "remove"):
                remove_calls += 1
                return subprocess.CompletedProcess(arguments, 0, b"", b"")
            return original_run_git(repository, *arguments)

        with (
            mock.patch(
                "runtime.token_saver.resources._write_private_json_at",
                side_effect=write_then_swap,
            ),
            mock.patch(
                "runtime.token_saver.resources._run_git",
                side_effect=observe_git,
            ),
        ):
            result = cleanup_invocation(self.resources)

        self.assertEqual(result.status, "rejected")
        self.assertEqual(remove_calls, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside")

    def test_cleanup_never_treats_the_source_repository_as_a_worktree(self) -> None:
        manifest = json.loads(
            self.resources.manifest_path.read_text(encoding="utf-8")
        )
        manifest["worktree_path"] = os.fspath(self.repository)
        manifest["worktree_registration_path"] = os.fspath(self.repository)
        self.resources.manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        for marker_index in (1, 2):
            marker_path = self.resources.marker_paths[marker_index]
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["target_path"] = os.fspath(self.repository)
            marker_path.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        forged = replace(
            self.resources,
            worktree_path=self.repository,
            worktree_registration_path=self.repository,
        )

        self._assert_rejected_without_removing_owned_directories(forged)
        self.assertTrue((self.repository / ".git").exists())
        self.assertEqual(
            (self.repository / "tracked.txt").read_text(encoding="utf-8"),
            "baseline\n",
        )

    def test_cleanup_rejects_a_fully_forged_layout_inside_source_repo(self) -> None:
        old_root = self.resources.invocation_root
        forged_root = self.repository / old_root.name
        old_root.rename(forged_root)

        def relocated(path: Path) -> Path:
            return forged_root / path.relative_to(old_root)

        forged = replace(
            self.resources,
            temp_parent=self.repository,
            invocation_root=forged_root,
            worktree_path=relocated(self.resources.worktree_path),
            worktree_registration_path=relocated(
                self.resources.worktree_registration_path
            ),
            route_state_path=relocated(self.resources.route_state_path),
            evidence_path=relocated(self.resources.evidence_path),
            plan_evidence_path=relocated(self.resources.plan_evidence_path),
            final_evidence_path=relocated(self.resources.final_evidence_path),
            delta_bundle_path=relocated(self.resources.delta_bundle_path),
            manifest_path=relocated(self.resources.manifest_path),
            marker_paths=tuple(
                relocated(path) for path in self.resources.marker_paths
            ),
        )
        marker_targets = (
            forged.invocation_root,
            forged.worktree_path,
            forged.worktree_registration_path,
            forged.route_state_path,
            forged.evidence_path,
            forged.plan_evidence_path,
            forged.final_evidence_path,
            forged.delta_bundle_path,
        )
        for marker_path, target_path in zip(
            forged.marker_paths, marker_targets, strict=True
        ):
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["target_path"] = os.fspath(target_path)
            marker_path.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        manifest = json.loads(forged.manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "temp_parent": os.fspath(forged.temp_parent),
                "invocation_root": os.fspath(forged.invocation_root),
                "worktree_path": os.fspath(forged.worktree_path),
                "worktree_registration_path": os.fspath(
                    forged.worktree_registration_path
                ),
                "route_state_path": os.fspath(forged.route_state_path),
                "evidence_path": os.fspath(forged.evidence_path),
                "plan_evidence_path": os.fspath(forged.plan_evidence_path),
                "final_evidence_path": os.fspath(forged.final_evidence_path),
                "delta_bundle_path": os.fspath(forged.delta_bundle_path),
                "manifest_path": os.fspath(forged.manifest_path),
                "markers": [os.fspath(path) for path in forged.marker_paths],
            }
        )
        forged.manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        result = cleanup_invocation(forged)

        self.assertEqual(result.status, "rejected")
        self.assertTrue(forged.invocation_root.is_dir())
        self.assertEqual(
            (self.repository / "tracked.txt").read_text(encoding="utf-8"),
            "baseline\n",
        )

    def test_manifest_can_be_consumed_exactly_once(self) -> None:
        first = cleanup_invocation(self.resources)
        second = cleanup_invocation(self.resources)

        self.assertEqual(first.status, "cleaned")
        self.assertEqual(second.status, "already_consumed")
        self.assertTrue(second.rejected)
        self.assertEqual(second.removed_paths, ())
        self.assertFalse(second.worktree_removed)

    def test_cleanup_invokes_only_exact_direct_argv_git_operations(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        self.resources.worktree_path.mkdir()

        def fake_run(
            arguments: list[str], **options: object
        ) -> subprocess.CompletedProcess[bytes]:
            calls.append((arguments, options))
            if arguments[-4:] == ["worktree", "list", "--porcelain", "-z"]:
                output = (
                    b"worktree "
                    + os.fsencode(self.repository)
                    + b"\0HEAD 0000000000000000000000000000000000000000\0\0"
                    + b"worktree "
                    + os.fsencode(self.resources.worktree_path)
                    + b"\0HEAD 0000000000000000000000000000000000000000\0detached\0\0"
                )
                return subprocess.CompletedProcess(arguments, 0, output, b"")
            return subprocess.CompletedProcess(arguments, 0, b"", b"")

        with mock.patch(
            "runtime.token_saver.resources.subprocess.run", side_effect=fake_run
        ):
            result = cleanup_invocation(self.resources)

        self.assertEqual(result.status, "cleaned")
        self.assertEqual(len(calls), 2)
        remove_arguments, remove_options = calls[1]
        self.assertEqual(
            remove_arguments[-4:],
            [
                "worktree",
                "remove",
                "--force",
                os.fspath(self.resources.worktree_path),
            ],
        )
        self.assertNotIn("prune", remove_arguments)
        self.assertFalse(any("*" in argument for argument in remove_arguments))
        self.assertIs(remove_options["check"], True)
        self.assertNotIn("shell", remove_options)
        self.assertEqual(
            remove_options["env"],
            {
                "LC_ALL": "C",
                "LANG": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            },
        )

    def test_git_probe_failure_does_not_consume_the_active_manifest(self) -> None:
        with mock.patch(
            "runtime.token_saver.resources._run_git",
            side_effect=subprocess.CalledProcessError(1, ["git", "worktree", "list"]),
        ):
            failed = cleanup_invocation(self.resources)

        self.assertEqual(failed.status, "rejected")
        self.assertTrue(self.resources.manifest_path.is_file())
        self.assertEqual(
            list(self.temp_parent.glob(".token-saver-consumed-*.json")),
            [],
        )

        retried = cleanup_invocation(self.resources)
        self.assertEqual(retried.status, "cleaned")

    def test_git_remove_failure_retains_a_resumable_consuming_claim(self) -> None:
        _git(
            self.repository,
            "worktree",
            "add",
            "--detach",
            os.fspath(self.resources.worktree_path),
            "HEAD",
        )
        list_output = (
            b"worktree "
            + os.fsencode(self.repository)
            + b"\0HEAD 0000000000000000000000000000000000000000\0\0"
            + b"worktree "
            + os.fsencode(self.resources.worktree_path)
            + b"\0HEAD 0000000000000000000000000000000000000000\0detached\0\0"
        )

        def fail_remove(
            repository: Path, *arguments: str
        ) -> subprocess.CompletedProcess[bytes]:
            if arguments == ("worktree", "list", "--porcelain", "-z"):
                return subprocess.CompletedProcess(arguments, 0, list_output, b"")
            raise subprocess.CalledProcessError(1, ["git", *arguments])

        with mock.patch(
            "runtime.token_saver.resources._run_git", side_effect=fail_remove
        ):
            failed = cleanup_invocation(self.resources)

        self.assertEqual(failed.status, "rejected")
        self.assertTrue(self.resources.manifest_path.is_file())
        receipts = list(self.temp_parent.glob(".token-saver-consumed-*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            json.loads(receipts[0].read_text(encoding="utf-8"))["state"],
            "consuming",
        )
        self.assertIn(
            self.resources.worktree_path,
            _registered_worktrees(self.repository),
        )

        retried = cleanup_invocation(self.resources)
        self.assertEqual(retried.status, "cleaned")

    def test_cleanup_removes_a_registered_worktree_after_its_directory_vanished(
        self,
    ) -> None:
        _git(
            self.repository,
            "worktree",
            "add",
            "--detach",
            os.fspath(self.resources.worktree_path),
            "HEAD",
        )
        shutil.rmtree(self.resources.worktree_path)

        self.assertFalse(self.resources.worktree_path.exists())
        self.assertIn(
            self.resources.worktree_path,
            _registered_worktrees(self.repository),
        )

        with mock.patch(
            "runtime.token_saver.resources._remove_owned_directory",
            side_effect=OSError("injected interruption after git removal"),
        ):
            interrupted = cleanup_invocation(self.resources)

        self.assertEqual(interrupted.status, "rejected")
        self.assertTrue(interrupted.worktree_removed is False)
        self.assertNotIn(
            self.resources.worktree_path,
            _registered_worktrees(self.repository),
        )
        receipts = list(self.temp_parent.glob(".token-saver-consumed-*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            json.loads(receipts[0].read_text(encoding="utf-8"))["state"],
            "consuming",
        )

        cleaned = cleanup_invocation(self.resources)
        repeated = cleanup_invocation(self.resources)

        self.assertEqual(cleaned.status, "cleaned")
        self.assertEqual(repeated.status, "already_consumed")

    def test_consuming_receipt_resumes_after_interruption_before_deletion(self) -> None:
        with mock.patch(
            "runtime.token_saver.resources._remove_owned_directory",
            side_effect=OSError("injected interruption"),
        ):
            interrupted = cleanup_invocation(self.resources)

        self.assertEqual(interrupted.status, "rejected")
        receipts = list(self.temp_parent.glob(".token-saver-consumed-*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            json.loads(receipts[0].read_text(encoding="utf-8"))["state"],
            "consuming",
        )

        resumed = cleanup_invocation(self.resources)
        repeated = cleanup_invocation(self.resources)
        self.assertEqual(resumed.status, "cleaned")
        self.assertEqual(repeated.status, "already_consumed")

    def test_claimed_receipt_rejects_a_missing_root_instead_of_consuming(self) -> None:
        with mock.patch(
            "runtime.token_saver.resources._remove_owned_directory",
            side_effect=OSError("injected interruption"),
        ):
            interrupted = cleanup_invocation(self.resources)
        self.assertEqual(interrupted.status, "rejected")
        moved_root = self.temp_parent / "moved-root"
        self.resources.invocation_root.rename(moved_root)

        resumed = cleanup_invocation(self.resources)

        self.assertEqual(resumed.status, "rejected")
        receipts = list(self.temp_parent.glob(".token-saver-consumed-*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            json.loads(receipts[0].read_text(encoding="utf-8"))["state"],
            "consuming",
        )
        self.assertTrue((moved_root / "manifest.json").is_file())

    def test_claimed_receipt_rejects_a_same_name_directory_replacement(self) -> None:
        with mock.patch(
            "runtime.token_saver.resources._remove_owned_directory",
            side_effect=OSError("injected interruption"),
        ):
            interrupted = cleanup_invocation(self.resources)
        self.assertEqual(interrupted.status, "rejected")

        original_root = self.temp_parent / "original-root"
        self.resources.invocation_root.rename(original_root)
        self.resources.invocation_root.mkdir()
        replacement_route = self.resources.invocation_root / "route-state"
        replacement_route.mkdir()
        route_sentinel = replacement_route / "must-survive"
        route_sentinel.write_text("route", encoding="utf-8")
        replacement_evidence = self.resources.invocation_root / "evidence"
        replacement_evidence.mkdir()
        evidence_sentinel = replacement_evidence / "must-survive"
        evidence_sentinel.write_text("evidence", encoding="utf-8")

        resumed = cleanup_invocation(self.resources)

        self.assertEqual(resumed.status, "rejected")
        self.assertEqual(route_sentinel.read_text(encoding="utf-8"), "route")
        self.assertEqual(
            evidence_sentinel.read_text(encoding="utf-8"), "evidence"
        )
        self.assertTrue((original_root / "manifest.json").is_file())
        receipts = list(self.temp_parent.glob(".token-saver-consumed-*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            json.loads(receipts[0].read_text(encoding="utf-8"))["state"],
            "consuming",
        )

    def test_consuming_receipt_resumes_after_partial_marker_deletion(self) -> None:
        original_unlink = os.unlink
        marker_unlinks = 0

        def fail_second_marker(path, *arguments, **options):
            nonlocal marker_unlinks
            if os.fspath(path).endswith(".owner.json"):
                marker_unlinks += 1
                if marker_unlinks == 2:
                    raise OSError("injected marker interruption")
            return original_unlink(path, *arguments, **options)

        with mock.patch(
            "runtime.token_saver.resources.os.unlink",
            side_effect=fail_second_marker,
        ):
            interrupted = cleanup_invocation(self.resources)

        self.assertEqual(interrupted.status, "rejected")
        self.assertFalse(self.resources.marker_paths[0].exists())
        receipts = list(self.temp_parent.glob(".token-saver-consumed-*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            json.loads(receipts[0].read_text(encoding="utf-8"))["state"],
            "consuming",
        )

        resumed = cleanup_invocation(self.resources)
        self.assertEqual(resumed.status, "cleaned")

    def test_consuming_receipt_resumes_when_final_transition_failed(self) -> None:
        with mock.patch(
            "runtime.token_saver.resources._finish_consumption_receipt",
            side_effect=OSError("injected final transition failure"),
        ):
            interrupted = cleanup_invocation(self.resources)

        self.assertEqual(interrupted.status, "rejected")
        self.assertFalse(self.resources.manifest_path.exists())
        receipts = list(self.temp_parent.glob(".token-saver-consumed-*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            json.loads(receipts[0].read_text(encoding="utf-8"))["state"],
            "consuming",
        )

        resumed = cleanup_invocation(self.resources)
        self.assertEqual(resumed.status, "cleaned")

    def test_consuming_receipt_reuses_completed_atomic_transition_file(self) -> None:
        original_replace = os.replace
        failed_once = False

        def fail_first_receipt_replace(source, destination, *arguments, **options):
            nonlocal failed_once
            if (
                not failed_once
                and "consumed-complete" in os.fspath(source)
                and os.fspath(destination).endswith(".json")
            ):
                failed_once = True
                raise OSError("injected atomic transition failure")
            return original_replace(source, destination, *arguments, **options)

        with mock.patch(
            "runtime.token_saver.resources.os.replace",
            side_effect=fail_first_receipt_replace,
        ):
            interrupted = cleanup_invocation(self.resources)

        self.assertEqual(interrupted.status, "rejected")
        self.assertEqual(
            len(
                list(
                    self.temp_parent.glob(
                        ".token-saver-consumed-complete-*.json"
                    )
                )
            ),
            1,
        )

        resumed = cleanup_invocation(self.resources)
        self.assertEqual(resumed.status, "cleaned")


if __name__ == "__main__":
    unittest.main()
