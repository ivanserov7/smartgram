"""Регрессии Gemini на подставных ответах. Ни одно тестовое аудио не является речью."""

import base64
import copy
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from audio_files import inspect_wav
from comparison_audio import pcm_wav
from file_io import atomic_write_json
from gemini_voice import GeminiVoiceAPI
from test_full_voiceover import FakeVoiceAPI, KEYS, make_script, wav_bytes
from voice_alignment import validate_check
from voice_errors import AudioContractError, VoiceoverError, VoiceServiceError
from voice_factory import create_voice_api
from voice_settings import VoiceoverSettings, load_voiceover_settings
from voiceover import VoiceoverRun


class GeminiMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def sdk_response(self, wav):
        return {
            "id": "test-gemini-response",
            "model": "gemini-3.8-flash-tts",
            "status": "completed",
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "audio",
                            "mime_type": "audio/wav",
                            "data": base64.b64encode(wav).decode(),
                        }
                    ],
                }
            ],
            "usage": {
                "total_input_tokens": 100,
                "total_output_tokens": 25,
                "total_tokens": 125,
            },
        }

    def test_real_sdk_payload_keeps_exact_text_style_separate_and_ready_wav_bytes(self):
        wav = pcm_wav(b"\x01\x00" * 48000, rate=48000)
        sent = []

        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(
                200,
                json=self.sdk_response(wav),
                headers={"x-request-id": "test-google-id"},
            )

        original = httpx.Client

        def client_factory(**options):
            return original(transport=httpx.MockTransport(handler), **options)

        settings = VoiceoverSettings()
        with patch("gemini_voice.httpx.Client", side_effect=client_factory):
            content, meta = GeminiVoiceAPI(self.root, settings, KEYS).synthesize(
                {"narration_text": "Как GPS работает?", "script_version_id": "test"},
                self.root,
            )
        self.assertEqual(content, wav)
        self.assertEqual(meta["sample_rate"], 48000)
        self.assertEqual(meta["request_id"], "test-google-id")
        part = sent[0]["input"][0]["content"][0]
        self.assertEqual(part["text"], "Как GPS работает?")
        self.assertEqual(part["annotations"][0]["style"], settings.tts_style)
        self.assertEqual(sent[0]["response_format"], {"type": "audio"})
        self.assertEqual(len(sent), 1)
        self.assertTrue((self.root / "gemini_usage.json").exists())
        self.assertFalse((self.root / "openai_usage.json").exists())

    def test_factory_defaults_are_gemini_and_explicit_stale_openai_is_visible(self):
        settings = VoiceoverSettings()
        self.assertIsInstance(
            create_voice_api(self.root, settings, KEYS), GeminiVoiceAPI
        )
        env = self.root / ".env"
        env.write_text(
            "VOICE_TTS_PROVIDER=openai\nVOICE_TTS_API=realtime\nVOICE_TTS_MODEL=gpt-realtime-2.1-mini\nVOICE_TTS_VOICE=marin\n"
        )
        with patch.dict("os.environ", {}, clear=True):
            _, loaded = load_voiceover_settings(env, require_keys=False)
        self.assertEqual(loaded.tts_provider, "openai")
        self.assertNotIsInstance(
            create_voice_api(self.root, loaded, KEYS), GeminiVoiceAPI
        )

    def test_provider_style_and_voice_invalidate_tts_but_check_prompt_only_does_not(
        self,
    ):
        script = make_script(self.root)
        api = FakeVoiceAPI()
        base = VoiceoverSettings()
        VoiceoverRun(self.root, base, KEYS, api).run()
        VoiceoverRun(
            self.root, replace(base, tts_style="Другой стиль."), KEYS, api
        ).run()
        self.assertEqual(len(api.syntheses), 2)
        prompt = self.root / "check.txt"
        prompt.write_text("Новая проверка без изменения речи.")
        VoiceoverRun(
            self.root, replace(base, check_prompt_file=str(prompt)), KEYS, api
        ).run()
        self.assertEqual(len(api.syntheses), 2)
        VoiceoverRun(
            self.root,
            replace(
                base,
                tts_provider="openai",
                tts_api="realtime",
                tts_model="gpt-realtime-2.1-mini",
            ),
            KEYS,
            api,
        ).run()
        self.assertEqual(len(api.syntheses), 3)

    def review(self, result="minor_asr_variation", decision="Можно использовать"):
        return {
            "verification_version": 2,
            "decision": decision,
            "issues": [],
            "fragments": [{"fragment_index": 1, "first_word_id": 1, "last_word_id": 2}],
            "assessments": [
                {
                    "fragment_index": 1,
                    "result": result,
                    "deepgram_supports_script": False,
                    "openai_supports_script": True,
                    "same_material_error_in_both": False,
                    "changes_meaning": False,
                    "explanation": "Deepgram Ак-пирамида; OpenAI и сценарий Так пирамида. Ошибка ASR.",
                }
            ],
        }

    def test_ak_pyramid_accepted_without_requiring_equal_transcripts(self):
        script = {"fragments": [{"text": "Так пирамида", "visual": "Пирамида"}]}
        words = [{"start": 0.0, "end": 1.0}, {"start": 1.5, "end": 2.0}]
        review, mapped = validate_check(self.review(), script, words, 3, confirmed=True)
        self.assertEqual(review["decision"], "Можно использовать")
        self.assertEqual(mapped[-1]["display_end"], 3)

    def test_number_or_negation_and_contradictory_consensus_are_rejected(self):
        script = {"fragments": [{"text": "Не четыре спутника", "visual": "Схема"}]}
        words = [{"start": 0.0, "end": 1.0}, {"start": 1.5, "end": 2.0}]
        for value in (self.review(decision="Есть расхождения"), self.review()):
            if value["decision"] == "Можно использовать":
                value["assessments"][0]["changes_meaning"] = True
            with self.assertRaises(AudioContractError):
                validate_check(value, script, words, 3, confirmed=True)

    def test_every_wav_has_independent_asr_even_when_deepgram_times_fail(self):
        make_script(self.root)
        api = FakeVoiceAPI()
        original = api.transcribe

        def bad(*args):
            raw = original(*args)
            raw["results"]["channels"][0]["alternatives"][0]["words"][-1]["end"] = 99
            return raw

        api.transcribe = bad
        with self.assertRaises(VoiceoverError):
            VoiceoverRun(self.root, VoiceoverSettings(), KEYS, api).run()
        self.assertEqual(len(api.confirmations), 1)
        self.assertEqual(len(api.syntheses), 1)
        self.assertFalse(api.checks)

    def test_comparison_mode_never_rewrites_or_regenerates_for_duration(self):
        make_script(self.root)
        api = FakeVoiceAPI(durations=(46,))
        with self.assertRaisesRegex(VoiceoverError, "не переписывался"):
            VoiceoverRun(self.root, VoiceoverSettings(), KEYS, api).run(
                comparison_mode=True
            )
        manifest = json.loads((self.root / "audio_manifest.json").read_text())
        self.assertEqual(manifest["status"], "duration_out_of_range")
        self.assertTrue(manifest["text_verified"])
        self.assertEqual(len(api.syntheses), 1)

    def test_deepgram_negative_cannot_veto_explicit_openai_confirmation(self):
        make_script(self.root)
        api = FakeVoiceAPI()

        def contradictory(script, *args, **kwargs):
            result = self.review(decision="Есть расхождения")
            result["fragments"] = [
                {"fragment_index": i, "first_word_id": i, "last_word_id": i}
                for i in (1, 2)
            ]
            result["assessments"] = [
                {**result["assessments"][0], "fragment_index": i} for i in (1, 2)
            ]
            return result

        api.check = contradictory
        path=VoiceoverRun(self.root, VoiceoverSettings(), KEYS, api).run()
        self.assertEqual(json.loads(path.read_text())['status'],'completed')
        self.assertEqual(len(api.syntheses), 1)

    def test_service_retry_is_bounded_and_billing_failure_not_retried(self):
        settings = VoiceoverSettings()
        run = VoiceoverRun(self.root, settings, KEYS, FakeVoiceAPI())
        with patch("voiceover.time.sleep"), patch.object(
            run.api,
            "confirm",
            side_effect=[VoiceServiceError("HTTP 503", status=503), {"text": "ok"}],
        ) as call:
            result = run.operation(
                "audio_confirm",
                {"test": 1},
                {},
                lambda: run.api.confirm(None, None, None),
            )
            self.assertEqual(call.call_count, 2)
        with patch.object(
            run.api,
            "confirm",
            side_effect=VoiceServiceError(
                "quota", status=429, details={"code": "insufficient_quota"}
            ),
        ) as call:
            with self.assertRaises(VoiceoverError):
                run.operation(
                    "audio_confirm",
                    {"test": 2},
                    {},
                    lambda: run.api.confirm(None, None, None),
                )
            self.assertEqual(call.call_count, 1)

    def test_explicit_new_asr_round_never_calls_synthesis_or_scriptwriter(self):
        from voice_recheck import recheck_saved

        make_script(self.root)
        api = FakeVoiceAPI()
        settings = VoiceoverSettings()
        VoiceoverRun(self.root, settings, KEYS, api).run()
        before = (self.root / "script_state.json").read_bytes()
        recheck_saved(self.root, new_round=True, api=api, keys=KEYS, settings=settings)
        self.assertEqual(len(api.syntheses), 1)
        self.assertEqual(len(api.transcriptions), 2)
        self.assertEqual(len(api.confirmations), 2)
        recheck_saved(self.root, api=api, keys=KEYS, settings=settings)
        self.assertEqual(len(api.transcriptions), 2)
        self.assertEqual((self.root / "script_state.json").read_bytes(), before)

    def test_checker_self_contradiction_rechecks_only_gpt_not_tts_or_asr(self):
        make_script(self.root)
        api = FakeVoiceAPI()
        original = api.check

        def check(
            script,
            transcription,
            audio_hash,
            confirmation=None,
            validation_feedback=None,
        ):
            result = original(script, transcription, audio_hash, confirmation)
            if len(api.checks) == 1:
                result["decision"] = "Есть расхождения"
                expected = script["fragments"][1]["text"]
                result["issues"] = [
                    {
                        "fragment_index": 2,
                        "expected": expected,
                        "observed_deepgram": "иная фраза",
                        "observed_openai": expected.replace("ё", "е"),
                        "reason": "ТЕСТ: модель ошибочно утверждает, что обе ASR подтверждают ошибку.",
                    }
                ]
                result["assessments"][1].update(
                    result="confirmed_material_error",
                    deepgram_supports_script=False,
                    openai_supports_script=False,
                    same_material_error_in_both=True,
                    changes_meaning=True,
                )
            else:
                self.assertIsNotNone(validation_feedback)
            return result

        api.check = check
        VoiceoverRun(self.root, VoiceoverSettings(), KEYS, api).run()
        self.assertEqual(len(api.syntheses), 1)
        self.assertEqual(len(api.transcriptions), 1)
        self.assertEqual(len(api.confirmations), 1)
        self.assertEqual(len(api.checks), 2)
        self.assertTrue(
            list((self.root / "voiceover").glob("*/check_1_contract_error.json"))
        )

    def test_material_error_requires_quotes_from_actual_asr_text(self):
        check = self.review(
            result="confirmed_material_error", decision="Есть расхождения"
        )
        check["assessments"][0].update(
            deepgram_supports_script=False,
            openai_supports_script=False,
            same_material_error_in_both=True,
            changes_meaning=True,
        )
        check["issues"] = [
            {
                "fragment_index": 1,
                "expected": "четыре",
                "observed_deepgram": "пять",
                "observed_openai": "пять",
                "reason": "ТЕСТ: выдуманная цитата ASR",
            }
        ]
        with self.assertRaisesRegex(AudioContractError, "цитатой"):
            validate_check(
                check,
                {"fragments": [{"text": "четыре"}]},
                [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}],
                3,
                confirmed=True,
                transcripts=("шесть", "шесть"),
            )


if __name__ == "__main__":
    unittest.main()
