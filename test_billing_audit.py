"""Offline regressions for report concurrency and untrusted saved metadata."""

import hashlib
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from datetime import datetime, timezone
from unittest.mock import patch

import html_report_sync
import release_sync
import billing_sync
from billing_estimates import effective_cost
from billing_history import import_calls
from billing_units import consumption
from review_archive_sync import DRIVE_FOLDER_ID, MARKER_NAME, ReviewSyncError


class BillingAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.drive = self.root / "Drive"
        self.drive.mkdir()
        (self.drive / MARKER_NAME).write_text(
            json.dumps({"drive_folder_id": DRIVE_FOLDER_ID})
        )

    def test_concurrent_reports_preserve_every_index_entry(self):
        reports = [self.root / f"report_{i}.html" for i in range(8)]
        for index, report in enumerate(reports):
            report.write_text(f"<html>Report {index}</html>")
        start = Barrier(len(reports))
        read_index = html_report_sync.read_index

        def slow_read(directory):
            result = read_index(directory)
            # Expose the read/replace race without assuming thread scheduling.
            time.sleep(0.02)
            return result

        def publish(report):
            start.wait(timeout=5)
            return html_report_sync.sync_html_report(report, self.drive)

        with patch.object(html_report_sync, "read_index", side_effect=slow_read):
            with ThreadPoolExecutor(max_workers=len(reports)) as pool:
                results = list(pool.map(publish, reports))
        index = read_index(self.drive)
        self.assertEqual(
            {entry["report_name"] for entry in index["reports"]},
            {entry["report_name"] for entry in results},
        )
        self.assertEqual(len(index["reports"]), len(reports))

    def make_release(self, names=("octopus",), **changes):
        source = self.root / "video.mp4"
        source.write_bytes(b"accepted-test-video")
        entries = [
            {
                "name": name,
                "topic": "Octopus",
                "status": "ready",
                "video": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "size_bytes": source.stat().st_size,
                **changes,
            }
            for name in names
        ]
        status = self.root / "assembly_status.json"
        status.write_text(json.dumps({"schema_version": 1, "status": "ready", "topics": entries}))
        return status

    def test_release_rejects_names_that_escape_the_version_directory(self):
        for name in ("../escaped", str(self.root / "absolute"), "dir\\file", "", "bad\nname"):
            with self.subTest(name=name):
                status = self.make_release((name,))
                with self.assertRaises(ReviewSyncError):
                    release_sync.sync_videos(status, self.drive)
                self.assertFalse(list(self.drive.glob("Smartgram_videos_*")))
                self.assertFalse((self.drive / "escaped.mp4").exists())
                self.assertFalse((self.root / "absolute.mp4").exists())

    def test_release_validates_all_entries_before_copying(self):
        for names, changes in (
            (("same", "same"), {}),
            (("secret",), {"topic": "local-sensitive-value"}),
        ):
            status = self.make_release(names, **changes)
            with patch.object(release_sync, "local_secret_values", return_value=["local-sensitive-value"]):
                with self.assertRaises(ReviewSyncError):
                    release_sync.sync_videos(status, self.drive)
            self.assertFalse(list(self.drive.glob("Smartgram_videos_*")))

    def test_release_rejects_non_video_files_before_publication(self):
        source = self.root / "private.txt"
        source.write_bytes(b"not a video")
        status = self.make_release(video=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                                   size_bytes=source.stat().st_size)
        with self.assertRaises(ReviewSyncError):
            release_sync.sync_videos(status, self.drive)
        self.assertFalse(list(self.drive.glob("Smartgram_videos_*")))

    def test_valid_release_preserves_unicode_name_and_checksums(self):
        receipt = release_sync.sync_videos(self.make_release(("Осьминог 1",)), self.drive)
        folder = Path(receipt["directory"])
        self.assertEqual((folder / "Осьминог 1.mp4").read_bytes(), b"accepted-test-video")
        self.assertTrue((folder / "assembly_status.json").is_file())
        self.assertFalse(receipt["cloud_upload_confirmed"])

    def test_releases_in_the_same_second_preserve_both_versions(self):
        status = self.make_release()
        with patch.object(release_sync, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 2, tzinfo=timezone.utc)
            first = release_sync.sync_videos(status, self.drive)
            second = release_sync.sync_videos(status, self.drive)
        self.assertNotEqual(first["directory"], second["directory"])
        for receipt in (first, second):
            self.assertTrue((Path(receipt["directory"]) / "octopus.mp4").is_file())

    def test_parallel_billing_refreshes_do_not_interleave_exports(self):
        start = Barrier(2)
        state_lock = Lock()
        active = peak = 0
        save_exports = billing_sync.save_exports

        def slow_export(*args):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.02)
                return save_exports(*args)
            finally:
                with state_lock:
                    active -= 1

        def refresh(_):
            start.wait(timeout=5)
            return billing_sync.sync_billing(directory=self.root / "billing", output_root=self.root, offline=True)

        with patch.object(billing_sync, "save_exports", side_effect=slow_export):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(refresh, range(2)))
        self.assertEqual(peak, 1)
        data = json.loads((self.root / "billing" / "analytics.json").read_text())
        self.assertIn(data["export_id"], (self.root / "billing" / "billing_report.html").read_text())

    def test_bad_journal_record_does_not_hide_later_valid_calls(self):
        def record(identifier):
            return {"telemetry": {"call_id": identifier, "model": "test-model",
                                  "status": "completed", "usage": {"input_tokens": 1}}}

        for invalid in (None, {"telemetry": {"attempts": [None]}},
                        {"model": []}, {"usage": [1]}, {"tool_usage": [1]}):
            with self.subTest(invalid=invalid):
                journal = self.root / "openai_usage.json"
                journal.write_text(json.dumps({"requests": [record("first"), invalid, record("last")]}))
                calls, warnings = import_calls(self.root, {})
                self.assertEqual({call["call_id"] for call in calls}, {"first", "last"})
                self.assertTrue(warnings)

    def test_cached_audio_is_not_priced_as_uncached_for_plural_usage_keys(self):
        usage = {"input_tokens": 20, "output_tokens": 30,
                 "input_tokens_details": {"text_tokens": 10, "audio_tokens": 10,
                                          "image_tokens": 0, "cached_tokens": 5},
                 "output_tokens_details": {"text_tokens": 10, "audio_tokens": 20}}
        call = {"model": "gpt-realtime-2.1-mini", "usage": usage,
                "units": consumption(usage), "attempts_complete": True,
                "service_tier": "default", "batch": False}
        value = effective_cost(call)
        self.assertIsNone(value["amount"])
        self.assertFalse(value["complete"])
        self.assertIn("кешем", " ".join(value["missing"]))


if __name__ == "__main__":
    unittest.main()
