"""Архив и передача проверяются во временных папках, без Google Drive API и настоящей сети."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import build_review_archive as builder
import review_archive_sync as sync


class ReviewSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "Drive test folder"
        self.directory.mkdir()
        (self.directory / sync.MARKER_NAME).write_text(json.dumps({"drive_folder_id": sync.DRIVE_FOLDER_ID}))
        self.archive = self.root / "Smartgram_review.zip"
        with ZipFile(self.archive, "w") as archive:
            archive.writestr("Smartgram/main.py", "print('test archive')\n")
        self.created = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
        clock = patch.object(sync, "utc_now", return_value=self.created)
        clock.start()
        self.addCleanup(clock.stop)
        self.old_archive = self.directory / "Smartgram_review_20260101T000000Z.zip"
        self.old_archive.write_bytes(self.archive.read_bytes())
        self.old_latest = b'{"schema_version": 1, "archive_name": "Smartgram_review_20260101T000000Z.zip"}\n'
        (self.directory / "latest.json").write_bytes(self.old_latest)

    def assert_previous_preserved(self):
        self.assertEqual((self.directory / "latest.json").read_bytes(), self.old_latest)
        self.assertEqual(self.old_archive.read_bytes(), self.archive.read_bytes())
        self.assertFalse(list(self.directory.glob(".*.tmp")))

    def test_successful_copy_size_sha_and_latest(self):
        metadata = sync.sync_archive(self.archive, self.directory)
        target = self.directory / metadata["archive_name"]
        self.assertEqual(target.read_bytes(), self.archive.read_bytes())
        self.assertEqual(metadata, {
            "schema_version": 1, "archive_name": "Smartgram_review_20261101T000000Z.zip",
            "created_at_utc": "2026-11-01T00:00:00Z", "size_bytes": self.archive.stat().st_size,
            "sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
        })
        self.assertEqual(json.loads((self.directory / "latest.json").read_text()), metadata)
        with ZipFile(target) as archive:
            self.assertIsNone(archive.testzip())
        self.assertTrue(self.old_archive.exists())
        self.assertFalse(list(self.directory.glob(".*.tmp")))

    def test_missing_directory_is_not_created(self):
        missing = self.root / "missing drive folder"
        with self.assertRaisesRegex(sync.ReviewSyncError, "недоступна"):
            sync.sync_archive(self.archive, missing)
        self.assertFalse(missing.exists())
        self.assert_previous_preserved()

    def test_marker_missing_invalid_wrong_id_or_symlink_blocks_copy(self):
        marker = self.directory / sync.MARKER_NAME
        for content in (None, "not json", "[]", '{"drive_folder_id": "wrong-account-folder"}'):
            marker.unlink(missing_ok=True)
            if content is not None:
                marker.write_text(content)
            with self.subTest(content=content), self.assertRaises(sync.ReviewSyncError):
                sync.sync_archive(self.archive, self.directory)
            self.assert_previous_preserved()
        marker.unlink()
        elsewhere = self.root / "marker.json"
        elsewhere.write_text(json.dumps({"drive_folder_id": sync.DRIVE_FOLDER_ID}))
        marker.symlink_to(elsewhere)
        with self.assertRaisesRegex(sync.ReviewSyncError, "ссылкой"):
            sync.sync_archive(self.archive, self.directory)

    def test_name_collision_uses_next_second_and_preserves_old_zip(self):
        existing = self.directory / "Smartgram_review_20261101T000000Z.zip"
        existing.write_bytes(b"old version must not be overwritten")
        with patch.object(sync, "utc_now", side_effect=[self.created, self.created + timedelta(seconds=1)]), \
             patch.object(sync.time, "sleep") as sleep:
            metadata = sync.sync_archive(self.archive, self.directory)
        self.assertEqual(metadata["archive_name"], "Smartgram_review_20261101T000001Z.zip")
        self.assertEqual(metadata["created_at_utc"], "2026-11-01T00:00:01Z")
        self.assertEqual(existing.read_bytes(), b"old version must not be overwritten")
        sleep.assert_called_once_with(1.0)

    def test_collision_at_rename_is_also_protected(self):
        native_rename = sync.rename_new_file
        collisions = []
        def race(source, target):
            if not collisions:
                target.write_bytes(b"another concurrent build")
                collisions.append(target)
            native_rename(source, target)
        with patch.object(sync, "rename_new_file", side_effect=race), \
             patch.object(sync, "utc_now", side_effect=[self.created, self.created + timedelta(seconds=1)]), \
             patch.object(sync.time, "sleep"):
            metadata = sync.sync_archive(self.archive, self.directory)
        self.assertEqual(collisions[0].read_bytes(), b"another concurrent build")
        self.assertTrue((self.directory / metadata["archive_name"]).exists())

    def test_permanent_collision_is_bounded_and_latest_is_unchanged(self):
        existing = self.directory / "Smartgram_review_20261101T000000Z.zip"
        existing.write_bytes(b"keep")
        with patch.object(sync.time, "sleep") as sleep:
            with self.assertRaisesRegex(sync.ReviewSyncError, "свободное имя"):
                sync.sync_archive(self.archive, self.directory)
        self.assertEqual(sleep.call_count, sync.MAX_NAME_ATTEMPTS - 1)
        self.assertEqual(existing.read_bytes(), b"keep")
        self.assert_previous_preserved()

    def test_copy_permission_error_keeps_previous_latest_and_archives(self):
        with patch.object(sync.shutil, "copyfileobj", side_effect=PermissionError("test denied")):
            with self.assertRaisesRegex(sync.ReviewSyncError, "PermissionError"):
                sync.sync_archive(self.archive, self.directory)
        self.assert_previous_preserved()
        self.assertEqual(len(list(self.directory.glob("*.zip"))), 1)

    def test_corrupted_copy_or_different_valid_zip_never_updates_latest(self):
        different = io.BytesIO()
        with ZipFile(different, "w") as archive:
            archive.writestr("different.txt", "wrong archive")
        for content in (b"corrupt zip", different.getvalue()):
            with self.subTest(valid=content.startswith(b"PK")):
                def copy_wrong(source, destination, **kwargs):
                    destination.write(content)
                with patch.object(sync.shutil, "copyfileobj", side_effect=copy_wrong), \
                     patch.object(sync, "update_latest") as latest:
                    with self.assertRaises(sync.ReviewSyncError):
                        sync.sync_archive(self.archive, self.directory)
                    latest.assert_not_called()
                self.assert_previous_preserved()

    def test_latest_replace_failure_preserves_previous_pointer(self):
        with patch.object(sync.os, "replace", side_effect=PermissionError("test latest denied")):
            with self.assertRaisesRegex(sync.ReviewSyncError, "latest.json не обновлён"):
                sync.sync_archive(self.archive, self.directory)
        self.assert_previous_preserved()
        # Новая исправная копия может остаться без указателя, но прежняя версия не удаляется.
        self.assertEqual(len(list(self.directory.glob("*.zip"))), 2)

    def test_damaged_source_is_not_published(self):
        damaged = self.root / "damaged.zip"
        damaged.write_bytes(b"not a zip")
        with self.assertRaises(sync.ReviewSyncError):
            sync.sync_archive(damaged, self.directory)
        self.assert_previous_preserved()

    def test_settings_empty_absolute_and_relative(self):
        env = self.root / ".env"
        env.write_text('SMARTGRAM_REVIEW_SYNC_DIR=""\n')
        self.assertIsNone(sync.configured_sync_directory(env))
        env.write_text(f'SMARTGRAM_REVIEW_SYNC_DIR="{self.directory}"\n')
        self.assertEqual(sync.configured_sync_directory(env), self.directory)
        env.write_text('SMARTGRAM_REVIEW_SYNC_DIR=relative/folder\n')
        with self.assertRaisesRegex(sync.ReviewSyncError, "абсолютным"):
            sync.configured_sync_directory(env)


class ArchiveBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        for name in ("AGENTS.md", "main.py", "pipeline.py", "research.py", "README.md", "requirements.txt", "build_review_archive.py"):
            (self.project / name).write_text("# safe test source\n")
        (self.project / ".env.example").write_text("OPENAI_API_KEY=your_api_key_here\nSMARTGRAM_REVIEW_SYNC_DIR=\n")
        patcher = patch.object(builder, "PROJECT_DIRECTORY", self.project)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = self.root / "Desktop archive.zip"

    def main(self, *extra):
        with patch("sys.argv", ["build_review_archive.py", "--output", str(self.output), *extra]), \
             redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            result = builder.main()
        return result, stdout.getvalue(), stderr.getvalue()

    def test_archive_filter_still_excludes_secrets_environment_and_outputs(self):
        test_key = "sk-" + "x" * 40
        (self.project / ".env").write_text("OPENAI_API_KEY=" + test_key)
        for folder in (".venv", ".git", "__pycache__", "outputs", "temp"):
            (self.project / folder).mkdir()
            (self.project / folder / "discard.py").write_text("discard")
        (self.project / "credentials.json").write_text("discard")
        (self.project / ".gitignore").write_text(".env\n")
        builder.build_archive(self.output)
        with ZipFile(self.output) as archive:
            names = archive.namelist()
            self.assertIn("Smartgram/.env.example", names)
            self.assertIn("Smartgram/AGENTS.md", names)
            self.assertTrue(all(not {".env", ".venv", ".git", "__pycache__", "outputs", "temp", "credentials.json"}.intersection(Path(name).parts) for name in names))
            self.assertTrue(all(test_key.encode() not in archive.read(name) for name in names))

    def test_normal_command_builds_before_sync_and_reports_only_local_copy(self):
        metadata = {"archive_name": "Smartgram_review_20261101T000000Z.zip", "size_bytes": 123, "sha256": "a" * 64}
        def fake_sync(path, directory):
            self.assertTrue(path.exists())
            with ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
            return metadata
        with patch.object(builder, "configured_sync_directory", return_value=self.root), \
             patch.object(builder, "sync_archive", side_effect=fake_sync) as transfer:
            code, stdout, stderr = self.main()
        self.assertEqual(code, 0)
        transfer.assert_called_once_with(self.output, self.root)
        self.assertIn("локально", stdout)
        self.assertIn("не подтверждено", stdout)
        self.assertFalse(stderr)

    def test_sync_failure_does_not_remove_successful_desktop_archive(self):
        with patch.object(builder, "configured_sync_directory", return_value=self.root), \
             patch.object(builder, "sync_archive", side_effect=sync.ReviewSyncError("Папка недоступна")):
            code, stdout, stderr = self.main()
        self.assertEqual(code, 1)
        with ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
        self.assertIn("Передача не завершена", stderr)
        self.assertIn("локальный архив сохранён", stderr)
        self.assertNotIn("latest.json обновлён", stdout)

    def test_build_failure_never_calls_sync_and_keeps_previous_desktop_file(self):
        self.output.write_bytes(b"previous desktop file")
        (self.project / "main.py").write_text("sk-" + "x" * 40)
        with patch.object(builder, "sync_archive") as transfer:
            code, stdout, stderr = self.main()
        self.assertEqual(code, 1)
        self.assertEqual(self.output.read_bytes(), b"previous desktop file")
        transfer.assert_not_called()

    def test_no_sync_and_unconfigured_sync_skip_transfer(self):
        with patch.object(builder, "configured_sync_directory", side_effect=AssertionError("Settings read")), \
             patch.object(builder, "sync_archive") as transfer:
            self.assertEqual(self.main("--no-sync")[0], 0)
            transfer.assert_not_called()
        with patch.object(builder, "configured_sync_directory", return_value=None), patch.object(builder, "sync_archive") as transfer:
            code, stdout, stderr = self.main()
            self.assertEqual(code, 0)
            self.assertIn("не настроена", stdout)
            transfer.assert_not_called()

    def test_empty_sync_example_allowed_personal_path_and_other_empty_settings_rejected(self):
        builder.validate_env_example("SMARTGRAM_REVIEW_SYNC_DIR=\n")
        for value in ('SMARTGRAM_REVIEW_SYNC_DIR="/Users/example/CloudStorage"', "OPENAI_MODEL="):
            with self.subTest(value=value), self.assertRaises(ValueError):
                builder.validate_env_example(value)


if __name__ == "__main__":
    unittest.main()
