"""Тестовые WAV содержат тишину. SDK и файлы проверяются без настоящей речи и сети."""

import base64
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx

import audio_files
import build_review_archive as builder
import main
import tts_check
from voice_errors import VoiceoverError
try:
    import gemini_tts
    import legacy_voiceover as voiceover
    from gemini_tts import VoiceSettings
except ImportError:
    gemini_tts = voiceover = VoiceSettings = None


def test_wav():
    """Только 0,25 с тишины для проверки контейнера WAV."""
    return audio_files.demo_wav("", None, "")


def speech_response(content=None, status="completed"):
    return {"id": "test", "status": status, "steps": [{"type": "model_output", "content": [{
        "type": "audio", "data": base64.b64encode(test_wav() if content is None else content).decode(),
        "mime_type": "audio/wav",
    }]}]}


@unittest.skipIf(gemini_tts is None, "Не установлен необязательный Google SDK для исторических тестов")
class VoiceoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "scenes.json"
        self.plan = {"mode": "real", "verification_status": "passed", "scenes": [
            {"id": 1, "narration": "  Привет,\nмир!  ", "estimated_duration_seconds": 9},
            {"id": 2, "narration": "Это вторая сцена.", "estimated_duration_seconds": 7},
            {"id": 3, "narration": "Это завершение.", "estimated_duration_seconds": 6},
        ]}
        self.save_plan()
        self.settings = VoiceSettings()
        self.settings_mock = patch.object(voiceover, "load_voice_settings", return_value=("test-google-not-secret", self.settings)).start()
        self.addCleanup(patch.stopall)
        self.synthesize = patch.object(voiceover, "synthesize_wav", return_value=test_wav()).start()
        patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Real network forbidden")).start()
        self.output = redirect_stdout(StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def save_plan(self):
        self.path.write_text(json.dumps(self.plan, ensure_ascii=False))

    def run_voiceover(self):
        voiceover.voiceover_from_file(self.path)
        return json.loads((self.directory / "audio_manifest.json").read_text())

    def test_text_and_measured_durations_preserved(self):
        manifest = self.run_voiceover()
        self.assertEqual(self.synthesize.call_count, 3)
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["total_duration_seconds"], 0.75)
        for scene, entry, call in zip(self.plan["scenes"], manifest["scenes"], self.synthesize.call_args_list):
            self.assertEqual(entry["narration"], scene["narration"])
            self.assertEqual(call.args[0], scene["narration"])
            self.assertEqual(call.args[1], self.settings)
            self.assertEqual(entry["duration_seconds"], 0.25)
            self.assertEqual(entry["model"], self.settings.model)
            self.assertEqual(entry["voice"], self.settings.voice)
            self.assertTrue((self.directory / entry["audio_path"]).is_file())
        self.assertNotIn("test-google-not-secret", (self.directory / "audio_manifest.json").read_text())

    def test_unchanged_audio_reused_without_api(self):
        self.run_voiceover()
        self.synthesize.reset_mock()
        manifest = self.run_voiceover()
        self.synthesize.assert_not_called()
        self.assertTrue(all(entry["reused"] for entry in manifest["scenes"]))

    def test_text_change_regenerates_only_changed_scene(self):
        self.run_voiceover()
        self.synthesize.reset_mock()
        self.plan["scenes"][0]["narration"] += " "
        self.save_plan()
        manifest = self.run_voiceover()
        self.synthesize.assert_called_once()
        self.assertEqual([entry["reused"] for entry in manifest["scenes"]], [False, True, True])

    def test_changed_voice_model_or_style_invalidates_cache(self):
        self.run_voiceover()
        for field, value in (("voice", "Puck"), ("model", "other-test-model"), ("style", "Другой стиль")):
            with self.subTest(field=field):
                self.synthesize.reset_mock()
                self.settings_mock.return_value = ("test", replace(self.settings, **{field: value}))
                manifest = self.run_voiceover()
                self.assertEqual(self.synthesize.call_count, 3)
                self.assertFalse(any(entry["reused"] for entry in manifest["scenes"]))

    def test_corrupt_audio_or_metadata_is_regenerated(self):
        for damage in ("truncated", "changed_bytes", "missing_metadata", "bad_metadata"):
            with self.subTest(damage=damage):
                manifest = self.run_voiceover()
                path = self.directory / manifest["scenes"][0]["audio_path"]
                if damage == "truncated":
                    path.write_bytes(path.read_bytes()[:-20])
                elif damage == "changed_bytes":
                    content = path.read_bytes()
                    path.write_bytes(content[:-1] + b"\x01")
                elif damage == "missing_metadata":
                    path.with_suffix(".json").unlink()
                else:
                    path.with_suffix(".json").write_text("[]")
                self.synthesize.reset_mock()
                self.run_voiceover()
                self.synthesize.assert_called_once()

    def test_partial_failure_saved_and_resume_skips_completed_scene(self):
        def fail_second(narration, settings, key):
            if narration == self.plan["scenes"][1]["narration"]:
                saved = json.loads((self.directory / "audio_manifest.json").read_text())
                self.assertEqual(len(saved["scenes"]), 1)
                raise VoiceoverError("Тестовый сбой")
            return test_wav()

        self.synthesize.side_effect = fail_second
        with self.assertRaisesRegex(VoiceoverError, "сцены 2"):
            self.run_voiceover()
        partial = json.loads((self.directory / "audio_manifest.json").read_text())
        self.assertEqual(partial["status"], "error")
        self.assertEqual(partial["failed_scene_id"], 2)
        self.assertEqual(len(partial["scenes"]), 1)
        self.assertEqual(self.synthesize.call_count, 2)
        self.synthesize.reset_mock(side_effect=True)
        manifest = self.run_voiceover()
        self.assertEqual(self.synthesize.call_count, 2)
        self.assertTrue(manifest["scenes"][0]["reused"])

    def test_manifest_can_be_rebuilt_from_scene_receipts(self):
        self.run_voiceover()
        (self.directory / "audio_manifest.json").write_text("broken")
        self.synthesize.reset_mock()
        manifest = self.run_voiceover()
        self.synthesize.assert_not_called()
        self.assertEqual(len(manifest["scenes"]), 3)

    def test_duration_in_metadata_not_trusted(self):
        manifest = self.run_voiceover()
        path = (self.directory / manifest["scenes"][0]["audio_path"]).with_suffix(".json")
        metadata = json.loads(path.read_text())
        metadata["duration_seconds"] = 999
        path.write_text(json.dumps(metadata))
        self.synthesize.reset_mock()
        manifest = self.run_voiceover()
        self.assertEqual(manifest["scenes"][0]["duration_seconds"], 0.25)
        self.synthesize.assert_not_called()

    def test_invalid_generated_wav_saves_error_without_retry(self):
        self.synthesize.return_value = b"not a WAV"
        with self.assertRaisesRegex(VoiceoverError, "сцены 1"):
            self.run_voiceover()
        self.synthesize.assert_called_once()
        manifest = json.loads((self.directory / "audio_manifest.json").read_text())
        self.assertEqual(manifest["status"], "error")
        self.assertEqual(manifest["scenes"], [])
        self.assertFalse(list(self.directory.glob("audio/*.wav")))

    def test_invalid_scenes_never_call_api(self):
        examples = [
            {**self.plan, "verification_status": "needs_revision"},
            {**self.plan, "mode": "demo"}, {**self.plan, "scenes": []},
            {**self.plan, "scenes": [{"id": 1, "narration": " "}]},
            {**self.plan, "scenes": [{"id": True, "narration": "test"}]},
            {**self.plan, "scenes": [{"id": 1, "narration": "one"}, {"id": 1, "narration": "two"}]},
        ]
        for plan in examples:
            with self.subTest(plan=plan):
                self.path.write_text(json.dumps(plan))
                with self.assertRaises(VoiceoverError):
                    voiceover.voiceover_from_file(self.path)
        self.synthesize.assert_not_called()

    def test_demo_is_silence_and_cannot_be_reused_as_real(self):
        self.plan.update(mode="demo", verification_status="demo_passed")
        self.save_plan()
        voiceover.voiceover_from_file(self.path, demo=True)
        manifest = json.loads((self.directory / "audio_manifest.json").read_text())
        self.assertEqual(manifest["mode"], "demo")
        self.assertEqual(manifest["audio_kind"], "test_silence")
        self.synthesize.assert_not_called()
        self.settings_mock.assert_not_called()
        self.plan.update(mode="real", verification_status="passed")
        self.save_plan()
        self.run_voiceover()
        self.assertEqual(self.synthesize.call_count, 3)

    def test_old_plan_requires_matching_script_and_report(self):
        self.plan.pop("verification_status")
        for scene in self.plan["scenes"]:
            scene["line_ids"] = [scene["id"]]
        self.save_plan()
        script = {"mode": "real", "verification_status": "passed", "lines": [
            {"id": scene["id"], "text": scene["narration"]} for scene in self.plan["scenes"]]}
        (self.directory / "script.json").write_text(json.dumps(script))
        (self.directory / "verification.json").write_text(json.dumps({"status": "passed", "mode": "real"}))
        self.run_voiceover()
        self.plan["scenes"][0]["narration"] = "Непроверенное изменение"
        self.save_plan()
        self.synthesize.reset_mock()
        with self.assertRaises(VoiceoverError):
            self.run_voiceover()
        self.synthesize.assert_not_called()

    def test_scenes_dry_run_does_not_read_files(self):
        with patch("sys.argv", ["main.py", "--scenes-file", "/missing/scenes.json", "--dry-run"]), \
             patch.object(main, "voiceover_from_file", side_effect=AssertionError("Audio called")):
            self.assertEqual(main.main(), 0)


@unittest.skipIf(gemini_tts is None, "Не установлен необязательный Google SDK для исторических тестов")
class GeminiSDKTests(unittest.TestCase):
    def run_with_http(self, handler):
        """Настоящий SDK и его настройки; заменяется только HTTP-транспорт."""
        original_client = httpx.Client

        def make_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        with patch.object(gemini_tts.httpx, "Client", side_effect=make_client):
            return gemini_tts.synthesize_wav("  Привет!\nЭто проверка.  ", VoiceSettings(), "test-google-placeholder")

    def test_official_sdk_request_and_wav(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            self.assertIn("interactions", str(request.url))
            return httpx.Response(200, json=speech_response())

        result = self.run_with_http(handler)
        self.assertEqual(audio_files.inspect_wav(result)["duration_seconds"], 0.25)
        self.assertEqual(len(bodies), 1)
        body = bodies[0]
        text = body["input"][0]["content"][0]
        self.assertEqual(text["text"], "  Привет!\nЭто проверка.  ")
        self.assertEqual(text["annotations"][0]["style"], VoiceSettings().style)
        self.assertEqual(body["generation_config"]["speech_config"], [{"voice": "Kore"}])
        self.assertEqual(body["model"], VoiceSettings().model)
        self.assertEqual(body["response_format"]["mime_type"], "audio/wav")
        self.assertFalse(body["store"])
        self.assertFalse(body["stream"])

    def test_http_errors_never_retry_or_leak_response(self):
        for status in (400, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                calls = []

                def handler(request):
                    calls.append(request)
                    return httpx.Response(status, json={"error": {"code": status, "message": "DO-NOT-PRINT"}})

                with self.assertRaises(VoiceoverError) as caught:
                    self.run_with_http(handler)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("DO-NOT-PRINT", str(caught.exception))
                self.assertNotIn("test-google-placeholder", str(caught.exception))

    def test_timeout_does_not_retry(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("DO-NOT-PRINT", request=request)

        with self.assertRaises(VoiceoverError):
            self.run_with_http(handler)
        self.assertEqual(len(calls), 1)

    def test_incomplete_or_empty_audio_rejected(self):
        for response in (speech_response(status="requires_continuation"), {"status": "completed"}, speech_response(content=b"")):
            calls = []

            def handler(request):
                calls.append(request)
                return httpx.Response(200, json=response)

            with self.subTest(response=response), self.assertRaises(VoiceoverError):
                self.run_with_http(handler)
            self.assertEqual(len(calls), 1)

    def test_missing_google_key_does_not_require_openai(self):
        with patch.object(gemini_tts, "dotenv_values", return_value={}), \
             patch.object(gemini_tts.genai, "Client") as client:
            with self.assertRaisesRegex(VoiceoverError, "GEMINI_API_KEY"):
                gemini_tts.load_voice_settings()
            client.assert_not_called()

    def test_empty_voice_settings_rejected_before_sdk(self):
        for name in ("GEMINI_TTS_MODEL", "GEMINI_TTS_VOICE", "GEMINI_TTS_STYLE"):
            with self.subTest(name=name), \
                 patch.object(gemini_tts, "dotenv_values", return_value={"GEMINI_API_KEY": "test", name: " "}):
                with self.assertRaisesRegex(VoiceoverError, name):
                    gemini_tts.load_voice_settings()

    def test_google_key_is_the_only_required_setting(self):
        with patch.object(gemini_tts, "dotenv_values", return_value={"GEMINI_API_KEY": "test-google-placeholder"}):
            key, settings = gemini_tts.load_voice_settings()
        self.assertEqual(key, "test-google-placeholder")
        self.assertEqual(settings, VoiceSettings())



class ArchiveSettingsTests(unittest.TestCase):
    def test_extended_example_is_safe(self):
        text = (builder.PROJECT_DIRECTORY / ".env.example").read_text()
        builder.validate_env_example(text)
        builder.validate_env_example(text + "\nFUTURE_API_KEY=your_api_key_here\nAUDIO_SAMPLE_RATE=24000\n")

    def test_non_placeholder_keys_are_rejected(self):
        for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "FUTURE_SECRET"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                builder.validate_env_example(f"{name}=not-a-placeholder")

    def test_known_key_formats_rejected_even_in_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "example.py"
            path.write_text("# " + "AIza" + "x" * 35)
            with self.assertRaises(ValueError):
                builder.read_safe_content(path)


if __name__ == "__main__":
    unittest.main()
