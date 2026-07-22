from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import runtime.token_saver.bundle as bundle_module
from runtime.token_saver.bundle import (
    BundleAlreadySealedError,
    BundleError,
    BundleTooLargeError,
    BundleUnsupportedPlatformError,
    SealedGateEvidence,
    build_final_review_packet,
    probe_bundle_capability,
    read_final_review_receipt,
    read_sealed_delta_bundle,
    seal_delta_bundle,
    seal_final_review_receipt,
)
from runtime.token_saver.evidence import (
    ApprovalBinding,
    EvidenceRecord,
    MODE_ABSENT,
    MODE_EXECUTABLE,
    MODE_REGULAR,
    MODE_SYMLINK,
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
from runtime.token_saver.repository import project_task_patch
from runtime.token_saver.repository import (
    capture_source_snapshot,
    capture_worker_delta,
    create_worktree,
    materialize_snapshot,
)
from runtime.token_saver.resources import create_invocation_resources


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _text(
    path: bytes,
    *,
    diff: bytes = b"diff --git a/file b/file\n",
    status: RecordStatus = RecordStatus.MODIFIED,
    old_mode: int = MODE_REGULAR,
    new_mode: int = MODE_REGULAR,
) -> EvidenceRecord:
    return EvidenceRecord(
        tag=RecordTag.TEXT_DIFF,
        path=path,
        status=status,
        old_mode=old_mode,
        new_mode=new_mode,
        canonical_diff=diff,
    )


def _untracked(
    path: bytes,
    content: bytes,
    *,
    mode: int = MODE_REGULAR,
) -> EvidenceRecord:
    return EvidenceRecord(
        tag=RecordTag.UNTRACKED,
        path=path,
        status=RecordStatus.UNTRACKED,
        old_mode=MODE_ABSENT,
        new_mode=mode,
        content=content,
    )


def _symlink(path: bytes, target: bytes) -> EvidenceRecord:
    return EvidenceRecord(
        tag=RecordTag.SYMLINK,
        path=path,
        status=RecordStatus.UNTRACKED,
        old_mode=MODE_ABSENT,
        new_mode=MODE_SYMLINK,
        content=target,
    )


def _git(repository: Path, *arguments: bytes) -> None:
    subprocess_arguments = [b"git", b"-C", os.fsencode(repository), *arguments]
    subprocess.run(
        subprocess_arguments,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", ""),
        },
    )


def _write_raw(repository: Path, path: bytes, content: bytes) -> None:
    destination = os.path.join(os.fsencode(repository), *path.split(b"/"))
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "wb") as stream:
        stream.write(content)


def _fixture(
    repository: Path,
    resources,
) -> tuple[SourceSnapshot, WorkerDelta]:
    staged_path = b"src/staged-\xe2\x98\x83.txt"
    binary_path = b"src/binary.dat"
    untracked_path = b"new\nname.bin"
    symlink_path = b"links/current"
    worker_path = b"worker/tool.sh"
    allowed = (
        staged_path,
        binary_path,
        untracked_path,
        symlink_path,
        worker_path,
    )
    _write_raw(repository, staged_path, b"baseline text\n")
    _write_raw(repository, binary_path, b"\x00baseline\xff")
    _git(repository, b"add", b"--", staged_path, binary_path)
    _git(repository, b"commit", b"-q", b"-m", b"baseline")
    _write_raw(repository, staged_path, b"staged source\n")
    _git(repository, b"add", b"--", staged_path)
    _write_raw(repository, binary_path, b"\x00source\xff\x10")
    _write_raw(repository, untracked_path, b"source\x00\xff")
    link_destination = os.path.join(
        os.fsencode(repository), *symlink_path.split(b"/")
    )
    os.makedirs(os.path.dirname(link_destination), exist_ok=True)
    os.symlink(b"../src/staged-\xe2\x98\x83.txt", link_destination)
    _write_raw(repository, b"private/cache-\xe2\x98\x82.bin", b"private")

    snapshot = capture_source_snapshot(repository, allowed)
    handle = create_worktree(repository, snapshot, resources.worktree_path)
    materialize_snapshot(handle, snapshot)
    _write_raw(
        resources.worktree_path,
        worker_path,
        b"#!/bin/sh\nprintf 'ok\\n'\n",
    )
    os.chmod(
        os.path.join(os.fsencode(resources.worktree_path), *worker_path.split(b"/")),
        0o755,
    )
    delta = capture_worker_delta(handle, snapshot, allowed)
    return snapshot, delta


class BundleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        repository = base / "repository"
        repository.mkdir()
        _git(repository, b"init", b"-q")
        _git(repository, b"config", b"user.name", b"Token Saver Tests")
        _git(
            repository,
            b"config",
            b"user.email",
            b"token-saver@example.invalid",
        )
        _git(repository, b"config", b"core.autocrlf", b"false")
        temporary_parent = base / "invocations"
        temporary_parent.mkdir()
        self.resources = create_invocation_resources(repository, temporary_parent)
        self.snapshot, self.delta = _fixture(repository, self.resources)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _rewrite(self, raw: bytes, *, mode: int = 0o400) -> None:
        path = self.resources.delta_bundle_path
        if path.is_symlink() or path.exists():
            path.unlink()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, raw)
            os.fsync(descriptor)
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)


class BundleRoundTripTests(BundleTestCase):
    def test_round_trip_preserves_every_raw_field_and_binds_three_hashes(self) -> None:
        metadata = seal_delta_bundle(self.resources, self.snapshot, self.delta)
        sealed = read_sealed_delta_bundle(self.resources)

        self.assertEqual(sealed.metadata, metadata)
        self.assertEqual(
            encode_source_snapshot(sealed.snapshot),
            encode_source_snapshot(self.snapshot),
        )
        self.assertEqual(
            encode_worker_delta(sealed.delta),
            encode_worker_delta(self.delta),
        )
        self.assertIsNotNone(sealed.delta.projected_snapshot)
        assert sealed.delta.projected_snapshot is not None
        assert self.delta.projected_snapshot is not None
        self.assertEqual(
            encode_source_snapshot(sealed.delta.projected_snapshot),
            encode_source_snapshot(self.delta.projected_snapshot),
        )
        self.assertEqual(
            metadata.source_snapshot_hash,
            _digest(encode_source_snapshot(self.snapshot)),
        )
        self.assertEqual(
            metadata.worker_delta_hash,
            _digest(encode_worker_delta(self.delta)),
        )
        self.assertEqual(
            metadata.projected_task_patch_hash,
            _digest(encode_canonical_patch(project_task_patch(self.snapshot, self.delta))),
        )
        self.assertEqual(
            metadata.bundle_sha256,
            _digest(self.resources.delta_bundle_path.read_bytes()),
        )

        file_metadata = os.lstat(self.resources.delta_bundle_path)
        self.assertTrue(stat.S_ISREG(file_metadata.st_mode))
        self.assertEqual(stat.S_IMODE(file_metadata.st_mode), 0o400)
        self.assertEqual(file_metadata.st_nlink, 1)
        self.assertEqual(metadata.bundle_device, file_metadata.st_dev)
        self.assertEqual(metadata.bundle_inode, file_metadata.st_ino)
        self.assertEqual(metadata.bundle_size, file_metadata.st_size)

    def test_envelope_is_canonical_ascii_json_with_canonical_base64(self) -> None:
        seal_delta_bundle(self.resources, self.snapshot, self.delta)
        raw = self.resources.delta_bundle_path.read_bytes()
        decoded = json.loads(raw)

        self.assertEqual(
            raw,
            json.dumps(
                decoded,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii"),
        )
        encoded_baseline = decoded["source_snapshot"]["baseline_oid_b64"]
        self.assertEqual(
            base64.b64encode(base64.b64decode(encoded_baseline)).decode("ascii"),
            encoded_baseline,
        )
        self.assertNotIn(b"private/cache-", raw)
        self.assertNotIn(b"staged-\xe2\x98\x83", raw)

    def test_private_records_are_internal_only_and_never_enter_review_evidence(
        self,
    ) -> None:
        seal_delta_bundle(self.resources, self.snapshot, self.delta)
        envelope = json.loads(self.resources.delta_bundle_path.read_bytes())
        internal_path = base64.b64decode(
            envelope["source_snapshot"]["private"][0]["path_b64"],
            validate=True,
        )
        review_packet = encode_canonical_patch(
            project_task_patch(self.snapshot, self.delta)
        )

        self.assertEqual(internal_path, self.snapshot.private[0].path)
        self.assertNotIn(internal_path, review_packet)
        self.assertNotIn(
            base64.b64encode(internal_path),
            review_packet,
        )

    def test_projected_snapshot_may_be_absent_but_hash_is_still_carried(self) -> None:
        delta = WorkerDelta(records=())
        metadata = seal_delta_bundle(self.resources, self.snapshot, delta)
        sealed = read_sealed_delta_bundle(self.resources)

        self.assertIsNone(sealed.delta.projected_snapshot)
        self.assertEqual(
            metadata.projected_task_patch_hash,
            _digest(encode_canonical_patch(project_task_patch(self.snapshot, delta))),
        )

    def test_reading_never_executes_git_or_other_repository_commands(self) -> None:
        seal_delta_bundle(self.resources, self.snapshot, self.delta)
        with mock.patch("subprocess.run", side_effect=AssertionError("must not execute")):
            sealed = read_sealed_delta_bundle(self.resources)
        self.assertEqual(sealed.metadata.invocation_id, self.resources.invocation_id)

    def test_green_gate_evidence_round_trips_inside_the_sealed_bundle(self) -> None:
        gate = SealedGateEvidence(
            argv=("python3", "-m", "unittest"),
            cwd=".",
            status="ok",
            exit_code=0,
            stdout_hash="1" * 64,
            stderr_hash="2" * 64,
            duration_milliseconds=17,
        )

        seal_delta_bundle(
            self.resources,
            self.snapshot,
            self.delta,
            gates=(gate,),
            authority_mode="max",
        )

        sealed = read_sealed_delta_bundle(self.resources)
        self.assertEqual(sealed.gates, (gate,))
        self.assertEqual(sealed.authority_mode, "max")

    def test_final_review_receipt_is_bound_to_packet_bundle_and_reviewer(self) -> None:
        gate = SealedGateEvidence(
            argv=("true",),
            cwd=".",
            status="ok",
            exit_code=0,
            stdout_hash="1" * 64,
            stderr_hash="2" * 64,
            duration_milliseconds=1,
        )
        seal_delta_bundle(
            self.resources,
            self.snapshot,
            self.delta,
            gates=(gate,),
            authority_mode="max",
        )
        sealed = read_sealed_delta_bundle(self.resources)
        packet = build_final_review_packet(
            sealed,
            {
                "version": 1,
                "goal": "implement the bounded change",
                "approved_plan": "change only the allowed files",
                "acceptance_criteria": ["the exact gate is green"],
                "main_loop_verdict": "approve",
            },
        )

        created = seal_final_review_receipt(
            self.resources,
            packet=packet,
            decision="approve",
            approval_binding_hash=(
                ApprovalBinding(
                    source_snapshot_hash=sealed.metadata.source_snapshot_hash,
                    worker_delta_hash=sealed.metadata.worker_delta_hash,
                    projected_task_patch_hash=(
                        sealed.metadata.projected_task_patch_hash
                    ),
                ).canonical_hash
            ),
            reviewer_route_id="authority-reviewer",
            reviewer_fingerprint="example:authority-v1:default",
            fingerprint_evidence_source="identity-handshake",
            reviewer_read_only_enforced=True,
            main_fingerprint="example:balanced-v1:default",
            message="approved exact packet",
            requested_changes=(),
        )

        self.assertEqual(read_final_review_receipt(self.resources), created)
        self.assertEqual(created.bundle_sha256, sealed.metadata.bundle_sha256)
        self.assertEqual(created.authority_mode, "max")
        self.assertEqual(created.review_packet_sha256, _digest(packet))
        self.assertEqual(
            stat.S_IMODE(os.lstat(self.resources.final_evidence_path).st_mode),
            0o400,
        )

        self.resources.final_evidence_path.chmod(0o600)
        with self.assertRaisesRegex(BundleError, "mode"):
            read_final_review_receipt(self.resources)


class BundleSealSafetyTests(BundleTestCase):
    def test_post_write_resource_validation_failure_rolls_back_for_safe_retry(
        self,
    ) -> None:
        original_validation = bundle_module._validate_exact_layout
        calls = 0

        def fail_post_write(resources, anchor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise BundleError("injected post-write validation failure")
            return original_validation(resources, anchor)

        with mock.patch.object(
            bundle_module,
            "_validate_exact_layout",
            side_effect=fail_post_write,
        ):
            with self.assertRaisesRegex(BundleError, "post-write"):
                seal_delta_bundle(self.resources, self.snapshot, self.delta)

        self.assertFalse(os.path.lexists(self.resources.delta_bundle_path))
        receipt = bundle_module._seal_receipt_path(self.resources)
        self.assertFalse(os.path.lexists(receipt))
        metadata = seal_delta_bundle(self.resources, self.snapshot, self.delta)
        self.assertEqual(
            read_sealed_delta_bundle(self.resources).metadata,
            metadata,
        )

    def test_deleting_bundle_does_not_reset_the_one_shot_seal(self) -> None:
        seal_delta_bundle(self.resources, self.snapshot, self.delta)
        receipt = bundle_module._seal_receipt_path(self.resources)
        self.assertTrue(receipt.is_file())
        self.resources.delta_bundle_path.unlink()

        with self.assertRaises(BundleAlreadySealedError):
            seal_delta_bundle(self.resources, self.snapshot, self.delta)

        self.assertFalse(self.resources.delta_bundle_path.exists())
        self.assertTrue(receipt.is_file())

    def test_external_receipt_is_outside_worker_writable_roots(self) -> None:
        seal_delta_bundle(self.resources, self.snapshot, self.delta)
        receipt = bundle_module._seal_receipt_path(self.resources)
        receipt_metadata = os.lstat(receipt)

        self.assertEqual(receipt.parent, self.resources.temp_parent)
        self.assertNotEqual(receipt.parent, self.resources.invocation_root)
        self.assertFalse(receipt.is_relative_to(self.resources.route_state_path))
        self.assertFalse(receipt.is_relative_to(self.resources.worktree_path))
        self.assertEqual(stat.S_IMODE(receipt_metadata.st_mode), 0o400)
        self.assertEqual(receipt_metadata.st_nlink, 1)
        self.assertNotIn(b"key", receipt.read_bytes().lower())

    def test_abandoned_sealing_claim_and_partial_bundle_are_recoverable(self) -> None:
        envelope, _ = bundle_module._build_envelope(
            self.resources,
            self.snapshot,
            self.delta,
        )
        raw = bundle_module._canonical_json(envelope)
        receipt = bundle_module._seal_receipt_path(self.resources)
        receipt.write_bytes(
            bundle_module._canonical_json(
                bundle_module._receipt_payload(
                    self.resources,
                    raw,
                    state="sealing",
                )
            )
        )
        receipt.chmod(0o600)
        self.resources.delta_bundle_path.write_bytes(raw[: len(raw) // 2])
        self.resources.delta_bundle_path.chmod(0o600)

        metadata = seal_delta_bundle(self.resources, self.snapshot, self.delta)

        self.assertEqual(
            read_sealed_delta_bundle(self.resources).metadata,
            metadata,
        )
        self.assertEqual(stat.S_IMODE(os.lstat(receipt).st_mode), 0o400)

    def test_directory_fsync_failure_after_atomic_receipt_replace_stays_sealed(
        self,
    ) -> None:
        real_fsync = os.fsync
        parent_identity = (
            self.resources.temp_parent_device,
            self.resources.temp_parent_inode,
        )
        parent_syncs = 0

        def fail_after_replace(descriptor: int) -> None:
            nonlocal parent_syncs
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) == parent_identity:
                parent_syncs += 1
                if parent_syncs == 3:
                    raise OSError("injected final directory fsync failure")
            real_fsync(descriptor)

        with mock.patch.object(bundle_module.os, "fsync", side_effect=fail_after_replace):
            metadata = seal_delta_bundle(
                self.resources,
                self.snapshot,
                self.delta,
            )

        self.assertEqual(
            read_sealed_delta_bundle(self.resources).metadata,
            metadata,
        )
        with self.assertRaises(BundleAlreadySealedError):
            seal_delta_bundle(self.resources, self.snapshot, self.delta)

    def test_second_seal_never_overwrites_the_first_bundle(self) -> None:
        first = seal_delta_bundle(self.resources, self.snapshot, self.delta)
        original = self.resources.delta_bundle_path.read_bytes()

        with self.assertRaises(BundleAlreadySealedError):
            seal_delta_bundle(
                self.resources,
                self.snapshot,
                WorkerDelta(records=()),
            )

        self.assertEqual(self.resources.delta_bundle_path.read_bytes(), original)
        self.assertEqual(read_sealed_delta_bundle(self.resources).metadata, first)

    def test_existing_symlink_is_not_followed_or_overwritten(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"sentinel")
        self.resources.delta_bundle_path.symlink_to(outside)

        with self.assertRaises(BundleAlreadySealedError):
            seal_delta_bundle(self.resources, self.snapshot, self.delta)

        self.assertEqual(outside.read_bytes(), b"sentinel")
        self.assertTrue(self.resources.delta_bundle_path.is_symlink())

    def test_oversized_envelope_is_rejected_without_leaving_a_partial_file(self) -> None:
        with mock.patch.object(bundle_module, "MAX_BUNDLE_BYTES", 128):
            with self.assertRaises(BundleTooLargeError):
                seal_delta_bundle(self.resources, self.snapshot, self.delta)
        self.assertFalse(os.path.lexists(self.resources.delta_bundle_path))

    def test_resources_are_validated_before_creation(self) -> None:
        forged = replace(
            self.resources,
            repository_inode=self.resources.repository_inode + 1,
        )
        with self.assertRaisesRegex(BundleError, "repository_path"):
            seal_delta_bundle(forged, self.snapshot, self.delta)
        self.assertFalse(self.resources.delta_bundle_path.exists())

    def test_rejects_structurally_untrusted_delta_projection_contracts(self) -> None:
        source = self.snapshot
        assert self.delta.projected_snapshot is not None
        projected = self.delta.projected_snapshot
        outside = _untracked(b"outside-scope.txt", b"outside")
        private_projection = SourceSnapshot(
            baseline_oid=source.baseline_oid,
            allowed_paths=source.allowed_paths,
            staged=source.staged,
            unstaged=projected.unstaged,
            untracked=projected.untracked,
            private=source.private,
        )
        changed_index_projection = SourceSnapshot(
            baseline_oid=source.baseline_oid,
            allowed_paths=source.allowed_paths,
            staged=(),
            unstaged=projected.unstaged,
            untracked=projected.untracked,
        )
        changed_baseline_projection = SourceSnapshot(
            baseline_oid=b"b" * 40,
            allowed_paths=source.allowed_paths,
            staged=source.staged,
            unstaged=projected.unstaged,
            untracked=projected.untracked,
        )
        changed_allowlist_projection = SourceSnapshot(
            baseline_oid=source.baseline_oid,
            allowed_paths=(*source.allowed_paths, b"extra-clean.txt"),
            staged=source.staged,
            unstaged=projected.unstaged,
            untracked=projected.untracked,
        )
        forged_replay_projection = SourceSnapshot(
            baseline_oid=source.baseline_oid,
            allowed_paths=source.allowed_paths,
            staged=source.staged,
            unstaged=source.unstaged,
            untracked=source.untracked,
        )
        cases = (
            (
                "allowlist",
                WorkerDelta(
                    records=(outside,),
                    projected_snapshot=projected,
                ),
            ),
            (
                "private",
                WorkerDelta(
                    records=self.delta.records,
                    projected_snapshot=private_projection,
                ),
            ),
            (
                "staged",
                WorkerDelta(
                    records=self.delta.records,
                    projected_snapshot=changed_index_projection,
                ),
            ),
            (
                "baseline",
                WorkerDelta(
                    records=self.delta.records,
                    projected_snapshot=changed_baseline_projection,
                ),
            ),
            (
                "allowlist",
                WorkerDelta(
                    records=self.delta.records,
                    projected_snapshot=changed_allowlist_projection,
                ),
            ),
            (
                "replay",
                WorkerDelta(
                    records=self.delta.records,
                    projected_snapshot=forged_replay_projection,
                ),
            ),
        )
        for message, delta in cases:
            with self.subTest(message):
                with self.assertRaisesRegex(BundleError, message):
                    seal_delta_bundle(self.resources, source, delta)
                self.assertFalse(self.resources.delta_bundle_path.exists())

    def test_cross_invocation_rebinding_requires_external_trust_anchor(self) -> None:
        seal_delta_bundle(self.resources, self.snapshot, self.delta)
        envelope = json.loads(self.resources.delta_bundle_path.read_bytes())
        second = create_invocation_resources(
            self.resources.repository_path,
            self.resources.temp_parent,
        )
        binding = envelope["binding"]
        binding.update(
            {
                "delta_bundle_path_b64": base64.b64encode(
                    os.fsencode(second.delta_bundle_path)
                ).decode("ascii"),
                "invocation_id": second.invocation_id,
                "invocation_root_device": second.invocation_root_device,
                "invocation_root_inode": second.invocation_root_inode,
                "invocation_root_path_b64": base64.b64encode(
                    os.fsencode(second.invocation_root)
                ).decode("ascii"),
            }
        )
        rebound = json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        second.delta_bundle_path.write_bytes(rebound)
        second.delta_bundle_path.chmod(0o400)

        with self.assertRaisesRegex(BundleError, "receipt|trust"):
            read_sealed_delta_bundle(second)


class BundlePlatformTests(BundleTestCase):
    def test_platform_probe_explicitly_limits_native_support(self) -> None:
        native = probe_bundle_capability()
        windows = probe_bundle_capability(system="Windows", os_name="nt")

        self.assertTrue(native.supported)
        self.assertIn(native.system, {"Darwin", "Linux"})
        self.assertFalse(windows.supported)
        self.assertIn("WSL", windows.reason)

    def test_public_entries_raise_explicit_error_on_native_windows(self) -> None:
        with mock.patch.object(bundle_module.platform, "system", return_value="Windows"):
            with self.assertRaises(BundleUnsupportedPlatformError):
                seal_delta_bundle(self.resources, self.snapshot, self.delta)
            with self.assertRaises(BundleUnsupportedPlatformError):
                read_sealed_delta_bundle(self.resources)
        self.assertFalse(self.resources.delta_bundle_path.exists())


class BundleReadSafetyTests(BundleTestCase):
    def setUp(self) -> None:
        super().setUp()
        seal_delta_bundle(self.resources, self.snapshot, self.delta)

    def test_rejects_unknown_and_duplicate_json_keys(self) -> None:
        raw = self.resources.delta_bundle_path.read_bytes()
        original = json.loads(raw)

        with self.subTest("unknown"):
            original["unexpected"] = True
            self._rewrite(
                json.dumps(original, sort_keys=True, separators=(",", ":")).encode(
                    "ascii"
                )
            )
            with self.assertRaisesRegex(BundleError, "unknown|keys"):
                read_sealed_delta_bundle(self.resources)

        with self.subTest("nested unknown"):
            nested = json.loads(raw)
            nested["worker_delta"]["unexpected"] = None
            self._rewrite(
                json.dumps(nested, sort_keys=True, separators=(",", ":")).encode(
                    "ascii"
                )
            )
            with self.assertRaisesRegex(BundleError, "unknown|keys"):
                read_sealed_delta_bundle(self.resources)

        with self.subTest("duplicate"):
            self._rewrite(b'{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(BundleError, "duplicate"):
                read_sealed_delta_bundle(self.resources)

    def test_rejects_noncanonical_json_and_noncanonical_base64(self) -> None:
        raw = self.resources.delta_bundle_path.read_bytes()
        with self.subTest("json whitespace"):
            self._rewrite(raw + b"\n")
            with self.assertRaisesRegex(BundleError, "canonical"):
                read_sealed_delta_bundle(self.resources)

        with self.subTest("base64"):
            value = json.loads(raw)
            value["source_snapshot"]["baseline_oid_b64"] += "="
            self._rewrite(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
                    "ascii"
                )
            )
            with self.assertRaisesRegex(BundleError, "base64"):
                read_sealed_delta_bundle(self.resources)

    def test_rejects_a_changed_hash_even_in_canonical_json(self) -> None:
        raw = self.resources.delta_bundle_path.read_bytes()
        for field, message in (
            ("source_snapshot_sha256", "source snapshot hash"),
            ("worker_delta_sha256", "worker delta hash"),
            ("projected_task_patch_sha256", "projected task patch hash"),
        ):
            with self.subTest(field):
                value = json.loads(raw)
                value["hashes"][field] = "0" * 64
                self._rewrite(
                    json.dumps(
                        value, sort_keys=True, separators=(",", ":")
                    ).encode("ascii")
                )
                with self.assertRaisesRegex(BundleError, message):
                    read_sealed_delta_bundle(self.resources)

    def test_read_rejects_structurally_forged_delta_and_projection(self) -> None:
        raw = self.resources.delta_bundle_path.read_bytes()
        mutations = {
            "allowlist": lambda value: value["worker_delta"]["records"][0].__setitem__(
                "path_b64", base64.b64encode(b"outside.txt").decode("ascii")
            ),
            "private": lambda value: value["worker_delta"][
                "projected_snapshot"
            ].__setitem__("private", value["source_snapshot"]["private"]),
            "staged": lambda value: value["worker_delta"][
                "projected_snapshot"
            ].__setitem__("staged", []),
            "baseline": lambda value: value["worker_delta"][
                "projected_snapshot"
            ].__setitem__(
                "baseline_oid_b64", base64.b64encode(b"b" * 40).decode("ascii")
            ),
        }
        for message, mutate in mutations.items():
            with self.subTest(message):
                value = json.loads(raw)
                mutate(value)
                self._rewrite(
                    json.dumps(
                        value, sort_keys=True, separators=(",", ":")
                    ).encode("ascii")
                )
                with self.assertRaisesRegex(BundleError, message):
                    read_sealed_delta_bundle(self.resources)

    def test_external_receipt_rejects_a_self_consistent_rehashed_envelope(self) -> None:
        value = json.loads(self.resources.delta_bundle_path.read_bytes())
        changed_baseline = base64.b64encode(b"b" * 40).decode("ascii")
        value["source_snapshot"]["baseline_oid_b64"] = changed_baseline
        value["worker_delta"]["projected_snapshot"][
            "baseline_oid_b64"
        ] = changed_baseline
        rebuilt_source = bundle_module._decode_snapshot(
            value["source_snapshot"],
            "source_snapshot",
        )
        value["hashes"]["source_snapshot_sha256"] = _digest(
            encode_source_snapshot(rebuilt_source)
        )
        forged = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        self._rewrite(forged)

        with self.assertRaisesRegex(BundleError, "receipt.*bind"):
            read_sealed_delta_bundle(self.resources)

    def test_rejects_wrong_invocation_repository_and_root_bindings(self) -> None:
        raw = self.resources.delta_bundle_path.read_bytes()
        mutations = {
            "invocation": lambda value: value["binding"].__setitem__(
                "invocation_id", "00000000-0000-4000-8000-000000000000"
            ),
            "repository path": lambda value: value["binding"].__setitem__(
                "repository_path_b64", base64.b64encode(b"/elsewhere").decode("ascii")
            ),
            "repository inode": lambda value: value["binding"].__setitem__(
                "repository_inode", self.resources.repository_inode + 1
            ),
            "invocation root": lambda value: value["binding"].__setitem__(
                "invocation_root_inode", self.resources.invocation_root_inode + 1
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                value = json.loads(raw)
                mutate(value)
                self._rewrite(
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
                        "ascii"
                    )
                )
                with self.assertRaisesRegex(BundleError, "binding"):
                    read_sealed_delta_bundle(self.resources)

    def test_rejects_wrong_mode_symlinks_hardlinks_and_oversized_files(self) -> None:
        path = self.resources.delta_bundle_path
        raw = path.read_bytes()

        with self.subTest("mode"):
            path.chmod(0o600)
            with self.assertRaisesRegex(BundleError, "mode"):
                read_sealed_delta_bundle(self.resources)

        with self.subTest("symlink"):
            path.unlink()
            outside = Path(self.temporary.name) / "outside.bundle"
            outside.write_bytes(raw)
            outside.chmod(0o400)
            path.symlink_to(outside)
            with self.assertRaisesRegex(BundleError, "symlink|regular"):
                read_sealed_delta_bundle(self.resources)

        with self.subTest("hard link"):
            path.unlink()
            self._rewrite(raw)
            hardlink = Path(self.temporary.name) / "bundle-hardlink"
            os.link(path, hardlink)
            with self.assertRaisesRegex(BundleError, "link"):
                read_sealed_delta_bundle(self.resources)
            hardlink.unlink()

        with self.subTest("size"):
            self._rewrite(b"x" * 256)
            with mock.patch.object(bundle_module, "MAX_BUNDLE_BYTES", 128):
                with self.assertRaises(BundleTooLargeError):
                    read_sealed_delta_bundle(self.resources)

    def test_rejects_forged_resource_layout_and_replaced_invocation_root(self) -> None:
        forged = replace(
            self.resources,
            delta_bundle_path=self.resources.invocation_root / "nested" / "worker.delta",
        )
        with self.assertRaisesRegex(BundleError, "layout|delta_bundle_path"):
            read_sealed_delta_bundle(forged)

        old_root = self.resources.invocation_root
        replacement = old_root.with_name(old_root.name + "-old")
        old_root.rename(replacement)
        old_root.mkdir(mode=0o700)
        with self.assertRaisesRegex(BundleError, "identity"):
            read_sealed_delta_bundle(self.resources)

    def test_rejects_owner_mismatch_and_an_inode_swap_between_lstat_and_open(
        self,
    ) -> None:
        with mock.patch.object(
            bundle_module,
            "_current_uid",
            return_value=os.geteuid() + 1,
        ):
            with self.assertRaisesRegex(BundleError, "owner"):
                read_sealed_delta_bundle(self.resources)

        raw = self.resources.delta_bundle_path.read_bytes()
        real_open = os.open
        swapped = False

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if (
                path == self.resources.delta_bundle_path.name
                and flags & os.O_ACCMODE == os.O_RDONLY
            ):
                if not swapped:
                    swapped = True
                    os.unlink(path, dir_fd=dir_fd)
                    replacement = real_open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=dir_fd,
                    )
                    try:
                        os.write(replacement, raw)
                        os.fchmod(replacement, 0o400)
                    finally:
                        os.close(replacement)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch.object(bundle_module.os, "open", side_effect=racing_open):
            with self.assertRaisesRegex(BundleError, "inode changed"):
                read_sealed_delta_bundle(self.resources)


if __name__ == "__main__":
    unittest.main()
