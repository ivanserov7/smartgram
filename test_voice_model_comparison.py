"""Подставные тоны и API: это тесты контракта, а не настоящие образцы русской речи."""

import io
import base64
import json
import math
import struct
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from comparison_audio import normalize_samples, pcm_wav, read_audio, save_audio
from voice_comparison_api import ComparisonAPI, ComparisonError
from voice_model_comparison import ComparisonRun, VARIANTS, estimate, main, PROJECT
from voice_settings import VoiceoverSettings


def tone(marker=1, rate=24000):
    pcm = b"".join(
        struct.pack("<h", int(2000 * math.sin(i * 0.1 * marker)))
        for i in range(rate // 10)
    )
    return pcm_wav(pcm, rate)


class FakeComparison:
    def __init__(self):
        self.generated, self.transcribed, self.checked = [], [], []
        self.fail = None

    def generate(self, variant, directory):
        self.generated.append(variant["id"])
        if variant["id"] == self.fail:
            raise ComparisonError(
                "ТЕСТ: exact model_not_found",
                status="unavailable",
                details={"code": "model_not_found"},
            )
        return tone(ord(variant["id"]) - 64), {
            "usage": {
                "input_tokens": 20,
                "output_tokens": 30,
                "output_token_details": {"audio_tokens": 20},
            }
        }

    def transcribe(self, path, directory):
        self.transcribed.append(path)
        return {"text": "Тестовый независимый текст."}, {"usage": {"seconds": 1}}

    def check(self, transcript, directory):
        self.checked.append(transcript)
        return {"status": "Совпадает", "full_text_present": True, "issues": []}, {
            "usage": {"input_tokens": 20, "output_tokens": 20}
        }


class VoiceComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "comparison"
        self.api = FakeComparison()
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def run_comparison(self):
        run = ComparisonRun(self.root, self.api, key="test-not-a-real-key")
        run.run()
        return run

    def test_eight_variants_resume_free_and_preserve_text_blind_order(self):
        run = self.run_comparison()
        self.assertEqual(len(self.api.generated), 8)
        self.assertEqual(len(self.api.transcribed), 8)
        self.assertEqual(len(self.api.checked), 8)
        order = run.state["blind_order"][:]
        for entry in run.state["entries"]:
            self.assertEqual(
                (self.root / entry["id"] / "source_text.txt").read_text(), run.text
            )
            self.assertLessEqual(entry["level_matched"]["peak"], 0.951)
        self.run_comparison()
        self.assertEqual(len(self.api.generated), 8)
        self.assertEqual(len(self.api.transcribed), 8)
        self.assertEqual(len(self.api.checked), 8)
        self.assertEqual(
            json.loads((self.root / "comparison.json").read_text())["blind_order"],
            order,
        )
        page = (self.root / "report.html").read_text()
        self.assertEqual(page.count("<article data-label="), 8)
        self.assertEqual(page.count('src="data:audio/wav;base64,'), 16)
        self.assertIn(".sensitive{display:none}", page)

    def test_unavailable_variant_has_own_reason_and_no_borrowed_audio(self):
        self.api.fail = "C"
        run = self.run_comparison()
        entry = run.state["entries"][2]
        self.assertEqual(entry["status"], "unavailable")
        self.assertIn("model_not_found", entry["error"])
        self.assertFalse((self.root / "C" / "original.wav").exists())
        self.assertEqual(len(self.api.transcribed), 7)

    def test_budget_blocks_before_api_and_unknown_cost_stays_reserved(self):
        run = ComparisonRun(self.root, self.api, key="test")
        run.state["operations"]["unknown"] = {
            "stage": "generation",
            "status": "failed",
            "attempts": [{"reserved_usd": 2, "estimated_usd": None}],
        }
        run.save()
        run.run()
        self.assertFalse(self.api.generated)
        self.assertTrue(
            all(e["status"] == "budget_blocked" for e in run.state["entries"])
        )

    def test_only_one_technical_retry_and_failed_attempt_is_not_free(self):
        run = ComparisonRun(self.root, self.api, key="test")
        function = MagicMock(
            side_effect=ComparisonError("ТЕСТ: timeout", technical=True)
        )
        with self.assertRaises(ComparisonError):
            run.operation("test", "generation", 0.3, function, "gpt-audio-1.5")
        self.assertEqual(function.call_count, 2)
        self.assertEqual(float(run.committed()), 0.6)
        with self.assertRaises(ComparisonError):
            run.operation("test", "generation", 0.3, function, "gpt-audio-1.5")
        self.assertEqual(function.call_count, 2)

    def test_text_disagreement_never_regenerates_audio(self):
        self.api.check = lambda *args: (
            {
                "status": "Есть расхождения в расшифровке",
                "full_text_present": False,
                "issues": [],
            },
            {"usage": None},
        )
        run = self.run_comparison()
        self.assertEqual(len(self.api.generated), 8)
        self.assertTrue(
            all(
                entry["status"] == "text_completeness_uncertain"
                for entry in run.state["entries"]
            )
        )

    def test_original_rate_and_bytes_preserved_and_linear_gain_no_clipping(self):
        self.root.mkdir()
        original = tone(rate=48000)
        measured = save_audio(self.root, original)
        self.assertEqual(measured["sample_rate"], 48000)
        self.assertEqual((self.root / "provider_response.wav").read_bytes(), original)
        entry = {"id": "x", "audio": measured}
        directory = self.root / "x"
        directory.mkdir()
        (directory / "original.wav").write_bytes(
            (self.root / "original.wav").read_bytes()
        )
        normalize_samples([entry], self.root)
        self.assertEqual(entry["level_matched"]["sample_rate"], 48000)
        self.assertEqual(
            entry["level_matched"]["duration_seconds"], measured["duration_seconds"]
        )
        self.assertLessEqual(entry["level_matched"]["peak"], 0.951)

    def test_speech_hd_omits_instructions_and_chat_uses_own_endpoint(self):
        api = ComparisonAPI(
            self.root,
            "test",
            VoiceoverSettings(),
            "Текст GPS.",
            "Инструкция.",
            "Проверка.",
        )
        with patch.object(api, "http", return_value=(tone(), {})) as call:
            api.generate(VARIANTS[6], self.root)
            self.assertNotIn("instructions", call.call_args.kwargs["body"])
            self.assertEqual(call.call_args.args[2], "audio/speech")
            api.generate(VARIANTS[5], self.root)
            self.assertEqual(
                call.call_args.kwargs["body"]["instructions"], "Инструкция."
            )

    def test_missing_usage_not_invented_and_hd_price_uses_characters(self):
        self.assertIsNone(estimate("gpt-4o-mini-tts", {"usage": None}))
        self.assertAlmostEqual(estimate("tts-1-hd", {}, "абв"), 0.00009)
        self.assertAlmostEqual(
            estimate(
                "gpt-live-1", {"usage": {"seconds": 60}, "session_open_seconds": 80}
            ),
            0.05,
        )

    def test_transcription_never_receives_expected_text_and_uses_russian(self):
        api = ComparisonAPI(
            self.root,
            "test",
            VoiceoverSettings(),
            "Секретный ожидаемый сценарий.",
            "Инструкция.",
            "Проверка.",
        )
        with patch.object(
            api, "http", return_value=({"text": "Ответ ASR."}, {})
        ) as call:
            api.transcribe(Path("audio.wav"), self.root)
        self.assertEqual(
            call.call_args.kwargs["body"],
            {"model": "gpt-transcribe", "languages[]": "ru"},
        )
        self.assertNotIn("Секретный", str(call.call_args))

    def test_live_has_separate_protocol_silence_input_graceful_close_and_final_usage(
        self,
    ):
        self.root.mkdir()
        api = ComparisonAPI(
            self.root,
            "test",
            VoiceoverSettings(),
            "Исходный текст.",
            "Инструкция.",
            "Проверка.",
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.response.headers = {}
        speech = struct.pack("<h", 2000) * 2400
        silence = bytes(48000 * 3)
        connection.recv.side_effect = [
            {
                "type": "session.started",
                "session": {"id": "live_test", "model": "gpt-live-1"},
            },
            {
                "type": "session.output_audio.delta",
                "delta": base64.b64encode(speech).decode(),
            },
            {
                "type": "session.output_audio.delta",
                "delta": base64.b64encode(silence).decode(),
            },
            {
                "type": "session.closed",
                "reason": "close_requested",
                "usage": {"seconds": 5},
                "session": {"id": "live_test", "model": "gpt-live-1"},
            },
        ]
        raw_events = connection.recv.side_effect
        connection.recv.side_effect = [json.dumps(event) for event in raw_events]
        clock = iter([0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 12])
        with patch(
            "voice_comparison_api.connect", return_value=connection
        ) as connect, patch("voice_comparison_api.threading.Thread"), patch(
            "voice_comparison_api.time.monotonic", side_effect=lambda: next(clock)
        ):
            content, metadata = api.live(VARIANTS[7], self.root)
        self.assertEqual(
            connect.call_args.args[0], "wss://api.openai.com/v1/live/sessions"
        )
        sent = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        self.assertEqual(
            [event["type"] for event in sent],
            ["session.start", "session.commentary.append", "session.close"],
        )
        self.assertEqual(
            sent[0]["session"]["input"][0]["content"][0]["text"], "Исходный текст."
        )
        self.assertEqual(sent[0]["session"]["instructions"], "Инструкция.")
        self.assertEqual(metadata["usage"]["seconds"], 5)
        self.assertEqual(metadata["backend_requests"], 0)
        self.assertEqual(read_audio(content)[2]["sample_rate"], 24000)

    def test_default_command_resumes_saved_experiment_instead_of_starting_new_paid_batch(
        self,
    ):
        project = self.root / "project"
        (project / "prompts").mkdir(parents=True)
        for name in (
            "comparison_text.txt",
            "prompts/comparison_narrator.txt",
            "prompts/comparison_check.txt",
        ):
            shutil.copyfile(PROJECT / name, project / name)
        (project / ".env").write_text("OPENAI_API_KEY=test-not-a-real-key\n")
        with patch("voice_model_comparison.PROJECT", project), patch(
            "voice_model_comparison.ComparisonAPI", return_value=self.api
        ):
            with patch("sys.argv", ["voice_model_comparison.py"]):
                self.assertEqual(main(), 0)
                self.assertEqual(main(), 0)
        self.assertEqual(len(self.api.generated), 8)
        self.assertEqual(len(self.api.transcribed), 8)
        self.assertEqual(len(self.api.checked), 8)

    def test_broken_resume_pointer_never_creates_new_paid_experiment(self):
        project = self.root / "project"
        (project / "outputs").mkdir(parents=True)
        (project / "outputs/voice_model_comparison_latest.json").write_text(
            json.dumps({"schema_version": 1, "directory": str(project / "missing")})
        )
        with patch("voice_model_comparison.PROJECT", project), patch(
            "voice_model_comparison.ComparisonAPI"
        ) as api, patch("sys.argv", ["voice_model_comparison.py"]):
            self.assertEqual(main(), 1)
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
