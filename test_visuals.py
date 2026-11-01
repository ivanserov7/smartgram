"""Подставные ответы Wikimedia и искусственные тестовые JPEG/PNG; сети и платных API нет."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

import commons_api
import main
import visuals
from commons_api import VisualError, accepted_license
from image_files import inspect_image


def image_bytes(size=(1200, 1600), image_format="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(210, 80, 40)).save(buffer, format=image_format)
    return buffer.getvalue()


def commons_page(name="Red sunset sky", width=1200, height=1600, license_name="CC BY-SA 4.0",
                 license_url="https://creativecommons.org/licenses/by-sa/4.0/", mime="image/png"):
    title = name.replace(" ", "_") + (".png" if mime == "image/png" else ".jpg")
    return {"title": f"File:{title}", "imageinfo": [{
        "url": f"https://upload.wikimedia.org/wikipedia/commons/4/40/{title}",
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{title}",
        "width": width, "height": height, "mime": mime,
        "extmetadata": {
            "ObjectName": {"value": name},
            "Artist": {"value": '<a href="https://example.org/author">Test Author</a>'},
            "ImageDescription": {"value": f"<p>{name}, horizon.</p>"},
            "LicenseShortName": {"value": license_name}, "LicenseUrl": {"value": license_url},
            "UsageTerms": {"value": license_name}, "AttributionRequired": {"value": "true"},
            "Restrictions": {"value": ""}, "Credit": {"value": "Own work"},
        },
    }]}


class VisualTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "scenes.json"
        self.scene = {"id": 1, "title": "Красный закат", "search_query": "red sunset sky",
                      "visual_description": "Красное небо во время заката", "material_type": "image"}
        self.plan = {"mode": "real", "scenes": [self.scene]}
        self.path.write_text(json.dumps(self.plan))
        self.pages = [commons_page()]
        self.content = image_bytes()
        self.requests = []
        self.api_status = 200
        self.download_status = 200
        self.download_headers = {}
        self.transport_patch = patch.object(visuals, "create_commons_client", side_effect=self.make_client)
        self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)
        network = patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Real network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def handler(self, request):
        self.requests.append(request)
        if request.url.host == "commons.wikimedia.org":
            return httpx.Response(self.api_status, json={"query": {"pages": self.pages}})
        return httpx.Response(self.download_status, headers={"content-type": "image/png", **self.download_headers}, content=self.content)

    def make_client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def run_selection(self):
        manifest_path = visuals.collect_visuals(self.path)
        return json.loads(manifest_path.read_text())

    def test_successful_search_and_attribution(self):
        result = self.run_selection()
        self.assertEqual(result["status"], "ready_for_visual_review")
        self.assertEqual(result["visual_check_status"], "not_performed")
        selected = result["scenes"][0]["selected"]
        self.assertEqual(selected["author"], "Test Author")
        self.assertEqual(selected["license"], "CC BY-SA 4.0")
        self.assertEqual(selected["license_url"], "https://creativecommons.org/licenses/by-sa/4.0/")
        self.assertIn("File:Red_sunset_sky.png", selected["source_page"])
        self.assertIn("Test Author", selected["attribution"])
        self.assertTrue((self.directory / selected["local_path"]).exists())
        self.assertIn("предварительный подбор", result["scenes"][0]["reason"])
        query = self.requests[0].url.params
        self.assertEqual(query["gsrnamespace"], "6")
        self.assertEqual(query["gsrsearch"], self.scene["search_query"])
        self.assertEqual(query["gsrlimit"], "5")
        self.assertIn("extmetadata", query["iiprop"])
        self.assertEqual(len(self.requests), 2)

    def test_empty_search_needs_visual(self):
        self.pages = []
        result = self.run_selection()
        self.assertEqual(result["status"], "needs_visual")
        self.assertIsNone(result["scenes"][0]["selected"])
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(list((self.directory / "visuals").glob("*.png")))

    def test_unknown_or_restricted_license_skips_download(self):
        examples = [("Unknown", ""), ("CC BY-NC 4.0", "https://creativecommons.org/licenses/by-nc/4.0/"),
                    ("CC BY 4.0", "https://evil.example/licenses/by/4.0/"),
                    ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by/4.0/")]
        for name, url in examples:
            with self.subTest(license=name, url=url):
                self.pages = [commons_page(license_name=name, license_url=url)]
                (self.directory / "visual_manifest.json").unlink(missing_ok=True)
                self.requests.clear()
                result = self.run_selection()
                self.assertEqual(result["scenes"][0]["status"], "needs_visual")
                self.assertEqual(len(self.requests), 1)
                self.assertIn("Лицензия", result["scenes"][0]["candidates"][0]["reason"])

    def test_corrupted_download_rejected(self):
        self.content = b"not a picture"
        result = self.run_selection()
        self.assertEqual(result["scenes"][0]["status"], "needs_visual")
        self.assertIn("повреждён", result["scenes"][0]["candidates"][0]["reason"])
        self.assertFalse(list((self.directory / "visuals").glob("*.png")))

    def test_actual_resolution_checked_after_download(self):
        self.content = image_bytes((100, 100))
        result = self.run_selection()
        self.assertEqual(result["status"], "needs_visual")
        self.assertIn("Недостаточное разрешение", result["scenes"][0]["candidates"][0]["reason"])

    def test_irrelevant_file_not_selected(self):
        self.pages = [commons_page("Motorcycle racing")]
        result = self.run_selection()
        self.assertEqual(result["status"], "needs_visual")
        self.assertEqual(len(self.requests), 1)

    def test_more_relevant_candidate_wins_over_resolution(self):
        self.pages = [commons_page("Sunset sky", width=4000, height=4000), commons_page("Red sunset sky")]
        result = self.run_selection()
        self.assertEqual(result["scenes"][0]["selected"]["title"], "Red sunset sky")

    def test_quality_breaks_relevance_tie(self):
        self.pages = [commons_page("Red sunset sky small", width=800, height=1280),
                      commons_page("Red sunset sky large", width=1200, height=1600)]
        result = self.run_selection()
        self.assertEqual(result["scenes"][0]["selected"]["title"], "Red sunset sky large")

    def test_broken_candidate_falls_back_to_next_relevant_candidate(self):
        self.pages = [commons_page("Red sunset sky broken"), commons_page("Red sunset sky valid")]
        original = self.handler

        def handler(request):
            if "broken" in request.url.path:
                self.requests.append(request)
                return httpx.Response(200, headers={"content-type": "image/png"}, content=b"broken")
            return original(request)

        with patch.object(self, "handler", side_effect=handler):
            result = self.run_selection()
        self.assertEqual(result["scenes"][0]["status"], "selected")
        self.assertEqual(result["scenes"][0]["selected"]["title"], "Red sunset sky valid")

    def test_video_not_replaced_with_image(self):
        self.scene["material_type"] = "video"
        self.path.write_text(json.dumps(self.plan))
        result = self.run_selection()
        self.assertEqual(result["scenes"][0]["status"], "video_not_implemented")
        self.assertEqual(result["status"], "needs_visual")
        self.assertFalse(self.requests)

    def test_intact_download_reused_without_network(self):
        self.run_selection()
        self.requests.clear()
        result = self.run_selection()
        self.assertFalse(self.requests)
        self.assertTrue(result["scenes"][0]["reused"])

    def test_changed_query_or_description_invalidates_cache(self):
        self.run_selection()
        self.requests.clear()
        self.scene["search_query"] = "sunset sky"
        self.path.write_text(json.dumps(self.plan))
        self.run_selection()
        self.assertEqual(len(self.requests), 2)
        self.requests.clear()
        self.scene["visual_description"] += " у горизонта"
        self.path.write_text(json.dumps(self.plan))
        self.run_selection()
        self.assertEqual(len(self.requests), 2)

    def test_corrupted_cache_is_downloaded_again(self):
        result = self.run_selection()
        (self.directory / result["scenes"][0]["selected"]["local_path"]).write_bytes(b"broken")
        self.requests.clear()
        result = self.run_selection()
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(result["scenes"][0]["reused"])

    def test_lost_manifest_recovered_from_receipt(self):
        self.run_selection()
        (self.directory / "visual_manifest.json").write_text("broken")
        self.requests.clear()
        result = self.run_selection()
        self.assertEqual(result["scenes"][0]["status"], "selected")
        self.assertFalse(self.requests)

    def test_download_size_limit(self):
        self.download_headers = {"content-length": str(commons_api.MAX_IMAGE_BYTES + 1)}
        result = self.run_selection()
        self.assertEqual(result["status"], "needs_visual")
        self.assertIn("размер", result["scenes"][0]["candidates"][0]["reason"])

    def test_stream_limit_without_content_length(self):
        with patch.object(commons_api, "MAX_IMAGE_BYTES", 20):
            result = self.run_selection()
        self.assertEqual(result["status"], "needs_visual")

    def test_api_error_preserved_without_retry(self):
        self.api_status = 429
        with self.assertRaisesRegex(VisualError, "429"):
            self.run_selection()
        result = json.loads((self.directory / "visual_manifest.json").read_text())
        self.assertEqual(result["status"], "error")
        self.assertIn("429", result["scenes"][0]["reason"])
        self.assertEqual(len(self.requests), 1)

    def test_download_http_error_preserved(self):
        self.download_status = 404
        with self.assertRaisesRegex(VisualError, "404"):
            self.run_selection()
        result = json.loads((self.directory / "visual_manifest.json").read_text())
        self.assertEqual(result["status"], "error")
        self.assertIn("404", result["reason"])

    def test_official_thumbnail_host_allowed(self):
        self.pages[0]["imageinfo"][0]["thumburl"] = "https://thumb.wikimedia.org/wikipedia/commons/thumb/4/40/Sunset.png/3840px-Sunset.png"
        self.pages[0]["imageinfo"][0].update(thumbwidth=1200, thumbheight=1600, thumbmime="image/png")
        result = self.run_selection()
        self.assertEqual(result["scenes"][0]["status"], "selected")
        self.assertEqual(self.requests[1].url.host, "thumb.wikimedia.org")

    def test_lookalike_thumbnail_host_rejected(self):
        self.pages[0]["imageinfo"][0]["url"] = "https://thumb.wikimedia.org.evil.example/wikipedia/commons/a/b/Sunset.png"
        result = self.run_selection()
        self.assertEqual(result["scenes"][0]["status"], "needs_visual")
        self.assertEqual(len(self.requests), 1)

    def test_invalid_scene_has_clear_error(self):
        for name, value in (("search_query", None), ("material_type", []), ("id", True)):
            scene = {**self.scene, name: value}
            self.path.write_text(json.dumps({"scenes": [scene]}))
            with self.subTest(field=name), self.assertRaises(VisualError):
                self.run_selection()

    def test_standalone_cli_skips_voice_and_pipeline(self):
        with patch("sys.argv", ["main.py", "--visuals-file", str(self.path)]), \
             patch.object(main, "voiceover_from_file", side_effect=AssertionError("Voice called")), \
             patch.object(main, "run_pipeline", side_effect=AssertionError("Pipeline called")):
            self.assertEqual(main.main(), 0)
        self.assertEqual(len(self.requests), 2)

    def test_demo_does_not_create_network_client_or_fake_image(self):
        with patch.object(visuals, "create_commons_client", side_effect=AssertionError("Network client called")):
            path = visuals.collect_visuals(self.path, demo=True)
        result = json.loads(path.read_text())
        self.assertEqual(result["mode"], "demo")
        self.assertEqual(result["status"], "needs_visual")
        self.assertFalse(list((self.directory / "visuals").glob("*.png")))

    def test_dry_run_skips_file_reads(self):
        with patch("sys.argv", ["main.py", "--visuals-file", "/missing/scenes.json", "--dry-run"]), \
             patch.object(main, "collect_visuals", side_effect=AssertionError("Visuals called")):
            self.assertEqual(main.main(), 0)


class ImageValidationTests(unittest.TestCase):
    def test_configurable_user_agent_without_keys(self):
        user_agent = "SmartgramBot/0.1 (local test contact placeholder)"
        with patch.object(commons_api, "dotenv_values", return_value={"WIKIMEDIA_USER_AGENT": user_agent}):
            with commons_api.create_commons_client() as client:
                self.assertEqual(client.headers["User-Agent"], user_agent)

    def test_invalid_user_agent_is_clear_error(self):
        with patch.object(commons_api, "dotenv_values", return_value={"WIKIMEDIA_USER_AGENT": "bad\nheader"}):
            with self.assertRaisesRegex(VisualError, "WIKIMEDIA_USER_AGENT"):
                commons_api.create_commons_client()

    def test_jpeg_and_png_open(self):
        for image_format in ("JPEG", "PNG"):
            with self.subTest(image_format=image_format):
                self.assertEqual(inspect_image(image_bytes(image_format=image_format))["format"], image_format)

    def test_truncated_files_rejected(self):
        for image_format in ("JPEG", "PNG"):
            with self.subTest(image_format=image_format), self.assertRaises(VisualError):
                inspect_image(image_bytes(image_format=image_format)[:100])

    def test_supported_license_urls(self):
        self.assertEqual(accepted_license("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"), "cc-by-4.0")
        self.assertEqual(accepted_license("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"), "cc0-1.0")
        self.assertIsNone(accepted_license("Public domain", ""))
        self.assertIsNone(accepted_license("GFDL", "https://www.gnu.org/licenses/fdl-1.3.html"))

    def test_localized_license_deeds(self):
        self.assertEqual(accepted_license("CC0", "http://creativecommons.org/publicdomain/zero/1.0/deed.en"), "cc0-1.0")
        self.assertEqual(accepted_license("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/deed.ru"), "cc-by-sa-4.0")
        self.assertIsNone(accepted_license("CC0", "https://creativecommons.org/publicdomain/zero/1.0/unknown"))


class WikimediaNetworkTests(unittest.TestCase):
    """Транспортные ошибки не превращаются в NameError и не повторяются автоматически."""

    def test_connect_error_and_read_timeout_in_both_operations(self):
        for operation in ("search", "download"):
            for error_type in (httpx.ConnectError, httpx.ReadTimeout):
                with self.subTest(operation=operation, error=error_type.__name__):
                    calls = []

                    def handler(request):
                        calls.append(request)
                        raise error_type("Private transport details", request=request)

                    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                        with self.assertRaises(commons_api.VisualServiceError) as caught:
                            if operation == "search":
                                commons_api.search_images("sunset", client)
                            else:
                                commons_api.download_image("https://upload.wikimedia.org/wikipedia/commons/a/ab/Sunset.jpg", client)
                    self.assertEqual(len(calls), 1)
                    self.assertIn(error_type.__name__, str(caught.exception))
                    self.assertIn("ошибка сети или таймаут", str(caught.exception))
                    self.assertNotIn("Private", str(caught.exception))
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(caught.exception.__suppress_context__)

    def test_cli_network_error_has_no_traceback_or_retry(self):
        for error_type in (httpx.ConnectError, httpx.ReadTimeout):
            for download in (False, True):
                with self.subTest(error=error_type.__name__, download=download), tempfile.TemporaryDirectory() as temporary:
                    folder = Path(temporary)
                    path = folder / "scenes.json"
                    path.write_text(json.dumps({"scenes": [{"id": 1, "material_type": "image", "search_query": "red sunset sky", "visual_description": "Закат"}]}))
                    failed_calls = []

                    def handler(request):
                        if download and request.url.host == "commons.wikimedia.org":
                            return httpx.Response(200, json={"query": {"pages": [commons_page()]}})
                        failed_calls.append(request)
                        raise error_type("private", request=request)

                    client = httpx.Client(transport=httpx.MockTransport(handler))
                    with patch.object(visuals, "create_commons_client", return_value=client), \
                         patch("sys.argv", ["main.py", "--visuals-file", str(path)]), \
                         patch("sys.stderr", new_callable=io.StringIO) as errors, redirect_stdout(io.StringIO()):
                        self.assertEqual(main.main(), 1)
                    self.assertEqual(len(failed_calls), 1)
                    self.assertIn(error_type.__name__, errors.getvalue())
                    self.assertNotIn("Traceback", errors.getvalue())
                    saved = json.loads((folder / "visual_manifest.json").read_text())
                    self.assertEqual(saved["status"], "error")


if __name__ == "__main__":
    unittest.main()
