from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runtime.model_boss import repository as repository_module
from runtime.model_boss.evidence import (
    RecordStatus,
    RecordTag,
    WorkerDelta,
    encode_canonical_patch,
    encode_source_snapshot,
    encode_worker_delta,
)
from runtime.model_boss.repository import (
    RepositoryError,
    ScopeViolationError,
    capture_destination,
    capture_source_snapshot,
    capture_worker_delta,
    create_worktree,
    materialize_snapshot,
    project_task_patch,
    replay_worker_delta_projection,
)


GIT_ENV = {
    "LC_ALL": "C",
    "LANG": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Model Boss Test",
    "GIT_AUTHOR_EMAIL": "model-boss@example.invalid",
    "GIT_COMMITTER_NAME": "Model Boss Test",
    "GIT_COMMITTER_EMAIL": "model-boss@example.invalid",
}


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-pager", *args],
        cwd=repo,
        env=GIT_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "core.filemode", "true")
    (repo / "both.txt").write_bytes(b"base\n")
    _git(repo, "add", "--", "both.txt")
    _git(repo, "commit", "--quiet", "-m", "base")
    return repo


class RepositoryCaptureTests(unittest.TestCase):
    def test_git_environment_disables_lazy_fetch_and_terminal_prompts(self) -> None:
        environment = repository_module._git_environment()

        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    def test_capture_rejects_allowlist_spelling_that_aliases_an_index_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            (repo / "FOO").write_bytes(b"tracked bytes must not be relabeled\n")
            _git(repo, "add", "--", "FOO")
            _git(repo, "commit", "--quiet", "-m", "case fixture")

            with self.assertRaisesRegex(RepositoryError, "alias"):
                capture_source_snapshot(repo, (b"foo",))

    def test_capture_rejects_index_flags_that_can_hide_worktree_changes(self) -> None:
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temp_dir:
                repo = _init_repo(Path(temp_dir))
                (repo / "hidden.txt").write_bytes(b"tracked\n")
                _git(repo, "add", "--", "hidden.txt")
                _git(repo, "commit", "--quiet", "-m", "hidden fixture")
                _git(repo, "update-index", flag, "--", "hidden.txt")
                (repo / "hidden.txt").write_bytes(b"concealed mutation\n")

                with self.assertRaisesRegex(RepositoryError, "visibility flag"):
                    capture_source_snapshot(repo, (b"both.txt",))

    def test_capture_does_not_refresh_the_original_index_stat_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            allowed = (b"both.txt",)
            expected = capture_source_snapshot(repo, allowed)
            index_path = repo / ".git" / "index"
            original_index = index_path.read_bytes()

            metadata = (repo / "both.txt").stat()
            os.utime(
                repo / "both.txt",
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 5_000_000_000),
            )

            destination = capture_destination(repo, allowed)

            self.assertEqual(
                encode_source_snapshot(destination),
                encode_source_snapshot(expected),
            )
            self.assertEqual(index_path.read_bytes(), original_index)

    @unittest.skipIf(
        os.name == "nt"
        or not hasattr(os, "geteuid")
        or os.geteuid() == 0,
        "requires POSIX permissions as an unprivileged user",
    )
    def test_capture_fails_closed_when_git_cannot_scan_an_untracked_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            secret = repo / "secret"
            secret.mkdir()
            (secret / "private.txt").write_bytes(b"must not be omitted\n")
            secret.chmod(0)
            try:
                with self.assertRaisesRegex(RepositoryError, "diagnostics"):
                    capture_source_snapshot(repo, (b"both.txt",))
            finally:
                secret.chmod(0o700)

    def test_capture_fails_closed_before_repository_filters_can_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = _init_repo(root)
            (repo / ".gitattributes").write_bytes(b"*.txt filter=hostile\n")
            _git(repo, "add", ".gitattributes")
            _git(repo, "commit", "--quiet", "-m", "attributes")

            sentinel = root / "filter-ran"
            helper = root / "hostile-filter.sh"
            helper.write_text(
                f"#!/bin/sh\n: > '{sentinel}'\nexit 97\n",
                encoding="utf-8",
            )
            os.chmod(helper, 0o755)
            _git(repo, "config", "filter.hostile.clean", str(helper))
            _git(repo, "config", "filter.hostile.smudge", str(helper))
            _git(repo, "config", "filter.hostile.process", str(helper))
            (repo / "both.txt").write_bytes(b"changed\n")

            with self.assertRaisesRegex(RepositoryError, "filter"):
                capture_source_snapshot(repo, (b"both.txt",))
            self.assertFalse(sentinel.exists())

    def test_captures_staged_and_unstaged_edits_to_the_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            (repo / "both.txt").write_bytes(b"staged\n")
            _git(repo, "add", "--", "both.txt")
            (repo / "both.txt").write_bytes(b"unstaged\n")

            snapshot = capture_source_snapshot(repo, (b"both.txt",))

            self.assertEqual([record.path for record in snapshot.staged], [b"both.txt"])
            self.assertEqual(
                [record.path for record in snapshot.unstaged], [b"both.txt"]
            )

    def test_captures_all_record_kinds_without_running_diff_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = _init_repo(root)
            (repo / "delete.txt").write_bytes(b"delete\n")
            script = repo / "script.sh"
            script.write_bytes(b"#!/bin/sh\n")
            os.chmod(script, 0o644)
            (repo / "asset.bin").write_bytes(b"base\0binary")
            (repo / "outside.txt").write_bytes(b"private-v1\n")
            (repo / ".gitattributes").write_bytes(b"*.txt diff=hostile\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "--quiet", "-m", "fixture")

            sentinel = root / "helper-ran"
            helper = root / "hostile.sh"
            helper.write_text(
                f"#!/bin/sh\n: > '{sentinel}'\nexit 97\n",
                encoding="utf-8",
            )
            os.chmod(helper, 0o755)
            _git(repo, "config", "diff.hostile.command", str(helper))
            _git(repo, "config", "diff.hostile.textconv", str(helper))

            (repo / "both.txt").write_bytes(b"stage\n")
            _git(repo, "add", "--", "both.txt")
            (repo / "both.txt").write_bytes(b"worktree\n")
            _git(repo, "rm", "--quiet", "--", "delete.txt")
            os.chmod(script, 0o755)
            (repo / "asset.bin").write_bytes(b"changed\0binary")
            os.symlink("both.txt", repo / "link")
            (repo / "outside.txt").write_bytes(b"private-v2\n")

            hostile_path = b"raw/space \tline\nslash\\non-utf8-\xff.txt"
            raw_repo = os.fsencode(repo)
            os.makedirs(raw_repo + b"/raw")
            try:
                with open(raw_repo + b"/" + hostile_path, "wb") as stream:
                    stream.write(b"raw path\n")
            except OSError:
                hostile_path = b"raw/space \tline\nslash\\utf8-\xc3\xa9.txt"
                with open(raw_repo + b"/" + hostile_path, "wb") as stream:
                    stream.write(b"raw path\n")

            allowed = (
                b"both.txt",
                b"delete.txt",
                b"script.sh",
                b"asset.bin",
                b"link",
                hostile_path,
            )
            snapshot = capture_source_snapshot(repo, allowed)
            self.assertEqual(
                {record.path for record in snapshot.staged},
                {b"both.txt", b"delete.txt"},
            )
            self.assertEqual(
                {record.path for record in snapshot.unstaged},
                {b"both.txt", b"script.sh", b"asset.bin"},
            )
            self.assertEqual(
                {record.path for record in snapshot.untracked},
                {b"link", hostile_path},
            )
            self.assertEqual(
                tuple(record.path for record in snapshot.private),
                (b"outside.txt",),
            )
            self.assertEqual(
                next(
                    record
                    for record in snapshot.unstaged
                    if record.path == b"script.sh"
                ).tag,
                RecordTag.MODE_ONLY,
            )
            self.assertEqual(
                next(
                    record
                    for record in snapshot.unstaged
                    if record.path == b"asset.bin"
                ).tag,
                RecordTag.BINARY,
            )
            self.assertEqual(
                next(
                    record
                    for record in snapshot.untracked
                    if record.path == b"link"
                ).tag,
                RecordTag.SYMLINK,
            )
            self.assertFalse(sentinel.exists())

            first = hashlib.sha256(encode_source_snapshot(snapshot)).digest()
            (repo / "outside.txt").write_bytes(b"private-v3-same-status\n")
            destination = capture_destination(repo, allowed)
            second = hashlib.sha256(encode_source_snapshot(destination)).digest()
            self.assertNotEqual(first, second)
            self.assertFalse(sentinel.exists())

    def test_rename_and_cached_replacement_keep_raw_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            (repo / "old.txt").write_bytes(b"old\n")
            (repo / "cached.txt").write_bytes(b"cached\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "--quiet", "-m", "paths")
            _git(repo, "mv", "--", "old.txt", "new.txt")
            _git(repo, "rm", "--cached", "--quiet", "--", "cached.txt")

            snapshot = capture_source_snapshot(
                repo,
                (b"old.txt", b"new.txt", b"cached.txt"),
            )
            staged = {record.path: record.status for record in snapshot.staged}
            self.assertEqual(staged[b"old.txt"], RecordStatus.DELETED)
            self.assertEqual(staged[b"new.txt"], RecordStatus.ADDED)
            self.assertEqual(staged[b"cached.txt"], RecordStatus.DELETED)
            self.assertIn(b"cached.txt", {r.path for r in snapshot.untracked})

    def test_private_cached_replacement_is_one_digest_bound_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            (repo / "private.txt").write_bytes(b"tracked\n")
            _git(repo, "add", "private.txt")
            _git(repo, "commit", "--quiet", "-m", "private")
            _git(repo, "rm", "--cached", "--quiet", "--", "private.txt")
            (repo / "private.txt").write_bytes(b"replacement-one\n")

            first = capture_source_snapshot(repo, (b"both.txt",))

            self.assertEqual(len(first.private), 1)
            self.assertEqual(first.private[0].path, b"private.txt")
            (repo / "private.txt").write_bytes(b"replacement-two\n")
            second = capture_destination(repo, (b"both.txt",))
            self.assertNotEqual(first.private[0].digest, second.private[0].digest)

    def test_canonical_diff_ignores_repository_diff_tuning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            lines = [f"line-{number:02d}\n" for number in range(30)]
            (repo / "both.txt").write_text("".join(lines), encoding="ascii")
            _git(repo, "add", "both.txt")
            _git(repo, "commit", "--quiet", "-m", "diff fixture")
            lines[2] = "line-02 changed\n"
            lines[25] = "line-25 changed\n"
            (repo / "both.txt").write_text("".join(lines), encoding="ascii")
            _git(repo, "config", "diff.algorithm", "patience")
            _git(repo, "config", "diff.context", "0")
            _git(repo, "config", "diff.interHunkContext", "100")
            _git(repo, "config", "diff.indentHeuristic", "false")
            first = capture_source_snapshot(repo, (b"both.txt",))

            _git(repo, "config", "diff.algorithm", "histogram")
            _git(repo, "config", "diff.context", "12")
            _git(repo, "config", "diff.interHunkContext", "0")
            _git(repo, "config", "diff.indentHeuristic", "true")
            second = capture_destination(repo, (b"both.txt",))

            self.assertEqual(
                first.unstaged[0].canonical_diff,
                second.unstaged[0].canonical_diff,
            )

    def test_canonical_diff_pins_non_ascii_path_quoting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            raw_path = "café.txt".encode("utf-8")
            filesystem_path = os.fsencode(repo) + b"/" + raw_path
            with open(filesystem_path, "wb") as stream:
                stream.write(b"baseline\n")
            _git(repo, "add", "--", os.fsdecode(raw_path))
            _git(repo, "commit", "--quiet", "-m", "non-ascii path fixture")
            with open(filesystem_path, "wb") as stream:
                stream.write(b"changed\n")

            _git(repo, "config", "core.quotePath", "true")
            quoted = capture_source_snapshot(repo, (raw_path,))
            _git(repo, "config", "core.quotePath", "false")
            unquoted = capture_destination(repo, (raw_path,))

            self.assertEqual(
                quoted.unstaged[0].canonical_diff,
                unquoted.unstaged[0].canonical_diff,
            )

    def test_clean_gitlink_in_the_full_index_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            vendor = repo / "vendor"
            vendor.mkdir()
            _git(vendor, "init", "--quiet")
            (vendor / "nested.txt").write_bytes(b"nested\n")
            _git(vendor, "add", "nested.txt")
            _git(vendor, "commit", "--quiet", "-m", "nested")
            head = _git(vendor, "rev-parse", "HEAD").stdout.strip().decode("ascii")
            _git(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{head},vendor",
            )
            _git(repo, "commit", "--quiet", "-m", "clean gitlink")

            with self.assertRaisesRegex(RepositoryError, "submodule"):
                capture_source_snapshot(repo, (b"both.txt",))

    def test_windows_paths_fail_closed_before_filesystem_resolution(self) -> None:
        invalid = (
            b"..\\escape",
            b"C:relative",
            b"file.txt:stream",
            b"trailing-dot.",
            b"trailing-space ",
            b".GIT/config",
            b"dir\\file.txt",
            b"NUL",
            b"NUL.txt",
            b"aux.log",
            b"COM1",
            b"lpt9.data",
        )
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(ValueError):
                repository_module._validate_platform_path(path, windows=True)
        repository_module._validate_platform_path(b"dir/file.txt", windows=True)

    def test_private_untracked_content_is_hashed_in_bounded_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            path = repo / "private.bin"
            content = b"private-streaming-content" * 100_000
            path.write_bytes(content)
            real_open = open
            read_sizes: list[int] = []

            class GuardedStream:
                def __init__(self, stream: object) -> None:
                    self._stream = stream

                def __enter__(self) -> GuardedStream:
                    return self

                def __exit__(self, *args: object) -> None:
                    self._stream.close()  # type: ignore[attr-defined]

                def read(self, size: int = -1) -> bytes:
                    if size <= 0:
                        raise AssertionError("private hashing must use bounded reads")
                    read_sizes.append(size)
                    return self._stream.read(size)  # type: ignore[attr-defined]

            def guarded_open(file: object, *args: object, **kwargs: object) -> object:
                stream = real_open(file, *args, **kwargs)
                if os.fsdecode(file) == os.fspath(path):  # type: ignore[arg-type]
                    return GuardedStream(stream)
                return stream

            with mock.patch.object(
                repository_module,
                "open",
                side_effect=guarded_open,
                create=True,
            ):
                mode, size, digest = repository_module._private_untracked_digest(
                    repo,
                    b"private.bin",
                )

            self.assertEqual(mode, 0o100644)
            self.assertEqual(size, len(content))
            self.assertEqual(digest, hashlib.sha256(content).hexdigest())
            self.assertGreater(len(read_sizes), 1)

    def test_ignored_path_is_captured_only_when_explicitly_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            (repo / ".gitignore").write_bytes(b"ignored.txt\n")
            _git(repo, "add", ".gitignore")
            _git(repo, "commit", "--quiet", "-m", "ignore")
            (repo / "ignored.txt").write_bytes(b"requested\n")

            hidden = capture_source_snapshot(repo, ())
            self.assertFalse(hidden.untracked)
            self.assertFalse(hidden.private)
            explicit = capture_source_snapshot(repo, (b"ignored.txt",))
            self.assertEqual(
                tuple(record.path for record in explicit.untracked),
                (b"ignored.txt",),
            )

    def test_rejects_paths_special_files_submodules_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = _init_repo(Path(temp_dir))
            for invalid in (b"/absolute", b"../escape", b"a/../b", b"a//b"):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    capture_source_snapshot(repo, (invalid,))
            with self.assertRaisesRegex(ValueError, "alias"):
                capture_source_snapshot(repo, (b"both.txt", b"BOTH.TXT"))

            fifo = repo / "pipe"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(RepositoryError, "special"):
                capture_source_snapshot(repo, (b"pipe",))
            fifo.unlink()

            os.symlink("../outside", repo / "escape-link")
            with self.assertRaisesRegex(RepositoryError, "symlink"):
                capture_source_snapshot(repo, (b"escape-link",))
            (repo / "escape-link").unlink()

            head = _git(repo, "rev-parse", "HEAD").stdout.strip().decode("ascii")
            _git(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{head},submodule",
            )
            with self.assertRaisesRegex(RepositoryError, "submodule"):
                capture_source_snapshot(repo, (b"submodule",))


class WorktreeTests(unittest.TestCase):
    def test_independently_replays_delta_records_before_trusting_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = _init_repo(root)
            allowed = (b"both.txt", b"worker.txt")
            source = capture_source_snapshot(repo, allowed)
            index_before = (repo / ".git" / "index").read_bytes()
            worker_path = root / "worker-capture"
            handle = create_worktree(repo, source, worker_path)
            try:
                materialize_snapshot(handle, source)
                (worker_path / "both.txt").write_bytes(b"worker change\n")
                (worker_path / "worker.txt").write_bytes(b"new worker file\n")
                delta = capture_worker_delta(handle, source, allowed)
            finally:
                _git(
                    repo,
                    "worktree",
                    "remove",
                    "--force",
                    str(worker_path),
                    check=False,
                )

            replayed = replay_worker_delta_projection(repo, source, delta)

            assert delta.projected_snapshot is not None
            self.assertEqual(
                encode_source_snapshot(replayed),
                encode_source_snapshot(delta.projected_snapshot),
            )
            self.assertEqual((repo / ".git" / "index").read_bytes(), index_before)
            self.assertEqual(
                _git(repo, "worktree", "list", "--porcelain").stdout.count(
                    b"worktree "
                ),
                1,
            )

            forged = WorkerDelta(
                records=delta.records,
                projected_snapshot=source,
            )
            with self.assertRaisesRegex(ScopeViolationError, "replay|projected"):
                replay_worker_delta_projection(repo, source, forged)

    def test_temporary_index_objects_never_leak_into_the_source_object_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = _init_repo(root)
            source_bytes = b"source-untracked-never-persisted\n"
            worker_bytes = b"worker-untracked-never-persisted\n"
            (repo / "planned.txt").write_bytes(source_bytes)
            allowed = (b"both.txt", b"planned.txt", b"worker.txt")
            source = capture_source_snapshot(repo, allowed)
            source_oid = subprocess.run(
                ["git", "hash-object", "--stdin"],
                cwd=repo,
                env=GIT_ENV,
                input=source_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            worker_oid = subprocess.run(
                ["git", "hash-object", "--stdin"],
                cwd=repo,
                env=GIT_ENV,
                input=worker_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout.strip()

            for oid in (source_oid, worker_oid):
                self.assertNotEqual(
                    _git(
                        repo,
                        "cat-file",
                        "-e",
                        oid.decode("ascii"),
                        check=False,
                    ).returncode,
                    0,
                )

            worktree_path = root / "isolated-objects-worktree"
            handle = create_worktree(repo, source, worktree_path)
            isolated_object_directory = handle.object_directory
            try:
                materialize_snapshot(handle, source)
                (worktree_path / "worker.txt").write_bytes(worker_bytes)
                delta = capture_worker_delta(handle, source, allowed)
                self.assertEqual(
                    {record.path for record in delta.records},
                    {b"worker.txt"},
                )
            finally:
                _git(
                    repo,
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                    check=False,
                )

            self.assertTrue(isolated_object_directory.is_dir())
            for oid in (source_oid, worker_oid):
                self.assertNotEqual(
                    _git(
                        repo,
                        "cat-file",
                        "-e",
                        oid.decode("ascii"),
                        check=False,
                    ).returncode,
                    0,
                )

    def test_materializes_source_then_captures_only_worker_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = _init_repo(root)
            (repo / "both.txt").write_bytes(b"source staged\n")
            _git(repo, "add", "--", "both.txt")
            (repo / "both.txt").write_bytes(b"source final\n")
            (repo / "planned.txt").write_bytes(b"source untracked\n")
            allowed = (b"both.txt", b"planned.txt", b"worker.txt")
            source = capture_source_snapshot(repo, allowed)
            original_index = (repo / ".git" / "index").read_bytes()

            worktree_path = root / "owned" / "worktree"
            worktree_path.parent.mkdir()
            handle = create_worktree(repo, source, worktree_path)
            try:
                materialize_snapshot(handle, source)
                self.assertEqual((repo / ".git" / "index").read_bytes(), original_index)
                self.assertEqual(
                    (worktree_path / "both.txt").read_bytes(),
                    b"source final\n",
                )
                self.assertEqual(
                    (worktree_path / "planned.txt").read_bytes(),
                    b"source untracked\n",
                )
                (worktree_path / "both.txt").write_bytes(b"worker final\n")
                (worktree_path / "worker.txt").write_bytes(b"worker file\n")

                delta = capture_worker_delta(handle, source, allowed)
                self.assertEqual(
                    {record.path for record in delta.records},
                    {b"both.txt", b"worker.txt"},
                )
                self.assertNotIn(b"source untracked\n", encode_worker_delta(delta))
                patch = project_task_patch(source, delta)
                encoded = encode_canonical_patch(patch)
                self.assertIn(b"both.txt", encoded)
                self.assertIn(b"worker.txt", encoded)
                self.assertEqual(patch.private_summary, source.private_summary)
            finally:
                _git(
                    repo,
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                    check=False,
                )

    def test_worker_out_of_scope_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = _init_repo(root)
            (repo / "outside.txt").write_bytes(b"outside\n")
            _git(repo, "add", "outside.txt")
            _git(repo, "commit", "--quiet", "-m", "outside")
            source = capture_source_snapshot(repo, (b"both.txt",))
            worktree_path = root / "scope-worktree"
            handle = create_worktree(repo, source, worktree_path)
            try:
                materialize_snapshot(handle, source)
                (worktree_path / "outside.txt").write_bytes(b"breach\n")
                with self.assertRaises(ScopeViolationError):
                    capture_worker_delta(handle, source, source.allowed_paths)
            finally:
                _git(
                    repo,
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                    check=False,
                )

    def test_worker_index_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = _init_repo(root)
            source = capture_source_snapshot(repo, (b"both.txt",))
            worktree_path = root / "index-worktree"
            handle = create_worktree(repo, source, worktree_path)
            try:
                materialize_snapshot(handle, source)
                (worktree_path / "both.txt").write_bytes(b"worker staged\n")
                _git(worktree_path, "add", "--", "both.txt")
                with self.assertRaisesRegex(ScopeViolationError, "index"):
                    capture_worker_delta(handle, source, source.allowed_paths)
            finally:
                _git(
                    repo,
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                    check=False,
                )


if __name__ == "__main__":
    unittest.main()
