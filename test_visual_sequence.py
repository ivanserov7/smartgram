"""Короткие проверки уникальности, непрерывного времени и сохранённых реальных входов."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from production_state import ProductionError, read, config, Journal
from visual_sequence import (
    first_appearance_sequence,
    validate_sequence,
    same_source,
    journal_source_in_use,
)
from video_render import schedule, sample_schedule
from editing_inputs import (
    snapshot_inputs,
    import_manifest,
    checked_audio,
    checked_visuals,
)
from script_workflow import approved_script
from subtitle_binding import binding
from visual_shots import shot_timeline
from visual_materials import VisualMaterials
from file_io import atomic_write_json


def asset(name, hash_value=None):
    return {
        "original_path": name + ".jpg",
        "original_sha256": hash_value or name * 64,
        "source": {
            "file_title": "File:" + name + ".jpg",
            "file_sha256": hash_value or name * 64,
        },
        "motion": {"composition": {"zoom_percent": 0}},
    }


def times(count):
    return [
        {
            "fragment_index": i + 1,
            "shot_id": "s" + str(i + 1),
            "display_start": i * 2.0,
            "display_end": (i + 1) * 2.0,
        }
        for i in range(count)
    ]


class SequenceTests(unittest.TestCase):
    def test_adjacent_and_return_merge_duration_and_preserve_inputs(self):
        for names, expected in [
            ("AAB", ["A", "B"]),
            ("ABA", ["A", "B"]),
            ("ABAC", ["A", "B", "C"]),
        ]:
            assets = [asset(n) for n in names]
            timeline = times(len(names))
            before = copy.deepcopy(timeline)
            selected, merged = first_appearance_sequence(assets, timeline)
            self.assertEqual(
                [a["original_path"] for a in selected], [n + ".jpg" for n in expected]
            )
            self.assertEqual(merged[-1]["display_end"], len(names) * 2)
            self.assertEqual(sum(len(s["covered_shots"]) for s in merged), len(names))
            self.assertEqual(timeline, before)
            scenes, total = schedule(merged, len(names) * 2, 30)
            self.assertEqual(scenes[-1]["end_frame"], total)
            validate_sequence(selected, scenes)
            restored = copy.deepcopy(scenes)
            validate_sequence(selected, restored)
            with self.assertRaises(ProductionError):
                validate_sequence(selected + [selected[0]], restored + [restored[0]])
        selected, merged = first_appearance_sequence(
            [asset("A"), asset("B"), asset("A")], times(3)
        )
        self.assertEqual((merged[1]["display_start"], merged[1]["display_end"]), (2, 6))
        self.assertEqual(merged[1]["fragment_indices"], [2, 3])

    def test_other_crop_reencoding_and_different_id_same_hash_are_not_new_sources(self):
        original = asset("A")
        cropped = copy.deepcopy(original)
        cropped.update(original_path="renamed.jpg", original_sha256="different")
        cropped["source"]["file_sha256"] = "different"
        cropped["motion"]["composition"]["zoom_percent"] = 10
        same_bytes = asset("B", original["original_sha256"])
        self.assertTrue(same_source(original, cropped))
        self.assertTrue(same_source(original, same_bytes))
        generated = {
            "candidate_id": "generated",
            "original_sha256": "working_one",
            "source": {"origin": "generated", "raw_sha256": "native"},
        }
        another = {
            "candidate_id": "other",
            "original_sha256": "working_two",
            "source": {"origin": "generated", "raw_sha256": "native"},
        }
        self.assertTrue(same_source(generated, another))
        self.assertEqual(
            len(
                first_appearance_sequence([original, cropped, same_bytes], times(3))[0]
            ),
            1,
        )

    def test_occupancy_import_resume_rejection_release_and_other_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "one"
            second = root / "two"
            first.mkdir()
            second.mkdir()
            value = {"shot_id": "s1", "status": "approved", "asset": asset("A")}
            state = {"jobs": {"k": {"approved": {"s1": value}}}}
            self.assertTrue(journal_source_in_use(first, state, asset("A"), "s2"))
            self.assertFalse(journal_source_in_use(first, state, asset("A"), "s1"))
            self.assertFalse(
                journal_source_in_use(second, {"jobs": {}}, asset("A"), "s2")
            )
            state["jobs"]["k"]["approved"] = {}
            state["jobs"]["k"]["rejections"] = [value]
            self.assertFalse(journal_source_in_use(first, state, asset("A"), "s2"))
            atomic_write_json(
                first / "visual_materials.json",
                {
                    "fragments": [
                        {"fragment_index": 1, "status": "approved", "shots": [value]}
                    ]
                },
            )
            self.assertTrue(journal_source_in_use(first, state, asset("A"), "s2"))

    def test_legacy_metadata_ownership_and_vision_rejection_release(self):
        from visual_sequence import legacy_source_in_use
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);candidate=asset('A')['source']
            entries=[{'scene_id':1,'status':'selected','selected':candidate}]
            atomic_write_json(root/'visual_manifest.json',{'scenes':entries})
            self.assertTrue(legacy_source_in_use(root,candidate,2))
            self.assertFalse(legacy_source_in_use(root,candidate,1))
            entries.append({'scene_id':2,'status':'selected','selected':candidate})
            atomic_write_json(root/'visual_manifest.json',{'scenes':entries})
            self.assertTrue(legacy_source_in_use(root,candidate,1))
            atomic_write_json(root/'visual_review.json',{'scenes':[{'scene_id':2,'status':'needs_visual','selected':None}]})
            self.assertFalse(legacy_source_in_use(root,candidate,1))

    def test_future_selector_checks_global_ownership_and_releases_rejected_attempts(
        self,
    ):
        from fast_materials import FastMaterials

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = FastMaterials(root, model=lambda *args: None)
            runner.limits = config()["visuals"]["search"]
            runner.journal.state["jobs"]["first"] = {
                "approved": {
                    "s1": {"shot_id": "s1", "status": "approved", "asset": asset("A")}
                }
            }
            candidate = dict(
                asset("B")["source"], candidate_id="b", file_sha256="A" * 64
            )
            runner.pool["candidates"]["b"] = candidate
            job = {
                "approved": {},
                "assessments": {
                    "s2:b": {
                        "shot_id": "s2",
                        "candidate_id": "b",
                        "suitable": True,
                        "score": 90,
                        "selection_mode": "illustrative_background",
                        "reason": "photo",
                        "composition": {},
                    }
                },
                "rejections": [],
            }
            with patch.object(runner, "original") as download:
                runner.finalize_choices(job, {}, None, [{"shot_id": "s2"}])
                download.assert_not_called()
            runner.journal.state["jobs"]["first"]["approved"] = {}
            with patch.object(runner, "original", return_value=candidate), patch.object(
                runner,
                "prepare_and_review",
                return_value={"status": "approved", "asset": asset("B")},
            ) as review:
                runner.finalize_choices(job, {}, None, [{"shot_id": "s2"}])
                self.assertEqual(review.call_count, 1)
            self.assertIn("s2", job["approved"])
            self.assertEqual(runner.pool["global_rejections"], {})

    def test_subtitle_fragment_and_visual_section_are_independent(self):
        selected, merged = first_appearance_sequence([asset("A"), asset("A")], times(2))
        scenes, total = schedule(merged, 4, 30)
        samples = sample_schedule(
            [
                {"cue_id": 1, "fragment_index": 1, "start": 0, "end": 2},
                {"cue_id": 2, "fragment_index": 2, "start": 2, "end": 4},
            ],
            scenes,
            total,
            30,
        )
        self.assertEqual({x["fragment_index"] for x in samples}, {1, 2})
        self.assertEqual({x["visual_section_id"] for x in samples}, {"section_1"})

    def test_saved_subtitle_width_repair_preserves_verified_words_and_script(self):
        from subtitles import (
            materialize,
            fit_existing_lines,
            split_overwide_verified_cues,
        )
        from media_tools import load_media_settings
        from PIL import ImageFont

        root = Path(__file__).parent / "outputs/assembly_existing_20261101"
        if not (root / "topic_3/editing_state.json").exists():
            self.skipTest("Локальные сохранённые ответы не включаются в архив.")
        settings = config()["editing"]["subtitle_layout"]
        font = ImageFont.truetype(str(load_media_settings().font), 56)
        allowed = 1080 * settings["width_percent"] / 100
        for number in (1, 3):
            run = root / f"topic_{number}"
            script = approved_script(run)
            audio = read(run / "audio_manifest.json")
            original_hash = audio["audio_sha256"]
            _, _, words, fragments = checked_audio(script, audio, run)
            before = copy.deepcopy(words)
            aliases = config()["editing"]["subtitle_segment"].get("equivalences", {})
            proof = binding(script, words, fragments, aliases)
            operations = read(run / "editing_state.json")["operations"]
            op = next(
                o
                for o in reversed(list(operations.values()))
                if o["step"] == "subtitle_segment"
                and o["status"] == "completed"
                and o.get("result", {}).get("status") == "ok"
            )
            cues = materialize(
                op["result"], script, words, fragments, aliases, binding_proof=proof
            )
            self.assertTrue(fit_existing_lines(cues, font, allowed, 2))
            splits = split_overwide_verified_cues(
                cues, words, proof, font, allowed, settings
            )
            self.assertEqual(len(splits), int(number == 3))
            materialize(
                {"status": "ok", "reason": None, "cues": cues},
                script,
                words,
                fragments,
                aliases,
                binding_proof=proof,
            )
            self.assertEqual(words, before)
            self.assertEqual(
                read(run / "audio_manifest.json")["audio_sha256"], original_hash
            )
            for cue in cues:
                self.assertLessEqual(len(cue["line_text"]), 2)
                self.assertLessEqual(
                    max(font.getlength(line) + 14 for line in cue["line_text"]), allowed
                )
                self.assertAlmostEqual(
                    cue["start"], words[cue["first_audio_word_id"] - 1]["start"]
                )
                self.assertAlmostEqual(
                    cue["end"], words[cue["last_audio_word_id"] - 1]["end"]
                )

    def test_subtitle_split_requires_proven_boundary_and_keeps_limits(self):
        from subtitles import split_overwide_verified_cues
        from PIL import ImageFont

        font = ImageFont.load_default()
        cue = {
            "cue_id": 1,
            "first_token_id": 1,
            "last_token_id": 2,
            "first_audio_word_id": 1,
            "last_audio_word_id": 2,
            "text": "first second",
            "start": 0,
            "end": 2,
            "line_text": ["first second"],
        }
        words = [{"start": 0, "end": 0.2}, {"start": 0.2, "end": 2}]
        settings = {
            "max_lines": 2,
            "min_duration_ms": 600,
            "max_duration_ms": 6000,
            "max_characters_per_second": 22,
        }
        for proof in (
            {"blocks": []},
            {"blocks": [{"last_token_id": 1, "last_audio_word_id": 1}]},
        ):
            cues = [copy.deepcopy(cue)]
            self.assertEqual(
                split_overwide_verified_cues(cues, words, proof, font, 50, settings), []
            )
            self.assertEqual(cues[0]["text"], cue["text"])

    def test_release_copy_checks_marker_sha_and_keeps_previous_versions(self):
        from release_sync import sync_videos
        from review_archive_sync import MARKER_NAME, DRIVE_FOLDER_ID, ReviewSyncError
        from production_state import sha

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            drive = root / "drive"
            drive.mkdir()
            atomic_write_json(drive / MARKER_NAME, {"drive_folder_id": DRIVE_FOLDER_ID})
            (drive / "old.mp4").write_bytes(b"previous")
            (drive / "latest.json").write_text("previous archive pointer")
            # Synthetic copy fixture; format decoding is covered by rendering tests.
            fake = root / "test-copy.mp4"
            fake.write_bytes(b"Explicit test bytes; not a rendered movie")
            status = root / "status.json"
            atomic_write_json(
                status,
                {
                    "status": "ready",
                    "topics": [
                        {
                            "name": "test",
                            "status": "ready",
                            "video": str(fake),
                            "sha256": sha(fake),
                            "size_bytes": fake.stat().st_size,
                        }
                    ],
                },
            )
            receipt = sync_videos(status, drive)
            self.assertEqual(sha(Path(receipt["files"][0]["path"])), sha(fake))
            self.assertEqual((drive / "old.mp4").read_bytes(), b"previous")
            self.assertEqual(
                (drive / "latest.json").read_text(), "previous archive pointer"
            )
            self.assertFalse(receipt["cloud_upload_confirmed"])
            with self.assertRaises(ReviewSyncError):
                sync_videos(status, root / "missing")
            self.assertFalse((root / "missing").exists())

    def test_real_accepted_inputs_import_generated_paths_and_unique_counts(self):
        from visual_materials import verify_asset

        root = Path(__file__).parent / "outputs"
        if not (root / "visuals_associative_20261101").exists():
            self.skipTest("Локальные реальные входы отсутствуют в исходниковом архиве.")
        with tempfile.TemporaryDirectory() as temporary:
            for n, count in ((1, 3), (2, 3), (3, 2)):
                visual = root / "visuals_associative_20261101" / f"topic_{n}"
                destination = Path(temporary) / f"topic_{n}"
                snapshot_inputs(visual, destination, include_audio=False)
                import_manifest(
                    root
                    / "gemini_migration_20261101"
                    / f"topic_{n}"
                    / "audio_manifest.json",
                    destination,
                    "audio",
                )
                import_manifest(
                    visual / "visual_materials.json", destination, "visuals"
                )
                script = approved_script(destination)
                a = read(destination / "audio_manifest.json")
                v = read(destination / "visual_materials.json")
                _, duration, words, fragments = checked_audio(script, a, destination)
                assets, size = checked_visuals(script, v, destination)
                aliases = config()["editing"]["subtitle_segment"]["equivalences"]
                proof = binding(script, words, fragments, aliases)
                raw = shot_timeline(
                    script, v, fragments, words, aliases, binding_proof=proof
                )
                selected, merged = first_appearance_sequence(assets, raw)
                self.assertEqual(len(selected), count)
                self.assertAlmostEqual(merged[-1]["display_end"], duration)
                for asset_value in selected:
                    verify_asset(
                        {"asset": asset_value, "review": {"decision": "approved"}},
                        destination,
                    )


if __name__ == "__main__":
    unittest.main()
