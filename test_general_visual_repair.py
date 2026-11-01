"""Общий ремонт: реальные ветки/геометрия и подставные модель, поиск, Image API."""

import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from PIL import Image

from fast_materials import FastMaterials, candidate_id
from production_state import (
    config,
    read,
    sha,
    digest,
    ProductionError,
    ProductionServiceError,
)
from file_io import atomic_write_json
from visual_shots import bind_shots
from visual_repair import start_repair, resolve_targets, assessment_key
from visual_sequence import first_appearance_sequence, validate_sequence, same_source
from editing_inputs import checked_visuals
from editing import validate_batch, ready
from test_production import metadata, composition


class GeneralRepairTests(unittest.TestCase):
    def setUp(self):
        # Даже непредусмотренная ветка не сможет обратиться к платному API.
        for target in ("socket.socket.connect", "socket.getaddrinfo"):
            guard = patch(
                target,
                side_effect=AssertionError("Real network forbidden in repair tests"),
            )
            guard.start()
            self.addCleanup(guard.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calls = []
        self.fail_step = None
        self.reject = False
        self.script = {
            "script_version_id": "offline-approved",
            "fragments": [
                {
                    "text": "Первый объект знакомит зрителя. Второй показывает смысл рассказа.",
                    "visual": "Объекты",
                }
            ],
        }
        atomic_write_json(self.root / "script.json", self.script)
        atomic_write_json(
            self.root / "research.json", {"facts": [{"statement": "Тестовый факт."}]}
        )
        settings = config()["visuals"]
        self.runner = FastMaterials(self.root, settings, self.model)
        self.runner.facts = []
        self.runner.limits = settings["search"]
        self.shots = bind_shots(
            self.script["fragments"][0],
            1,
            [
                self.draft("Первый объект знакомит зрителя."),
                self.draft("Второй показывает смысл рассказа."),
            ],
        )
        self.parent = {
            "key": "parent",
            "fragment": self.script["fragments"][0],
            "fragment_index": 1,
            "shots": self.shots,
            "approved": {},
            "rounds": [
                {
                    "number": 1,
                    "calls": 0,
                    "finished": True,
                    "preview_ids": [str(i) for i in range(10)],
                },
                {
                    "number": 2,
                    "calls": 0,
                    "finished": True,
                    "preview_ids": [str(i) for i in range(10, 20)],
                },
            ],
            "budget": {
                "cards": [str(i) for i in range(100)],
                "previews": [str(i) for i in range(20)],
                "finalists": ["old1", "old2", "old3"],
            },
            "corrections": {},
            "rejections": [],
            "status": "approved",
            "workflow_version": 3,
            "plan_import_complete": True,
        }
        for number, shot in enumerate(self.shots):
            c = self.candidate("initial" + str(number), ["navy", "olive"][number])
            rd = {
                "selection_reasons": {
                    shot["shot_id"] + ":" + c["candidate_id"]: "TEST accepted source"
                }
            }
            result = self.runner.prepare_and_review(
                self.parent, rd, shot, c, composition("crop", 0)
            )
            self.parent["approved"][shot["shot_id"]] = result
        self.key = digest({"fragment": self.script["fragments"][0], "index": 1})
        self.runner.journal.state["jobs"][self.key] = self.parent
        self.runner.journal.save()
        self.visuals = {
            "schema_version": 2,
            "mode": "real",
            "script_version_id": self.script["script_version_id"],
            "settings_snapshot": settings,
            "status": "approved",
            "fragments": [self.runner.fragment_result(self.parent)],
        }
        atomic_write_json(self.root / "visual_materials.json", self.visuals)
        self.issue = {
            "frame_id": "frame_000060",
            "fragment_index": 1,
            "cue_id": 2,
            "problem": "Нерелевантный объект в кадре.",
            "correction": "Покажи тематический природный пейзаж.",
            "target_step": "visual_materials",
        }
        (self.root / "candidate.mp4").write_bytes(
            b"TEST reviewed candidate; not an actual rendered movie"
        )
        atomic_write_json(
            self.root / "quality_report.json",
            {
                "candidate_sha256": sha(self.root / "candidate.mp4"),
                "samples": [
                    {
                        "frame_id": "frame_000060",
                        "cue_id": 2,
                        "visual_section_id": "section_2",
                    }
                ],
            },
        )
        atomic_write_json(
            self.root / "montage_plan.json",
            {
                "sections": [
                    {
                        "section_id": "section_1",
                        "shot_id": self.shots[0]["shot_id"],
                        "start_frame": 0,
                        "end_frame": 60,
                    },
                    {
                        "section_id": "section_2",
                        "shot_id": self.shots[1]["shot_id"],
                        "start_frame": 60,
                        "end_frame": 120,
                    },
                ]
            },
        )
        self.calls.clear()
        self.client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: self.fail("Unexpected HTTP; API must be substituted")
            )
        )
        self.addCleanup(self.client.close)

    def draft(self, text):
        return {
            "text": text,
            "visual_task": "Первоначальный замысел",
            "queries": ["old query"],
            "must_show": [],
            "must_not_show": [],
            "preferences": [],
            "role": "illustrative",
            "role_reason": "Тематический видеоряд",
            "creation": {
                "kind": "raster",
                "prompt": "Old generated idea",
                "labels": [],
                "links": [],
                "fact_ids": [],
            },
        }

    def candidate(self, name, color="teal"):
        path = self.root / (name + ".jpg")
        Image.new("RGB", (1300, 2300), color).save(path)
        source = dict(
            metadata(),
            file_title="File:" + name + ".jpg",
            title=name,
            source_page="https://commons.wikimedia.org/wiki/File:" + name + ".jpg",
            original_path=path.name,
            file_sha256=sha(path),
            source_sha1=digest(name),
            original_width=1300,
            original_height=2300,
        )
        source["candidate_id"] = candidate_id(source)
        preview = (
            self.root
            / "visual_materials/thumbnails"
            / (source["candidate_id"] + ".jpg")
        )
        preview.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (200, 300), color).save(preview)
        source["preview_sha256"] = sha(preview)
        return source

    def model(self, step, settings, schema, data, images):
        self.calls.append((step, copy.deepcopy(data)))
        if step == self.fail_step:
            raise ProductionServiceError("TEST HTTP 429 insufficient_quota")
        if step == "visual_queries":
            shots = copy.deepcopy(data["existing_shots"])
            for shot in shots:
                if shot["shot_id"] in data["affected_shot_ids"]:
                    shot["queries"] = ["new thematic landscape"]
                    shot["visual_task"] = (
                        "Обновлённый замысел: "
                        + data["final_repair_context"]["issues"][0]["correction"]
                    )
                    shot["creation"]["prompt"] = (
                        "New landscape: "
                        + data["final_repair_context"]["issues"][0]["correction"]
                    )
            return {"fragment_index": 1, "shots": shots}
        if step == "visual_select":
            return {
                "assessments": [
                    {
                        "shot_id": s["shot_id"],
                        "candidate_id": c["candidate_id"],
                        "suitable": not self.reject,
                        "selection_mode": "illustrative_background",
                        "score": 90,
                        "reason": "TEST thematic picture",
                        "composition": composition("crop", 0),
                    }
                    for s in data["shots"]
                    for c in data["candidates"]
                ]
            }
        if step == "visual_review":
            return {
                "fragment_index": 1,
                "decision": "approved",
                "issues": [],
                "corrections": None,
            }
        self.fail("Unknown model step " + step)

    def run_repair(self, search=None):
        with patch(
            "fast_materials.create_commons_client",
            side_effect=lambda: httpx.Client(
                transport=httpx.MockTransport(lambda r: self.fail("Unexpected HTTP"))
            ),
        ), patch("fast_materials.resolve_rights", side_effect=lambda c, g: c), patch(
            "fast_materials.search_first",
            side_effect=search
            or (
                lambda *a, **k: {
                    "candidates": [],
                    "continue": {},
                    "provider": "wikimedia",
                }
            ),
        ):
            return self.runner.repair_approved(
                self.script,
                read(self.root / "visual_materials.json"),
                {1},
                [self.issue],
            )

    def work(self):
        return self.runner.journal.state["jobs"][
            self.parent["pending_repair"]["work_key"]
        ]

    def test_final_issue_to_replacement_preserves_good_shot_and_passes_checks(self):
        good = copy.deepcopy(self.parent["approved"][self.shots[0]["shot_id"]])
        before = copy.deepcopy(self.parent["budget"])
        replacement = self.candidate("new-landscape")
        search_calls = []

        def search(*args, **kwargs):
            search_calls.append(1)
            return {
                "candidates": [replacement],
                "continue": {},
                "provider": "wikimedia",
            }

        updated = self.run_repair(search)
        self.assertEqual(updated["status"], "approved")
        self.assertTrue(search_calls)
        self.assertEqual(updated["fragments"][0]["shots"][0], good)
        self.assertEqual(self.parent["budget"], before)
        self.assertEqual(
            self.work()["rounds"][0]["preview_ids"], [replacement["candidate_id"]]
        )
        self.assertIn("new thematic", self.work()["shots"][0]["queries"][0])
        assets, size = checked_visuals(self.script, updated, self.root)
        selected, times = first_appearance_sequence(
            assets,
            [
                {
                    "fragment_index": 1,
                    "shot_id": s["shot_id"],
                    "display_start": i * 2,
                    "display_end": i * 2 + 2,
                }
                for i, s in enumerate(updated["fragments"][0]["shots"])
            ],
        )
        validate_sequence(selected, times)
        batch = [{"frame_id": "frame_000060", "fragment_index": 1, "cue_id": 2}]
        verdict = {
            "decision": "approved",
            "issues": [],
            "reviewed_frame_ids": ["frame_000060"],
        }
        validate_batch(verdict, batch)
        self.assertTrue(ready({"passed": True}, [verdict], ["frame_000060"]))
        # Тот же ремонт после resume не вызывает ни модель, ни поиск ещё раз.
        identifier = self.parent["pending_repair"]["repair_id"]
        calls = len(self.calls)
        operations = copy.deepcopy(self.runner.journal.state["operations"])
        self.run_repair(lambda *a, **k: self.fail("No repeated search"))
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.runner.journal.state["operations"], operations)
        self.assertEqual(self.parent["pending_repair"]["repair_id"], identifier)

    def test_different_corrections_change_plan_but_not_boundaries(self):
        for correction in [
            "Покажи ночное небо и звёзды.",
            "Покажи берег и открытое море.",
        ]:
            self.issue["correction"] = correction
            # У каждой независимой темы собственный журнал и отдельный ID.
            self.parent.pop("pending_repair", None)
            self.parent.pop("repair_jobs", None)
            self.parent["approved"] = {
                s["shot_id"]: copy.deepcopy(s)
                for s in self.visuals["fragments"][0]["shots"]
            }
            atomic_write_json(self.root / "visual_materials.json", self.visuals)
            start_repair(self.runner, self.script, self.visuals, [self.issue])
            work = self.work()
            self.runner.plan_repair(work)
            old = self.shots[1]
            new = work["shots"][0]
            self.assertEqual(
                [new[k] for k in ("shot_id", "text", "text_start", "text_end")],
                [old[k] for k in ("shot_id", "text", "text_start", "text_end")],
            )
            self.assertIn(correction, new["creation"]["prompt"])
            self.assertIn(correction, new["visual_task"])

    def test_three_originals_do_not_block_generated_reserve_and_cache_is_repair_specific(
        self,
    ):
        generated = []

        def image_api(directory, settings, prompt):
            generated.append((settings, prompt))
            out = io.BytesIO()
            Image.new("RGB", (1024, 1536), "maroon").save(out, "JPEG")
            return out.getvalue(), {
                "parameters": {
                    k: settings[k] for k in ("model", "quality", "n", "size")
                },
                "prompt_sha256": digest(prompt),
                "usage": {"input_tokens": 1, "output_tokens": 2},
            }

        from visual_generation import create_material

        old_shot = self.shots[1]
        # Уже существующая генерация с прежним замыслом не заменяет исправленную.
        with patch("visual_generation.generate_image", side_effect=image_api):
            old = create_material(
                self.root,
                old_shot,
                dict(
                    self.runner.settings["generation"],
                    size="1024x1536",
                    generated_upscale_limit=1.25,
                ),
                self.runner.journal,
            )
            updated = self.run_repair()
        self.assertEqual(len(generated), 2)
        self.assertEqual(updated["status"], "approved")
        new = updated["fragments"][0]["shots"][1]["asset"]["source"]
        self.assertNotEqual(old["candidate_id"], new["candidate_id"])
        self.assertIn("Покажи тематический", generated[-1][1])
        self.assertEqual(generated[-1][0]["generated_upscale_limit"], 1.25)
        self.assertEqual(self.parent["budget"]["finalists"], ["old1", "old2", "old3"])
        self.assertEqual(
            len(self.work()["budget"]["generated:" + old_shot["shot_id"]]), 1
        )

    def test_service_error_preserves_id_limits_history_and_never_retries_paid_operation(
        self,
    ):
        self.fail_step = "visual_queries"
        with self.assertRaisesRegex(ProductionServiceError, "429"):
            self.run_repair()
        old = copy.deepcopy(self.work())
        calls = len(self.calls)
        self.fail_step = None
        self.runner = FastMaterials(self.root, self.runner.settings, self.model)
        self.parent = self.runner.journal.state["jobs"][self.key]
        with self.assertRaisesRegex(ProductionServiceError, "429"):
            self.run_repair()
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.work()["repair_id"], old["repair_id"])
        self.assertEqual(self.work()["search_limits"], old["search_limits"])
        self.assertEqual(self.work()["budget"], old["budget"])
        self.assertIn("429", read(self.root / "visual_materials.json")["service_error"])
        self.assertNotIn(
            "исчерпан", read(self.root / "visual_materials.json")["service_error"]
        )

    def test_old_positive_assessment_and_rejected_source_alias_cannot_bypass_repair(
        self,
    ):
        old = self.parent["approved"][self.shots[1]["shot_id"]]["asset"]
        renamed = dict(
            old["source"], candidate_id="different-id", file_title="File:Renamed.jpg"
        )
        self.runner.pool["candidates"]["different-id"] = renamed
        self.parent["assessments"] = {
            self.shots[1]["shot_id"] + ":different-id": {"suitable": True}
        }
        start_repair(self.runner, self.script, self.visuals, [self.issue])
        work = self.work()
        self.runner.plan_repair(work)
        from visual_repair import candidate_allowed

        self.assertFalse(
            candidate_allowed(self.runner, work, self.shots[1]["shot_id"], renamed)
        )
        self.assertNotEqual(
            assessment_key(work, self.shots[1]["shot_id"], "different-id"),
            self.shots[1]["shot_id"] + ":different-id",
        )

    def test_changed_reviewed_video_stops_before_repair_or_api(self):
        (self.root / "candidate.mp4").write_bytes(b"TEST changed candidate")
        with self.assertRaisesRegex(ProductionError, "хеш"):
            start_repair(self.runner, self.script, self.visuals, [self.issue])
        self.assertEqual(self.calls, [])
        self.assertNotIn("pending_repair", self.parent)

    def test_actual_merged_section_wins_over_narration_fragment_and_ambiguous_cue_stops(
        self,
    ):
        issue = dict(self.issue, fragment_index=9)
        self.assertEqual(
            resolve_targets(self.root, self.visuals, [issue]),
            {1: [self.shots[1]["shot_id"]]},
        )
        bad = dict(self.issue, frame_id="", cue_id=999)
        with self.assertRaisesRegex(ProductionError, "Неоднозначная"):
            resolve_targets(self.root, self.visuals, [bad])

    def test_generation_success_is_cached_when_later_paid_review_fails(self):
        generated = []

        def image_api(directory, settings, prompt):
            generated.append(prompt)
            out = io.BytesIO()
            Image.new("RGB", (1024, 1536), "brown").save(out, "JPEG")
            return out.getvalue(), {
                "parameters": {"model": settings["model"]},
                "prompt_sha256": digest(prompt),
                "usage": {"output_tokens": 2},
            }

        self.fail_step = "visual_review"
        with patch("visual_generation.generate_image", side_effect=image_api):
            with self.assertRaisesRegex(ProductionServiceError, "429"):
                self.run_repair()
        before = copy.deepcopy(self.work())
        calls = len(self.calls)
        with patch(
            "visual_generation.generate_image",
            side_effect=AssertionError("Already completed generation must be reused"),
        ):
            with self.assertRaisesRegex(ProductionServiceError, "429"):
                self.run_repair()
        self.assertEqual(len(generated), 1)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.work()["budget"], before["budget"])
        self.assertEqual(self.work()["repair_id"], before["repair_id"])
        successful = [
            o
            for o in self.runner.journal.state["operations"].values()
            if o["step"] == "visual_create" and o["status"] == "completed"
        ]
        self.assertEqual(len(successful), 1)

    def test_two_repair_rounds_have_independent_preview_and_search_limits(self):
        self.reject = True
        # 50 карточек на раунд, собственные HTTP-счётчики; никакой реальной сети.
        seed = self.candidate("candidate-seed")
        for i in range(100):
            c = dict(
                seed,
                file_title=f"File:New{i}.jpg",
                title="new thematic landscape",
                source_sha1=str(i),
            )
            c["candidate_id"] = candidate_id(c)
            target = (
                self.root / "visual_materials/thumbnails" / (c["candidate_id"] + ".jpg")
            )
            target.write_bytes(
                (
                    self.root
                    / "visual_materials/thumbnails"
                    / (seed["candidate_id"] + ".jpg")
                ).read_bytes()
            )
        calls = []

        def search(gateway, query, *args, **kwargs):
            number = gateway.budget["number"]
            gateway.budget["calls"] += 1
            calls.append(number)
            group = []
            for i in range((number - 1) * 50, number * 50):
                c = dict(
                    seed,
                    file_title=f"File:New{i}.jpg",
                    title="new thematic landscape",
                    source_sha1=str(i),
                )
                c["candidate_id"] = candidate_id(c)
                group.append(c)
            return {"candidates": group, "continue": {}, "provider": "wikimedia"}

        # Два разных запроса обеспечивают новый маршрут второго раунда.
        original = self.model

        def model(step, settings, schema, data, images):
            result = original(step, settings, schema, data, images)
            if step == "visual_queries":
                for shot in result["shots"]:
                    shot["queries"] = ["new thematic landscape", "another landscape"]
            return result

        self.runner.model = model
        with patch(
            "visual_generation.generate_image",
            side_effect=ProductionServiceError("TEST generation unavailable"),
        ):
            with self.assertRaisesRegex(
                ProductionServiceError, "generation unavailable"
            ):
                self.run_repair(search)
        work = self.work()
        self.assertEqual(calls, [1, 2])
        self.assertEqual([len(r["preview_ids"]) for r in work["rounds"]], [10, 10])
        self.assertEqual([r["calls"] for r in work["rounds"]], [1, 1])
        self.assertEqual(len(work["budget"]["cards"]), 100)
        self.assertEqual(len(work["budget"]["previews"]), 20)
        self.assertEqual(len(self.parent["budget"]["previews"]), 20)

    def test_legacy_exhausted_resume_allocates_repair_resource_only_once(self):
        self.parent["status"] = "exhausted"
        self.parent["pending_repair"] = {
            "status": "replacement_required",
            "script_version_id": self.script["script_version_id"],
            "issues": [self.issue],
            "previous_result": copy.deepcopy(self.visuals["fragments"][0]),
        }
        self.parent["approved"].pop(self.shots[1]["shot_id"])
        before = copy.deepcopy(self.parent["budget"])
        replacement = self.candidate("cached-free-replacement")
        self.runner.pool["candidates"][replacement["candidate_id"]] = replacement
        self.runner.save_pool()
        with patch(
            "fast_materials.create_commons_client", return_value=self.client
        ), patch("fast_materials.resolve_rights", side_effect=lambda c, g: c):
            result = self.runner.fragment(self.script["fragments"][0], 1)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(self.parent["budget"], before)
        self.assertEqual(len(self.parent["repair_jobs"]), 1)
        identifier = self.parent["pending_repair"]["repair_id"]
        calls = len(self.calls)
        result = self.runner.fragment(self.script["fragments"][0], 1)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.parent["pending_repair"]["repair_id"], identifier)
        self.assertEqual(len(self.parent["repair_jobs"]), 1)

    def test_unknown_second_generation_is_not_repeated_after_cached_first_rejection(
        self,
    ):
        generated = []

        def image_api(directory, settings, prompt):
            generated.append(prompt)
            if len(generated) == 2:
                raise ProductionServiceError("TEST second generation cost unknown")
            out = io.BytesIO()
            Image.new("RGB", (1024, 1536), "indigo").save(out, "JPEG")
            return out.getvalue(), {
                "parameters": {"model": settings["model"]},
                "prompt_sha256": digest(prompt),
                "usage": {"output_tokens": 3},
            }

        original_model = self.model

        def model(step, settings, schema, data, images):
            if step == "visual_review":
                return {
                    "fragment_index": 1,
                    "decision": "replace_candidate",
                    "issues": ["На новой иллюстрации не показано открытое море."],
                    "corrections": None,
                }
            return original_model(step, settings, schema, data, images)

        self.runner.model = model
        with patch("visual_generation.generate_image", side_effect=image_api):
            with self.assertRaisesRegex(ProductionServiceError, "cost unknown"):
                self.run_repair()
        before = copy.deepcopy(self.work()["budget"])
        with patch(
            "visual_generation.generate_image",
            side_effect=AssertionError("No third request"),
        ):
            with self.assertRaisesRegex(ProductionServiceError, "cost unknown"):
                self.run_repair()
        self.assertEqual(len(generated), 2)
        self.assertEqual(self.work()["budget"], before)

    def test_revocation_precedes_public_write_and_resume_keeps_repair_id(self):
        from production_state import UpstreamBlocked

        with patch(
            "file_io.atomic_write_json",
            side_effect=KeyboardInterrupt("TEST interrupted public write"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                start_repair(self.runner, self.script, self.visuals, [self.issue])
        identifier = self.parent["pending_repair"]["repair_id"]
        self.assertEqual(
            read(self.root / "visual_materials.json")["status"], "approved"
        )
        with self.assertRaises(UpstreamBlocked):
            checked_visuals(self.script, self.visuals, self.root)
        replacement = self.candidate("resume-replacement")
        self.runner.pool["candidates"][replacement["candidate_id"]] = replacement
        updated = self.run_repair()
        self.assertEqual(updated["status"], "approved")
        self.assertEqual(self.parent["pending_repair"]["repair_id"], identifier)
        self.assertEqual(len(self.parent["repair_jobs"]), 1)

    def test_editing_routes_final_issue_to_repair_then_rechecks_new_candidate(self):
        from editing import EditingRun
        from types import SimpleNamespace

        replacement = self.candidate("accepted-replacement")
        self.runner.pool["candidates"][replacement["candidate_id"]] = replacement
        self.runner.save_pool()
        words = [
            {"word_id": i + 1, "word": word, "start": i * 0.5, "end": (i + 1) * 0.5}
            for i, word in enumerate(self.script["fragments"][0]["text"].split())
        ]
        fragments = [
            {
                "fragment_index": 1,
                "first_word_id": 1,
                "last_word_id": len(words),
                "display_start": 0,
                "display_end": 4,
                "audio_start": 0,
                "audio_end": 4,
            }
        ]
        wav = self.root / "voice.wav"
        wav.write_bytes(b"OFFLINE TEST audio adapter; not actual speech")
        audio = {"audio_path": "voice.wav", "audio_sha256": sha(wav)}
        atomic_write_json(self.root / "audio_manifest.json", audio)
        font = self.root / "font.ttf"
        font.write_bytes(b"TEST font adapter")
        media = SimpleNamespace(font=font)
        final_calls = []
        subtitle_calls = []

        def model(step, settings, schema, data, images):
            if step == "subtitle_segment":
                subtitle_calls.append(1)
                return {
                    "status": "ok",
                    "reason": None,
                    "cues": [
                        {
                            "cue_id": i + 1,
                            "first_token_id": start,
                            "last_token_id": end,
                            "first_audio_word_id": start,
                            "last_audio_word_id": end,
                            "lines": [{"first_token_id": start, "last_token_id": end}],
                        }
                        for i, (start, end) in enumerate([(1, 4), (5, 8)])
                    ],
                }
            if step == "final_check":
                final_calls.append(1)
                chosen = next(f for f in data["frames"] if f["cue_id"] == 2)
                issue = {k: chosen[k] for k in ("frame_id", "fragment_index", "cue_id")}
                issue.update(
                    problem="Показан неподходящий объект.",
                    correction="Покажи тематический пейзаж.",
                    target_step="visual_materials",
                )
                return {
                    "decision": "needs_fix" if len(final_calls) == 1 else "approved",
                    "reason": None,
                    "subtitle_appearance": None,
                    "reviewed_frame_ids": [f["frame_id"] for f in data["frames"]],
                    "issues": [issue] if len(final_calls) == 1 else [],
                }
            return self.model(step, settings, schema, data, images)

        def render(root, assets, *args):
            (root / "silent.mp4").write_bytes(
                b"TEST video render " + digest(assets).encode()
            )

        def compose(root, *args):
            candidate = root / "candidate.mp4"
            candidate.write_bytes(b"TEST compose " + (root / "silent.mp4").read_bytes())
            return candidate, {}

        def layout(root, *args, **kwargs):
            (root / "subtitles.ass").write_text("TEST ASS")
            (root / "subtitles.srt").write_text("TEST SRT")
            return {"passed": True, "issues": []}

        def extract(candidate, folder, samples, *args):
            folder.mkdir(parents=True, exist_ok=True)
            color = "#" + sha(candidate)[:6]
            for sample in samples:
                p = folder / (sample["frame_id"] + ".jpg")
                Image.new("RGB", (50, 80), color).save(p)
                sample["path"] = str(p)

        with patch(
            "editing.checked_audio", return_value=(wav, 4, words, fragments)
        ), patch(
            "editing.inspect_tools",
            return_value={"font_family": "Arial", "versions": {"adapter": "TEST"}},
        ), patch(
            "editing.render_video", side_effect=render
        ), patch(
            "editing.compose", side_effect=compose
        ), patch(
            "editing.layout", side_effect=layout
        ), patch(
            "editing.technical_check",
            return_value={"passed": True, "checks": {"TEST_adapter": True}},
        ), patch(
            "editing.extract_frames", side_effect=extract
        ), patch(
            "video_schedule_check.check_schedule", return_value={"passed": True}
        ), patch(
            "fast_materials.create_commons_client", return_value=self.client
        ), patch(
            "fast_materials.resolve_rights", side_effect=lambda c, g: c
        ):
            editor = EditingRun(self.root, model=model, media=media)
            path = editor.execute(self.script, audio, self.visuals, allow_test=True)
        manifest = read(path)
        quality = read(self.root / "quality_report.json")
        self.assertEqual(manifest["status"], "test_passed")
        self.assertTrue(quality["test_passed"])
        self.assertEqual(len(subtitle_calls), 1)
        self.assertEqual(len(final_calls), 2)
        self.assertEqual(sha(wav), audio["audio_sha256"])
        self.assertEqual(read(self.root / "script.json"), self.script)
        self.assertTrue(quality["technical"]["checks"]["unique_visual_sources"])
        self.assertNotEqual(
            read(self.root / "visual_materials.json")["fragments"][0]["shots"][1][
                "asset"
            ]["source"]["candidate_id"],
            self.visuals["fragments"][0]["shots"][1]["asset"]["source"]["candidate_id"],
        )


if __name__ == "__main__":
    unittest.main()
