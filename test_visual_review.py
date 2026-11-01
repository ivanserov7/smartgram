"""Проверки кода Vision на подставном HTTP. Распознавание настоящих изображений здесь не тестируется."""

import base64
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

import main
import review_gallery
import visual_review
from commons_api import VisualError, parse_candidate
from image_files import inspect_image
from test_research import search_payload
from test_visuals import commons_page, image_bytes


class VisualReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.scene = {"id": 1, "narration": "Императорские пингвины на льду.",
                      "visual_description": "Живой взрослый императорский пингвин на снегу.",
                      "search_query": "emperor penguin", "material_type": "image"}
        self.candidates = []
        for number in (1, 2):
            content = image_bytes()
            path = self.directory / f"image_{number}.png"
            path.write_bytes(content)
            candidate = {**parse_candidate(commons_page(f"Test candidate {number}")),
                         **inspect_image(content), "candidate_id": number,
                         "analysis_image_path": path.name, "analysis_image_sha256": inspect_image(content)["file_sha256"],
                         "local_path": path.name, "eligible_for_video": True, "eligibility_reason": "",
                         "relevance_score": 0.6}
            self.candidates.append(candidate)
        self.checks = [
            {"candidate_id": 1, "observed_content": "Тестовое описание птицы.", "verdict": "suitable", "score": 90,
             "reason": "Подставное положительное заключение."},
            {"candidate_id": 2, "observed_content": "Тестовое описание здания.", "verdict": "unsuitable", "score": 0,
             "reason": "Подставное отрицательное заключение."},
        ]
        network = patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Real network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def request(self, handler):
        # Настоящий OpenAI SDK, без реального ключа и с подменённым транспортом.
        client = OpenAI(api_key="test-placeholder", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.object(visual_review, "load_settings", return_value=("test-placeholder", "test-model")), \
             patch.object(visual_review, "OpenAI", return_value=client):
            return visual_review.review_image_contents(self.scene, self.candidates, self.directory)

    def success(self, checks=None):
        payload = search_payload()
        payload["model"] = "test-exact-model"
        payload["output"] = [payload["output"][1]]
        payload["output"][0]["content"][0]["text"] = json.dumps({"checks": self.checks if checks is None else checks})
        payload["usage"] = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                            "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
        return httpx.Response(200, json=payload)

    def test_actual_bytes_and_scene_text_are_sent(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return self.success()

        result = self.request(handler)
        self.assertEqual(len(bodies), 1)
        body = bodies[0]
        parts = body["input"][0]["content"]
        images = [part for part in parts if part["type"] == "input_image"]
        self.assertEqual(len(images), 2)
        self.assertIn(self.scene["narration"], parts[0]["text"])
        for part, candidate in zip(images, self.candidates):
            self.assertEqual(base64.b64decode(part["image_url"].split(",", 1)[1]),
                             (self.directory / candidate["analysis_image_path"]).read_bytes())
            self.assertEqual(part["detail"], "high")
        self.assertNotIn("file_title", parts[0]["text"])
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertFalse(body["store"])
        self.assertEqual(result["model"], "test-exact-model")
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertEqual(result["usage"]["output_tokens"], 50)

    def test_success_and_explicit_rejection(self):
        result = self.request(lambda request: self.success())
        entry = visual_review.apply_assessments(self.scene, self.candidates, result)
        self.assertEqual(entry["selected"]["candidate_id"], 1)
        self.assertEqual(entry["candidates"][1]["status"], "rejected")
        self.assertIn("отрицательное", entry["candidates"][1]["reason"])

    def test_all_rejected_means_no_random_selection(self):
        for check in self.checks:
            check.update(verdict="unsuitable", score=0)
        result = self.request(lambda request: self.success())
        entry = visual_review.apply_assessments(self.scene, self.candidates, result)
        self.assertIsNone(entry["selected"])
        self.assertEqual(entry["status"], "needs_visual")

    def test_uncertainty_and_low_score_do_not_pass(self):
        for verdict, score in (("uncertain", 100), ("suitable", 74)):
            with self.subTest(verdict=verdict, score=score):
                self.checks[0].update(verdict=verdict, score=score)
                result = self.request(lambda request: self.success())
                self.assertIsNone(visual_review.apply_assessments(self.scene, self.candidates, result)["selected"])

    def test_ai_cannot_override_license_or_file_gate(self):
        self.candidates[0].update(eligible_for_video=False, eligibility_reason="Нет допустимой лицензии.")
        result = self.request(lambda request: self.success())
        entry = visual_review.apply_assessments(self.scene, self.candidates, result)
        self.assertIsNone(entry["selected"])
        self.assertIn("лицензии", entry["candidates"][0]["reason"])

    def test_missing_duplicate_and_unknown_ids_are_rejected(self):
        for checks in (self.checks[:1], self.checks + self.checks[:1], [{**self.checks[0], "candidate_id": 999}, self.checks[1]]):
            with self.subTest(checks=checks):
                result = self.request(lambda request: self.success(checks))
                self.assertEqual(result["status"], "invalid_response")
                self.assertIsNone(visual_review.apply_assessments(self.scene, self.candidates, result)["selected"])

    def test_error_is_saved_without_secret_and_without_retry(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(429, json={"error": {"code": "credit_balance_exhausted", "type": "insufficient_quota",
                                                       "message": "No credits. test-placeholder"}})

        result = self.request(handler)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "api_error")
        self.assertIn("credit_balance_exhausted", result["error"])
        self.assertNotIn("test-placeholder", result["error"])

    def test_timeout_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("test", request=request)

        result = self.request(handler)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "api_error")

    def test_invalid_json_keeps_usage_and_does_not_select(self):
        def handler(request):
            payload = json.loads(self.success().content)
            payload["output"][0]["content"][0]["text"] = "not JSON"
            return httpx.Response(200, json=payload)
        result = self.request(handler)
        self.assertEqual(result["status"], "invalid_response")
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertIsNone(visual_review.apply_assessments(self.scene, self.candidates, result)["selected"])

    def test_preparation_rechecks_license_instead_of_trusting_flag(self):
        self.candidates[0].update(license="Unknown", license_url="", license_id=None, eligible_for_video=True)
        entry = {"scene_id": 1, "candidates": self.candidates, "selected": None}
        client = httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("Unexpected download")))
        with patch.object(visual_review, "create_commons_client", return_value=client):
            prepared = visual_review.prepare_candidates(entry, self.directory)
        self.assertFalse(prepared[0]["eligible_for_video"])
        self.assertTrue(prepared[1]["eligible_for_video"])

    def test_changed_file_blocks_request(self):
        (self.directory / self.candidates[0]["analysis_image_path"]).write_bytes(image_bytes(size=(900, 1400)))
        with patch.object(visual_review, "OpenAI", side_effect=AssertionError("API called")):
            with self.assertRaisesRegex(VisualError, "изменился"):
                visual_review.review_image_contents(self.scene, self.candidates, self.directory)

    def test_damaged_metadata_raises_clear_error_before_api(self):
        with patch.object(visual_review, "OpenAI", side_effect=AssertionError("API called")):
            with self.assertRaisesRegex(VisualError, "метаданные"):
                visual_review.prepare_candidates({"candidates": [{"file_title": "broken"}]}, self.directory)
            with self.assertRaisesRegex(VisualError, "путь"):
                visual_review.local_image(self.directory, None)

    def test_demo_never_calls_api(self):
        plan = {"mode": "demo", "scenes": [self.scene]}
        (self.directory / "scenes.json").write_text(json.dumps(plan))
        (self.directory / "visual_manifest.json").write_text(json.dumps({"mode": "demo", "scenes": []}))
        with patch.object(visual_review, "review_image_contents", side_effect=AssertionError("API called")), redirect_stdout(StringIO()):
            path = visual_review.review_visuals(self.directory / "scenes.json", demo=True)
        report = json.loads(path.read_text())
        self.assertEqual(report["status"], "needs_visual")
        self.assertEqual(report["mode"], "demo")
        self.assertIsNone(report["scenes"][0]["selected"])

    def test_ai_gallery_contains_model_tokens_and_explanations(self):
        result = self.request(lambda request: self.success())
        entry = visual_review.apply_assessments(self.scene, self.candidates, result)
        previews = {candidate["file_title"]: {"status": "downloaded", "local_path": candidate["analysis_image_path"]}
                    for candidate in self.candidates}
        page = review_gallery.render_gallery({"selection_method": "openai_vision", "scenes": [entry]}, self.directory, previews)
        self.assertIn("OpenAI получил сами изображения", page)
        self.assertIn("test-exact-model", page)
        self.assertIn("входных токенов: 100", page)
        self.assertIn("Подставное отрицательное", page)
        self.assertEqual(page.count('data:image/'), 3)

    def test_new_dry_run_flag_does_not_read_or_call(self):
        with patch("sys.argv", ["main.py", "--review-visuals-file", "/missing/scenes.json", "--dry-run"]), \
             patch.object(main, "review_visuals", side_effect=AssertionError("API called")), redirect_stdout(StringIO()):
            self.assertEqual(main.main(), 0)


if __name__ == "__main__":
    unittest.main()
