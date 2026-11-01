"""Регрессии на подставных WAV/ответах: сеть и настоящая речь здесь не используются."""

import copy
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from asr_diagnostics import audit_audio, diagnose, review_saved
from audio_files import inspect_wav
from file_io import atomic_write_json
from test_full_voiceover import FakeVoiceAPI, KEYS, make_script, wav_bytes
from timing_report import build_report
from voice_alignment import validate_check, validate_words
from voice_api import VoiceAPI
from voice_errors import VoiceoverError
from voice_settings import VoiceoverSettings


class DiagnosticAPI(FakeVoiceAPI):
    def transcribe(self, path, script_id, audio_hash):
        result = super().transcribe(path, script_id, audio_hash)
        result["metadata"].update(sha256=audio_hash, request_id="new-test-response")
        return result


class TimingDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.script = make_script(self.source)
        self.original = self.source / "voiceover" / "original"
        self.original.mkdir(parents=True)
        self.path = self.original / "audio.wav"
        self.path.write_bytes(wav_bytes(35))
        self.audio = {**inspect_wav(self.path.read_bytes()), "audio_path": "voiceover/original/audio.wav"}
        atomic_write_json(self.source / "audio_manifest.json", self.audio)
        atomic_write_json(self.original / "audio.json", self.audio)
        raw = DiagnosticAPI().transcribe(self.path, "script", self.audio["audio_sha256"])
        raw["metadata"]["request_id"] = "old-test-response"
        raw["results"]["channels"][0]["alternatives"][0]["words"][-1]["end"] = 37
        atomic_write_json(self.original / "deepgram.json", raw)
        atomic_write_json(self.original / "words.json", {"words": [
            {**word, "word_id": index} for index, word in enumerate(raw["results"]["channels"][0]["alternatives"][0]["words"], 1)]})
        atomic_write_json(self.source / "deepgram_usage.json", {"requests": [{"request": {
            "audio_sha256": self.audio["audio_sha256"]}, "response": raw}]})
        self.destination = self.root / "diagnostic"
        self.settings = VoiceoverSettings()
        self.api = DiagnosticAPI()

    def diagnose(self, model="nova-2", settings=None):
        return diagnose(self.source, self.destination, model, KEYS, settings or self.settings, self.api)

    def test_audit_proves_same_wav_and_no_timestamp_transformation(self):
        path, audit = audit_audio(self.source)
        self.assertEqual(path, self.path.resolve())
        self.assertTrue(all(audit["hash_matches"].values()))
        self.assertTrue(audit["original_words_unchanged_by_conversion"])
        self.assertTrue(audit["receipt_duration_matches"])

    def test_diagnostic_is_fresh_then_resumes_without_paid_request(self):
        result = self.diagnose()
        self.assertEqual(result["metadata"]["request_id"], "new-test-response")
        self.assertTrue(result["timing"]["valid"])
        self.diagnose(settings=replace(self.settings, check_model="another-check-model"))
        self.assertEqual(len(self.api.transcriptions), 1)
        old = json.loads((self.original / "deepgram.json").read_text())
        self.assertEqual(old["metadata"]["request_id"], "old-test-response")
        self.assertEqual(old["results"]["channels"][0]["alternatives"][0]["words"][-1]["end"], 37)

    def test_other_model_cannot_overwrite_diagnostic(self):
        self.diagnose()
        with self.assertRaisesRegex(VoiceoverError, "другой диагностике"):
            self.diagnose("nova-3")
        self.assertEqual(len(self.api.transcriptions), 1)

    def test_failed_post_is_not_repeated(self):
        with patch.object(self.api, "transcribe", side_effect=VoiceoverError("ТЕСТ: timeout")) as request:
            for _ in range(2):
                with self.assertRaises(VoiceoverError):
                    self.diagnose()
            request.assert_called_once()

    def test_bad_source_hash_stops_before_request(self):
        self.path.write_bytes(wav_bytes(35, marker=2))
        with self.assertRaisesRegex(VoiceoverError, "хеши"):
            self.diagnose()
        self.assertFalse(self.api.transcriptions)

    def test_saved_review_never_synthesizes_rewrites_or_retranscribes(self):
        self.diagnose()
        before = self.path.read_bytes()
        first = review_saved(self.destination, KEYS, self.settings, self.api)
        self.assertEqual(first["status"], "completed")
        second = review_saved(self.destination, KEYS, self.settings, self.api)
        self.assertEqual(first, second)
        self.assertEqual(len(self.api.transcriptions), 1)
        self.assertEqual(len(self.api.checks), 1)
        self.assertFalse(self.api.syntheses)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(first["fragments"][-1]["speech_end"], 34)

    def test_reading_error_kept_separate_from_valid_timing_without_regeneration(self):
        self.api = DiagnosticAPI(decisions=("Есть расхождения",))
        self.diagnose()
        result = review_saved(self.destination, KEYS, self.settings, self.api)
        self.assertTrue(result["timing_validated"])
        self.assertFalse(result["text_verified"])
        self.assertEqual(result["status"], "needs_review")
        self.assertFalse(result["fragments"])
        self.assertEqual(len(result["provisional_fragments"]), 2)
        self.assertFalse(self.api.syntheses)
        self.assertEqual(len(self.api.confirmations), 1)

    def test_invalid_timing_never_clipped_or_sent_to_text_check(self):
        self.diagnose()
        raw = json.loads((self.destination / "deepgram.json").read_text())
        raw["results"]["channels"][0]["alternatives"][0]["words"][-1]["end"] = 35.001
        atomic_write_json(self.destination / "deepgram.json", raw)
        with self.assertRaises(VoiceoverError):
            review_saved(self.destination, KEYS, self.settings, self.api)
        self.assertFalse(self.api.checks)
        self.assertEqual(json.loads((self.destination / "deepgram.json").read_text()), raw)
        with self.assertRaises(VoiceoverError):
            validate_words(raw, 35, .02)
        words = copy.deepcopy(raw["results"]["channels"][0]["alternatives"][0]["words"])
        check = DiagnosticAPI().check(self.script, {}, "hash")
        with self.assertRaises(VoiceoverError):
            validate_check(check, self.script, words, 35, .02)

    def test_changed_upload_hash_never_reaches_http(self):
        with patch("voice_api.httpx.Client") as client:
            with self.assertRaisesRegex(VoiceoverError, "изменился перед отправкой"):
                VoiceAPI(self.root, self.settings, KEYS).transcribe(self.path, "script", "wrong-hash")
            client.assert_not_called()

    def test_report_embeds_wavs_preserves_histories_and_distinguishes_text(self):
        self.diagnose()
        review_saved(self.destination, KEYS, self.settings, self.api)
        for index in (1, 2, 3):
            shutil.copytree(self.destination, self.root / f"topic_{index}_nova2")
        diagnose(self.source, self.root / "topic_3_nova3", "nova-3", KEYS, self.settings, DiagnosticAPI())
        report = build_report(self.root)
        page = report.read_text()
        self.assertEqual(page.count('<audio id="audio_'), 3)
        self.assertIn("data:audio/wav;base64,", page)
        self.assertIn("Текст Nova-3 не переносился", page)
        self.assertIn("Старый ответ Nova-3", page)
        summary = json.loads((self.root / "summary.json").read_text())
        self.assertEqual(summary["total_new_model_requests"], 0)
        self.assertEqual(summary["tts_requests"], 0)


if __name__ == "__main__":
    unittest.main()
