"""Проверки состояния, кеша и двух раундов поиска. Все HTTP-ответы подставные."""

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

import image_files
import scene_planner
import visual_review as review
import visual_retry
import visuals
from commons_api import VisualError, VisualServiceError, parse_candidate
from demo_data import DemoResponses, demo_research
from research import ResearchError
from scriptwriter import write_script
from test_research import search_payload
from test_visuals import commons_page, image_bytes
from vision_state import usage_history


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.path = self.folder / "scenes.json"
        self.scenes = [{"id": number, "narration": f"Текст сцены {number}",
                        "visual_description": "Красный закат над морем", "search_query": "red sunset sky",
                        "material_type": "image"} for number in (1, 2)]
        self.image_size = (1500, 2000)
        self.source = {"mode": "real", "scenes": []}
        for number in (1, 2):
            # Different sources and bytes are required by the one-source-per-
            # scene rule. Both fixtures also support a full 1080x1920 crop.
            size = tuple(value + (number - 1) * step for value, step in zip(self.image_size, (3, 4)))
            content = image_bytes(size)
            local_path = f"original_{number}.png"
            (self.folder / local_path).write_bytes(content)
            candidate = {
                **parse_candidate(commons_page(f"Red sunset sky {number}", width=size[0], height=size[1])),
                **image_files.inspect_image(content),
                "local_path": local_path, "relevance_score": .9,
            }
            self.source["scenes"].append({
                "scene_id": number, "search_query": "red sunset sky", "status": "selected",
                "search_rounds": 1, "candidates": [copy.deepcopy(candidate)], "selected": candidate,
            })
        self.save()
        self.calls = []
        for mocked in (
            patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Real network forbidden")),
            patch.object(review, "load_settings", return_value=("test-placeholder", "vision-model")),
            patch.object(review, "dotenv_values", return_value={"OPENAI_MODEL": "vision-model"}),
            patch.object(review, "OpenAI", side_effect=self.make_client),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        output = redirect_stdout(StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def save(self):
        self.path.write_text(json.dumps({"mode": "real", "scenes": self.scenes}))
        (self.folder / "visual_manifest.json").write_text(json.dumps(self.source))

    def handler(self, request):
        self.calls.append(json.loads(request.content))
        payload = search_payload()
        payload["id"] = f"response_{len(self.calls)}"
        payload["model"] = "exact-vision-model"
        payload["output"] = [payload["output"][1]]
        payload["output"][0]["content"][0]["text"] = json.dumps({"checks": [{
            "candidate_id": 1, "observed_content": "Подставная картинка", "verdict": "suitable",
            "score": 90, "reason": "Тестовое заключение"}]})
        payload["usage"] = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                            "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
        return httpx.Response(200, json=payload)

    def make_client(self, **kwargs):
        return OpenAI(api_key="test-placeholder", max_retries=0,
                      http_client=httpx.Client(transport=httpx.MockTransport(self.handler)))

    def run_review(self, scene_id=None):
        return json.loads(review.review_visuals(self.path, scene_id=scene_id).read_text())

    def test_partial_review_preserves_other_scene_and_global_status(self):
        first = self.run_review(1)
        self.assertEqual(first["status"], "needs_visual")
        second = self.run_review(2)
        self.assertEqual(second["status"], "ai_reviewed")
        saved_scene_two = copy.deepcopy(second["scenes"][1])
        third = self.run_review(1)
        self.assertEqual(len(third["scenes"]), 2)
        self.assertEqual(third["scenes"][1], saved_scene_two)
        self.assertEqual(len(self.calls), 2)

    def test_cache_has_no_new_paid_usage_and_keeps_history(self):
        self.run_review(1)
        second = self.run_review(1)
        api = second["scenes"][0]["api"]
        self.assertTrue(api["cache_hit"])
        self.assertEqual(api["request_count"], 0)
        self.assertIsNone(api["usage"])
        self.assertEqual(len(usage_history(self.folder)), 1)
        self.assertEqual(usage_history(self.folder)[0]["usage"]["total_tokens"], 120)

    def test_text_and_description_invalidate_cache(self):
        self.run_review(1)
        for field in ("narration", "visual_description"):
            self.scenes[0][field] += " изменено"
            self.path.write_text(json.dumps({"scenes": self.scenes}))
            self.run_review(1)
        self.assertEqual(len(self.calls), 3)

    def test_model_parameters_prompt_and_image_identity_invalidate_cache(self):
        self.run_review(1)
        with patch.object(review, "dotenv_values", return_value={"OPENAI_VISION_MODEL": "another-model"}):
            self.run_review(1)
            self.assertEqual(self.calls[-1]["model"], "another-model")
        with patch.object(review, "PROMPT_VERSION", 99):
            self.run_review(1)
        with patch.dict(review.REQUEST_PARAMETERS, {"max_output_tokens": 2400}):
            self.run_review(1)
        source = json.loads((self.folder / "visual_manifest.json").read_text())
        source["scenes"][0]["candidates"][0]["file_title"] = "File:Another_identity.png"
        (self.folder / "visual_manifest.json").write_text(json.dumps(source))
        self.run_review(1)
        self.assertEqual(len(self.calls), 5)

    def test_changed_original_bytes_invalidates_cache(self):
        self.run_review(1)
        source = json.loads((self.folder / "visual_manifest.json").read_text())
        candidate = source["scenes"][0]["candidates"][0]
        content = image_bytes((1600, 2100))
        (self.folder / candidate["local_path"]).write_bytes(content)
        candidate.update(image_files.inspect_image(content))
        (self.folder / "visual_manifest.json").write_text(json.dumps(source))
        self.run_review(1)
        self.assertEqual(len(self.calls), 2)

    def test_corrupt_file_never_reuses_success(self):
        self.run_review(1)
        source = json.loads((self.folder / "visual_manifest.json").read_text())
        candidate = source["scenes"][0]["candidates"][0]
        (self.folder / candidate["local_path"]).write_bytes(b"broken")
        source["scenes"][0]["search_rounds"] = 2
        (self.folder / "visual_manifest.json").write_text(json.dumps(source))
        report = self.run_review(1)
        self.assertEqual(report["status"], "needs_visual")
        self.assertIsNone(report["scenes"][0]["selected"])
        self.assertEqual(len(self.calls), 1)

    def test_new_limits_invalidate_other_scene_readiness(self):
        self.run_review()
        with patch.object(image_files, "MIN_HEIGHT", 2500), \
             patch.object(review, "reformulate_query", return_value={"status": "query_rewritten", "search_query": "another sunset"}), \
             patch.object(review, "select_image", return_value={"search_query": "another sunset", "status": "needs_visual", "candidates": [], "selected": None}):
            report = self.run_review(1)
        self.assertEqual(report["status"], "needs_visual")

    def test_all_locally_rejected_allow_second_search_without_first_vision_call(self):
        for problem in ("license", "corrupt"):
            with self.subTest(problem=problem):
                original = copy.deepcopy(self.source)
                entry = original["scenes"][0]
                entry["selected"] = None
                if problem == "license":
                    entry["candidates"][0]["license_url"] = "https://evil.example/license"
                else:
                    (self.folder / "broken.png").write_bytes(b"broken")
                    entry["candidates"][0]["local_path"] = "broken.png"
                (self.folder / "visual_manifest.json").write_text(json.dumps(original))
                (self.folder / "visual_review.json").unlink(missing_ok=True)
                new = copy.deepcopy(self.source["scenes"][0])
                new["search_query"] = "another valid sunset"
                with patch.object(review, "reformulate_query", return_value={"status": "query_rewritten", "search_query": new["search_query"]}) as rewrite, \
                     patch.object(review, "select_image", return_value=new) as search:
                    report = self.run_review(1)
                    self.run_review(1)
                result = report["scenes"][0]
                self.assertEqual(rewrite.call_count, 1)
                self.assertEqual(search.call_count, 1)
                self.assertEqual(result["search_rounds"], 2)
                self.assertEqual(result["status"], "selected")
                self.assertEqual(result["rounds"][0]["review"]["api"]["request_count"], 0)
                self.assertFalse(result["rounds"][0]["review"]["api"]["images_sent"])

    def test_broken_preview_of_other_scene_blocks_global_readiness(self):
        report = self.run_review()
        path = self.folder / report["scenes"][1]["selected"]["analysis_image_path"]
        path.write_bytes(b"broken")
        self.assertFalse(review.entry_is_current(report["scenes"][1], self.scenes[1], self.folder))

    def test_api_failure_preserves_completed_scene_and_exact_reason(self):
        self.run_review(1)
        def failed(request):
            self.calls.append({})
            return httpx.Response(429, json={"error": {"code": "test_quota", "type": "test_limit", "message": "Test exhausted"}})
        with patch.object(self, "handler", side_effect=failed), patch.object(review, "reformulate_query") as rewrite:
            with self.assertRaisesRegex(VisualError, "test_quota"):
                self.run_review(2)
            rewrite.assert_not_called()
        report = json.loads((self.folder / "visual_review.json").read_text())
        self.assertEqual(report["scenes"][0]["status"], "selected")
        self.assertEqual(report["status"], "error")
        self.assertIn("test_limit", report["error"])
        self.run_review(2)  # Ошибка не закеширована: отдельный явный запуск обращается к API.
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(usage_history(self.folder)), 3)

    def test_empty_search_gets_one_rewrite_and_second_search(self):
        source_entry = self.source["scenes"][0]
        new = copy.deepcopy(source_entry)
        source_entry.update(candidates=[], selected=None, status="needs_visual", reason="Пустой поиск")
        self.save()
        new["search_query"] = "sunset ocean horizon"
        with patch.object(review, "reformulate_query", return_value={"status": "query_rewritten", "search_query": new["search_query"]}) as rewrite, \
             patch.object(review, "select_image", return_value=new) as search:
            report = self.run_review(1)
        self.assertEqual(rewrite.call_count, 1)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(report["scenes"][0]["status"], "selected")
        self.assertEqual(report["scenes"][0]["search_rounds"], 2)
        self.assertEqual(len(self.calls), 1)

    def test_ai_rejects_all_two_rounds_then_stops_even_after_resume(self):
        response = {"status": "model_reviewed", "checks": [{"candidate_id": 1, "observed_content": "Здание",
                    "verdict": "unsuitable", "score": 0, "reason": "Не закат"}]}
        new = copy.deepcopy(self.source["scenes"][0])
        new["search_query"] = "another sunset query"
        with patch.object(review, "review_image_contents", return_value=response) as vision, \
             patch.object(review, "reformulate_query", return_value={"status": "query_rewritten", "search_query": new["search_query"]}) as rewrite, \
             patch.object(review, "select_image", return_value=new) as search:
            report = self.run_review(1)
            self.run_review(1)
        self.assertEqual(report["scenes"][0]["status"], "needs_visual")
        self.assertIsNone(report["scenes"][0]["selected"])
        self.assertEqual(search.call_count, 1)
        self.assertEqual(rewrite.call_count, 1)
        self.assertEqual(vision.call_count, 3)
        self.assertIn("Не закат", report["scenes"][0]["rounds"][0]["review"]["candidates"][0]["reason"])

    def test_two_empty_searches_stop_without_vision(self):
        for item in self.source["scenes"]:
            item.update(candidates=[], selected=None, status="needs_visual")
        self.save()
        new = {**self.source["scenes"][0], "search_query": "another query"}
        with patch.object(review, "reformulate_query", return_value={"status": "query_rewritten", "search_query": "another query"}) as rewrite, \
             patch.object(review, "select_image", return_value=new) as search:
            report = self.run_review(1)
            self.run_review(1)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(rewrite.call_count, 1)
        self.assertFalse(self.calls)
        self.assertEqual(report["status"], "needs_visual")

    def test_wikimedia_failure_is_not_reformulated(self):
        self.source["scenes"][0].update(status="error", reason="Wikimedia HTTP 403")
        self.save()
        with patch.object(review, "reformulate_query") as rewrite:
            with self.assertRaisesRegex(VisualError, "403"):
                self.run_review(1)
            rewrite.assert_not_called()
        self.assertFalse(self.calls)

    def test_network_error_during_preparation_stops_without_rewrite(self):
        self.source["scenes"][0]["candidates"][0].pop("local_path")
        self.source["scenes"][0]["selected"] = None
        self.save()
        with patch.object(review, "download_image", side_effect=VisualServiceError("HTTP 503")), \
             patch.object(review, "reformulate_query") as rewrite:
            with self.assertRaisesRegex(VisualError, "503"):
                self.run_review(1)
            rewrite.assert_not_called()

    def test_previews_ignore_gallery_and_keep_original(self):
        entry = self.source["scenes"][0]
        first = review.prepare_candidates(entry, self.folder)[0]
        (self.folder / "gallery_previews.json").write_text("broken irrelevant gallery data")
        second = review.prepare_candidates(entry, self.folder)[0]
        self.assertEqual(first["analysis_image_sha256"], second["analysis_image_sha256"])
        self.assertEqual((first["analysis_width"], first["analysis_height"]), (768, 1024))
        self.assertNotEqual(first["local_path"], first["analysis_image_path"])
        self.assertEqual((self.folder / first["local_path"]).read_bytes(), image_bytes(self.image_size))

    def test_forged_license_and_damaged_file_are_not_sent(self):
        entry = copy.deepcopy(self.source["scenes"][0])
        entry["candidates"][0]["license_url"] = "https://evil.example/license"
        prepared = review.prepare_candidates(entry, self.folder)
        self.assertFalse(prepared[0]["eligible_for_video"])
        self.assertIsNone(prepared[0]["analysis_image_path"])

    def test_mvp_image_adapter_and_future_material_configuration(self):
        from scene_planner import scenes_from_script, available_material_types
        from scriptwriter import script_identity
        responses = DemoResponses()
        script = write_script(demo_research("test", "success"), generate=responses.generate, demo=True)
        script.update(script_version_id=script_identity(script), mode="demo")
        with patch.object(scene_planner, "dotenv_values", return_value={}):
            plan = scenes_from_script(script, demo=True)
            self.assertTrue(all(scene["material_type"] == "image" for scene in plan["scenes"]))
        with patch.object(scene_planner, "dotenv_values", return_value={"AVAILABLE_MATERIAL_TYPES": "image,video"}):
            self.assertEqual(available_material_types(), ["image", "video"])
        self.assertIsNone(plan["scenes"][0]["estimated_duration_seconds"])

    def test_existing_video_is_not_replaced_or_sent_to_openai(self):
        self.scenes[0]["material_type"] = "video"
        self.save()
        report = self.run_review(1)
        self.assertEqual(report["scenes"][0]["material_type"], "video")
        self.assertIsNone(report["scenes"][0]["selected"])
        self.assertFalse(self.calls)

    def test_rewrite_sdk_uses_scene_rejections_and_has_no_sdk_retries(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            payload = search_payload()
            payload["output"] = [payload["output"][1]]
            payload["output"][0]["content"][0]["text"] = json.dumps({
                "search_query": "ocean sunset horizon", "reason": "Уточнён вид горизонта"})
            return httpx.Response(200, json=payload)

        client = OpenAI(api_key="test-placeholder", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.object(visual_retry, "load_settings", return_value=("test-placeholder", "query-model")), \
             patch.object(visual_retry, "OpenAI", return_value=client) as constructor:
            result = visual_retry.reformulate_query(self.scenes[0], {"search_query": "sunset", "reason": "Не закат"}, self.folder)
        self.assertEqual(result["status"], "query_rewritten")
        self.assertEqual(len(requests), 1)
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
        self.assertIn("Не закат", requests[0]["input"])
        self.assertEqual(usage_history(self.folder)[0]["operation"], "query_rewrite")

    def test_rewrite_quota_error_preserves_details_without_second_request(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(429, json={"error": {"code": "test_code", "type": "test_type", "message": "test-placeholder exhausted"}})

        client = OpenAI(api_key="test-placeholder", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.object(visual_retry, "load_settings", return_value=("test-placeholder", "query-model")), \
             patch.object(visual_retry, "OpenAI", return_value=client):
            result = visual_retry.reformulate_query(self.scenes[0], {"search_query": "sunset"}, self.folder)
        self.assertEqual(len(calls), 1)
        self.assertIn("test_code", result["error"])
        self.assertIn("test_type", result["error"])
        self.assertNotIn("test-placeholder", result["error"])
        self.assertEqual(result["status"], "api_error")


if __name__ == "__main__":
    unittest.main()
