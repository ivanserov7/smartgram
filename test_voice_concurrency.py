"""Concurrent speech commands cannot duplicate paid work or overwrite approvals."""

import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from asr_diagnostics import diagnose, review_saved
from production_state import run_lock
from test_full_voiceover import FakeVoiceAPI, KEYS, make_script
from voice_errors import VoiceoverError
from voice_local_recheck import reassess_saved
from voice_lock import voice_lock
from voice_recheck import recheck_saved
from voice_settings import VoiceoverSettings
from voiceover import VoiceoverRun


class VoiceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = VoiceoverSettings()

    def make_script(self):
        with redirect_stdout(io.StringIO()):
            return make_script(self.root)

    def test_overlapping_synthesis_is_rejected_without_changing_winner(self):
        self.make_script()
        entered = threading.Event()
        release = threading.Event()
        winner_api, loser_api = FakeVoiceAPI(), FakeVoiceAPI()
        synthesize = winner_api.synthesize

        def pause_synthesis(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Test did not release synthesis")
            return synthesize(*args)

        winner_api.synthesize = pause_synthesis
        first = VoiceoverRun(self.root, self.settings, KEYS, winner_api)
        second = VoiceoverRun(self.root, self.settings, KEYS, loser_api)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(first.run)
            try:
                self.assertTrue(entered.wait(10))
                before = (self.root / "voiceover_state.json").read_bytes()
                with self.assertRaisesRegex(VoiceoverError, "уже выполняется"):
                    second.run()
                self.assertEqual((self.root / "voiceover_state.json").read_bytes(), before)
                self.assertEqual(loser_api.syntheses, [])
                self.assertEqual(loser_api.transcriptions, [])
                self.assertEqual(loser_api.confirmations, [])
            finally:
                release.set()
            self.assertEqual(running.result(timeout=10), self.root / "audio_manifest.json")

    def test_stale_instance_reloads_completed_paid_operations(self):
        self.make_script()
        first_api, stale_api = FakeVoiceAPI(), FakeVoiceAPI()
        first = VoiceoverRun(self.root, self.settings, KEYS, first_api)
        stale = VoiceoverRun(self.root, self.settings, KEYS, stale_api)
        first.run()
        stale.run()
        self.assertEqual(len(first_api.syntheses), 1)
        self.assertEqual(stale_api.syntheses, [])
        self.assertEqual(stale_api.transcriptions, [])
        self.assertEqual(stale_api.confirmations, [])
        self.assertEqual(stale_api.checks, [])

    def test_rechecks_share_the_voice_lock_and_stop_before_reading_inputs(self):
        actions = (
            lambda: reassess_saved(self.root, self.settings),
            lambda: recheck_saved(self.root, api=FakeVoiceAPI(), keys=KEYS, settings=self.settings),
            lambda: diagnose(self.root / "missing", self.root, "nova-2", KEYS, self.settings, FakeVoiceAPI()),
            lambda: review_saved(self.root, KEYS, self.settings, FakeVoiceAPI()),
        )
        with voice_lock(self.root):
            for action in actions:
                with self.subTest(action=action), self.assertRaisesRegex(VoiceoverError, "уже выполняется"):
                    action()
        self.assertFalse((self.root / "voiceover_state.json").exists())
        self.assertFalse((self.root / "audio_manifest.json").exists())

    def test_voice_lock_does_not_block_parallel_visual_worker(self):
        run = VoiceoverRun(self.root, self.settings, KEYS, FakeVoiceAPI())
        with run_lock(self.root), patch.object(run, "_run", return_value="completed"):
            self.assertEqual(run.run(), "completed")

    def test_lock_is_released_on_failure(self):
        run = VoiceoverRun(self.root, self.settings, KEYS, FakeVoiceAPI())
        with patch.object(run, "_run", side_effect=RuntimeError("test failure")):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                run.run()
        with patch.object(run, "_run", return_value="completed"):
            self.assertEqual(run.run(), "completed")

    def test_expected_script_version_is_checked_inside_locked_run(self):
        self.make_script()
        run = VoiceoverRun(self.root, self.settings, KEYS, FakeVoiceAPI())
        with patch.object(run, "_run") as execute:
            with self.assertRaisesRegex(VoiceoverError, "script changed"):
                run.run(expected_script_version_id="stale-version")
        execute.assert_not_called()
        self.assertFalse((self.root / "voiceover_state.json").exists())


if __name__ == "__main__":
    unittest.main()
