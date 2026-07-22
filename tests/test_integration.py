from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import runtime.model_boss.integration as integration_module
from runtime.model_boss.bundle import seal_delta_bundle
from runtime.model_boss.evidence import (
    ApprovalBinding,
    CanonicalPatch,
    EvidenceRecord,
    PrivateDigestKind,
    PrivateRecord,
    RecordStatus,
    RecordTag,
    SourceSnapshot,
    WorkerDelta,
    encode_canonical_patch,
    encode_source_snapshot,
    encode_worker_delta,
)
from runtime.model_boss.integration import (
    Approval,
    integrate_reviewed_delta,
    integrate_sealed_delta_bundle,
)
from runtime.model_boss.repository import (
    RepositoryError,
    capture_source_snapshot,
    capture_worker_delta,
    create_worktree,
    materialize_snapshot,
    project_task_patch,
)
from runtime.model_boss.resources import (
    CleanupResult,
    cleanup_invocation,
    create_invocation_resources,
)


EMPTY_SNAPSHOT = SourceSnapshot(baseline_oid=b"0" * 40)
EMPTY_DELTA = WorkerDelta(records=())
PLACEHOLDER_BINDING = ApprovalBinding(
    source_snapshot_hash="1" * 64,
    worker_delta_hash="2" * 64,
    projected_task_patch_hash="3" * 64,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _approval_for(
    snapshot: SourceSnapshot,
    delta: WorkerDelta,
    projected: CanonicalPatch,
) -> Approval:
    binding = ApprovalBinding(
        source_snapshot_hash=_sha256(encode_source_snapshot(snapshot)),
        worker_delta_hash=_sha256(encode_worker_delta(delta)),
        projected_task_patch_hash=_sha256(encode_canonical_patch(projected)),
    )
    return Approval(
        version=1,
        decision="approve",
        binding=binding,
        approval_binding_hash=binding.canonical_hash,
    )


def _modified_text(path: bytes, canonical_diff: bytes) -> EvidenceRecord:
    return EvidenceRecord(
        tag=RecordTag.TEXT_DIFF,
        path=path,
        status=RecordStatus.MODIFIED,
        old_mode=0o100644,
        new_mode=0o100644,
        canonical_diff=canonical_diff,
    )


def _untracked(path: bytes, content: bytes = b"worker output\n") -> EvidenceRecord:
    return EvidenceRecord(
        tag=RecordTag.UNTRACKED,
        path=path,
        status=RecordStatus.UNTRACKED,
        old_mode=0,
        new_mode=0o100644,
        content=content,
    )


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
    }
    completed = subprocess.run(
        ("git", "--no-pager", "-C", os.fspath(repo), *args),
        check=True,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    return completed.stdout


def _worktree_state(repo: Path, destination: SourceSnapshot) -> tuple[object, ...]:
    entries: list[tuple[bytes, str, int, bytes]] = []
    root = os.fsencode(repo)
    for current_root, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory_names[:] = [name for name in directory_names if name != b".git"]
        for name in file_names:
            path = os.path.join(current_root, name)
            relative = os.path.relpath(path, root).replace(os.sep.encode(), b"/")
            metadata = os.lstat(path)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                entries.append((relative, "symlink", mode, os.readlink(path)))
            else:
                with open(path, "rb") as stream:
                    entries.append((relative, "file", mode, stream.read()))
    entries.sort(key=lambda entry: entry[0])
    return (
        tuple(entries),
        (repo / ".git" / "index").read_bytes(),
        _git(repo, "ls-files", "--stage", "-z"),
        encode_source_snapshot(destination),
    )


@contextmanager
def _repository_functions(repository_stub: types.ModuleType):
    with (
        mock.patch.object(
            integration_module,
            "project_task_patch",
            repository_stub.project_task_patch,
        ),
        mock.patch.object(
            integration_module,
            "capture_destination",
            repository_stub.capture_destination,
        ),
    ):
        yield


class ApprovalGuardTests(unittest.TestCase):
    def test_mutated_approval_version_is_rejected_before_repository_access(self) -> None:
        approval = _approval_for(
            EMPTY_SNAPSHOT,
            EMPTY_DELTA,
            project_task_patch(EMPTY_SNAPSHOT, EMPTY_DELTA),
        )
        object.__setattr__(approval, "version", 999)

        result = integrate_reviewed_delta(
            Path("/repository-must-not-be-read-for-mutated-approval"),
            EMPTY_SNAPSHOT,
            EMPTY_DELTA,
            approval,
        )

        self.assertEqual(result.status, "approval_stale")
        self.assertFalse(result.applied)

    def test_mutated_revise_decision_cannot_become_an_approval(self) -> None:
        approved_shape = _approval_for(
            EMPTY_SNAPSHOT,
            EMPTY_DELTA,
            project_task_patch(EMPTY_SNAPSHOT, EMPTY_DELTA),
        )
        revise = Approval(
            version=approved_shape.version,
            decision="revise",
            binding=approved_shape.binding,
            approval_binding_hash=approved_shape.approval_binding_hash,
        )
        object.__setattr__(revise, "decision", "approve")

        result = integrate_reviewed_delta(
            Path("/repository-must-not-be-read-for-mutated-decision"),
            EMPTY_SNAPSHOT,
            EMPTY_DELTA,
            revise,
        )

        self.assertEqual(result.status, "approval_stale")
        self.assertFalse(result.applied)

    def test_git_environment_preserves_windows_systemroot(self) -> None:
        with (
            mock.patch.object(integration_module.os, "name", "nt"),
            mock.patch.dict(
                integration_module.os.environ,
                {"SYSTEMROOT": r"C:\\Windows"},
                clear=False,
            ),
        ):
            environment = integration_module._git_environment()

        self.assertEqual(environment["SYSTEMROOT"], r"C:\\Windows")

    def test_windows_symlink_targets_reject_aliases_and_device_names(self) -> None:
        def symlink(target: bytes) -> EvidenceRecord:
            return EvidenceRecord(
                tag=RecordTag.SYMLINK,
                path=b"links/output",
                status=RecordStatus.UNTRACKED,
                old_mode=0,
                new_mode=0o120000,
                content=target,
            )

        for unsafe_target in (
            b"../.git./config",
            b"../.git /config",
            b"NUL",
            b"NUL.txt",
            b"safe.txt:stream",
            b"COM1.log",
        ):
            with self.subTest(target=unsafe_target):
                self.assertFalse(
                    integration_module._symlink_target_is_safe(
                        symlink(unsafe_target),
                        windows=True,
                    )
                )
        self.assertTrue(
            integration_module._symlink_target_is_safe(
                symlink(b"../safe/target.txt"),
                windows=True,
            )
        )

    def test_windows_patch_paths_reject_aliases_devices_and_ads(self) -> None:
        for unsafe_path in (
            b"C:relative.txt",
            b"dir\\escape.txt",
            b"safe.txt:stream",
            b"trailing.",
            b"trailing ",
            b".GIT/config",
            b"NUL",
            b"NUL.txt",
            b"COM1.log",
            b"lpt9",
        ):
            with self.subTest(path=unsafe_path):
                self.assertFalse(
                    integration_module._windows_patch_path_is_safe(unsafe_path)
                )
        self.assertTrue(
            integration_module._windows_patch_path_is_safe(b"safe/path.txt")
        )

    def test_non_approve_decision_is_rejected_before_repository_access(self) -> None:
        approval = Approval(
            version=1,
            decision="revise",
            binding=PLACEHOLDER_BINDING,
            approval_binding_hash=PLACEHOLDER_BINDING.canonical_hash,
        )

        result = integrate_reviewed_delta(
            Path("/repository-must-not-be-read"),
            EMPTY_SNAPSHOT,
            EMPTY_DELTA,
            approval,
        )

        self.assertEqual(result.version, 1)
        self.assertEqual(result.status, "review_revise")
        self.assertFalse(result.applied)


class DestinationGuardTests(unittest.TestCase):
    def test_same_status_destination_content_mutation_is_rejected(self) -> None:
        source_record = _modified_text(b"task.txt", b"source-content-diff\n")
        mutated_record = _modified_text(b"task.txt", b"mutated-content-diff\n")
        snapshot = SourceSnapshot(
            baseline_oid=b"0" * 40,
            allowed_paths=(b"task.txt",),
            unstaged=(source_record,),
        )
        mutated_destination = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            unstaged=(mutated_record,),
        )
        projected = CanonicalPatch(
            records=(source_record,),
            private_summary=snapshot.private_summary,
        )
        approval = _approval_for(snapshot, EMPTY_DELTA, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, delta: projected
        repository_stub.capture_destination = (
            lambda repo, allowed_paths: mutated_destination
        )

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                Path("/repository-is-not-accessed-after-capture"),
                snapshot,
                EMPTY_DELTA,
                approval,
            )

        self.assertEqual(result.status, "destination_changed")
        self.assertFalse(result.applied)

    def test_mode_only_destination_mutation_is_rejected(self) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=b"0" * 40,
            allowed_paths=(b"task.sh",),
        )
        mode_change = EvidenceRecord(
            tag=RecordTag.MODE_ONLY,
            path=b"task.sh",
            status=RecordStatus.MODIFIED,
            old_mode=0o100644,
            new_mode=0o100755,
        )
        mutated_destination = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            unstaged=(mode_change,),
        )
        projected = CanonicalPatch(
            records=(),
            private_summary=snapshot.private_summary,
        )
        approval = _approval_for(snapshot, EMPTY_DELTA, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, delta: projected
        repository_stub.capture_destination = (
            lambda repo, allowed_paths: mutated_destination
        )

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                Path("/repository-is-not-accessed-after-capture"),
                snapshot,
                EMPTY_DELTA,
                approval,
            )

        self.assertEqual(result.status, "destination_changed")
        self.assertFalse(result.applied)

    def test_private_out_of_scope_content_mutation_is_rejected(self) -> None:
        original_private = PrivateRecord(
            digest_kind=PrivateDigestKind.CONTENT,
            path=b"private.txt",
            status=RecordStatus.MODIFIED,
            mode=0o100644,
            size=7,
            digest="1" * 64,
        )
        mutated_private = PrivateRecord(
            digest_kind=PrivateDigestKind.CONTENT,
            path=b"private.txt",
            status=RecordStatus.MODIFIED,
            mode=0o100644,
            size=7,
            digest="2" * 64,
        )
        snapshot = SourceSnapshot(
            baseline_oid=b"0" * 40,
            allowed_paths=(b"task.txt",),
            private=(original_private,),
        )
        mutated_destination = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            private=(mutated_private,),
        )
        projected = CanonicalPatch(
            records=(),
            private_summary=snapshot.private_summary,
        )
        approval = _approval_for(snapshot, EMPTY_DELTA, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, delta: projected
        repository_stub.capture_destination = (
            lambda repo, allowed_paths: mutated_destination
        )

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                Path("/repository-is-not-accessed-after-capture"),
                snapshot,
                EMPTY_DELTA,
                approval,
            )

        self.assertEqual(result.status, "destination_changed")
        self.assertFalse(result.applied)

    def test_worker_delta_path_outside_source_allowlist_is_rejected(self) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=b"0" * 40,
            allowed_paths=(b"allowed.txt",),
        )
        delta = WorkerDelta(records=(_untracked(b"outside.txt"),))
        projected = CanonicalPatch(
            records=delta.records,
            private_summary=snapshot.private_summary,
        )
        approval = _approval_for(snapshot, delta, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, worker_delta: projected
        repository_stub.capture_destination = lambda repo, allowed_paths: snapshot

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                Path("/repository-must-not-be-read-for-out-of-scope-delta"),
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "scope_violation")
        self.assertFalse(result.applied)

    def test_destination_capture_error_returns_structured_changed_result(self) -> None:
        projected = project_task_patch(EMPTY_SNAPSHOT, EMPTY_DELTA)
        approval = _approval_for(EMPTY_SNAPSHOT, EMPTY_DELTA, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = project_task_patch

        def capture_destination(repo: Path, allowed_paths: tuple[bytes, ...]):
            raise RepositoryError("destination capture failed")

        repository_stub.capture_destination = capture_destination

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                Path("/repository-capture-fails-closed"),
                EMPTY_SNAPSHOT,
                EMPTY_DELTA,
                approval,
            )

        self.assertEqual(result.status, "destination_changed")
        self.assertFalse(result.applied)


class RealGitIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="model-boss-integration-test-"
        )
        self.repo = Path(self.temporary_directory.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Model Boss Test")
        _git(self.repo, "config", "user.email", "model-boss@example.invalid")
        _git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "task.txt").write_bytes(b"actual\n")
        _git(self.repo, "add", "--", "task.txt")
        _git(self.repo, "commit", "-q", "-m", "baseline")
        self.baseline_oid = _git(self.repo, "rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_empty_delta_is_a_verified_noop(self) -> None:
        snapshot = capture_source_snapshot(self.repo, (b"task.txt",))
        delta = WorkerDelta(records=())
        approval = _approval_for(
            snapshot,
            delta,
            project_task_patch(snapshot, delta),
        )
        before = _worktree_state(self.repo, snapshot)

        result = integrate_reviewed_delta(self.repo, snapshot, delta, approval)

        self.assertEqual(result.status, "ok")
        self.assertFalse(result.applied)
        self.assertEqual(
            result.projected_task_patch_hash,
            approval.binding.projected_task_patch_hash,
        )
        self.assertEqual(_worktree_state(self.repo, snapshot), before)

    def test_sealed_bundle_entry_integrates_and_cleans_exact_invocation(self) -> None:
        temp_parent = self.repo.parent / "managed-temporary"
        temp_parent.mkdir()
        resources = create_invocation_resources(self.repo, temp_parent)
        snapshot = capture_source_snapshot(self.repo, (b"managed-output.txt",))
        worker_record = _untracked(
            b"managed-output.txt",
            b"sealed worker result\n",
        )
        projected_snapshot = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            untracked=(worker_record,),
        )
        delta = WorkerDelta(
            records=(worker_record,),
            projected_snapshot=projected_snapshot,
        )
        projected = project_task_patch(snapshot, delta)
        seal_delta_bundle(resources, snapshot, delta)
        approval = _approval_for(snapshot, delta, projected)

        result = integrate_sealed_delta_bundle(resources, approval)

        self.assertEqual(result.transaction.status, "ok")
        self.assertTrue(result.transaction.applied)
        self.assertEqual(result.cleanup.status, "cleaned")
        self.assertFalse(resources.invocation_root.exists())
        self.assertEqual(
            (self.repo / "managed-output.txt").read_bytes(),
            b"sealed worker result\n",
        )

    def test_sealed_bundle_rejection_still_consumes_invocation(self) -> None:
        temp_parent = self.repo.parent / "rejected-managed-temporary"
        temp_parent.mkdir()
        resources = create_invocation_resources(self.repo, temp_parent)
        snapshot = capture_source_snapshot(self.repo, ())
        delta = WorkerDelta(records=())
        seal_delta_bundle(resources, snapshot, delta)
        approval = _approval_for(
            snapshot,
            delta,
            project_task_patch(snapshot, delta),
        )
        rejected_approval = Approval(
            version=approval.version,
            decision="revise",
            binding=approval.binding,
            approval_binding_hash=approval.approval_binding_hash,
        )

        result = integrate_sealed_delta_bundle(resources, rejected_approval)

        self.assertEqual(result.transaction.status, "review_revise")
        self.assertFalse(result.transaction.applied)
        self.assertEqual(result.cleanup.status, "cleaned")
        self.assertFalse(resources.invocation_root.exists())

    def test_malformed_sealed_bundle_fails_before_integration_and_is_cleaned(
        self,
    ) -> None:
        temp_parent = self.repo.parent / "malformed-managed-temporary"
        temp_parent.mkdir()
        resources = create_invocation_resources(self.repo, temp_parent)
        resources.delta_bundle_path.write_bytes(b"not a sealed bundle")
        resources.delta_bundle_path.chmod(0o400)
        before = (self.repo / "task.txt").read_bytes()
        approval = Approval(
            version=1,
            decision="approve",
            binding=PLACEHOLDER_BINDING,
            approval_binding_hash=PLACEHOLDER_BINDING.canonical_hash,
        )

        with mock.patch.object(
            integration_module,
            "integrate_reviewed_delta",
        ) as transaction:
            result = integrate_sealed_delta_bundle(resources, approval)

        transaction.assert_not_called()
        self.assertEqual(result.transaction.status, "transport_error")
        self.assertFalse(result.transaction.applied)
        self.assertEqual(result.cleanup.status, "cleaned")
        self.assertFalse(resources.invocation_root.exists())
        self.assertEqual((self.repo / "task.txt").read_bytes(), before)

    def test_cleanup_failure_does_not_hide_a_successful_sealed_integration(self) -> None:
        temp_parent = self.repo.parent / "cleanup-failure-temporary"
        temp_parent.mkdir()
        resources = create_invocation_resources(self.repo, temp_parent)
        snapshot = capture_source_snapshot(self.repo, (b"cleanup-output.txt",))
        worker_record = _untracked(b"cleanup-output.txt", b"integrated\n")
        projected_snapshot = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            untracked=(worker_record,),
        )
        delta = WorkerDelta(
            records=(worker_record,),
            projected_snapshot=projected_snapshot,
        )
        seal_delta_bundle(resources, snapshot, delta)
        approval = _approval_for(
            snapshot,
            delta,
            project_task_patch(snapshot, delta),
        )
        rejected_cleanup = CleanupResult(
            status="rejected",
            invocation_id=resources.invocation_id,
            message="injected cleanup refusal",
        )

        with mock.patch.object(
            integration_module,
            "cleanup_invocation",
            return_value=rejected_cleanup,
        ) as cleanup:
            result = integrate_sealed_delta_bundle(resources, approval)

        cleanup.assert_called_once_with(resources)
        self.assertEqual(result.transaction.status, "ok")
        self.assertTrue(result.transaction.applied)
        self.assertEqual(result.cleanup.status, "rejected")
        self.assertEqual(
            (self.repo / "cleanup-output.txt").read_bytes(),
            b"integrated\n",
        )
        self.assertEqual(cleanup_invocation(resources).status, "cleaned")

    def test_patch_conflict_preserves_complete_destination_state(self) -> None:
        private_bytes = b"\x00private-out-of-scope\xff"
        (self.repo / "private.bin").write_bytes(private_bytes)
        private_records = [
            PrivateRecord(
                digest_kind=PrivateDigestKind.CONTENT,
                path=b"private.bin",
                status=RecordStatus.UNTRACKED,
                mode=0o100644,
                size=len(private_bytes),
                digest=_sha256(private_bytes),
            )
        ]
        if hasattr(os, "symlink"):
            os.symlink("task.txt", self.repo / "private-link")
            private_records.append(
                PrivateRecord(
                    digest_kind=PrivateDigestKind.CONTENT,
                    path=b"private-link",
                    status=RecordStatus.UNTRACKED,
                    mode=0o120000,
                    size=len(b"task.txt"),
                    digest=_sha256(b"task.txt"),
                )
            )
        snapshot = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=(b"task.txt",),
            private=tuple(private_records),
        )
        conflicting_diff = b"".join(
            (
                b"diff --git a/task.txt b/task.txt\n",
                b"--- a/task.txt\n",
                b"+++ b/task.txt\n",
                b"@@ -1 +1 @@\n",
                b"-different-old-content\n",
                b"+worker-content\n",
            )
        )
        delta = WorkerDelta(
            records=(_modified_text(b"task.txt", conflicting_diff),)
        )
        projected = CanonicalPatch(
            records=delta.records,
            private_summary=snapshot.private_summary,
        )
        approval = _approval_for(snapshot, delta, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, worker_delta: projected
        repository_stub.capture_destination = lambda repo, allowed_paths: snapshot
        before = _worktree_state(self.repo, snapshot)

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                self.repo,
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "transport_error")
        self.assertFalse(result.applied)
        self.assertEqual(_worktree_state(self.repo, snapshot), before)

    def test_patch_header_path_escape_is_rejected_as_scope_violation(self) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=(b"task.txt",),
        )
        escaping_diff = b"".join(
            (
                b"diff --git a/task.txt b/../escaped.txt\n",
                b"--- a/task.txt\n",
                b"+++ b/../escaped.txt\n",
                b"@@ -1 +1 @@\n",
                b"-actual\n",
                b"+escaped\n",
            )
        )
        delta = WorkerDelta(records=(_modified_text(b"task.txt", escaping_diff),))
        projected = CanonicalPatch(
            records=delta.records,
            private_summary=snapshot.private_summary,
        )
        approval = _approval_for(snapshot, delta, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, worker_delta: projected
        repository_stub.capture_destination = lambda repo, allowed_paths: snapshot
        before = _worktree_state(self.repo, snapshot)
        escaped_path = self.repo.parent / "escaped.txt"

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                self.repo,
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "scope_violation")
        self.assertFalse(result.applied)
        self.assertFalse(escaped_path.exists())
        self.assertEqual(_worktree_state(self.repo, snapshot), before)

    def test_patch_old_header_path_escape_is_rejected_as_scope_violation(self) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=(b"task.txt",),
        )
        escaping_diff = b"".join(
            (
                b"diff --git a/task.txt b/task.txt\n",
                b"--- a/../escaped.txt\n",
                b"+++ b/task.txt\n",
                b"@@ -1 +1 @@\n",
                b"-actual\n",
                b"+escaped\n",
            )
        )
        delta = WorkerDelta(records=(_modified_text(b"task.txt", escaping_diff),))
        projected = CanonicalPatch(
            records=delta.records,
            private_summary=snapshot.private_summary,
        )
        approval = _approval_for(snapshot, delta, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, worker_delta: projected
        repository_stub.capture_destination = lambda repo, allowed_paths: snapshot
        before = _worktree_state(self.repo, snapshot)

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                self.repo,
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "scope_violation")
        self.assertFalse(result.applied)
        self.assertEqual(_worktree_state(self.repo, snapshot), before)

    def test_success_applies_only_delta_and_returns_approved_projected_hash(
        self,
    ) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=(b"task.txt",),
        )
        worker_diff = b"".join(
            (
                b"diff --git a/task.txt b/task.txt\n",
                b"--- a/task.txt\n",
                b"+++ b/task.txt\n",
                b"@@ -1 +1 @@\n",
                b"-actual\n",
                b"+worker-content\n",
            )
        )
        worker_record = _modified_text(b"task.txt", worker_diff)
        delta = WorkerDelta(records=(worker_record,))
        projected = CanonicalPatch(
            records=(worker_record,),
            private_summary=snapshot.private_summary,
        )
        final_destination = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            unstaged=(worker_record,),
        )
        approval = _approval_for(snapshot, delta, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, worker_delta: projected

        def capture_destination(repo: Path, allowed_paths: tuple[bytes, ...]):
            if (self.repo / "task.txt").read_bytes() == b"actual\n":
                return snapshot
            return final_destination

        repository_stub.capture_destination = capture_destination
        index_before = _git(self.repo, "ls-files", "--stage", "-z")

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                self.repo,
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.applied)
        self.assertEqual(
            result.projected_task_patch_hash,
            approval.binding.projected_task_patch_hash,
        )
        self.assertEqual((self.repo / "task.txt").read_bytes(), b"worker-content\n")
        self.assertEqual(_git(self.repo, "ls-files", "--stage", "-z"), index_before)

    def test_destination_is_rechecked_immediately_after_apply_check(self) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=(b"task.txt",),
        )
        worker_diff = b"".join(
            (
                b"diff --git a/task.txt b/task.txt\n",
                b"--- a/task.txt\n",
                b"+++ b/task.txt\n",
                b"@@ -1 +1 @@\n",
                b"-actual\n",
                b"+worker-content\n",
            )
        )
        worker_record = _modified_text(b"task.txt", worker_diff)
        delta = WorkerDelta(records=(worker_record,))
        projected = CanonicalPatch(
            records=(worker_record,),
            private_summary=snapshot.private_summary,
        )
        raced_record = _modified_text(b"task.txt", b"user-race-diff\n")
        raced_destination = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            unstaged=(raced_record,),
        )
        approval = _approval_for(snapshot, delta, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, worker_delta: projected
        captures = 0

        def capture_destination(repo: Path, allowed_paths: tuple[bytes, ...]):
            nonlocal captures
            captures += 1
            if captures == 1:
                return snapshot
            (self.repo / "task.txt").write_bytes(b"user-race\n")
            return raced_destination

        repository_stub.capture_destination = capture_destination

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                self.repo,
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "destination_changed")
        self.assertFalse(result.applied)
        self.assertEqual((self.repo / "task.txt").read_bytes(), b"user-race\n")

    def test_untracked_binary_delta_is_applied_without_index_mutation(self) -> None:
        binary_content = b"\x00\xffworker-binary\n\x00"
        snapshot = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=(b"asset.bin",),
        )
        binary_record = _untracked(b"asset.bin", binary_content)
        delta = WorkerDelta(records=(binary_record,))
        projected = CanonicalPatch(
            records=(binary_record,),
            private_summary=snapshot.private_summary,
        )
        final_destination = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            untracked=(binary_record,),
        )
        approval = _approval_for(snapshot, delta, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, worker_delta: projected
        repository_stub.capture_destination = (
            lambda repo, allowed_paths: (
                final_destination if (self.repo / "asset.bin").exists() else snapshot
            )
        )
        index_before = _git(self.repo, "ls-files", "--stage", "-z")

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                self.repo,
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.applied)
        self.assertEqual((self.repo / "asset.bin").read_bytes(), binary_content)
        self.assertEqual(_git(self.repo, "ls-files", "--stage", "-z"), index_before)

    def test_mode_only_worker_delta_is_applied_without_index_mutation(self) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=(b"task.txt",),
        )
        mode_record = EvidenceRecord(
            tag=RecordTag.MODE_ONLY,
            path=b"task.txt",
            status=RecordStatus.MODIFIED,
            old_mode=0o100644,
            new_mode=0o100755,
        )
        delta = WorkerDelta(records=(mode_record,))
        projected = CanonicalPatch(
            records=(mode_record,),
            private_summary=snapshot.private_summary,
        )
        final_destination = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            unstaged=(mode_record,),
        )
        approval = _approval_for(snapshot, delta, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, worker_delta: projected
        repository_stub.capture_destination = (
            lambda repo, allowed_paths: (
                final_destination
                if stat.S_IMODE((self.repo / "task.txt").stat().st_mode) == 0o755
                else snapshot
            )
        )
        index_before = _git(self.repo, "ls-files", "--stage", "-z")

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                self.repo,
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.applied)
        self.assertEqual(
            stat.S_IMODE((self.repo / "task.txt").stat().st_mode),
            0o755,
        )
        self.assertEqual(_git(self.repo, "ls-files", "--stage", "-z"), index_before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_untracked_symlink_delta_is_applied_without_following_target(self) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=(b"worker-link",),
        )
        symlink_record = EvidenceRecord(
            tag=RecordTag.SYMLINK,
            path=b"worker-link",
            status=RecordStatus.UNTRACKED,
            old_mode=0,
            new_mode=0o120000,
            content=b"task.txt",
        )
        delta = WorkerDelta(records=(symlink_record,))
        projected = CanonicalPatch(
            records=(symlink_record,),
            private_summary=snapshot.private_summary,
        )
        final_destination = SourceSnapshot(
            baseline_oid=self.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            untracked=(symlink_record,),
        )
        approval = _approval_for(snapshot, delta, projected)
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda source, worker_delta: projected
        repository_stub.capture_destination = (
            lambda repo, allowed_paths: (
                final_destination
                if os.path.lexists(self.repo / "worker-link")
                else snapshot
            )
        )
        index_before = _git(self.repo, "ls-files", "--stage", "-z")

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                self.repo,
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.applied)
        self.assertTrue((self.repo / "worker-link").is_symlink())
        self.assertEqual(os.readlink(self.repo / "worker-link"), "task.txt")
        self.assertEqual((self.repo / "task.txt").read_bytes(), b"actual\n")
        self.assertEqual(_git(self.repo, "ls-files", "--stage", "-z"), index_before)

    def test_forged_projected_cache_is_rejected_before_destination_write(self) -> None:
        snapshot = capture_source_snapshot(self.repo, (b"task.txt",))
        malicious_diff = b"".join(
            (
                b"diff --git a/task.txt b/task.txt\n",
                b"--- a/task.txt\n",
                b"+++ b/task.txt\n",
                b"@@ -1 +1 @@\n",
                b"-actual\n",
                b"+MALICIOUS\n",
            )
        )
        reviewed_diff = b"".join(
            (
                b"diff --git a/task.txt b/task.txt\n",
                b"--- a/task.txt\n",
                b"+++ b/task.txt\n",
                b"@@ -1 +1 @@\n",
                b"-actual\n",
                b"+REVIEWED\n",
            )
        )
        reviewed_record = _modified_text(b"task.txt", reviewed_diff)
        forged_projection = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            unstaged=(reviewed_record,),
        )
        delta = WorkerDelta(
            records=(_modified_text(b"task.txt", malicious_diff),),
            projected_snapshot=forged_projection,
        )
        approval = _approval_for(snapshot, delta, project_task_patch(snapshot, delta))
        before = _worktree_state(self.repo, snapshot)

        result = integrate_reviewed_delta(self.repo, snapshot, delta, approval)

        self.assertEqual(result.status, "transport_error")
        self.assertFalse(result.applied)
        self.assertEqual(_worktree_state(self.repo, snapshot), before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_escaping_symlink_target_is_rejected_before_destination_write(self) -> None:
        snapshot = capture_source_snapshot(self.repo, (b"worker-link",))
        escaping_record = EvidenceRecord(
            tag=RecordTag.SYMLINK,
            path=b"worker-link",
            status=RecordStatus.UNTRACKED,
            old_mode=0,
            new_mode=0o120000,
            content=b"../../outside",
        )
        forged_projection = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            untracked=(escaping_record,),
        )
        delta = WorkerDelta(
            records=(escaping_record,),
            projected_snapshot=forged_projection,
        )
        approval = _approval_for(snapshot, delta, project_task_patch(snapshot, delta))
        before = _worktree_state(self.repo, snapshot)

        result = integrate_reviewed_delta(self.repo, snapshot, delta, approval)

        self.assertEqual(result.status, "scope_violation")
        self.assertFalse(result.applied)
        self.assertFalse(os.path.lexists(self.repo / "worker-link"))
        self.assertEqual(_worktree_state(self.repo, snapshot), before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_symlink_through_an_ignored_real_repo_alias_is_rejected(self) -> None:
        outside = self.repo.parent / "outside"
        outside.mkdir()
        (outside / "payload").write_bytes(b"outside\n")
        (self.repo / ".gitignore").write_bytes(b"ignored\n")
        _git(self.repo, "add", "--", ".gitignore")
        _git(self.repo, "commit", "-q", "-m", "ignore fixture")
        os.symlink(os.fspath(outside), self.repo / "ignored")

        snapshot = capture_source_snapshot(self.repo, (b"worker-link",))
        worker_record = EvidenceRecord(
            tag=RecordTag.SYMLINK,
            path=b"worker-link",
            status=RecordStatus.UNTRACKED,
            old_mode=0,
            new_mode=0o120000,
            content=b"ignored/payload",
        )
        projected = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            untracked=(worker_record,),
        )
        delta = WorkerDelta(
            records=(worker_record,),
            projected_snapshot=projected,
        )
        approval = _approval_for(snapshot, delta, project_task_patch(snapshot, delta))
        before = _worktree_state(self.repo, snapshot)

        result = integrate_reviewed_delta(self.repo, snapshot, delta, approval)

        self.assertEqual(result.status, "scope_violation")
        self.assertFalse(result.applied)
        self.assertFalse(os.path.lexists(self.repo / "worker-link"))
        self.assertEqual(_worktree_state(self.repo, snapshot), before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_symlink_record_content_must_match_the_actual_patch_result(self) -> None:
        snapshot = capture_source_snapshot(self.repo, (b"task.txt",))
        worktree_path = self.repo.parent / "lying-symlink-worker"
        handle = create_worktree(self.repo, snapshot, worktree_path)
        try:
            materialize_snapshot(handle, snapshot)
            (handle.path / "task.txt").unlink()
            os.symlink("actual-target.txt", handle.path / "task.txt")
            captured = capture_worker_delta(handle, snapshot, snapshot.allowed_paths)
        finally:
            _git(
                self.repo,
                "worktree",
                "remove",
                "--force",
                os.fspath(handle.path),
            )
        actual_record = captured.records[0]
        lying_record = EvidenceRecord(
            tag=actual_record.tag,
            path=actual_record.path,
            status=actual_record.status,
            old_mode=actual_record.old_mode,
            new_mode=actual_record.new_mode,
            canonical_diff=actual_record.canonical_diff,
            content=b"claimed-safe-target.txt",
        )
        delta = WorkerDelta(
            records=(lying_record,),
            projected_snapshot=captured.projected_snapshot,
        )
        approval = _approval_for(snapshot, delta, project_task_patch(snapshot, delta))
        before = _worktree_state(self.repo, snapshot)

        result = integrate_reviewed_delta(self.repo, snapshot, delta, approval)

        self.assertEqual(result.status, "scope_violation")
        self.assertFalse(result.applied)
        self.assertFalse((self.repo / "task.txt").is_symlink())
        self.assertEqual(_worktree_state(self.repo, snapshot), before)

    def test_local_apply_ignore_whitespace_cannot_relax_exact_conflicts(self) -> None:
        (self.repo / "task.txt").write_bytes(b"alpha beta\n")
        _git(self.repo, "add", "--", "task.txt")
        _git(self.repo, "commit", "-q", "-m", "whitespace fixture")
        snapshot = capture_source_snapshot(self.repo, (b"task.txt",))
        worktree_path = self.repo.parent / "whitespace-worker"
        handle = create_worktree(self.repo, snapshot, worktree_path)
        try:
            materialize_snapshot(handle, snapshot)
            (handle.path / "task.txt").write_bytes(b"changed\n")
            projected = capture_source_snapshot(handle.path, snapshot.allowed_paths)
        finally:
            _git(
                self.repo,
                "worktree",
                "remove",
                "--force",
                os.fspath(handle.path),
            )
        inexact_diff = b"".join(
            (
                b"diff --git a/task.txt b/task.txt\n",
                b"--- a/task.txt\n",
                b"+++ b/task.txt\n",
                b"@@ -1 +1 @@\n",
                b"-alpha  beta\n",
                b"+changed\n",
            )
        )
        delta = WorkerDelta(
            records=(_modified_text(b"task.txt", inexact_diff),),
            projected_snapshot=projected,
        )
        approval = _approval_for(snapshot, delta, project_task_patch(snapshot, delta))
        _git(self.repo, "config", "apply.ignoreWhitespace", "change")
        _git(
            self.repo,
            "apply",
            "--check",
            "--binary",
            input_bytes=inexact_diff,
        )

        result = integrate_reviewed_delta(self.repo, snapshot, delta, approval)

        self.assertEqual(result.status, "transport_error")
        self.assertFalse(result.applied)
        self.assertEqual((self.repo / "task.txt").read_bytes(), b"alpha beta\n")

    def test_local_apply_whitespace_fix_cannot_rewrite_approved_bytes(self) -> None:
        snapshot = capture_source_snapshot(self.repo, (b"task.txt",))
        worktree_path = self.repo.parent / "trailing-space-worker"
        handle = create_worktree(self.repo, snapshot, worktree_path)
        intended = b"worker result with trailing spaces   \n"
        try:
            materialize_snapshot(handle, snapshot)
            (handle.path / "task.txt").write_bytes(intended)
            delta = capture_worker_delta(handle, snapshot, snapshot.allowed_paths)
            approval = _approval_for(
                snapshot,
                delta,
                project_task_patch(snapshot, delta),
            )
        finally:
            _git(
                self.repo,
                "worktree",
                "remove",
                "--force",
                os.fspath(handle.path),
            )
        _git(self.repo, "config", "apply.whitespace", "fix")

        result = integrate_reviewed_delta(self.repo, snapshot, delta, approval)

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.applied)
        self.assertEqual((self.repo / "task.txt").read_bytes(), intended)

    def test_concealed_index_change_is_rejected_before_worker_write(self) -> None:
        (self.repo / "hidden.txt").write_bytes(b"tracked private state\n")
        _git(self.repo, "add", "--", "hidden.txt")
        _git(self.repo, "commit", "-q", "-m", "hidden fixture")
        snapshot = capture_source_snapshot(self.repo, (b"worker-output.txt",))
        worker_record = _untracked(b"worker-output.txt", b"must not apply\n")
        projected = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            untracked=(worker_record,),
        )
        delta = WorkerDelta(
            records=(worker_record,),
            projected_snapshot=projected,
        )
        approval = _approval_for(snapshot, delta, project_task_patch(snapshot, delta))
        _git(self.repo, "update-index", "--assume-unchanged", "--", "hidden.txt")
        (self.repo / "hidden.txt").write_bytes(b"concealed mutation\n")

        result = integrate_reviewed_delta(self.repo, snapshot, delta, approval)

        self.assertEqual(result.status, "destination_changed")
        self.assertFalse(result.applied)
        self.assertFalse((self.repo / "worker-output.txt").exists())
        self.assertEqual(
            (self.repo / "hidden.txt").read_bytes(),
            b"concealed mutation\n",
        )

    def test_repository_capture_to_guarded_integration_end_to_end(self) -> None:
        (self.repo / "task.txt").write_bytes(b"staged-source\n")
        _git(self.repo, "add", "--", "task.txt")
        (self.repo / "task.txt").write_bytes(b"unstaged-source\n")
        allowed_paths = (b"task.txt", b"worker-output.bin")
        snapshot = capture_source_snapshot(self.repo, allowed_paths)
        worktree_path = self.repo.parent / "worker-worktree"
        handle = create_worktree(self.repo, snapshot, worktree_path)
        worker_content = b"\x00captured-worker-output\xff\n"
        try:
            materialize_snapshot(handle, snapshot)
            self.assertEqual(
                (handle.path / "task.txt").read_bytes(),
                b"unstaged-source\n",
            )
            (handle.path / "worker-output.bin").write_bytes(worker_content)
            delta = capture_worker_delta(handle, snapshot, allowed_paths)
            projected = project_task_patch(snapshot, delta)
            approval = _approval_for(snapshot, delta, projected)
        finally:
            _git(
                self.repo,
                "worktree",
                "remove",
                "--force",
                os.fspath(handle.path),
            )

        task_bytes_before = (self.repo / "task.txt").read_bytes()
        staged_diff_before = _git(self.repo, "diff", "--cached", "--binary")
        unstaged_diff_before = _git(self.repo, "diff", "--binary")
        index_before = _git(self.repo, "ls-files", "--stage", "-z")

        result = integrate_reviewed_delta(
            self.repo,
            snapshot,
            delta,
            approval,
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.applied)
        self.assertEqual(
            result.projected_task_patch_hash,
            approval.binding.projected_task_patch_hash,
        )
        self.assertEqual((self.repo / "task.txt").read_bytes(), task_bytes_before)
        self.assertEqual(
            _git(self.repo, "diff", "--cached", "--binary"),
            staged_diff_before,
        )
        self.assertEqual(_git(self.repo, "diff", "--binary"), unstaged_diff_before)
        self.assertEqual(_git(self.repo, "ls-files", "--stage", "-z"), index_before)
        self.assertEqual(
            (self.repo / "worker-output.bin").read_bytes(),
            worker_content,
        )
        final_snapshot = capture_source_snapshot(self.repo, allowed_paths)
        final_patch = project_task_patch(final_snapshot, WorkerDelta(records=()))
        self.assertEqual(
            _sha256(encode_canonical_patch(final_patch)),
            approval.binding.projected_task_patch_hash,
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_regular_to_symlink_type_change_integrates_exactly(self) -> None:
        allowed_paths = (b"task.txt",)
        snapshot = capture_source_snapshot(self.repo, allowed_paths)
        worktree_path = self.repo.parent / "symlink-type-worker-worktree"
        handle = create_worktree(self.repo, snapshot, worktree_path)
        try:
            materialize_snapshot(handle, snapshot)
            (handle.path / "task.txt").unlink()
            os.symlink("inside-target.txt", handle.path / "task.txt")
            delta = capture_worker_delta(handle, snapshot, allowed_paths)
            self.assertEqual(len(delta.records), 1)
            self.assertEqual(delta.records[0].status, RecordStatus.TYPE_CHANGED)
            approval = _approval_for(
                snapshot,
                delta,
                project_task_patch(snapshot, delta),
            )
        finally:
            _git(
                self.repo,
                "worktree",
                "remove",
                "--force",
                os.fspath(handle.path),
            )

        result = integrate_reviewed_delta(
            self.repo,
            snapshot,
            delta,
            approval,
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.applied)
        self.assertTrue((self.repo / "task.txt").is_symlink())
        self.assertEqual(os.readlink(self.repo / "task.txt"), "inside-target.txt")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_symlink_to_regular_type_change_integrates_exactly(self) -> None:
        (self.repo / "task.txt").unlink()
        os.symlink("inside-target.txt", self.repo / "task.txt")
        _git(self.repo, "add", "--", "task.txt")
        _git(self.repo, "commit", "-q", "-m", "symlink baseline")
        allowed_paths = (b"task.txt",)
        snapshot = capture_source_snapshot(self.repo, allowed_paths)
        worktree_path = self.repo.parent / "regular-type-worker-worktree"
        handle = create_worktree(self.repo, snapshot, worktree_path)
        try:
            materialize_snapshot(handle, snapshot)
            (handle.path / "task.txt").unlink()
            (handle.path / "task.txt").write_bytes(b"regular worker result\n")
            delta = capture_worker_delta(handle, snapshot, allowed_paths)
            self.assertEqual(len(delta.records), 1)
            self.assertEqual(delta.records[0].status, RecordStatus.TYPE_CHANGED)
            approval = _approval_for(
                snapshot,
                delta,
                project_task_patch(snapshot, delta),
            )
        finally:
            _git(
                self.repo,
                "worktree",
                "remove",
                "--force",
                os.fspath(handle.path),
            )

        result = integrate_reviewed_delta(
            self.repo,
            snapshot,
            delta,
            approval,
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.applied)
        self.assertFalse((self.repo / "task.txt").is_symlink())
        self.assertEqual(
            (self.repo / "task.txt").read_bytes(),
            b"regular worker result\n",
        )

    def test_post_apply_verification_error_never_reports_not_applied(self) -> None:
        allowed_paths = (b"worker-output.bin",)
        snapshot = capture_source_snapshot(self.repo, allowed_paths)
        worktree_path = self.repo.parent / "verification-worker-worktree"
        handle = create_worktree(self.repo, snapshot, worktree_path)
        worker_content = b"worker output requiring final verification\n"
        try:
            materialize_snapshot(handle, snapshot)
            (handle.path / "worker-output.bin").write_bytes(worker_content)
            delta = capture_worker_delta(handle, snapshot, allowed_paths)
            approval = _approval_for(
                snapshot,
                delta,
                project_task_patch(snapshot, delta),
            )
        finally:
            _git(
                self.repo,
                "worktree",
                "remove",
                "--force",
                os.fspath(handle.path),
            )

        real_capture = integration_module.capture_destination

        def fail_only_after_source_write(repo, paths):
            candidate = Path(os.fsdecode(repo)).resolve()
            if (
                candidate == self.repo.resolve()
                and (self.repo / "worker-output.bin").exists()
            ):
                raise RepositoryError("injected post-apply verification failure")
            return real_capture(repo, paths)

        with mock.patch.object(
            integration_module,
            "capture_destination",
            side_effect=fail_only_after_source_write,
        ):
            result = integrate_reviewed_delta(
                self.repo,
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "transport_error")
        self.assertTrue(result.applied)
        self.assertEqual(
            (self.repo / "worker-output.bin").read_bytes(),
            worker_content,
        )

    def test_failed_apply_that_partially_writes_is_reported_as_applied(self) -> None:
        snapshot = capture_source_snapshot(self.repo, (b"partial.txt",))
        worker_record = _untracked(b"partial.txt", b"approved worker output\n")
        projected_snapshot = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            untracked=(worker_record,),
        )
        delta = WorkerDelta(
            records=(worker_record,),
            projected_snapshot=projected_snapshot,
        )
        approval = _approval_for(
            snapshot,
            delta,
            project_task_patch(snapshot, delta),
        )
        real_apply = integration_module._run_apply

        def fail_after_partial_write(repo: Path, patch: bytes) -> bool:
            if Path(repo).resolve() == self.repo.resolve():
                (self.repo / "partial.txt").write_bytes(b"PARTIAL\n")
                return False
            return real_apply(repo, patch)

        with mock.patch.object(
            integration_module,
            "_run_apply",
            side_effect=fail_after_partial_write,
        ):
            result = integrate_reviewed_delta(
                self.repo,
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "transport_error")
        self.assertTrue(result.applied)
        self.assertEqual((self.repo / "partial.txt").read_bytes(), b"PARTIAL\n")


class ApprovalAndScopeGuardTests(unittest.TestCase):
    def test_nonempty_delta_without_projected_snapshot_is_not_integratable(
        self,
    ) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=b"0" * 40,
            allowed_paths=(b"allowed.txt",),
        )
        delta = WorkerDelta(records=(_untracked(b"allowed.txt"),))
        claimed = CanonicalPatch(
            records=delta.records,
            private_summary=snapshot.private_summary,
        )
        approval = _approval_for(snapshot, delta, claimed)

        result = integrate_reviewed_delta(
            Path("/repository-must-not-be-read-for-incomplete-delta"),
            snapshot,
            delta,
            approval,
        )

        self.assertEqual(result.status, "scope_violation")
        self.assertFalse(result.applied)

    def test_path_escape_in_mutated_delta_is_rejected_without_repository_access(
        self,
    ) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=b"0" * 40,
            allowed_paths=(b"allowed.txt",),
        )
        delta = WorkerDelta(records=(_untracked(b"allowed.txt"),))
        projected = CanonicalPatch(
            records=delta.records,
            private_summary=snapshot.private_summary,
        )
        approval = _approval_for(snapshot, delta, projected)
        object.__setattr__(delta.records[0], "path", b"../escaped.txt")

        result = integrate_reviewed_delta(
            Path("/repository-must-not-be-read-for-path-escape"),
            snapshot,
            delta,
            approval,
        )

        self.assertEqual(result.status, "scope_violation")
        self.assertFalse(result.applied)

    def test_path_escape_in_projected_cache_is_rejected_as_scope_violation(
        self,
    ) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=b"0" * 40,
            allowed_paths=(b"allowed.txt",),
        )
        projected_record = _untracked(b"allowed.txt")
        projected_snapshot = SourceSnapshot(
            baseline_oid=snapshot.baseline_oid,
            allowed_paths=snapshot.allowed_paths,
            untracked=(projected_record,),
        )
        delta = WorkerDelta(records=(), projected_snapshot=projected_snapshot)
        projected = CanonicalPatch(
            records=(projected_record,),
            private_summary=snapshot.private_summary,
            untracked=(projected_record,),
        )
        approval = _approval_for(snapshot, delta, projected)
        assert delta.projected_snapshot is not None
        object.__setattr__(
            delta.projected_snapshot.untracked[0],
            "path",
            b"../escaped.txt",
        )
        repository_stub = types.ModuleType("runtime.model_boss.repository")

        def project_task_patch(source: SourceSnapshot, worker_delta: WorkerDelta):
            projected_source = worker_delta.projected_snapshot or source
            records = (
                projected_source.staged
                + projected_source.unstaged
                + projected_source.untracked
            )
            return CanonicalPatch(
                records=records,
                private_summary=projected_source.private_summary,
                staged=projected_source.staged,
                unstaged=projected_source.unstaged,
                untracked=projected_source.untracked,
            )

        repository_stub.project_task_patch = project_task_patch
        repository_stub.capture_destination = lambda repo, allowed_paths: snapshot

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                Path("/repository-must-not-be-read-for-projected-path-escape"),
                snapshot,
                delta,
                approval,
            )

        self.assertEqual(result.status, "scope_violation")
        self.assertFalse(result.applied)

    def test_projected_snapshot_baseline_escape_returns_structured_scope_failure(
        self,
    ) -> None:
        snapshot = SourceSnapshot(
            baseline_oid=b"0" * 40,
            allowed_paths=(b"allowed.txt",),
        )
        projected_snapshot = SourceSnapshot(
            baseline_oid=b"1" * 40,
            allowed_paths=snapshot.allowed_paths,
        )
        delta = WorkerDelta(records=(), projected_snapshot=projected_snapshot)
        binding = ApprovalBinding(
            source_snapshot_hash=_sha256(encode_source_snapshot(snapshot)),
            worker_delta_hash=_sha256(encode_worker_delta(delta)),
            projected_task_patch_hash="3" * 64,
        )
        approval = Approval(
            version=1,
            decision="approve",
            binding=binding,
            approval_binding_hash=binding.canonical_hash,
        )

        result = integrate_reviewed_delta(
            Path("/repository-must-not-be-read-for-projected-baseline-escape"),
            snapshot,
            delta,
            approval,
        )

        self.assertEqual(result.status, "scope_violation")
        self.assertFalse(result.applied)

    def test_changed_source_snapshot_tuple_member_is_rejected(self) -> None:
        binding = ApprovalBinding(
            source_snapshot_hash="f" * 64,
            worker_delta_hash=_sha256(encode_worker_delta(EMPTY_DELTA)),
            projected_task_patch_hash="3" * 64,
        )
        approval = Approval(
            version=1,
            decision="approve",
            binding=binding,
            approval_binding_hash=binding.canonical_hash,
        )

        result = integrate_reviewed_delta(
            Path("/repository-must-not-be-read"),
            EMPTY_SNAPSHOT,
            EMPTY_DELTA,
            approval,
        )

        self.assertEqual(result.status, "approval_stale")
        self.assertFalse(result.applied)

    def test_changed_worker_delta_tuple_member_is_rejected(self) -> None:
        binding = ApprovalBinding(
            source_snapshot_hash=_sha256(encode_source_snapshot(EMPTY_SNAPSHOT)),
            worker_delta_hash="f" * 64,
            projected_task_patch_hash="3" * 64,
        )
        approval = Approval(
            version=1,
            decision="approve",
            binding=binding,
            approval_binding_hash=binding.canonical_hash,
        )

        result = integrate_reviewed_delta(
            Path("/repository-must-not-be-read"),
            EMPTY_SNAPSHOT,
            EMPTY_DELTA,
            approval,
        )

        self.assertEqual(result.status, "approval_stale")
        self.assertFalse(result.applied)

    def test_changed_projected_patch_tuple_member_is_rejected(self) -> None:
        projected = CanonicalPatch(
            records=(),
            private_summary=EMPTY_SNAPSHOT.private_summary,
        )
        binding = ApprovalBinding(
            source_snapshot_hash=_sha256(encode_source_snapshot(EMPTY_SNAPSHOT)),
            worker_delta_hash=_sha256(encode_worker_delta(EMPTY_DELTA)),
            projected_task_patch_hash="f" * 64,
        )
        approval = Approval(
            version=1,
            decision="approve",
            binding=binding,
            approval_binding_hash=binding.canonical_hash,
        )
        repository_stub = types.ModuleType("runtime.model_boss.repository")
        repository_stub.project_task_patch = lambda snapshot, delta: projected
        repository_stub.capture_destination = lambda repo, allowed_paths: EMPTY_SNAPSHOT

        with _repository_functions(repository_stub):
            result = integrate_reviewed_delta(
                Path("/repository-must-not-be-read"),
                EMPTY_SNAPSHOT,
                EMPTY_DELTA,
                approval,
            )

        self.assertNotEqual(
            _sha256(encode_canonical_patch(projected)),
            binding.projected_task_patch_hash,
        )
        self.assertEqual(result.status, "approval_stale")
        self.assertFalse(result.applied)

    def test_wrong_binding_hash_is_rejected_before_repository_access(self) -> None:
        approval = Approval(
            version=1,
            decision="approve",
            binding=PLACEHOLDER_BINDING,
            approval_binding_hash="0" * 64,
        )

        result = integrate_reviewed_delta(
            Path("/repository-must-not-be-read"),
            EMPTY_SNAPSHOT,
            EMPTY_DELTA,
            approval,
        )

        self.assertEqual(result.version, 1)
        self.assertEqual(result.status, "approval_stale")
        self.assertFalse(result.applied)


if __name__ == "__main__":
    unittest.main()
