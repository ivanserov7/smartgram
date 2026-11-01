"""Интеграция готовых модулей: подставные ответы, без сети и новой речи."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commons_api import VisualServiceError
from file_io import atomic_write_json
from production_state import ProductionServiceError, read
from test_full_voiceover import make_script
from test_production import metadata
from visual_materials import VisualMaterials
from visuals_only_batch import run_batch


class VisualIntegrationTests(unittest.TestCase):
    def test_shared_model_service_error_stops_other_fragments_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = {
                "script_version_id": "test",
                "fragments": [
                    {"text": "Земля", "visual": "Earth"},
                    {"text": "Спутник", "visual": "Satellite"},
                ],
            }
            with patch(
                "production_state.ModelClient.__call__",
                side_effect=ProductionServiceError("HTTP 429 insufficient_quota"),
            ) as api:
                first = read(VisualMaterials(root).run(script))
                second = read(VisualMaterials(root).run(script))
            self.assertEqual(api.call_count, 1)
            self.assertIn("429", first["service_error"])
            self.assertIn("429", second["service_error"])
            self.assertEqual(
                [entry["status"] for entry in second["fragments"]],
                ["error", "not_started"],
            )
            self.assertFalse((root / "audio_manifest.json").exists())

    def test_download_service_error_does_not_trigger_new_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = []

            def model(step, settings, schema, data, images):
                calls.append(step)
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

            runner = VisualMaterials(
                temporary,
                model=model,
                search=lambda *args: {"candidates": [metadata()], "continue": {}},
                download=lambda *args: (_ for _ in ()).throw(
                    VisualServiceError("HTTP 403")
                ),
            )
            result = read(
                runner.run(
                    {
                        "script_version_id": "test",
                        "fragments": [{"text": "Земля", "visual": "Earth"}],
                    }
                )
            )
            self.assertEqual(calls, ["visual_queries"])
            self.assertIn("403", result["service_error"])
            self.assertEqual(
                sum(
                    op["step"] == "visual_search"
                    for op in runner.journal.state["operations"].values()
                ),
                1,
            )

    def test_batch_uses_approved_snapshot_without_wav_or_editing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            make_script(source)
            (source / "audio_manifest.json").write_text('{"status":"needs_review"}')
            batch = root / "script_batch"
            batch.mkdir()
            atomic_write_json(
                batch / "batch.json",
                {"entries": [{"topic": "Тестовая тема", "directory": str(source)}]},
            )

            def visuals(directory):
                script = read(directory / "script.json")
                path = directory / "visual_materials.json"
                atomic_write_json(
                    path,
                    {
                        "status": "needs_visual",
                        "fragments": [
                            {
                                "fragment_index": i,
                                "status": "needs_visual",
                                "reason": "Тест: пустой поиск",
                            }
                            for i in range(1, len(script["fragments"]) + 1)
                        ],
                    },
                )
                return path

            with patch(
                "visuals_only_batch.run_visual_materials", side_effect=visuals
            ), patch(
                "voiceover.VoiceoverRun.run",
                side_effect=AssertionError("TTS/ASR forbidden"),
            ), patch(
                "editing.run_editing", side_effect=AssertionError("Editing forbidden")
            ):
                report = run_batch(batch, root / "result")
            self.assertTrue(report.exists())
            self.assertFalse((root / "result/topic_1/audio_manifest.json").exists())
            self.assertFalse((root / "result/topic_1/render_manifest.json").exists())
            self.assertEqual(
                read(root / "result/topic_1/script.json")["script_version_id"],
                read(source / "script.json")["script_version_id"],
            )

    def test_main_visuals_dry_run_does_not_read_files_or_call_api(self):
        import main

        with patch(
            "sys.argv",
            [
                "main.py",
                "--resume-production",
                "missing",
                "--production-stage",
                "visuals",
                "--dry-run",
            ],
        ), patch(
            "main.continue_production", side_effect=AssertionError("API forbidden")
        ):
            self.assertEqual(main.main(), 0)


if __name__ == "__main__":
    unittest.main()
