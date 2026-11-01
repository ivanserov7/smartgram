"""Проверки HTML-передачи: только временные папки, без Drive и сети."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import html_report_sync as sync
from review_archive_sync import DRIVE_FOLDER_ID, MARKER_NAME, ReviewSyncError


class HtmlReportSyncTests(unittest.TestCase):
    def setUp(self):
        billing = patch("billing_sync.refresh_after_report")
        billing.start()
        self.addCleanup(billing.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.drive = self.root / "Drive"
        self.drive.mkdir()
        (self.drive / MARKER_NAME).write_text(json.dumps({"drive_folder_id": DRIVE_FOLDER_ID}))
        self.report = self.root / "report.html"
        self.report.write_text('<!doctype html><html lang="ru">Три исследования</html>', encoding="utf-8")
        self.archive_latest = self.drive / "latest.json"
        self.archive_latest.write_text('{"archive_name":"previous.zip"}')

    def transfer(self):
        return sync.sync_html_report(self.report, self.drive)

    def test_copy_hash_index_and_archive_pointer(self):
        previous = self.archive_latest.read_bytes()
        result = self.transfer()
        copied = self.drive / result["report_name"]
        self.assertEqual(copied.read_bytes(), self.report.read_bytes())
        self.assertEqual(sync.fingerprint(copied.read_bytes())["sha256"], result["sha256"])
        index = json.loads((self.drive / sync.INDEX_NAME).read_text())
        self.assertEqual(index["latest_report_name"], copied.name)
        self.assertEqual(len(index["reports"]), 1)
        self.assertEqual(self.archive_latest.read_bytes(), previous)
        self.assertFalse(list(self.drive.glob("*.tmp")))

    def test_unchanged_report_reuses_intact_copy(self):
        first = self.transfer()
        second = self.transfer()
        self.assertEqual(first["report_name"], second["report_name"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(json.loads((self.drive / sync.INDEX_NAME).read_text())["reports"]), 1)

    def test_changed_report_and_name_collision_preserve_versions(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        with patch.object(sync, "utc_now", return_value=now):
            first = self.transfer()
            self.report.write_text("<html>Новая версия</html>")
            second = self.transfer()
        self.assertNotEqual(first["report_name"], second["report_name"])
        self.assertTrue(second["report_name"].endswith("_1.html"))
        self.assertIn("Три", (self.drive / first["report_name"]).read_text())

    def test_corrupted_copy_is_not_reused(self):
        first = self.transfer()
        (self.drive / first["report_name"]).write_text("broken")
        self.assertFalse(self.transfer()["reused"])

    def test_missing_directory_not_created(self):
        missing = self.root / "absent"
        with self.assertRaisesRegex(ReviewSyncError, "недоступна"):
            sync.sync_html_report(self.report, missing)
        self.assertFalse(missing.exists())

    def test_wrong_marker(self):
        (self.drive / MARKER_NAME).write_text('{"drive_folder_id":"wrong"}')
        with self.assertRaisesRegex(ReviewSyncError, "не совпадает"):
            self.transfer()
        self.assertFalse(list(self.drive.glob("*.html")))

    def test_index_failure_preserves_previous_index_and_reports(self):
        first = self.transfer()
        previous = (self.drive / sync.INDEX_NAME).read_bytes()
        self.report.write_text("<html>Другой отчёт</html>")
        with patch.object(sync, "atomic_write_json", side_effect=OSError("test")):
            with self.assertRaisesRegex(ReviewSyncError, "reports.json не обновлён"):
                self.transfer()
        self.assertEqual((self.drive / sync.INDEX_NAME).read_bytes(), previous)
        self.assertTrue((self.drive / first["report_name"]).exists())
        self.assertTrue(self.report.exists())

    def test_copy_failure_preserves_index(self):
        self.transfer()
        previous = (self.drive / sync.INDEX_NAME).read_bytes()
        self.report.write_text("<html>Изменение</html>")
        with patch.object(sync, "publish_report", side_effect=OSError("test")):
            with self.assertRaises(ReviewSyncError):
                self.transfer()
        self.assertEqual((self.drive / sync.INDEX_NAME).read_bytes(), previous)
        self.assertFalse(list(self.drive.glob("*.tmp")))

    def test_malformed_index_preserved(self):
        path = self.drive / sync.INDEX_NAME
        path.write_text('{"schema_version":1,"reports":[null]}')
        previous = path.read_bytes()
        with self.assertRaisesRegex(ReviewSyncError, "структуру"):
            self.transfer()
        self.assertEqual(path.read_bytes(), previous)

    def test_reject_keys_and_non_html(self):
        self.report.write_text("<html>local-example-value</html>")
        with self.assertRaisesRegex(ReviewSyncError, "секрет"):
            sync.sync_html_report(self.report, self.drive, ["local-example-value"])
        self.report.write_text("not HTML")
        with self.assertRaisesRegex(ReviewSyncError, "не похож"):
            self.transfer()
        self.assertFalse(list(self.drive.glob("*.html")))

    def test_wrapper_reads_env_without_printing_keys(self):
        env = self.root / ".env"
        env.write_text(f'SMARTGRAM_REVIEW_SYNC_DIR="{self.drive}"\nGOOGLE_API_KEY=example-local-value\n')
        self.report.write_text("<html>example-local-value</html>")
        output = io.StringIO()
        with patch.object(sync, "ENV_PATH", env), redirect_stdout(output), redirect_stderr(output):
            self.assertFalse(sync.sync_generated_report(self.report))
        self.assertNotIn("example-local-value", output.getvalue())
        self.assertIn("не передан", output.getvalue())

    def test_cli_entrypoints_transfer_generated_html(self):
        import research_batch
        import research_report
        import result_page
        import review_gallery

        self.report.write_text('<html>Report</html>')
        (self.root / "batch.json").write_text('{"results":[{"execution":{"state":"completed"}}]}')
        manifest = self.root / "visual_review.json"
        manifest.write_text('{"mode":"real","selection_method":"openai_vision","scenes":[]}')
        with patch.object(sync, "sync_generated_report", return_value=True) as copy, redirect_stdout(io.StringIO()):
            with patch("sys.argv", ["research_report.py", "--run-directory", str(self.root)]), \
                 patch.object(research_report, "build_research_report", return_value=self.report):
                self.assertEqual(research_report.main(), 0)
                copy.assert_called_with(self.report)
            with patch("sys.argv", ["result_page.py", "--run-directory", str(self.root)]), \
                 patch.object(result_page, "build_result_page", return_value=self.report):
                self.assertEqual(result_page.main(), 0)
                copy.assert_called_with(self.report)
            with patch("sys.argv", ["review_gallery.py", "--manifest", str(manifest), "--output", str(self.report)]), \
                 patch.object(review_gallery, "render_gallery", return_value="<html>Gallery</html>"):
                self.assertEqual(review_gallery.main(), 0)
                copy.assert_called_with(self.report)
        with patch("sys.argv", ["research_batch.py"]), redirect_stdout(io.StringIO()), \
             patch.object(research_batch, "run_topic_set", return_value=self.root), \
             patch.object(research_batch, "sync_generated_report", return_value=True) as copy:
            self.assertEqual(research_batch.main(), 0)
            copy.assert_called_once_with(self.report)


if __name__ == "__main__":
    unittest.main()
