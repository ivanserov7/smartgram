"""Новая стратегия: настоящий разбор HTTP/PNG, подставные модели; сеть запрещена."""

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from commons_materials import (
    parse_pages,
    resolve_rights,
    fetch_material,
    public_domain_proof,
    search_files,
)
from commons_requests import CommonsRequests, CommonsPaused, RequestExhausted
from file_io import atomic_write_json
from production_state import Journal, ProductionError, config, read, digest
from shot_materials import ShotMaterials
from test_production import composition, metadata
from visual_shots import bind_shots, validate_shot_ranges, shot_timeline


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (2000, 1200), "navy").save(buffer, format="PNG")
    return buffer.getvalue()


def candidate():
    value = metadata()
    value.update(
        original_mime="image/png",
        mime="image/png",
        thumbnail_mime="image/png",
        thumbnail_url="https://upload.wikimedia.org/wikipedia/commons/thumb/a/aa/Earth.png/2000px-Earth.png",
        original_width=2000,
        original_height=1200,
        original_size_bytes=30_000_000,
    )
    return value


def page_response():
    c = candidate()
    fields = {
        key: {"value": value}
        for key, value in {
            "Artist": c["author"],
            "LicenseShortName": c["license"],
            "LicenseUrl": c["license_url"],
            "ObjectName": "Earth",
        }.items()
    }
    return {
        "query": {
            "pages": [
                {
                    "title": c["file_title"],
                    "imageinfo": [
                        {
                            "url": c["original_url"],
                            "descriptionurl": c["source_page"],
                            "width": 2000,
                            "height": 1200,
                            "size": 30_000_000,
                            "mime": "image/png",
                            "thumburl": c["thumbnail_url"],
                            "thumbmime": "image/png",
                            "thumbwidth": 2000,
                            "thumbheight": 1200,
                            "extmetadata": fields,
                        }
                    ],
                }
            ]
        }
    }


class ImageSearchFixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def gateway(self, handler, clock=None):
        settings = config()["visuals"]["search"]
        budget = {"number": 1, "calls": 0}
        journal = Journal(self.root, "test_commons", {})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        clock = clock if clock is not None else [100.0]

        def sleep(seconds):
            clock[0] += seconds

        gateway = CommonsRequests(
            journal, budget, "test", client, settings, sleep=sleep, now=lambda: clock[0]
        )
        return gateway, budget, clock

    def test_exact_compound_plan_stable_ids_and_no_gaps(self):
        f = {
            "text": "Фото осьминога показывает окраску. Схема кожи объясняет механизм.",
            "visual": "Фото, затем схема",
        }
        drafts = [
            {
                "text": part,
                "visual_task": part,
                "queries": ["octopus"],
                "must_show": [],
                "must_not_show": [],
                "preferences": [],
            }
            for part in [
                "Фото осьминога показывает окраску.",
                "Схема кожи объясняет механизм.",
            ]
        ]
        shots = bind_shots(f, 1, drafts)
        validate_shot_ranges(f, shots)
        self.assertEqual("".join(s["text"] for s in shots), f["text"])
        self.assertEqual(
            [s["shot_id"] for s in shots],
            [s["shot_id"] for s in bind_shots(f, 1, drafts)],
        )
        broken = copy.deepcopy(shots)
        broken[1]["text_start"] += 1
        with self.assertRaises(ProductionError):
            validate_shot_ranges(f, broken)

    def test_single_shot_legacy_timeline_and_multishot_real_word_times(self):
        script = {
            "fragments": [{"text": "Раз два три. Четыре пять шесть.", "visual": "Two"}]
        }
        drafts = [
            {
                "text": text,
                "visual_task": "Тест",
                "queries": ["Earth"],
                "must_show": [],
                "must_not_show": [],
                "preferences": [],
            }
            for text in ["Раз два три.", "Четыре пять шесть."]
        ]
        shots = bind_shots(script["fragments"][0], 1, drafts)
        parent = {
            "fragment_index": 1,
            "first_word_id": 1,
            "last_word_id": 6,
            "speech_start": 1,
            "speech_end": 6.4,
            "display_start": 0,
            "display_end": 8,
        }
        words = [
            {"word_id": i + 1, "word": word, "start": i + 1, "end": i + 1.4}
            for i, word in enumerate(["раз", "два", "три", "четыре", "пять", "шесть"])
        ]
        result = shot_timeline(
            script, {"fragments": [{"shots": shots}]}, [parent], words
        )
        self.assertEqual(
            [(x["display_start"], x["display_end"]) for x in result], [(0, 4), (4, 8)]
        )
        old = shot_timeline(
            script, {"fragments": [{"status": "approved"}]}, [parent], words
        )
        self.assertEqual(old[0]["display_end"], 8)
        words[1]["word"] = "другое"
        with self.assertRaisesRegex(ProductionError, "awaiting_word_timings"):
            shot_timeline(script, {"fragments": [{"shots": shots}]}, [parent], words)

    def test_empty_query_broadens_and_categories_are_real_api_titles(self):
        requests = []

        def handler(request):
            p = dict(request.url.params)
            requests.append(p)
            return httpx.Response(
                200,
                json=(
                    {}
                    if p.get("gsrsearch") == "octopus chromatophores"
                    else page_response()
                ),
            )

        gateway, budget, _ = self.gateway(handler)
        self.assertEqual(
            search_files(gateway, "octopus chromatophores")["candidates"], []
        )
        self.assertTrue(search_files(gateway, "octopus")["candidates"])
        self.assertEqual(
            [p["gsrsearch"] for p in requests], ["octopus chromatophores", "octopus"]
        )
        self.assertFalse(any("filetype:bitmap" in str(p) for p in requests))
        self.assertEqual(budget["calls"], 2)

    def test_svg_is_retained_and_real_png_dimensions_not_nominal(self):
        png = png_bytes()
        requests = []
        c = candidate()
        c.update(
            candidate_id="svg_test",
            original_mime="image/svg+xml",
            original_width=100,
            original_height=60,
            original_url="https://upload.wikimedia.org/wikipedia/commons/a/aa/Test.svg",
        )

        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(
                200,
                content=(
                    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="60"/>'
                    if str(request.url).endswith(".svg")
                    else png
                ),
                headers={
                    "content-type": (
                        "image/svg+xml"
                        if str(request.url).endswith(".svg")
                        else "image/png"
                    )
                },
            )

        gateway, _, _ = self.gateway(handler)
        material = fetch_material(c, gateway)
        self.assertEqual((material["width"], material["height"]), (2000, 1200))
        self.assertTrue((self.root / material["source_original_path"]).exists())
        self.assertEqual((self.root / material["original_path"]).read_bytes(), png)
        self.assertEqual(len(requests), 2)

    def test_heavy_photo_downloads_provided_sufficient_thumbnail(self):
        requests = []

        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(
                200, content=png_bytes(), headers={"content-type": "image/png"}
            )

        gateway, _, _ = self.gateway(handler)
        c = dict(candidate(), candidate_id="photo_test")
        material = fetch_material(c, gateway)
        self.assertEqual(requests, [c["thumbnail_url"]])
        self.assertEqual(material["derivative_of"], c["original_url"])

    def test_missing_license_url_reads_real_link_and_unknown_rights_rejected(self):
        c = dict(candidate(), license_url="")
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "parse": {
                        "title": c["file_title"],
                        "text": '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>',
                        "templates": [],
                    }
                },
            )

        gateway, budget, _ = self.gateway(handler)
        verified = resolve_rights(c, gateway)
        self.assertEqual(
            verified["license_url"], "https://creativecommons.org/licenses/by/4.0/"
        )
        self.assertEqual(budget["metadata_calls"], 1)
        c["license"] = "Unknown rights"
        with self.assertRaisesRegex(ProductionError, "не подтверждён"):
            resolve_rights(c, gateway)
        self.assertEqual(len(calls), 1)  # Тот же безопасный ответ из кеша.

    def test_public_domain_needs_explicit_source_template_and_copyright_flag(self):
        c = dict(
            candidate(),
            license="Public domain",
            license_url="",
            raw_extmetadata={"Copyrighted": {"value": "False"}},
        )

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "parse": {
                        "title": c["file_title"],
                        "text": "Public domain",
                        "templates": [{"title": "Template:PD-self"}],
                    }
                },
            )

        gateway, _, _ = self.gateway(handler)
        verified = resolve_rights(c, gateway)
        self.assertTrue(public_domain_proof(verified))
        self.assertEqual(verified["license_url"], "")
        c["raw_extmetadata"]["Copyrighted"]["value"] = "True"
        with self.assertRaises(ProductionError):
            resolve_rights(c, gateway)

    def test_transient_http_and_maxlag_use_shared_two_retry_budget(self):
        hits = []

        def handler(request):
            hits.append(request)
            if len(hits) == 1:
                return httpx.Response(
                    200,
                    json={"error": {"code": "maxlag", "info": "Busy"}},
                    headers={"Retry-After": "2"},
                )
            if len(hits) == 2:
                return httpx.Response(503, headers={"Retry-After": "3"})
            return httpx.Response(200, json={"ok": True})

        gateway, budget, clock = self.gateway(handler)
        self.assertTrue(gateway.get({"q": "test"})["ok"])
        self.assertEqual((budget["calls"], budget["retry_calls"], len(hits)), (1, 2, 3))
        gateway.get({"q": "test"})
        self.assertEqual(len(hits), 3)

    def test_pause_resume_keeps_success_and_attempt_budget_without_paid_retry(self):
        hits = []

        def handler(request):
            hits.append(dict(request.url.params))
            if request.url.params.get("q") == "busy":
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"ok": True})

        gateway, budget, clock = self.gateway(handler)
        gateway.journal.state["jobs"]["test"] = {"rounds": [budget]}
        gateway.journal.save()
        gateway.get({"q": "success"})
        with self.assertRaises(CommonsPaused) as paused:
            gateway.get({"q": "busy"})
        self.assertEqual(len(hits), 4)
        with self.assertRaises(CommonsPaused):
            gateway.get({"q": "busy"})
        # Перезапускаем журнал, а не только функцию: затраты и HTTP-попытки остаются.
        restarted = Journal(self.root, "test_commons", {})
        budget = restarted.state["jobs"]["test"]["rounds"][0]
        gateway = CommonsRequests(
            restarted,
            budget,
            "test",
            gateway.client,
            config()["visuals"]["search"],
            sleep=lambda seconds: None,
            now=lambda: clock[0],
        )
        clock[0] = paused.exception.retry_at + 1
        with self.assertRaises(RequestExhausted):
            gateway.get({"q": "busy"})
        gateway.get({"q": "other"})
        gateway.get({"q": "success"})
        self.assertEqual((budget["calls"], budget["retry_calls"], len(hits)), (3, 2, 5))
        calls = []

        def failed():
            calls.append(1)
            raise ProductionError("Paid failed")

        for _ in range(2):
            with self.assertRaises(ProductionError):
                gateway.journal.operation("visual_select", {}, failed)
        self.assertEqual(calls, [1])

    def test_parent_shots_share_limits_and_resume_does_not_repeat_models(self):
        cfg = config()["visuals"]
        cfg["search"]["request_interval_seconds"] = 0
        fragments = {
            "text": "Первый объект хорошо виден. Второй объект хорошо виден. Третий объект хорошо виден.",
            "visual": "Три кадра",
        }
        chunks = [
            "Первый объект хорошо виден.",
            "Второй объект хорошо виден.",
            "Третий объект хорошо виден.",
        ]
        models = []
        http = []

        def model(step, settings, schema, data, images):
            models.append(step)
            return {
                "fragments": [
                    {
                        "fragment_index": 1,
                        "shots": [
                            {
                                "text": text,
                                "visual_task": "Объект",
                                "queries": ["specific object", "object"],
                                "must_show": [],
                                "must_not_show": [],
                                "preferences": [],
                            }
                            for text in chunks
                        ],
                    }
                ]
            }

        def client():
            def handler(request):
                http.append(str(request.url))
                return httpx.Response(200, json={})

            return httpx.Client(transport=httpx.MockTransport(handler))

        script = {"script_version_id": "test", "fragments": [fragments]}
        with patch("shot_materials.create_commons_client", side_effect=client):
            runner = ShotMaterials(self.root, settings=cfg, model=model)
            runner.run(script)
            rounds = next(iter(runner.journal.state["jobs"].values()))["rounds"]
            self.assertEqual(len(rounds), 2)
            self.assertTrue(all(r["calls"] <= 6 for r in rounds))
            self.assertLessEqual(len(http), 12)
            count = len(http)
            ShotMaterials(
                self.root,
                settings=cfg,
                model=lambda *args: (_ for _ in ()).throw(
                    AssertionError("Paid repeated")
                ),
            ).run(script)
            self.assertEqual(len(http), count)
            self.assertEqual(models, ["visual_queries", "visual_queries"])

    def test_model_sees_pyramid_neighbour_and_three_background_previews(self):
        cfg = config()["visuals"]
        cfg["search"]["request_interval_seconds"] = 0
        observations = []
        fragment = {
            "text": "Пирамида хорошо видна рядом со Сфинксом.",
            "visual": "Пирамида; соседний Сфинкс допустим.",
        }

        def model(step, settings, schema, data, images):
            observations.append((step, len(images)))
            if step == "visual_queries":
                return {
                    "fragments": [
                        {
                            "fragment_index": 1,
                            "shots": [
                                {
                                    "text": fragment["text"],
                                    "visual_task": "Видимая пирамида",
                                    "queries": ["pyramid"],
                                    "must_show": ["pyramid"],
                                    "must_not_show": [],
                                    "preferences": ["background"],
                                }
                            ],
                        }
                    ]
                }
            if step == "visual_select":
                return {
                    "fragment_index": 1,
                    "status": "selected",
                    "primary_candidate_id": data["candidates"][0]["candidate_id"],
                    "backup_candidate_id": None,
                    "reason": "ТЕСТ: соседний объект не запрещён",
                    "rejected_candidates": [],
                    "composition": composition("background", 0),
                }
            if step == "visual_review":
                for _, path in images:
                    self.assertTrue(Path(path).is_file())
                return {
                    "fragment_index": 1,
                    "decision": "approved",
                    "issues": [],
                    "corrections": None,
                }
            raise AssertionError(step)

        def client():
            return httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: (
                        httpx.Response(
                            200,
                            content=png_bytes(),
                            headers={"content-type": "image/png"},
                        )
                        if request.url.host == "upload.wikimedia.org"
                        else httpx.Response(200, json=page_response())
                    )
                )
            )

        with patch("shot_materials.create_commons_client", side_effect=client):
            result = read(
                ShotMaterials(self.root, settings=cfg, model=model).run(
                    {"script_version_id": "test", "fragments": [fragment]}
                )
            )
        self.assertEqual(result["status"], "approved")
        self.assertIn(("visual_select", 1), observations)
        self.assertIn(("visual_review", 4), observations)
        self.assertEqual(
            [
                p["position"]
                for p in result["fragments"][0]["shots"][0]["asset"]["motion"][
                    "previews"
                ]
            ],
            [0, 0.5, 1],
        )
        self.assertFalse((self.root / "audio_manifest.json").exists())

    def test_verified_category_is_used_without_invented_names(self):
        from commons_materials import search_categories, category_files

        calls = []

        def handler(request):
            params = dict(request.url.params)
            calls.append(params)
            if params.get("list") == "search":
                return httpx.Response(
                    200,
                    json={"query": {"search": [{"title": "Category:Chromatophores"}]}},
                )
            return httpx.Response(200, json=page_response())

        gateway, budget, _ = self.gateway(handler)
        categories = search_categories(gateway, "chromatophore")
        self.assertTrue(category_files(gateway, categories[0])["candidates"])
        self.assertEqual(calls[1]["gcmtitle"], calls[0] and categories[0])
        self.assertEqual(budget["calls"], 2)

    def test_download_404_is_local_not_global_service_pause(self):
        gateway, _, _ = self.gateway(lambda request: httpx.Response(404))
        with self.assertRaises(ProductionError) as error:
            gateway.get(
                {},
                kind="download",
                url="https://upload.wikimedia.org/wikipedia/commons/a/aa/Missing.png",
                destination=self.root / "missing.png",
            )
        self.assertNotIsInstance(error.exception, CommonsPaused)

    def test_invalid_selection_repair_preserves_raw_reply_and_search_budget(self):
        from repair_visual_selection import repair_saved_selection

        image = self.root / "candidate.png"
        image.write_bytes(png_bytes())
        c = dict(candidate(), candidate_id="current", original_path=image.name)
        shot = {
            "shot_id": "f1_test",
            "fragment_index": 1,
            "text": "Один исходный текст",
            "visual_task": "Картинка",
        }
        data = {
            "shot": shot,
            "fragment": {
                "fragment_index": 1,
                "text": shot["text"],
                "visual": shot["visual_task"],
            },
            "candidates": [c],
        }
        invalid = {
            "fragment_index": 1,
            "status": "needs_search",
            "primary_candidate_id": None,
            "backup_candidate_id": None,
            "reason": "Не подходит",
            "rejected_candidates": [
                {"candidate_id": "historical", "reason": "Старый кандидат"}
            ],
            "composition": None,
        }
        operation = {
            "step": "visual_select",
            "status": "completed",
            "identity": {"data": data},
            "result": invalid,
        }
        state = {
            "schema_version": 1,
            "snapshots": {},
            "operations": {"old": operation},
            "jobs": {
                "test": {
                    "key": "test",
                    "fragment_index": 1,
                    "rounds": [{"number": 1, "calls": 6, "candidates": [c]}],
                    "shots": [shot],
                    "approved": {},
                    "rejections": [],
                    "corrections": {},
                    "status": "running",
                }
            },
        }
        atomic_write_json(self.root / "visual_materials_state.json", state)
        valid = dict(
            invalid,
            rejected_candidates=[
                {"candidate_id": "current", "reason": "Текущая картинка не подходит"}
            ],
        )
        with patch(
            "production_state.ModelClient.__call__", return_value=valid
        ) as model:
            self.assertEqual(repair_saved_selection(self.root), 1)
            self.assertEqual(repair_saved_selection(self.root), 0)
            self.assertEqual(model.call_count, 1)
        saved = read(self.root / "visual_materials_state.json")
        self.assertEqual(saved["operations"]["old"]["result"], invalid)
        self.assertEqual(saved["jobs"]["test"]["rounds"][0]["calls"], 6)

    def test_technical_cache_upgrade_checks_files_and_does_not_override_limits(self):
        from revalidate_visual_cache import revalidate_cache
        from visual_geometry import prepare
        from production_state import sha

        cfg = config()["visuals"]
        old = copy.deepcopy(cfg)
        old["implementation_sha256"]["visual_shots.py"] = "previous_code"
        image = self.root / "image.png"
        image.write_bytes(png_bytes())
        motion = prepare(image, composition("background", 0), self.root / "prepared")
        for preview in motion["previews"]:
            preview["path"] = str(Path(preview["path"]).relative_to(self.root))
        fragment = {"text": "Весь исходный текст", "visual": "Иллюстрация"}
        shot = {
            "shot_id": "stable",
            "fragment_index": 1,
            "text": fragment["text"],
            "text_start": 0,
            "text_end": len(fragment["text"]),
        }
        result = dict(
            shot,
            status="approved",
            review={"decision": "approved"},
            asset={
                "original_path": image.name,
                "original_sha256": sha(image),
                "source": dict(metadata(), original_mime="image/png"),
                "motion": motion,
            },
        )
        old_id = digest(old)
        state = {
            "schema_version": 1,
            "operations": {},
            "snapshots": {old_id: old},
            "jobs": {
                "test": {
                    "fragment": fragment,
                    "shots": [shot],
                    "approved": {"stable": result},
                    "approved_settings_id": old_id,
                }
            },
        }
        atomic_write_json(self.root / "visual_materials_state.json", state)
        self.assertEqual(revalidate_cache(self.root), 1)
        upgraded = read(self.root / "visual_materials_state.json")["jobs"]["test"]
        self.assertEqual(
            upgraded["technical_revalidations"][0]["new_model_requests"], 0
        )
        old["preparation"]["max_zoom_percent"] = 9
        new_old_id = digest(old)
        state["snapshots"] = {new_old_id: old}
        state["jobs"]["test"]["approved_settings_id"] = new_old_id
        atomic_write_json(self.root / "visual_materials_state.json", state)
        with self.assertRaisesRegex(ProductionError, "ограничения изменились"):
            revalidate_cache(self.root)
        old["preparation"]["max_zoom_percent"] = cfg["preparation"]["max_zoom_percent"]
        old_id = digest(old)
        state["snapshots"] = {old_id: old}
        state["jobs"]["test"]["approved_settings_id"] = old_id
        atomic_write_json(self.root / "visual_materials_state.json", state)
        image.write_bytes(b"broken")
        with self.assertRaises(ProductionError):
            revalidate_cache(self.root)
        self.assertEqual(
            read(self.root / "visual_materials_state.json")["jobs"]["test"][
                "approved_settings_id"
            ],
            old_id,
        )


if __name__ == "__main__":
    unittest.main()
