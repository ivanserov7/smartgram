"""Offline contracts; real sockets and DNS are forbidden by run_tests.py."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from production_state import (
    Journal,
    ProductionError,
    UpstreamBlocked,
    config,
    digest,
    sha,
)
from visual_geometry import geometry, prepare, frame, oriented
from visual_materials import VisualMaterials, validate_selection, metadata_problem
from subtitles import Segmentation, canonical, materialize
from video_render import schedule, sample_schedule
from editing import ready, validate_batch
from editing_inputs import checked_audio, snapshot_inputs


def composition(mode="crop", zoom=10):
    return {
        "mode": mode,
        "initial_crop": [0, 0, 1, 1],
        "focus_point": [0.5, 0.5],
        "zoom_percent": zoom,
        "must_keep": [],
    }


def metadata():
    return {
        "title": "Earth",
        "file_title": "File:Earth.jpg",
        "source_page": "https://commons.wikimedia.org/wiki/File:Earth.jpg",
        "original_url": "https://upload.wikimedia.org/wikipedia/commons/a/aa/Earth.jpg",
        "download_url": "https://upload.wikimedia.org/wikipedia/commons/a/aa/Earth.jpg",
        "author": "NASA",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "mime": "image/jpeg",
        "original_mime": "image/jpeg",
        "width": 2200,
        "height": 2200,
        "restrictions": "",
    }


class GeometryTests(unittest.TestCase):
    def test_same_transformation_for_previews_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "source.jpg"
            Image.new("RGB", (1400, 2400), "blue").save(path)
            original_hash = sha(path)
            motion = prepare(path, composition(), root / "prepared")
            self.assertGreaterEqual(motion["end_crop"][2] - motion["end_crop"][0], 1080)
            image = oriented(path)
            for p in motion["previews"]:
                with Image.open(p["path"]) as preview:
                    self.assertEqual(
                        preview.tobytes(), frame(image, motion, p["position"]).tobytes()
                    )
            self.assertEqual(sha(path), original_hash)

    def test_landscape_and_insufficient_foreground(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wide.png"
            Image.new("RGB", (2000, 1000)).save(path)
            self.assertLessEqual(
                geometry(path, composition("background"))["end_scale"], 1
            )
            with self.assertRaisesRegex(ProductionError, "Insufficient pixels"):
                geometry(path, composition())
            Image.new("RGB", (500, 300)).save(path)
            with self.assertRaisesRegex(ProductionError, "foreground"):
                geometry(path, composition("background", 0))

    def test_exif_before_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.jpg"
            im = Image.new("RGB", (2400, 1400))
            exif = im.getexif()
            exif[274] = 6
            im.save(path, exif=exif)
            self.assertEqual(
                geometry(path, composition())["original_size"], [1400, 2400]
            )
            self.assertEqual(oriented(path).size, (1400, 2400))

    def test_must_keep_entire_path_and_zoom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.png"
            Image.new("RGB", (1400, 2400)).save(path)
            c = composition()
            c["must_keep"] = [{"label": "edge", "box": [0, 0, 0.1, 0.1]}]
            with self.assertRaisesRegex(ProductionError, "must_keep"):
                geometry(path, c)
            with self.assertRaisesRegex(ProductionError, "Zoom"):
                geometry(path, composition(zoom=11))


class SubtitleTests(unittest.TestCase):
    def data(self):
        script = {"fragments": [{"text": "Это 4 спутника.", "visual": "Earth"}]}
        words = [
            {"word_id": i, "word": w, "start": (i - 1) * 0.5, "end": i * 0.5}
            for i, w in enumerate(["это", "четыре", "спутника"], 1)
        ]
        fragments = [{"fragment_index": 1, "first_word_id": 1, "last_word_id": 3}]
        cue = {
            "cue_id": 1,
            "first_token_id": 1,
            "last_token_id": 3,
            "first_audio_word_id": 1,
            "last_audio_word_id": 3,
            "lines": [{"first_token_id": 1, "last_token_id": 3}],
        }
        return script, words, fragments, {"status": "ok", "reason": None, "cues": [cue]}

    def test_numbers_yo_and_many_to_many_tokenization(self):
        s, w, f, c = self.data()
        self.assertEqual(materialize(c, s, w, f)[0]["text"], "Это 4 спутника.")
        self.assertEqual(
            canonical("Ёж, т. е. 24."), canonical("еж то есть двадцать четыре")
        )
        aliases = config()["editing"]["subtitle_segment"]["equivalences"]
        self.assertEqual(
            canonical("GPS: 4 км", aliases),
            canonical("джи пи эс четыре километра", aliases),
        )
        s["fragments"][0]["text"] = "Т. е. четыре спутника."
        w = [
            {"word_id": i, "word": v, "start": i * 0.3, "end": i * 0.3 + 0.2}
            for i, v in enumerate(["то", "есть", "четыре", "спутника"], 1)
        ]
        c["cues"][0].update(
            last_token_id=4,
            last_audio_word_id=4,
            lines=[{"first_token_id": 1, "last_token_id": 4}],
        )
        f[0]["last_word_id"] = 4
        self.assertEqual(materialize(c, s, w, f)[0]["text"], "Т. е. четыре спутника.")

    def test_many_text_tokens_one_word(self):
        s, w, f, c = self.data()
        s["fragments"][0]["text"] = "По этому"
        w = [{"word_id": 1, "word": "поэтому", "start": 0, "end": 1}]
        f[0]["last_word_id"] = 1
        c["cues"][0].update(
            last_token_id=2,
            last_audio_word_id=1,
            lines=[{"first_token_id": 1, "last_token_id": 2}],
        )
        self.assertEqual(materialize(c, s, w, f)[0]["end"], 1)

    def test_gaps_duplicates_invented_ids_and_ambiguity(self):
        mutations = [
            lambda c: c["cues"][0].update(first_token_id=2),
            lambda c: c["cues"][0].update(last_audio_word_id=99),
            lambda c: c["cues"].append(copy.deepcopy(c["cues"][0])),
            lambda c: c["cues"][0].update(
                lines=[{"first_token_id": 2, "last_token_id": 3}]
            ),
        ]
        for mutate in mutations:
            s, w, f, c = self.data()
            mutate(c)
            with self.assertRaises(ProductionError):
                materialize(c, s, w, f)
        s, w, f, c = self.data()
        w[1]["word"] = "пять"
        with self.assertRaisesRegex(ProductionError, "Ambiguous"):
            materialize(c, s, w, f)

    def test_uncertain_and_blocked_schema_without_fake_cues(self):
        for status in ["uncertain", "blocked"]:
            result = Segmentation(
                status=status, reason="Cannot map", cues=[]
            ).model_dump()
            with self.assertRaises(ProductionError):
                materialize(result, {"fragments": []}, [], [])


class TimelineTests(unittest.TestCase):
    def test_absolute_rounding_pauses_short_scenes_dissolve(self):
        f = [
            {"fragment_index": 1, "display_start": 0, "display_end": 1.017},
            {"fragment_index": 2, "display_start": 1.017, "display_end": 1.12},
            {"fragment_index": 3, "display_start": 1.12, "display_end": 5.011},
        ]
        scenes, total = schedule(f, 5.011, 30, "dissolve", 1000)
        self.assertEqual(total, 151)
        self.assertEqual(scenes[-1]["end_frame"], 151)
        self.assertLessEqual(scenes[1]["dissolve_frames"], 2)
        self.assertEqual(sum(s["end_frame"] - s["start_frame"] for s in scenes), total)

    def test_unrepresentable_scene_blocks(self):
        with self.assertRaises(ProductionError):
            schedule(
                [
                    {"fragment_index": 1, "display_start": 0, "display_end": 0.001},
                    {"fragment_index": 2, "display_start": 0.001, "display_end": 1},
                ],
                1,
                30,
            )

    def test_every_cue_and_transition_sampled(self):
        scenes, total = schedule(
            [
                {"fragment_index": 1, "display_start": 0, "display_end": 1},
                {"fragment_index": 2, "display_start": 1, "display_end": 2},
            ],
            2,
            30,
        )
        samples = sample_schedule(
            [
                {"cue_id": 1, "start": 0.2, "end": 0.8},
                {"cue_id": 2, "start": 1.1, "end": 1.9},
            ],
            scenes,
            total,
            30,
        )
        self.assertEqual(
            {r for s in samples for r in s["reasons"]},
            {"cue_1", "cue_2", "transition_before", "transition_after"},
        )


class GateTests(unittest.TestCase):
    def test_fail_closed_for_technical_uncertain_missing_and_duplicate_batches(self):
        good = {"decision": "approved", "issues": [], "reviewed_frame_ids": ["a"]}
        self.assertTrue(ready({"passed": True}, [good], ["a"]))
        cases = [
            ({"passed": False}, [good], ["a"]),
            ({"passed": True}, [], ["a"]),
            ({"passed": True}, [{**good, "decision": "uncertain"}], ["a"]),
            ({"passed": True}, [good], ["a", "b"]),
            ({"passed": True}, [good, good], ["a"]),
        ]
        for t, b, e in cases:
            self.assertFalse(ready(t, b, e))

    def test_final_batch_coverage(self):
        with self.assertRaises(ProductionError):
            validate_batch(
                {"decision": "approved", "reviewed_frame_ids": [], "issues": []},
                [{"frame_id": "a", "fragment_index": 1, "cue_id": 1}],
            )

    def test_persist_failed_and_completed_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            j = Journal(tmp, "test", {"model": "x"})

            def fail():
                calls.append(1)
                raise ProductionError("Denied")

            with self.assertRaises(ProductionError):
                j.operation("api", {"x": 1}, fail)
            again = Journal(tmp, "test", {"model": "y"})
            with self.assertRaises(ProductionError):
                again.operation("api", {"x": 1}, fail)
            self.assertEqual(len(calls), 1)
            self.assertEqual(again.operation("api", {"x": 2}, lambda: 42), 42)
            self.assertEqual(again.operation("api", {"x": 2}, lambda: 1 / 0), 42)

    def test_unaccepted_wrong_version_and_demo_audio(self):
        with self.assertRaises(UpstreamBlocked):
            checked_audio(
                {"script_version_id": "x"}, {"status": "needs_review"}, Path("/tmp")
            )
        with self.assertRaisesRegex(UpstreamBlocked, "another script"):
            checked_audio(
                {"script_version_id": "x"},
                {
                    "mode": "real",
                    "status": "completed",
                    "text_verified": True,
                    "audio_kind": "ai_generated_speech",
                    "script_version_id": "y",
                },
                Path("/tmp"),
            )

    def test_unknown_license_and_invented_or_rejected_selection(self):
        c = metadata()
        self.assertIsNone(metadata_problem(c))
        c["license_url"] = "https://example.com/license"
        self.assertIn("license", metadata_problem(c))
        value = {
            "fragment_index": 1,
            "status": "selected",
            "primary_candidate_id": "x",
            "backup_candidate_id": None,
            "reason": "ok",
            "rejected_candidates": [],
            "composition": composition(),
        }
        with self.assertRaises(ProductionError):
            validate_selection(value, [{"candidate_id": "y"}], 1)
        value["rejected_candidates"] = [{"candidate_id": "x", "reason": "wrong"}]
        with self.assertRaises(ProductionError):
            validate_selection(value, [{"candidate_id": "x"}], 1)

    def test_search_independent_of_audio_and_resume_preserves_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = config()["visuals"]
            settings["search"]["max_search_api_calls_per_fragment_per_round"] = 2
            calls = []
            api = []

            def model(step, options, schema, data, images):
                api.append(step)
                if step == "visual_queries":
                    return {
                        "fragments": [
                            {
                                "fragment_index": 1,
                                "queries": ["earth", "земля"],
                                "must_show": ["Earth"],
                                "must_not_show": [],
                            }
                        ]
                    }
                raise AssertionError("No suitable images => no selection/review.")

            def search(q, cont, client):
                calls.append(q)
                return {"candidates": [], "continue": {"gsroffset": 5}}

            script = {
                "script_version_id": "test",
                "fragments": [{"text": "Земля", "visual": "Земля"}],
            }
            first = VisualMaterials(root, settings, model, search).run(script)
            self.assertEqual(json.loads(first.read_text())["status"], "needs_visual")
            self.assertEqual(len(calls), 4)
            VisualMaterials(root, settings, model, search).run(script)
            self.assertEqual(len(calls), 4)
            self.assertEqual(len(api), 2)
            self.assertFalse((root / "audio_manifest.json").exists())

    def test_immutable_snapshot_detects_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "script.json").write_text('{"a":1}')
            dest = snapshot_inputs(source, root / "snapshot", False)
            (source / "script.json").write_text('{"a":2}')
            with self.assertRaises(ProductionError):
                snapshot_inputs(source, dest, False)


class VisualWorkflowTests(unittest.TestCase):
    def image_bytes(self):
        import io

        buffer = io.BytesIO()
        Image.new("RGB", (2000, 1200), "navy").save(buffer, format="JPEG")
        return buffer.getvalue()

    def test_real_images_reach_models_and_duplicate_hashes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidates = [
                dict(metadata(), file_title=f"File:Image_{n}.jpg") for n in range(3)
            ]

            def search(*args):
                return {"candidates": candidates, "continue": {}}

            data = self.image_bytes()
            seen = []

            def model(step, options, schema, payload, images):
                seen.append((step, len(images)))
                if step == "visual_queries":
                    return {
                        "fragments": [
                            {
                                "fragment_index": 1,
                                "queries": ["Earth"],
                                "must_show": ["Earth"],
                                "must_not_show": [],
                            }
                        ]
                    }
                if step == "visual_select":
                    cid = payload["candidates"][0]["candidate_id"]
                    return {
                        "fragment_index": 1,
                        "status": "selected",
                        "primary_candidate_id": cid,
                        "backup_candidate_id": None,
                        "reason": "Test",
                        "rejected_candidates": [],
                        "composition": composition("background", 0),
                    }
                if step == "visual_review":
                    return {
                        "fragment_index": 1,
                        "decision": "approved",
                        "issues": [],
                        "corrections": None,
                    }
                raise AssertionError(step)

            runner = VisualMaterials(
                tmp, model=model, search=search, download=lambda *args: data
            )
            script = {
                "script_version_id": "x",
                "fragments": [{"text": "Земля", "visual": "Earth"}],
            }
            result = json.loads(runner.run(script).read_text())
            self.assertEqual(result["status"], "approved")
            self.assertIn(("visual_select", 1), seen)
            self.assertIn(("visual_review", 4), seen)
            job = next(iter(runner.journal.state["jobs"].values()))
            self.assertEqual(len(job["seen_hashes"]), 1)
            self.assertEqual(len(job["rejections"]), 2)
            before = list(seen)
            VisualMaterials(tmp, model=model, search=lambda *args: 1 / 0).run(script)
            self.assertEqual(seen, before)
            # Changing only another fragment does not consume another paid selection for this one.
            from production_state import inside

            asset = result["fragments"][0]["asset"]
            path = inside(tmp, asset["original_path"])
            path.write_bytes(path.read_bytes() + b"changed")
            cached = json.loads(
                VisualMaterials(tmp, model=model, search=lambda *args: 1 / 0)
                .run(script)
                .read_text()
            )
            self.assertEqual(cached["status"], "needs_visual")

    def test_framing_budget_not_reset_by_resume_or_prompt_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            counts = {"review": 0}
            data = self.image_bytes()

            def search(*args):
                return {"candidates": [metadata()], "continue": {}}

            def model(step, options, schema, payload, images):
                if step == "visual_queries":
                    return {
                        "fragments": [
                            {
                                "fragment_index": 1,
                                "queries": ["Earth"],
                                "must_show": [],
                                "must_not_show": [],
                            }
                        ]
                    }
                if step == "visual_select":
                    return {
                        "fragment_index": 1,
                        "status": "selected",
                        "primary_candidate_id": payload["candidates"][0][
                            "candidate_id"
                        ],
                        "backup_candidate_id": None,
                        "reason": "Test",
                        "rejected_candidates": [],
                        "composition": composition("background", 0),
                    }
                if step == "visual_review":
                    counts["review"] += 1
                    return {
                        "fragment_index": 1,
                        "decision": "fix_framing",
                        "issues": ["TEST move"],
                        "corrections": composition("background", 0),
                    }
                raise AssertionError(step)

            script = {
                "script_version_id": "x",
                "fragments": [{"text": "Земля", "visual": "Earth"}],
            }
            runner = VisualMaterials(
                tmp, model=model, search=search, download=lambda *args: data
            )
            runner.run(script)
            # Identical proposed correction is cached: it does not create an extra paid model request.
            job = next(iter(runner.journal.state["jobs"].values()))
            self.assertEqual(max(job["corrections"].values()), 2)
            self.assertEqual(len(job["rounds"]), 2)
            before = counts["review"]
            settings = config()["visuals"]
            settings["review"]["prompt"] += " explicit change"
            VisualMaterials(tmp, settings, model, search, lambda *args: data).run(
                script
            )
            self.assertEqual(counts["review"], before)


class AudioContractTests(unittest.TestCase):
    def test_hash_and_complete_pause_word_and_version_coverage(self):
        import wave
        from audio_files import inspect_wav

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "audio.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b"\1\0" * 24000)
            script = {
                "script_version_id": "version",
                "fragments": [{"text": "Четыре.", "visual": "Four"}],
            }
            words = [{"word_id": 1, "word": "четыре", "start": 0.2, "end": 0.7}]
            fragment = dict(
                script["fragments"][0],
                fragment_index=1,
                first_word_id=1,
                last_word_id=1,
                speech_start=0.2,
                speech_end=0.7,
                display_start=0,
                display_end=1,
            )
            audio = {
                **inspect_wav(path.read_bytes()),
                "mode": "real",
                "status": "completed",
                "text_verified": True,
                "audio_kind": "ai_generated_speech",
                "script_version_id": "version",
                "timing_status": "aligned_to_deepgram_words",
                "review": {"decision": "Можно использовать"},
                "audio_path": "audio.wav",
                "openai_transcript": {"text":"Четыре."},
                "total_duration_seconds": 1,
                "words": words,
                "fragments": [fragment],
            }
            self.assertEqual(checked_audio(script, audio, root)[1], 1)
            variants = []
            for change in [
                lambda a: a.update(audio_sha256="wrong"),
                lambda a: a.update(script_version_id="old"),
                lambda a: a.update(mode="demo"),
                lambda a: a["words"][0].update(end=1.1),
                lambda a: a["fragments"][0].update(display_start=0.1),
                lambda a: a["fragments"][0].update(display_end=0.9),
                lambda a: a["fragments"][0].update(visual="changed"),
            ]:
                bad = copy.deepcopy(audio)
                change(bad)
                with self.assertRaises(UpstreamBlocked):
                    checked_audio(script, bad, root)

    def test_search_failure_reserves_budget_and_cannot_repeat_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def model(step, options, schema, payload, images):
                return {
                    "fragments": [
                        {
                            "fragment_index": 1,
                            "queries": ["Earth"],
                            "must_show": [],
                            "must_not_show": [],
                        }
                    ]
                }

            def search(*args):
                calls.append(1)
                raise ProductionError("Service unavailable")

            script = {
                "script_version_id": "x",
                "fragments": [{"text": "Earth", "visual": "Earth"}],
            }
            runner = VisualMaterials(tmp, model=model, search=search)
            runner.run(script)
            job = next(iter(runner.journal.state["jobs"].values()))
            self.assertEqual(job["rounds"][0]["calls"], 1)
            VisualMaterials(tmp, model=model, search=search).run(script)
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
