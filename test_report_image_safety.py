"""Сканер изображений не ослабляет защиту текста и известных ключей."""

import base64
import io
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from html_report_sync import read_report
from report_image_safety import validate_jpeg
from review_archive_sync import ReviewSyncError


class ReportImageSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "report.html"

    def image(self, kind="JPEG", pixels=None):
        image = (
            Image.frombytes("RGB", (20, 20), pixels)
            if pixels
            else Image.new("RGB", (20, 20), "navy")
        )
        out = io.BytesIO()
        image.save(out, format=kind)
        return out.getvalue()

    def report(self, content, kind="jpeg", suffix=""):
        text = (
            '<html><img src="data:image/'
            + kind
            + ";base64,"
            + base64.b64encode(content).decode()
            + '">'
            + suffix
            + "</html>"
        )
        self.path.write_text(text)
        return text.encode()

    def test_valid_jpeg_png_bytes_unchanged(self):
        for kind, mime in [("JPEG", "jpeg"), ("PNG", "png")]:
            expected = self.report(self.image(kind), mime)
            self.assertEqual(read_report(self.path), expected)

    def test_exif_comment_and_trailing_attachment_rejected(self):
        jpeg = self.image()
        for value in (
            jpeg + b"extra",
            jpeg[:2] + b"\xff\xfe\x00\x08hidden" + jpeg[2:],
            jpeg[:2] + b"\xff\xe1\x00\x08hidden" + jpeg[2:],
        ):
            self.report(value)
            with self.assertRaises(ReviewSyncError):
                read_report(self.path)

    def test_known_secret_in_compressed_png_pixels_rejected(self):
        secret = "opaque_fixture_value"
        pixels = (secret.encode() + b"\0" * 1200)[:1200]
        self.report(self.image("PNG", pixels), "png")
        with self.assertRaisesRegex(ReviewSyncError, "секрет"):
            read_report(self.path, [secret])

    def test_visible_key_outside_image_still_rejected(self):
        self.report(self.image(), suffix="sk-" + "Z" * 25)
        with self.assertRaisesRegex(ReviewSyncError, "секрет"):
            read_report(self.path)

    def test_png_crc_and_unknown_text_chunks_rejected(self):
        import struct, zlib

        png = self.image("PNG")
        data = b"Software\0hidden"
        chunk = (
            struct.pack(">I", len(data))
            + b"tEXt"
            + data
            + struct.pack(">I", zlib.crc32(b"tEXt" + data))
        )
        for value in (
            png[:-1] + b"x",
            png[:33] + chunk + png[33:],
            b"\x89PNG\r\n\x1a\n",
        ):
            self.report(value, "png")
            with self.assertRaises(ReviewSyncError):
                read_report(self.path)

    def test_key_like_base64_in_valid_pixels_is_not_a_text_secret(self):
        import struct, zlib
        from build_review_archive import SECRET_VALUE

        pattern = base64.b64decode("AKIA" + "A" * 16)

        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data))
            )

        for offset in range(6):
            row = b"\0" * offset + pattern + b"\0" * (60 - offset - len(pattern))
            png = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 20, 1, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\0" + row, level=0))
                + chunk(b"IEND", b"")
            )
            expected = self.report(png, "png")
            if SECRET_VALUE.search(expected.decode()):
                self.assertEqual(read_report(self.path), expected)
                return
        self.fail("Fixture did not produce the required base64 coincidence")


if __name__ == "__main__":
    unittest.main()
