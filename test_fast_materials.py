"""Риски быстрого пути: подставные API, реальные JPEG и постоянные журналы."""

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import httpx
from PIL import Image

from fast_materials import FastMaterials, reserve, candidate_id, compact_candidate
from commons_materials import parse_pages
from production_state import (
    config,
    read,
    Journal,
    ProductionError,
    ProductionServiceError,
    digest,
)
from file_io import atomic_write_json
from test_production import metadata, composition
from visual_generation import (
    create_material,
    creation_identity,
    generate_image,
    image_estimate,
)
from visual_materials import verify_asset


def raster(size, color="navy"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "JPEG")
    return output.getvalue()


def draft(text, role="illustrative"):
    return dict(
        text=text,
        visual_task="Octopus skin",
        queries=["octopus chromatophores", "cephalopod skin", "octopus"],
        must_show=["осьминог"],
        must_not_show=[],
        preferences=[],
        role=role,
        role_reason="Иллюстрация речи",
        creation={
            "kind": "diagram",
            "prompt": "Цепочка",
            "labels": ["Нервный сигнал", "Изменение окраски"],
            "links": [{"source": 0, "target": 1}],
            "fact_ids": [1],
        },
    )


class FastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        atomic_write_json(
            self.root / "research.json",
            {"facts": [{"statement": "Нервные сигналы изменяют окраску."}]},
        )
        self.settings = copy.deepcopy(config()["visuals"])
        self.calls = []
        self.requests = []
        self.script = {
            "script_version_id": "test",
            "fragments": [
                {
                    "text": "Первый осьминог меняет цвет. Второй осьминог остаётся рядом.",
                    "visual": "Осьминог",
                }
            ],
        }
        self.suitable = False
        self.client = httpx.Client(transport=httpx.MockTransport(self.handler))
        self.addCleanup(self.client.close)

    def handler(self, request):
        self.requests.append(request)
        if request.url.host != "commons.wikimedia.org":
            small = "/thumb/" in request.url.path and "768px" in request.url.path
            return httpx.Response(
                200,
                content=raster((768, 460) if small else (2000, 1200)),
                headers={"content-type": "image/jpeg"},
            )
        params = dict(request.url.params)
        if "titles" in params:
            ids = [int(params["titles"].split("Item")[1].split(".")[0])]
        else:
            start = int(params.get("gsroffset", 0))
            ids = range(start, start + min(50, int(params["gsrlimit"])))
        pages = []
        for i in ids:
            title = f"File:Octopus Item{i}.jpg"
            side = params.get("iiurlwidth", "3072")
            pages.append(
                {
                    "pageid": i + 1,
                    "index": i + 1,
                    "title": title,
                    "imageinfo": [
                        {
                            "url": f"https://upload.wikimedia.org/wikipedia/commons/a/aa/Item{i}.jpg",
                            "descriptionurl": "https://commons.wikimedia.org/wiki/"
                            + title,
                            "width": 2000,
                            "height": 1200,
                            "size": 20000,
                            "mime": "image/jpeg",
                            "sha1": str(i),
                            "timestamp": "2026-11-01",
                            "thumburl": f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/aa/Item{i}.jpg/{side}px-Item{i}.jpg",
                            "thumbmime": "image/jpeg",
                            "extmetadata": {
                                k: {"value": v}
                                for k, v in {
                                    "Artist": "Test author",
                                    "LicenseShortName": "CC BY 4.0",
                                    "LicenseUrl": "https://creativecommons.org/licenses/by/4.0/",
                                    "ObjectName": "Octopus skin",
                                }.items()
                            },
                        }
                    ],
                }
            )
        return httpx.Response(
            200,
            json={
                "query": {"pages": list(reversed(pages))},
                "continue": {"gsroffset": 50, "continue": "gsroffset||"},
            },
        )

    def model(self, step, settings, schema, data, images):
        self.calls.append((step, data, len(images)))
        if step == "visual_queries":
            return {
                "fragment_index": 1,
                "shots": [
                    draft("Первый осьминог меняет цвет."),
                    draft("Второй осьминог остаётся рядом."),
                ],
            }
        if step == "visual_select":
            return {
                "assessments": [
                    {
                        "shot_id": s["shot_id"],
                        "candidate_id": c["candidate_id"],
                        "suitable": self.suitable,
                        "selection_mode": "illustrative_background",
                        "score": 90,
                        "reason": (
                            "Подходящий фон"
                            if self.suitable
                            else "Не подходит этому кадру"
                        ),
                        "composition": composition("background", 0),
                    }
                    for s in data["shots"]
                    for c in data["candidates"]
                ]
            }
        return {
            "fragment_index": 1,
            "decision": "approved",
            "issues": [],
            "corrections": None,
        }

    def runner(self):
        return FastMaterials(self.root, self.settings, self.model)

    def test_shared_budget_two_shots_two_rounds_and_resume(self):
        with patch("fast_materials.create_commons_client", return_value=self.client):
            runner = self.runner()
            result = read(runner.run(self.script))
        job = next(iter(runner.journal.state["jobs"].values()))
        self.assertEqual(len(job["budget"]["cards"]), 100)
        self.assertEqual(len(job["budget"]["previews"]), 20)
        self.assertEqual(len(job["rounds"]), 2)
        self.assertEqual(
            len([r for r in self.requests if r.url.host == "upload.wikimedia.org"]), 20
        )
        batches = [c for c in self.calls if c[0] == "visual_select"]
        self.assertEqual([c[2] for c in batches], [10, 10])
        self.assertTrue(all(len(c[1]["shots"]) == 2 for c in batches))
        before = (len(self.requests), len(self.calls))
        with patch(
            "fast_materials.create_commons_client",
            side_effect=AssertionError("Completed resume must not open network"),
        ):
            read(self.runner().run(self.script))
        self.assertEqual((len(self.requests), len(self.calls)), before)
        self.assertEqual(result["status"], "needs_visual")

    def test_early_stop_one_finalist_reused_for_two_shots(self):
        self.suitable = True
        with patch("fast_materials.create_commons_client", return_value=self.client):
            runner = self.runner()
            result = read(runner.run(self.script))
        job = next(iter(runner.journal.state["jobs"].values()))
        self.assertEqual(result["status"], "approved")
        self.assertEqual(len(job["budget"]["finalists"]), 1)
        self.assertEqual(len(job["rounds"]), 1)
        originals = [
            r
            for r in self.requests
            if r.url.host == "upload.wikimedia.org" and "3072px" in str(r.url)
        ]
        self.assertEqual(len(originals), 1)
        for shot in result["fragments"][0]["shots"]:
            verify_asset(shot, self.root)

    def test_rank_pagination_and_query_order(self):
        runner = self.runner()
        runner.limits = self.settings["search"]
        shots = [dict(draft("Текст"), shot_id="one")]
        rd = {"number": 1, "calls": 0, "outcomes": []}
        job = {"budget": {}, "rounds": [rd]}
        gateway = MagicMock()
        from commons_requests import CommonsRequests

        gateway = CommonsRequests(
            runner.journal, rd, "test", self.client, runner.limits
        )
        runner.acquire_cards(job, rd, gateway, shots)
        self.assertEqual(
            [c["file_title"] for c in list(runner.pool["candidates"].values())[:2]],
            ["File:Octopus Item0.jpg", "File:Octopus Item1.jpg"],
        )
        # Первый запрос точный, следующий продолжает релевантную выдачу, не прыгает к общему.
        self.assertEqual(
            self.requests[0].url.params["gsrsearch"], "octopus chromatophores"
        )
        rd2 = {"number": 2, "calls": 0, "outcomes": []}
        job["rounds"].append(rd2)
        gateway2 = CommonsRequests(
            runner.journal, rd2, "test", self.client, runner.limits
        )
        runner.acquire_cards(job, rd2, gateway2, shots)
        self.assertEqual(self.requests[-1].url.params["gsrsearch"], "cephalopod skin")

    def pool_ids(self, runner):
        return runner.pool["candidates"].keys()

    def test_100_results_in_two_paginated_calls(self):
        runner = self.runner()
        runner.limits = self.settings["search"]
        rd = {"number": 1, "calls": 0, "outcomes": []}
        job = {"budget": {}, "rounds": [rd]}
        page = self.handler(
            httpx.Request(
                "GET",
                "https://commons.wikimedia.org/w/api.php",
                params={"gsrlimit": 50},
            )
        ).json()
        runner.add_cards(job, parse_pages(page))
        self.requests.clear()
        from commons_requests import CommonsRequests

        gateway = CommonsRequests(
            runner.journal, rd, "test", self.client, runner.limits
        )
        runner.acquire_cards(job, rd, gateway, [dict(draft("Текст"), shot_id="one")])
        self.assertEqual(len(job["budget"]["cards"]), 100)
        self.assertEqual(rd["calls"], 2)
        self.assertEqual(self.requests[1].url.params["gsroffset"], "50")
        self.assertEqual(
            self.requests[0].url.params["gsrsearch"],
            self.requests[1].url.params["gsrsearch"],
        )
        self.assertEqual([c["search_rank"] for c in parse_pages(page)][:2], [1, 2])

    def test_semantic_rejection_does_not_ban_leucophore_for_other_shot(self):
        runner = self.runner()
        runner.facts = []
        candidate = dict(
            metadata(),
            candidate_id="leucophore",
            title="Leucophore layer composition.svg",
        )
        shots = [
            dict(draft("Первый"), shot_id="s1"),
            dict(draft("Отражатели", "explanatory_required"), shot_id="s2"),
        ]

        def model(step, settings, schema, data, images):
            return {
                "assessments": [
                    dict(
                        shot_id=s["shot_id"],
                        candidate_id="leucophore",
                        suitable=s["shot_id"] == "s2",
                        selection_mode="exact",
                        score=90,
                        reason="Именно отражатели",
                        composition=composition("background", 0),
                    )
                    for s in shots
                ]
            }

        runner.model = model
        job = {"assessments": {}}
        image = self.root / "test.jpg"
        image.write_bytes(raster((768, 460)))
        runner.assess_batch(job, shots, [(candidate, image)])
        self.assertFalse(job["assessments"]["s1:leucophore"]["suitable"])
        self.assertTrue(job["assessments"]["s2:leucophore"]["suitable"])
        self.assertNotIn("leucophore", runner.pool["global_rejections"])

    def test_background_cannot_close_required_or_documentary(self):
        runner = self.runner()
        runner.facts = []
        runner.model = lambda *args: {
            "assessments": [
                dict(
                    shot_id="s",
                    candidate_id="c",
                    suitable=True,
                    selection_mode="illustrative_background",
                    score=95,
                    reason="Фон",
                    composition=composition("background", 0),
                )
            ]
        }
        candidate = dict(metadata(), candidate_id="c")
        image = self.root / "p.jpg"
        image.write_bytes(raster((20, 20)))
        for role in ("explanatory_required", "documentary_exact", "illustrative"):
            job = {"assessments": {}}
            runner.assess_batch(
                job, [dict(draft("Текст", role), shot_id="s")], [(candidate, image)]
            )
            self.assertEqual(
                job["assessments"]["s:c"]["suitable"], role == "illustrative"
            )

    def test_service_failure_is_not_candidate_rejection(self):
        runner = self.runner()
        runner.limits = self.settings["search"]
        job = {
            "approved": {},
            "assessments": {
                "s:c": dict(
                    shot_id="s",
                    candidate_id="c",
                    suitable=True,
                    score=95,
                    reason="OK",
                    composition={},
                )
            },
            "rejections": [],
        }
        runner.pool["candidates"]["c"] = {}
        runner.original = MagicMock(side_effect=ProductionServiceError("HTTP 429"))
        with self.assertRaisesRegex(ProductionServiceError, "429"):
            runner.finalize_choices(job, {}, None, [{"shot_id": "s"}])
        self.assertEqual(job["rejections"], [])

    def test_generated_cache_and_failed_api_no_replay(self):
        shot = dict(draft("Изменение окраски", "explanatory_required"), shot_id="s")
        journal = Journal(self.root, "generation_test", {})
        result = create_material(self.root, shot, self.settings["generation"], journal)
        self.assertEqual(
            create_material(self.root, shot, self.settings["generation"], journal),
            result,
        )
        self.assertIsNone(result["license"])
        self.assertEqual(result["origin"], "deterministic")
        self.assertEqual(len(journal.state["operations"]), 1)
        shot["creation"]["kind"] = "raster"
        with patch(
            "visual_generation.generate_image",
            side_effect=ProductionServiceError("model_not_found"),
        ) as api:
            with self.assertRaises(ProductionServiceError):
                create_material(self.root, shot, self.settings["generation"], journal)
            with self.assertRaises(ProductionError):
                create_material(self.root, shot, self.settings["generation"], journal)
            self.assertEqual(api.call_count, 1)

    def test_generation_cannot_spend_after_finalist_limit(self):
        runner = self.runner()
        runner.limits = self.settings["search"]
        shot = dict(draft("Текст", "explanatory_required"), shot_id="s")
        job = {
            "shots": [shot],
            "approved": {},
            "budget": {"finalists": ["a", "b", "c"]},
        }
        with patch("fast_materials.create_material") as generate:
            runner.create_missing(job, {})
        generate.assert_not_called()
        self.assertIn("лимит", job["unresolved"]["s"])

    def test_copy_dependencies_creates_missing_target_and_preserves_source(self):
        runner = self.runner()
        old = self.root / "old"
        old.mkdir()
        source = old / "images" / "original.jpg"
        source.parent.mkdir()
        source.write_bytes(raster((2000, 1200)))
        asset = {
            "original_path": "images/original.jpg",
            "source": metadata(),
            "motion": {"previews": []},
        }
        runner.copy_dependencies(asset, old)
        self.assertEqual(
            (self.root / "images/original.jpg").read_bytes(), source.read_bytes()
        )
        self.assertTrue(source.exists())

    def test_refreshed_metadata_consumes_same_parent_card_budget(self):
        runner = self.runner()
        runner.limits = self.settings["search"]
        runner.pool["candidates"] = {
            str(i): dict(
                metadata(),
                candidate_id=str(i),
                file_title=f"File:Octopus Item{i}.jpg",
                source_sha1=str(i),
                needs_refresh=True,
            )
            for i in range(120)
        }
        job = {"budget": {}}
        gateway = MagicMock()
        gateway.get.return_value = {"query": {"pages": []}}
        runner.refresh_pool(job, gateway)
        self.assertEqual(len(job["budget"]["cards"]), 100)
        self.assertEqual(gateway.get.call_count, 2)
        self.assertEqual(
            sum(
                len(c.args[0]["titles"].split("|")) for c in gateway.get.call_args_list
            ),
            100,
        )

    def test_specific_query_keeps_egyptian_context_without_new_model_call(self):
        from visual_shots import short_query

        value = "first mound rising from water Egyptian myth"
        self.assertEqual(short_query(value), value)
        with self.assertRaises(ProductionError):
            short_query("one two three four five six seven eight nine")

    def test_commons_bucket_thumbnail_is_resized_without_original_fallback(self):
        from commons_requests import CommonsRequests

        runner = self.runner()
        runner.limits = self.settings["search"]
        candidate = dict(
            metadata(),
            candidate_id="test",
            thumbnail_url="https://thumb.wikimedia.org/wikipedia/commons/thumb/a/aa/Test.jpg/960px-Test.jpg",
        )
        job = {"budget": {}}
        rd = {"number": 1, "calls": 0}
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=raster((960, 960)),
                    headers={"content-type": "image/jpeg"},
                )
            )
        )
        self.addCleanup(client.close)
        gateway = CommonsRequests(runner.journal, rd, "test", client, runner.limits)
        path = runner.preview(job, candidate, gateway)
        with Image.open(path) as image:
            self.assertEqual(image.size, (768, 768))
        self.assertEqual(len(job["budget"]["previews"]), 1)
        self.assertNotIn("finalists", job["budget"])
        self.assertEqual(rd["preview_calls"], 1)
        # Повторная подготовка из тех же SHA-проверенных байтов не скачивает файл заново.
        path.unlink()
        candidate.pop("preview_sha256")
        self.assertEqual(runner.preview(job, candidate, gateway), path)
        self.assertEqual(rd["preview_calls"], 1)

    def test_thumbnail_reassessment_preserves_budgets_and_other_rejections(self):
        runner = self.runner()
        runner.pool["global_rejections"] = {
            "thumb": "API вернул превью больше 768px; не считаем оригинал превью.",
            "rights": "Неизвестная лицензия",
        }
        job = {
            "key": "k",
            "rounds": [],
            "status": "exhausted",
            "budget": {"previews": ["thumb"], "finalists": ["saved"]},
            "shots": [],
        }
        runner.journal.state["jobs"]["k"] = job
        runner.save_pool()
        resumed = self.runner()
        saved = resumed.journal.state["jobs"]["k"]
        self.assertEqual(
            saved["budget"], {"previews": ["thumb"], "finalists": ["saved"]}
        )
        self.assertTrue(saved["thumbnail_fix_pending"])
        self.assertNotIn("thumb", resumed.pool["global_rejections"])
        self.assertEqual(
            resumed.pool["global_rejections"]["rights"], "Неизвестная лицензия"
        )
        self.assertEqual(
            resumed.pool["technical_reassessment_history"][0]["candidate_ids"],
            ["thumb"],
        )

    def test_missing_thumbnail_is_local_failure_without_retry(self):
        from commons_requests import CommonsRequests

        runner = self.runner()
        budget = {"number": 1, "calls": 0}
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(404))
        )
        self.addCleanup(client.close)
        gateway = CommonsRequests(
            runner.journal,
            budget,
            "test",
            client,
            self.settings["search"],
            sleep=lambda seconds: None,
        )
        with self.assertRaises(ProductionError) as error:
            gateway.get(
                {},
                kind="preview",
                url="https://thumb.wikimedia.org/wikipedia/commons/thumb/a/aa/Missing.jpg/960px-Missing.jpg",
                destination=self.root / "missing.jpg",
            )
        self.assertNotIsInstance(error.exception, ProductionServiceError)
        self.assertEqual(budget["preview_calls"], 1)
        self.assertEqual(budget.get("retry_calls", 0), 0)

    def test_image_connection_error_preserves_reason_and_does_not_retry(self):
        from openai import APIConnectionError

        client = MagicMock()
        client.images.generate.side_effect = APIConnectionError(
            request=httpx.Request(
                "POST", "https://api.openai.com/v1/images/generations"
            )
        )
        with patch(
            "visual_generation.dotenv_values",
            return_value={"OPENAI_API_KEY": "offline-placeholder"},
        ), patch("visual_generation.OpenAI") as sdk:
            sdk.return_value.__enter__.return_value = client
            with self.assertRaisesRegex(ProductionServiceError, "APIConnectionError"):
                generate_image(self.root, self.settings["generation"], "Тест без сети")
        self.assertEqual(client.images.generate.call_count, 1)
        ledger = read(self.root / "openai_usage.json")["requests"][-1]
        self.assertEqual(ledger["telemetry"]["status"], "failed")
        self.assertIn("Connection", str(ledger["telemetry"]["error"]["details"]))

    def test_generated_only_portrait_enlargement_preserves_native_and_cache(self):
        from visual_generation import create_material
        from visual_geometry import geometry

        shot = dict(draft("Красивый океан"), shot_id="s")
        shot["creation"].update(kind="raster", labels=[], links=[], fact_ids=[])
        settings = dict(
            self.settings["generation"], size="1024x1536", generated_upscale_limit=1.25
        )
        journal = Journal(self.root, "portrait_test", {})
        with patch(
            "visual_generation.generate_image",
            return_value=(
                raster((1024, 1536), "gold"),
                {
                    "parameters": {"model": settings["model"], "size": "1024x1536"},
                    "prompt_sha256": "test",
                    "usage": {},
                    "estimate_usd": 0.01,
                },
            ),
        ) as api:
            material = create_material(self.root, shot, settings, journal)
            self.assertEqual(
                create_material(self.root, shot, settings, journal), material
            )
            self.assertEqual(api.call_count, 1)
        self.assertEqual(material["preparation"]["scale"], 1.25)
        self.assertFalse(material["preparation"]["padding"])
        with Image.open(self.root / material["raw_path"]) as raw:
            self.assertEqual(raw.size, (1024, 1536))
        motion = geometry(self.root / material["original_path"], composition("crop", 0))
        self.assertEqual(motion["output"], [1080, 1920])
        # Геометрия исходных фото по-прежнему запрещает увеличение.
        with self.assertRaises(ProductionError):
            geometry(self.root / material["raw_path"], composition("crop", 0))

    def test_compact_input_has_no_rights_history(self):
        candidate = dict(
            metadata(),
            candidate_id="c",
            raw_extmetadata={"huge": "x" * 100000},
            rights_evidence={"huge": "y" * 100000},
            description="x" * 1000,
        )
        self.assertLess(len(json.dumps(compact_candidate(candidate))), 1200)
        self.assertNotIn("rights_evidence", compact_candidate(candidate))

    def test_image_api_one_request_and_no_sdk_retries(self):
        import base64
        from types import SimpleNamespace

        response = SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json=base64.b64encode(raster((1024, 1024))).decode()
                )
            ],
            usage=SimpleNamespace(
                model_dump=lambda **kwargs: {
                    "input_tokens_details": {"text_tokens": 10, "image_tokens": 0},
                    "output_tokens": 100,
                }
            ),
            _request_id="test_request",
        )
        client = MagicMock()
        client.images.generate.return_value = response
        with patch(
            "visual_generation.dotenv_values",
            return_value={"OPENAI_API_KEY": "offline-placeholder"},
        ), patch("visual_generation.OpenAI") as sdk:
            sdk.return_value.__enter__.return_value = client
            data, record = generate_image(
                self.root, self.settings["generation"], "Неизменённый запрос"
            )
        self.assertEqual(sdk.call_args.kwargs["max_retries"], 0)
        self.assertEqual(client.images.generate.call_count, 1)
        self.assertEqual(client.images.generate.call_args.kwargs["quality"], "medium")
        self.assertEqual(record["parameters"]["model"], "gpt-image-2.5-flare")
        self.assertTrue(record["estimate_usd"] > 0)

    def test_second_generation_only_for_concrete_fix_and_cached_after_resume(self):
        runner = self.runner()
        runner.limits = self.settings["search"]
        shot = dict(draft("Текст", "explanatory_required"), shot_id="s")
        shot["creation"]["kind"] = "raster"
        job = {"shots": [shot], "approved": {}, "budget": {}, "rejections": []}

        def review(job, rd, shot, candidate, composition):
            if not job["rejections"]:
                job["rejections"].append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "shot_id": "s",
                        "reason": "Нечитаемая подпись",
                    }
                )
                return None
            return {"status": "approved"}

        runner.prepare_and_review = review
        with patch(
            "visual_generation.generate_image",
            return_value=(
                raster((1024, 1024)),
                {
                    "parameters": {"model": "test"},
                    "prompt_sha256": "hash",
                    "usage": {},
                    "estimate_usd": 0.01,
                },
            ),
        ) as api:
            runner.create_missing(job, {})
            self.assertEqual(api.call_count, 2)
            self.assertEqual(len(job["budget"]["finalists"]), 2)
            runner.create_missing(job, {})
            self.assertEqual(api.call_count, 2)
        self.assertEqual(job["approved"]["s"]["selection_mode"], "created")

    def test_fast_batch_does_not_build_html_or_refresh_costs(self):
        from test_full_voiceover import make_script
        from visuals_only_batch import run_batch

        source = self.root / "source"
        make_script(source)
        batch = self.root / "batch"
        batch.mkdir()
        atomic_write_json(
            batch / "batch.json",
            {"entries": [{"topic": "Тест", "directory": str(source)}]},
        )

        def visuals(directory):
            path = directory / "visual_materials.json"
            atomic_write_json(path, {"status": "needs_visual", "fragments": []})
            return path

        with patch(
            "visuals_only_batch.run_visual_materials", side_effect=visuals
        ), patch("visuals_only_batch.build_report") as html, patch(
            "billing_sync.refresh_after_report"
        ) as costs:
            result = run_batch(batch, self.root / "result")
        self.assertEqual(result.suffix, ".json")
        html.assert_not_called()
        costs.assert_not_called()

    def test_review_package_is_allowlisted_and_blocks_embedded_payload(self):
        import build_review_archive as builder

        root = self.root / "review"
        root.mkdir()
        (root / "selected_previews").mkdir()
        atomic_write_json(root / "summary.json", {"schema_version": 1})
        (root / "changes.txt").write_text("Изменения")
        preview = root / "selected_previews" / "picked.jpg"
        preview.write_bytes(raster((270, 480)))
        atomic_write_json(
            root / "visuals.json",
            {"shots": [{"preview": "selected_previews/picked.jpg"}]},
        )
        (root / "ignored.json").write_text("DO NOT PACKAGE")
        with patch.object(builder, "PROJECT_DIRECTORY", self.root):
            selected = builder.curated_review_files(())
            self.assertNotIn(root / "ignored.json", selected)
            self.assertNotIn(root / "ignored.json", builder.collect_project_files())
            preview.write_bytes(preview.read_bytes() + b"forbidden payload")
            with self.assertRaises(Exception):
                builder.curated_review_files(())
            atomic_write_json(
                root / "visuals.json", {"shots": [{"preview": "../outside.jpg"}]}
            )
            with self.assertRaises(ValueError):
                builder.curated_review_files(())

    def test_estimate_keeps_cached_rates_separate(self):
        self.assertEqual(
            image_estimate(
                {
                    "input_tokens_details": {
                        "text_tokens": 100,
                        "image_tokens": 200,
                        "cached_tokens_details": {
                            "text_tokens": 20,
                            "image_tokens": 50,
                        },
                    },
                    "output_tokens": 100,
                }
            ),
            0.004725,
        )


if __name__ == "__main__":
    unittest.main()
