"""Проверки локального отчёта: подставные изображения, без настоящих сетевых запросов."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import review_gallery
from commons_api import VisualError
from image_files import inspect_image
from test_visuals import commons_page, image_bytes


class ReviewGalleryTests(unittest.TestCase):
    def test_small_preview_does_not_relax_scene_image_requirements(self):
        content = image_bytes(size=(60, 60))
        self.assertTrue(review_gallery.image_data(content).startswith("data:image/png;base64,"))
        with self.assertRaises(VisualError):
            inspect_image(content)

    def test_links_reject_executable_schemes(self):
        self.assertEqual(review_gallery.link("javascript:alert(1)", "Источник"), "Источник")
        self.assertNotIn('<script>', review_gallery.link('https://example.org/', '<script>'))

    def test_local_path_cannot_escape_run_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            with self.assertRaisesRegex(VisualError, "пределы"):
                review_gallery.local_image_data(root, "../outside.png")

    def test_all_candidates_and_rejection_reasons_rendered_safely(self):
        from commons_api import parse_candidate
        candidate = {**parse_candidate(commons_page("<script>Red sunset sky</script>")),
                     "status": "rejected", "reason": "Слишком маленькое изображение.", "relevance_score": 0.7}
        candidate["title"] = "<script>untrusted</script>"
        manifest = {"scenes": [{"scene_id": 1, "search_query": "red sunset sky", "reason": "Нет пригодного изображения.",
                                 "candidates": [candidate], "selected": None}]}
        with tempfile.TemporaryDirectory() as temporary:
            page = review_gallery.render_gallery(manifest, Path(temporary), {})
        self.assertIn("red sunset sky", page)
        self.assertIn("Слишком маленькое изображение.", page)
        self.assertIn("&lt;script&gt;untrusted&lt;/script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("Отклонён", page)

    def test_preview_addresses_come_from_api(self):
        calls = []
        url = 'https://thumb.wikimedia.org/wikipedia/commons/thumb/a/b/Sunset.png/500px-Sunset.png'

        def handler(request):
            calls.append(request)
            if request.url.host == "commons.wikimedia.org":
                self.assertEqual(request.url.params['iiurlwidth'], '500')
                return httpx.Response(200, json={"query": {"pages": [{"title": "File:Sunset.png", "imageinfo": [{
                    "mime": "image/png", "thumburl": url}]}]}})
            self.assertEqual(str(request.url), url)
            return httpx.Response(200, headers={"content-type": "image/png"}, content=image_bytes(size=(60, 60)))

        manifest = {"scenes": [{"candidates": [{"file_title": "File:Sunset.png"}]}]}
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as temporary, patch.object(review_gallery, "create_commons_client", return_value=client):
            previews = review_gallery.fetch_previews(manifest, Path(temporary))
            self.assertEqual(previews["File:Sunset.png"]["url"], url)
            saved = json.loads((Path(temporary) / 'gallery_previews.json').read_text())
            self.assertEqual(saved['purpose'], 'review_only')
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
