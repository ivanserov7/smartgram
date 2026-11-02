"""Production photo fallback, provider failures and immutable generated provenance."""

import base64
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import httpx
from openai import RateLimitError
from PIL import Image
from pydantic import ValidationError

from fast_materials import FastMaterials, FastPlan, candidate_id
from file_io import atomic_write_json
from production_state import ModelClient, ProductionError, ProductionServiceError, config, read
from visual_generation import generate_image, image_estimate, verify_created
from visual_materials import verify_asset


def jpeg(size, color="navy"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


def illustration(text):
    return {
        "text": text,
        "visual_task": "Красивый осьминог в океане",
        "queries": ["octopus", "ocean"],
        "must_show": [],
        "must_not_show": [],
        "preferences": [],
        "role": "illustrative",
        "role_reason": "Ассоциативная связь с текстом",
        "creation": {
            "kind": "raster", "prompt": "Beautiful octopus underwater, portrait composition",
            "labels": [], "links": [], "fact_ids": [],
        },
    }


class ImageFallbackTests(unittest.TestCase):
    def setUp(self):
        proxy_environment = patch.dict(os.environ, {
            name: "" for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
        })
        proxy_environment.start()
        self.addCleanup(proxy_environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = copy.deepcopy(config()["visuals"])
        self.settings["search"]["request_interval_seconds"] = 0
        self.script = {
            "script_version_id": "fallback-test",
            "fragments": [{"text": "Осьминог меняет окраску своей кожи.", "visual": "Океан"}],
        }
        self.model_calls = []
        self.photos = False
        self.write_inputs(self.root)

    def write_inputs(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(directory / "research.json", {"facts": [{"statement": "Осьминог меняет окраску."}]})
        atomic_write_json(directory / "script.json", self.script)

    def model(self, step, settings, schema, data, images):
        self.model_calls.append(step)
        if step == "visual_queries":
            return {"fragment_index": data["fragment_index"], "shots": [illustration(data["fragment"]["text"])]}
        if step == "visual_select":
            return {"assessments": [
                {"shot_id": shot["shot_id"], "candidate_id": candidate["candidate_id"],
                 "suitable": False, "selection_mode": "illustrative_background", "score": 0,
                 "reason": "На фотографии отсутствует связь со смыслом фрагмента.",
                 "composition": {"mode": "crop", "initial_crop": [0, 0, 1, 1],
                                 "focus_point": [0.5, 0.5], "zoom_percent": 0, "must_keep": []}}
                for shot in data["shots"] for candidate in data["candidates"]
            ]}
        self.assertEqual(step, "visual_review")
        return {"fragment_index": data["fragment"]["fragment_index"], "decision": "approved",
                "issues": [], "corrections": None}

    def search_response(self, request):
        if request.url.host != "commons.wikimedia.org":
            return httpx.Response(200, content=jpeg((768, 512)), headers={"content-type": "image/jpeg"})
        pages = []
        if self.photos:
            pages = [{"pageid": 1, "index": 1, "title": "File:Octopus.jpg", "imageinfo": [{
                "url": "https://upload.wikimedia.org/wikipedia/commons/a/aa/Octopus.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Octopus.jpg",
                "width": 3000, "height": 2400, "size": 50000, "mime": "image/jpeg", "sha1": "native-source",
                "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/aa/Octopus.jpg/768px-Octopus.jpg",
                "thumbmime": "image/jpeg", "extmetadata": {
                    "Artist": {"value": "Test author"}, "LicenseShortName": {"value": "CC BY 4.0"},
                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                },
            }]}]
        return httpx.Response(200, json={"query": {"pages": pages}})

    def generated_response(self, directory, settings, prompt):
        self.assertEqual(settings["size"], "1024x1536")
        self.assertEqual(settings["generated_upscale_limit"], 1.25)
        return jpeg((1024, 1536)), {
            "parameters": {"model": settings["model"], "size": settings["size"]},
            "prompt_sha256": "mock-prompt", "usage": None, "estimate_usd": None,
        }

    def run_fallback(self, directory=None):
        directory = directory or self.root
        client = httpx.Client(transport=httpx.MockTransport(self.search_response))
        try:
            with patch("fast_materials.create_commons_client", return_value=client), patch.dict(os.environ, {"PEXELS_API_KEY": ""}), patch("pexels_materials.dotenv_values", return_value={}):
                return read(FastMaterials(directory, self.settings, self.model).run(self.script))
        finally:
            client.close()

    def test_empty_search_generates_reviewed_portrait_and_resume_is_free(self):
        with patch("visual_generation.generate_image", side_effect=self.generated_response) as api:
            result = self.run_fallback()
            self.assertEqual(result["status"], "approved")
            shot = result["fragments"][0]["shots"][0]
            self.assertEqual(shot["selection_mode"], "created")
            self.assertEqual(shot["asset"]["source"]["origin"], "generated")
            verify_asset(shot, self.root)
            self.assertEqual(candidate_id(shot["asset"]["source"]), shot["asset"]["candidate_id"])
            with patch("fast_materials.create_commons_client", side_effect=AssertionError("completed resume cannot search")):
                self.assertEqual(read(FastMaterials(self.root, self.settings, self.model).run(self.script))["status"], "approved")
            self.assertEqual(api.call_count, 1)

    def test_all_photo_candidates_rejected_still_generates(self):
        self.photos = True
        with patch("visual_generation.generate_image", side_effect=self.generated_response) as api:
            result = self.run_fallback()
        self.assertEqual(result["status"], "approved")
        self.assertIn("visual_select", self.model_calls)
        self.assertEqual(api.call_count, 1)

    def test_provider_error_stops_without_repeating_on_resume(self):
        with patch("visual_generation.generate_image", side_effect=ProductionServiceError("Image API HTTP 429")) as api:
            result = self.run_fallback()
            resumed = self.run_fallback()
        self.assertEqual(api.call_count, 1)
        self.assertIn("429", result["service_error"])
        self.assertIn("429", resumed["service_error"])
        self.assertEqual(result["status"], "needs_visual")

    def test_unchanged_generated_approval_reuses_verified_bytes_in_new_run(self):
        with patch("visual_generation.generate_image", side_effect=self.generated_response) as api:
            original = self.run_fallback()
            target = self.root / "next"
            self.write_inputs(target)
            atomic_write_json(target / "visual_previous.json", {"directory": str(self.root)})
            with patch("fast_materials.create_commons_client", side_effect=AssertionError("unchanged created approval cannot search")):
                result = read(FastMaterials(target, self.settings, self.model).run(self.script))
        self.assertEqual(result["status"], "approved")
        self.assertEqual(api.call_count, 1)
        new = result["fragments"][0]["shots"][0]
        verify_asset(new, target)
        self.assertEqual(new["asset"], original["fragments"][0]["shots"][0]["asset"])

    def test_changed_visual_intent_requires_new_review(self):
        with patch("visual_generation.generate_image", side_effect=self.generated_response) as api:
            original = self.run_fallback()
            original["fragments"][0]["shots"][0]["visual_task"] = "Совсем другой визуальный замысел"
            atomic_write_json(self.root / "visual_materials.json", original)
            target = self.root / "next"
            self.write_inputs(target)
            atomic_write_json(target / "visual_previous.json", {"directory": str(self.root)})
            self.assertEqual(self.run_fallback(target)["status"], "approved")
        self.assertEqual(api.call_count, 2)

    def test_created_metadata_must_match_saved_provenance(self):
        with patch("visual_generation.generate_image", side_effect=self.generated_response):
            source = self.run_fallback()["fragments"][0]["shots"][0]["asset"]["source"]
        source["model"] = "different-model"
        with self.assertRaisesRegex(ProductionError, "происхождением"):
            verify_created(source, self.root)

    def test_active_plan_schema_cannot_disable_raster_fallback(self):
        plan = {"fragment_index": 1, "shots": [illustration("Текст") ]}
        FastPlan.model_validate(plan)
        for role, kind in (("documentary_exact", "raster"), ("illustrative", "diagram")):
            invalid = copy.deepcopy(plan)
            invalid["shots"][0]["role"] = role
            invalid["shots"][0]["creation"]["kind"] = kind
            with self.assertRaises(ValidationError):
                FastPlan.model_validate(invalid)

    def test_image_provider_uses_process_key_and_records_unknown_usage(self):
        client = MagicMock()
        client.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(jpeg((1024, 1536))).decode())],
            usage=None, _request_id="request-test",
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "process-placeholder"}), patch("visual_generation.dotenv_values", return_value={"OPENAI_API_KEY": "file-placeholder"}), patch("visual_generation.OpenAI") as sdk:
            sdk.return_value.__enter__.return_value = client
            _, record = generate_image(self.root, self.settings["generation"], "Portrait")
        self.assertEqual(sdk.call_args.kwargs["api_key"], "process-placeholder")
        self.assertEqual(sdk.call_args.kwargs["max_retries"], 0)
        self.assertIsNone(record["usage"])
        self.assertIsNone(record["estimate_usd"])
        self.assertEqual(record["estimate_status"], "unknown")

    def test_failed_image_request_records_status_request_id_and_end(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/images/generations")
        response = httpx.Response(429, request=request, headers={"x-request-id": "failed-request"})
        error = RateLimitError("rate limit", response=response, body={"code": "rate_limit_exceeded"})
        client = MagicMock()
        client.images.generate.side_effect = error
        with patch.dict(os.environ, {"OPENAI_API_KEY": "process-placeholder"}), patch("visual_generation.OpenAI") as sdk:
            sdk.return_value.__enter__.return_value = client
            with self.assertRaises(ProductionServiceError):
                generate_image(self.root, self.settings["generation"], "Portrait")
        telemetry = read(self.root / "openai_usage.json")["requests"][-1]["telemetry"]
        self.assertEqual(telemetry["provider_request_id"], "failed-request")
        self.assertEqual(telemetry["error"]["http_status"], 429)
        self.assertIsNotNone(telemetry["ended_at_utc"])
        self.assertEqual(client.images.generate.call_count, 1)

    def test_image_cost_does_not_apply_flare_rates_to_other_models(self):
        usage = {"input_tokens_details": {"text_tokens": 10}, "output_tokens": 100}
        self.assertIsNone(image_estimate(usage, "another-image-model"))

    def test_visual_planner_also_uses_process_key(self):
        plan = {"fragment_index": 1, "shots": [illustration("Текст")]}
        response = SimpleNamespace(
            status="completed", output_text=json.dumps(plan),
            model_dump=lambda **kwargs: {"status": "completed"},
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "process-placeholder"}), patch(
            "production_state.dotenv_values", return_value={"OPENAI_API_KEY": "file-placeholder"}
        ), patch("production_state.OpenAI") as sdk, patch("production_state.OpenAIMeter.create", return_value=response):
            result = ModelClient(self.root)("visual_queries", self.settings["queries"], FastPlan, {})
        self.assertEqual(result, plan)
        self.assertEqual(sdk.call_args.kwargs["api_key"], "process-placeholder")


if __name__ == "__main__":
    unittest.main()
