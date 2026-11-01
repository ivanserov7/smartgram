"""HTML строится по локальным подставным материалам, без сети и настоящей речи."""

import json
import tempfile
import unittest
from pathlib import Path

from audio_files import demo_wav, inspect_wav
from commons_api import VisualError
from result_page import build_result_page


class ResultPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def save(self, name, value):
        (self.folder / name).write_text(json.dumps(value))

    def test_warnings_and_blockers_separate_when_script_never_created(self):
        self.save("research.json", {"schema_version": 2, "status": "insufficient_data",
                                    "warnings": ["Нет региональных измерений"],
                                    "blocking_issues": ["Основной механизм противоречив"]})
        page = build_result_page(self.folder).read_text()
        self.assertIn("Проверка не запускалась", page)
        self.assertNotIn("Не прошёл проверку", page)
        self.assertIn("Предупреждения (не блокируют ролик)", page)
        self.assertIn("Блокирующие проблемы исследования", page)
        self.assertIn("Нет региональных измерений", page)
        self.assertIn("Основной механизм противоречив", page)

    def test_stopped_run_shows_reason_and_draft_without_invented_materials(self):
        self.save("run.json", {"status": "stopped", "stage": "verification", "reason": "Источник недоступен"})
        self.save("research.json", {"status": "insufficient_data", "limitations": ["Недостаточно измерений"]})
        self.save("script_1.json", {"text": "Тестовый черновик <script>unsafe</script>"})
        page = build_result_page(self.folder).read_text()
        self.assertIn("Источник недоступен", page)
        self.assertIn("Недостаточно измерений", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("План сцен ещё не создан", page)
        self.assertNotIn("<audio", page)
        self.assertIn("длительность неизвестна", page)

    def test_duration_measured_from_wav_instead_of_estimate(self):
        content = demo_wav("Тест", None, "")
        (self.folder / "test.wav").write_bytes(content)
        self.save("scenes.json", {"scenes": [{"id": 1, "narration": "Тест", "search_query": "sunset",
                                             "visual_description": "Закат", "estimated_duration_seconds": 999}]})
        self.save("audio_manifest.json", {"scenes": [{"scene_id": 1, "narration": "Тест", "audio_path": "test.wav",
                                                      "duration_seconds": 999, **inspect_wav(content)}]})
        page = build_result_page(self.folder).read_text()
        self.assertIn("0.250 с", page)
        self.assertIn("data:audio/wav;base64,", page)
        self.assertIn("Проверенное изображение не выбрано", page)

    def test_path_outside_run_is_rejected(self):
        self.save("scenes.json", {"scenes": [{"id": 1, "narration": "Тест", "search_query": "sunset", "visual_description": "Закат"}]})
        self.save("audio_manifest.json", {"scenes": [{"scene_id": 1, "narration": "Тест", "audio_path": "../secret.wav"}]})
        with self.assertRaisesRegex(VisualError, "пределы"):
            build_result_page(self.folder)


if __name__ == "__main__":
    unittest.main()
