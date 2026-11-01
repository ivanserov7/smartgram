"""Короткие проверки адаптера: HTTP подменён, лимиты и технические файлы настоящие."""

import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from commons_requests import CommonsRequests
from fast_materials import FastMaterials, candidate_id
from production_state import Journal, ProductionError, config
from pexels_materials import (
    search_first,
    metadata_problem,
    verify_source,
    fetch_material,
)
from test_image_search_fix import page_response


def photo():
    return {
        "id": 123,
        "width": 3000,
        "height": 4000,
        "url": "https://www.pexels.com/photo/ocean-123/",
        "photographer": "Photographer",
        "photographer_url": "https://www.pexels.com/@photographer",
        "alt": "Beautiful ocean",
        "src": {
            "original": "https://images.pexels.com/photos/123/pexels-photo-123.jpeg",
            "medium": "https://images.pexels.com/photos/123/pexels-photo-123.jpeg?h=350",
        },
    }


class PexelsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = config()["visuals"]
        self.journal = Journal(self.root, "test", {})
        self.budget = {"number": 1, "calls": 0}
        self.requests = []
        self.key_patch = patch(
            "pexels_materials.dotenv_values",
            return_value={"PEXELS_API_KEY": "offline-placeholder"},
        )
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def gateway(self, handler):
        def record(request):
            self.requests.append(request)
            return handler(request)

        client = httpx.Client(transport=httpx.MockTransport(record))
        self.addCleanup(client.close)
        return CommonsRequests(
            self.journal,
            self.budget,
            "test",
            client,
            self.settings["search"],
            sleep=lambda seconds: None,
        )

    def test_success_cache_origin_and_download_preserve_checks(self):
        output = io.BytesIO()
        Image.new("RGB", (3000, 4000), "navy").save(output, "JPEG")

        def handler(request):
            if request.url.host == "api.pexels.com":
                return httpx.Response(
                    200,
                    json={"photos": [photo()]},
                    headers={
                        "X-Ratelimit-Remaining": "12",
                        "X-Ratelimit-Limit": "20000",
                        "X-Ratelimit-Reset": "9999999999",
                    },
                )
            if request.url.params.get("h") == "350":
                preview = io.BytesIO()
                Image.new("RGB", (262, 350), "navy").save(preview, "JPEG")
                return httpx.Response(
                    200,
                    content=preview.getvalue(),
                    headers={"content-type": "image/jpeg"},
                )
            return httpx.Response(
                200, content=output.getvalue(), headers={"content-type": "image/jpeg"}
            )

        gateway = self.gateway(handler)
        page = search_first(gateway, "ocean")
        candidate = page["candidates"][0]
        self.assertEqual(search_first(gateway, "ocean"), page)
        self.assertEqual(self.budget["calls"], 1)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.headers["authorization"], "offline-placeholder")
        self.assertNotIn("orientation", request.url.params)
        self.assertEqual(request.url.params["per_page"], "10")
        self.assertEqual(candidate["license"], "Pexels License")
        self.assertIsNone(metadata_problem(candidate))
        verify_source(candidate, self.root)
        material = fetch_material(
            dict(candidate, candidate_id=candidate_id(candidate)), gateway
        )
        self.assertEqual((material["width"], material["height"]), (3000, 4000))
        self.assertNotIn("authorization", self.requests[-1].headers)
        runner = FastMaterials(
            self.root,
            self.settings,
            model=lambda *args: {
                "fragment_index": 1,
                "decision": "approved",
                "issues": [],
                "corrections": None,
            },
        )
        runner.facts = []
        runner.limits = self.settings["search"]
        thumbnail = runner.preview(
            {"budget": {}},
            dict(candidate, candidate_id=candidate_id(candidate)),
            gateway,
        )
        with Image.open(thumbnail) as image:
            self.assertEqual(max(image.size), 350)

        shot = {
            "shot_id": "s",
            "fragment_index": 1,
            "text": "Океан",
            "visual_task": "Sea",
            "role": "illustrative",
            "must_show": [],
        }
        job = {"corrections": {}, "rejections": []}
        rd = {"selection_reasons": {"s:" + material["candidate_id"]: "Красивый океан"}}
        from test_production import composition

        approved = runner.prepare_and_review(
            job, rd, shot, material, composition("crop", 0)
        )
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["asset"]["changes"].startswith("Pexels"))
        self.assertEqual(
            self.journal.state["pexels"]["quota"]["X-Ratelimit-Remaining"], "12"
        )
        changed = dict(candidate, author="Invented author")
        with self.assertRaises(ProductionError):
            verify_source(changed, self.root)
        self.assertIsNotNone(metadata_problem(dict(candidate, license="CC BY 4.0")))

    def test_no_key_429_timeout_and_empty_fall_back_without_retries(self):
        for case in ("no_key", "429", "timeout", "empty"):
            with self.subTest(case=case):
                self.requests.clear()
                self.journal = Journal(self.root / case, "test", {})
                self.budget = {"number": 1, "calls": 0}

                def handler(request):
                    if request.url.host == "api.pexels.com":
                        if case == "429":
                            return httpx.Response(429)
                        if case == "timeout":
                            raise httpx.ReadTimeout("offline", request=request)
                        return httpx.Response(200, json={"photos": []})
                    self.assertNotIn("authorization", request.headers)
                    return httpx.Response(200, json=page_response())

                with patch(
                    "pexels_materials.dotenv_values",
                    return_value=(
                        {}
                        if case == "no_key"
                        else {"PEXELS_API_KEY": "offline-placeholder"}
                    ),
                ):
                    result = search_first(self.gateway(handler), "ocean")
                self.assertEqual(result["provider"], "wikimedia")
                self.assertTrue(result["candidates"])
                self.assertEqual(
                    sum(r.url.host == "api.pexels.com" for r in self.requests),
                    0 if case == "no_key" else 1,
                )
                self.assertLessEqual(self.budget["calls"], 2)

    def test_quota_zero_keeps_cache_and_stops_new_pexels_requests(self):
        def handler(request):
            if request.url.host == "api.pexels.com":
                return httpx.Response(
                    200,
                    json={"photos": [photo()]},
                    headers={"X-Ratelimit-Remaining": "0"},
                )
            return httpx.Response(200, json=page_response())

        gateway = self.gateway(handler)
        first = search_first(gateway, "ocean")
        self.assertEqual(search_first(gateway, "ocean"), first)
        self.assertEqual(search_first(gateway, "coral")["provider"], "wikimedia")
        self.assertEqual(sum(r.url.host == "api.pexels.com" for r in self.requests), 1)

    def test_cdn_unavailable_allows_commons_without_key_leak(self):
        from pexels_materials import download_file

        def handler(request):
            if request.url.host == "api.pexels.com":
                return httpx.Response(200, json={"photos": [photo()]})
            if request.url.host == "images.pexels.com":
                return httpx.Response(403)
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, json=page_response())

        gateway = self.gateway(handler)
        candidate = search_first(gateway, "ocean")["candidates"][0]
        with self.assertRaisesRegex(ProductionError, "Pexels CDN"):
            download_file(
                candidate,
                gateway,
                candidate["thumbnail_url"],
                self.root / "preview.jpg",
                "preview",
            )
        self.assertEqual(search_first(gateway, "sea")["provider"], "wikimedia")
        self.assertEqual(
            sum(r.url.host == "images.pexels.com" for r in self.requests), 1
        )

    def test_shared_parent_budget_and_reserve_provider(self):
        runner = FastMaterials(self.root, self.settings, model=lambda *args: None)
        runner.limits = self.settings["search"]
        job = {"budget": {}, "rounds": []}

        def handler(request):
            return httpx.Response(
                200,
                json=(
                    {"photos": [photo()]}
                    if request.url.host == "api.pexels.com"
                    else page_response()
                ),
            )

        gateway = self.gateway(handler)
        pexels = search_first(gateway, "ocean")
        commons = search_first(gateway, "coral", force_commons=True)
        runner.add_cards(job, pexels["candidates"])
        runner.add_cards(job, commons["candidates"])
        self.assertEqual(len(job["budget"]["cards"]), 2)
        self.assertEqual(self.budget["calls"], 2)
        self.assertNotEqual(
            candidate_id(pexels["candidates"][0]),
            candidate_id(commons["candidates"][0]),
        )

    def test_pexels_first_page_is_reviewed_before_commons(self):
        runner = FastMaterials(self.root, self.settings, model=lambda *args: None)
        runner.limits = self.settings["search"]
        rd = {"number": 1, "calls": 0, "outcomes": []}
        job = {"budget": {}, "rounds": [rd]}

        def handler(request):
            self.assertEqual(request.url.host, "api.pexels.com")
            return httpx.Response(200, json={"photos": [photo()]})

        gateway = self.gateway(handler)
        gateway.budget = rd
        runner.acquire_cards(
            job,
            rd,
            gateway,
            [
                {
                    "queries": ["ocean", "coral"],
                    "visual_task": "Sea",
                    "role": "illustrative",
                }
            ],
        )
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(rd["outcomes"][0]["provider"], "pexels")
        self.assertEqual(len(job["budget"]["cards"]), 1)


if __name__ == "__main__":
    unittest.main()
