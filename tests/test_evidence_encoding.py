from __future__ import annotations

import hashlib
import struct
import unittest
from dataclasses import replace
from pathlib import Path

from runtime.token_saver.evidence import (
    ApprovalBinding,
    CanonicalPatch,
    EvidenceRecord,
    PrivateDigestKind,
    PrivateRecord,
    RecordStatus,
    RecordTag,
    SourceSnapshot,
    WorkerDelta,
    display_git_path,
    encode_approval_binding,
    encode_canonical_patch,
    encode_source_snapshot,
    encode_worker_delta,
    summarize_private_records,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "evidence"
BASELINE = b"0" * 40
REGULAR = 0o100644
EXECUTABLE = 0o100755
SYMLINK = 0o120000

EXPECTED_SOURCE_HEX = (
    "544f4b454e2d53415645522d45564944454e4345000000000000000001000000"
    "0000000001000000000000002830303030303030303030303030303030303030"
    "3030303030303030303030303030303030303030300000000000000009000000"
    "00000000010000000000000005612e7478740000000000000001000000000000"
    "000100000000000000010000000000000005612e747874000000000000000200"
    "000000000081a400000000000081a40000000000000005646966660a00000000"
    "0000000000000000000000020000000000000000000000000000000300000000"
    "0000000000000000000000040000000000000000"
)
EXPECTED_SOURCE_SHA256 = "1b17ae0e7116cca1396e49308023082e4772ee2675f89fc0ca09311bc65795f0"

EXPECTED_APPROVAL_HASHES = (
    "519b622d6d57a6fbb3ab135a35872816405680c0f5475ccfd04aebe11e21ef91",
    "f1dfe722d615d51e641c349ab0742d366d8a2287ad5bf013379f973cada3e911",
    "2dc77b62ce9fd442aca4477aac249a69c78b7949ab151a9640fafef1c2190578",
    "06299d95b3e16b343b77d2a2dd1c2498ccb6b85e37cc70bf4027586ffbbb0015",
)
EXPECTED_RECORD_HASHES = (
    "390d15a725ab0c146f19d1cdd9c2b771c615387864828ef0f15e423840405dc2",
    "edc1312f30a4b5a49a0d559a6f4418d69df23a0970553e2d4a0f40839ac3bc25",
    "817b59dd88ddb4749fde032002073c3686d3730b5036d3c472406be5befe49ff",
    "f7759baf6ac0acfdd52909f88c4ece1e7fce70fa5adb98322bcea7ce8e6842b2",
)


def _text(path: bytes = b"a.txt", diff: bytes = b"diff\n") -> EvidenceRecord:
    return EvidenceRecord(
        tag=RecordTag.TEXT_DIFF,
        path=path,
        status=RecordStatus.MODIFIED,
        old_mode=REGULAR,
        new_mode=REGULAR,
        canonical_diff=diff,
    )


def _untracked(
    path: bytes = b"new.txt",
    content: bytes = b"new contents\n",
) -> EvidenceRecord:
    return EvidenceRecord(
        tag=RecordTag.UNTRACKED,
        path=path,
        status=RecordStatus.UNTRACKED,
        old_mode=0,
        new_mode=REGULAR,
        content=content,
    )


def _private(
    *,
    path: bytes = b"private/cache.bin",
    status: RecordStatus = RecordStatus.MODIFIED,
    mode: int = REGULAR,
    size: int = 0x0102030405060708,
    digest: str = "1" * 64,
    digest_kind: PrivateDigestKind = PrivateDigestKind.CONTENT,
) -> PrivateRecord:
    return PrivateRecord(
        digest_kind=digest_kind,
        path=path,
        status=status,
        mode=mode,
        size=size,
        digest=digest,
    )


def _snapshot(
    *,
    staged: tuple[EvidenceRecord, ...] = (),
    unstaged: tuple[EvidenceRecord, ...] = (),
    untracked: tuple[EvidenceRecord, ...] = (),
    private: tuple[PrivateRecord, ...] = (),
    baseline: bytes = BASELINE,
    allowed_paths: tuple[bytes, ...] | None = None,
) -> SourceSnapshot:
    if allowed_paths is None:
        allowed_paths = tuple(
            sorted(
                {
                    record.path
                    for record in staged + unstaged + untracked
                }
            )
        )
    return SourceSnapshot(
        baseline_oid=baseline,
        allowed_paths=allowed_paths,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        private=private,
    )


class GoldenEncodingTests(unittest.TestCase):
    def test_source_golden_bytes_and_hard_coded_sha256(self) -> None:
        snapshot = _snapshot(staged=(_text(),))
        encoded = encode_source_snapshot(snapshot)
        fixture = (FIXTURE_DIR / "source-v1.bin").read_bytes()
        fixture_hash = (FIXTURE_DIR / "source-v1.sha256").read_text(
            encoding="ascii"
        ).strip()

        self.assertEqual(encoded, bytes.fromhex(EXPECTED_SOURCE_HEX))
        self.assertEqual(fixture, bytes.fromhex(EXPECTED_SOURCE_HEX))
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), EXPECTED_SOURCE_SHA256)
        self.assertEqual(fixture_hash, EXPECTED_SOURCE_SHA256)

    def test_raw_git_paths_sort_as_bytes_and_display_is_hash_neutral(self) -> None:
        hostile = b"dir/space \tline\nslash\\non-utf8-\xff.txt"
        paths = (b"raw/\xff", hostile, b"raw/\x80", b"raw/\x7f")
        records = tuple(_untracked(path, path + b"-content") for path in paths)
        forward = encode_worker_delta(WorkerDelta(records=records))
        reverse = encode_worker_delta(WorkerDelta(records=tuple(reversed(records))))

        self.assertEqual(forward, reverse)
        ordered_paths = sorted(paths)
        offsets = [
            forward.index(struct.pack(">Q", len(path)) + path) for path in ordered_paths
        ]
        self.assertEqual(offsets, sorted(offsets))

        snapshot = _snapshot(untracked=(_untracked(hostile),))
        before = encode_source_snapshot(snapshot)
        rendered = display_git_path(hostile)
        after = encode_source_snapshot(snapshot)
        self.assertEqual(before, after)
        self.assertIn(r"\xff", rendered)
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\t", rendered)

    def test_document_domains_keep_empty_documents_distinct(self) -> None:
        empty_summary = summarize_private_records(())
        documents = (
            encode_source_snapshot(_snapshot()),
            encode_worker_delta(WorkerDelta(records=())),
            encode_canonical_patch(
                CanonicalPatch(records=(), private_summary=empty_summary)
            ),
            encode_approval_binding(
                ApprovalBinding(
                    source_snapshot_hash="a" * 64,
                    worker_delta_hash="b" * 64,
                    projected_task_patch_hash="c" * 64,
                )
            ),
        )
        self.assertEqual(len(set(documents)), len(documents))

    def test_clean_allowlist_paths_are_bound_into_the_source_hash(self) -> None:
        base = _snapshot(allowed_paths=(b"task.txt",))
        expanded = _snapshot(allowed_paths=(b"task.txt", b"clean-but-in-scope.txt"))
        self.assertNotEqual(
            hashlib.sha256(encode_source_snapshot(base)).digest(),
            hashlib.sha256(encode_source_snapshot(expanded)).digest(),
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            _snapshot(allowed_paths=(b"same.txt", b"same.txt"))
        with self.assertRaisesRegex(ValueError, "allowlist"):
            _snapshot(
                untracked=(_untracked(path=b"not-authorized.txt"),),
                allowed_paths=(b"different.txt",),
            )


class RecordCoverageTests(unittest.TestCase):
    def test_binary_untracked_symlink_and_mode_records_are_stable(self) -> None:
        records = (
            EvidenceRecord(
                tag=RecordTag.BINARY,
                path=b"asset.bin",
                status=RecordStatus.MODIFIED,
                old_mode=REGULAR,
                new_mode=REGULAR,
                canonical_diff=b"GIT binary patch\n",
                content=b"\x00\xffbinary",
            ),
            _untracked(),
            EvidenceRecord(
                tag=RecordTag.SYMLINK,
                path=b"link",
                status=RecordStatus.UNTRACKED,
                old_mode=0,
                new_mode=SYMLINK,
                content=b"target/path",
            ),
            EvidenceRecord(
                tag=RecordTag.MODE_ONLY,
                path=b"script.sh",
                status=RecordStatus.MODIFIED,
                old_mode=REGULAR,
                new_mode=EXECUTABLE,
            ),
        )
        encodings = tuple(
            encode_worker_delta(WorkerDelta(records=(record,))) for record in records
        )
        self.assertEqual(
            encodings,
            tuple(
                encode_worker_delta(WorkerDelta(records=(record,)))
                for record in records
            ),
        )
        self.assertEqual(
            tuple(hashlib.sha256(value).hexdigest() for value in encodings),
            EXPECTED_RECORD_HASHES,
        )

    def test_regular_symlink_type_changes_are_representable(self) -> None:
        records = (
            EvidenceRecord(
                tag=RecordTag.SYMLINK,
                path=b"regular-to-link",
                status=RecordStatus.TYPE_CHANGED,
                old_mode=REGULAR,
                new_mode=SYMLINK,
                canonical_diff=b"type-change patch\n",
                content=b"target",
            ),
            EvidenceRecord(
                tag=RecordTag.SYMLINK,
                path=b"link-to-regular",
                status=RecordStatus.TYPE_CHANGED,
                old_mode=SYMLINK,
                new_mode=REGULAR,
                canonical_diff=b"type-change patch\n",
                content=b"regular bytes",
            ),
        )
        encoded = encode_worker_delta(WorkerDelta(records=records))
        self.assertIn(b"regular-to-link", encoded)
        self.assertIn(b"link-to-regular", encoded)

    def test_private_manifest_every_field_changes_source_hash(self) -> None:
        base = _private()
        variants = (
            replace(base, digest_kind=PrivateDigestKind.CANONICAL_DIFF),
            replace(base, path=b"private/other.bin"),
            replace(base, status=RecordStatus.ADDED),
            replace(base, mode=EXECUTABLE),
            replace(base, size=base.size + 1),
            replace(base, digest="2" * 64),
        )
        base_hash = hashlib.sha256(
            encode_source_snapshot(_snapshot(private=(base,)))
        ).digest()
        variant_hashes = {
            hashlib.sha256(
                encode_source_snapshot(_snapshot(private=(variant,)))
            ).digest()
            for variant in variants
        }
        self.assertEqual(len(variant_hashes), len(variants))
        self.assertNotIn(base_hash, variant_hashes)

    def test_worker_delta_excludes_preexisting_source_bytes(self) -> None:
        source_sentinel = b"PREEXISTING-SOURCE-BYTES-MUST-NOT-ENTER-DELTA"
        first_source = _snapshot(staged=(_text(diff=source_sentinel),))
        second_source = _snapshot(unstaged=(_text(diff=b"different source"),))
        delta = WorkerDelta(
            records=(_untracked(content=b"worker-only"),),
            projected_snapshot=first_source,
        )

        first_encoding = encode_worker_delta(delta)
        self.assertNotIn(source_sentinel, first_encoding)
        self.assertNotIn(encode_source_snapshot(first_source), first_encoding)
        self.assertEqual(first_encoding, encode_worker_delta(delta))
        self.assertNotEqual(
            encode_source_snapshot(first_source),
            encode_source_snapshot(second_source),
        )

    def test_private_summary_is_the_only_private_reviewer_projection(self) -> None:
        private = _private()
        snapshot = _snapshot(private=(private,))
        summary = snapshot.private_summary
        patch = CanonicalPatch(records=(_untracked(),), private_summary=summary)
        encoded = encode_canonical_patch(patch)

        self.assertNotIn(private.path, encoded)
        self.assertNotIn(private.digest.encode("ascii"), encoded)
        self.assertNotIn(bytes.fromhex(private.digest), encoded)
        self.assertNotIn(struct.pack(">Q", private.size), encoded)
        self.assertNotIn("private/cache.bin", repr(summary))
        self.assertEqual(summary.status_counts, ((RecordStatus.MODIFIED, 1),))

        for diagnostic in (repr(private), repr(snapshot)):
            self.assertNotIn("private/cache.bin", diagnostic)
            self.assertNotIn(private.digest, diagnostic)
            self.assertNotIn(str(private.size), diagnostic)
            self.assertNotIn(oct(private.mode), diagnostic)

    def test_canonical_patch_preserves_cross_section_same_path_state(self) -> None:
        staged_delete = EvidenceRecord(
            tag=RecordTag.TEXT_DIFF,
            path=b"cached.txt",
            status=RecordStatus.DELETED,
            old_mode=REGULAR,
            new_mode=0,
            canonical_diff=b"delete patch\n",
        )
        untracked = _untracked(path=b"cached.txt", content=b"replacement")
        patch = CanonicalPatch(
            records=(),
            staged=(staged_delete,),
            untracked=(untracked,),
            private_summary=summarize_private_records(()),
        )
        encoded = encode_canonical_patch(patch)
        framed_path = struct.pack(">Q", len(b"cached.txt")) + b"cached.txt"
        self.assertEqual(encoded.count(framed_path), 2)


class ApprovalBindingTests(unittest.TestCase):
    def test_each_approval_tuple_member_changes_the_hard_coded_hash(self) -> None:
        base = ApprovalBinding(
            source_snapshot_hash="a" * 64,
            worker_delta_hash="b" * 64,
            projected_task_patch_hash="c" * 64,
        )
        variants = (
            base,
            replace(base, source_snapshot_hash="d" * 64),
            replace(base, worker_delta_hash="d" * 64),
            replace(base, projected_task_patch_hash="d" * 64),
        )
        self.assertEqual(
            tuple(binding.canonical_hash for binding in variants),
            EXPECTED_APPROVAL_HASHES,
        )
        self.assertEqual(len(set(EXPECTED_APPROVAL_HASHES)), 4)

    def test_length_prefixes_distinguish_ambiguous_field_splits(self) -> None:
        def independent_oracle(fields: tuple[bytes, ...]) -> bytes:
            return b"TEST\0" + b"".join(
                struct.pack(">Q", len(field)) + field for field in fields
            )

        first = independent_oracle((b"ab", b"c"))
        second = independent_oracle((b"a", b"bc"))
        self.assertEqual(
            first.hex(),
            "544553540000000000000000026162000000000000000163",
        )
        self.assertEqual(
            second.hex(),
            "544553540000000000000000016100000000000000026263",
        )
        self.assertEqual(
            hashlib.sha256(first).hexdigest(),
            "f41a162e5570b813c51721e0b16d4f04241555971aecbef0da38f285ad11e8e8",
        )
        self.assertEqual(
            hashlib.sha256(second).hexdigest(),
            "33a23b6cdc71d3379cf5da364390326e3b8617727236b929493012c6689c49e7",
        )
        self.assertNotEqual(first, second)


class StrictValidationTests(unittest.TestCase):
    def test_rejects_duplicates_but_allows_cross_section_git_states(self) -> None:
        record = _untracked()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            WorkerDelta(records=(record, record))
        with self.assertRaisesRegex(ValueError, "overlap"):
            _snapshot(untracked=(record,), private=(_private(path=record.path),))

        shared = _text(path=b"both.txt")
        snapshot = _snapshot(staged=(shared,), unstaged=(shared,))
        self.assertEqual(len(snapshot.staged), 1)
        self.assertEqual(len(snapshot.unstaged), 1)

        staged_delete = EvidenceRecord(
            tag=RecordTag.TEXT_DIFF,
            path=b"cached.txt",
            status=RecordStatus.DELETED,
            old_mode=REGULAR,
            new_mode=0,
            canonical_diff=b"delete from index\n",
        )
        replacement = _untracked(path=b"cached.txt", content=b"working tree copy")
        cached_snapshot = _snapshot(
            staged=(staged_delete,),
            untracked=(replacement,),
        )
        self.assertEqual(cached_snapshot.staged[0].path, replacement.path)

        ambiguous = _text(path=b"ambiguous.txt")
        ambiguous_untracked = _untracked(path=ambiguous.path)
        with self.assertRaisesRegex(ValueError, "overlap"):
            _snapshot(staged=(ambiguous,), untracked=(ambiguous_untracked,))
        with self.assertRaisesRegex(ValueError, "overlap"):
            _snapshot(unstaged=(ambiguous,), untracked=(ambiguous_untracked,))
        with self.assertRaisesRegex(ValueError, "overlap"):
            _snapshot(
                staged=(staged_delete,),
                unstaged=(_text(path=staged_delete.path),),
                untracked=(replacement,),
            )

    def test_rejects_absolute_traversal_and_noncanonical_paths(self) -> None:
        invalid_paths = (
            b"",
            b"/absolute",
            b"../escape",
            b"dir/../escape",
            b"dir/./file",
            b"dir//file",
            b"dir/",
            b".git/config",
            b"C:/absolute",
            b"nul\x00byte",
        )
        for path in invalid_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                _untracked(path=path)
        with self.assertRaisesRegex(ValueError, "raw Git path"):
            _untracked(path="unicode.txt")  # type: ignore[arg-type]

    def test_rejects_unsupported_modes_and_non_u64_values(self) -> None:
        for mode in (0o100600, -1, 2**64, True):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                EvidenceRecord(
                    tag=RecordTag.UNTRACKED,
                    path=b"mode",
                    status=RecordStatus.UNTRACKED,
                    old_mode=0,
                    new_mode=mode,
                    content=b"x",
                )
        for size in (-1, 2**64, True):
            with self.subTest(size=size), self.assertRaises(ValueError):
                _private(size=size)

    def test_rejects_unknown_tags_statuses_and_hashes(self) -> None:
        with self.assertRaisesRegex(ValueError, "record tag"):
            EvidenceRecord(
                tag=999,  # type: ignore[arg-type]
                path=b"tag",
                status=RecordStatus.UNTRACKED,
                old_mode=0,
                new_mode=REGULAR,
                content=b"x",
            )
        with self.assertRaisesRegex(ValueError, "record status"):
            EvidenceRecord(
                tag=RecordTag.UNTRACKED,
                path=b"status",
                status=999,  # type: ignore[arg-type]
                old_mode=0,
                new_mode=REGULAR,
                content=b"x",
            )
        with self.assertRaisesRegex(ValueError, "digest kind"):
            _private(digest_kind=999)  # type: ignore[arg-type]

        invalid_hashes = ("A" * 64, "a" * 63, "g" * 64, "a" * 64 + "\n")
        for digest in invalid_hashes:
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                _private(digest=digest)
            with self.assertRaises(ValueError):
                ApprovalBinding(digest, "b" * 64, "c" * 64)

    def test_rejects_invalid_record_shapes(self) -> None:
        with self.assertRaises(ValueError):
            _text(diff=b"")
        with self.assertRaises(ValueError):
            EvidenceRecord(
                tag=RecordTag.MODE_ONLY,
                path=b"mode",
                status=RecordStatus.MODIFIED,
                old_mode=REGULAR,
                new_mode=EXECUTABLE,
                content=b"must-not-exist",
            )
        with self.assertRaises(ValueError):
            EvidenceRecord(
                tag=RecordTag.SYMLINK,
                path=b"link",
                status=RecordStatus.UNTRACKED,
                old_mode=0,
                new_mode=REGULAR,
                content=b"target",
            )


if __name__ == "__main__":
    unittest.main()
