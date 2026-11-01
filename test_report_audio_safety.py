"""Защита HTML с WAV: ложное совпадение base64 не скрывает настоящий секрет."""

import base64
import struct
import tempfile
import unittest
from pathlib import Path

from build_review_archive import SECRET_VALUE
from comparison_audio import pcm_wav
from html_report_sync import read_report
from review_archive_sync import ReviewSyncError


class ReportAudioSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "report.html"

    def report(self, content, suffix=""):
        payload = base64.b64encode(content).decode()
        text = (
            '<html><audio src="data:audio/wav;base64,'
            + payload
            + '"></audio>'
            + suffix
            + "</html>"
        )
        self.path.write_text(text)
        return text

    def test_random_key_like_base64_passes_only_after_decoded_pcm_validation(self):
        # Данные выровнены так, чтобы base64 действительно содержал ложный AWS-паттерн.
        encoded = "AKIA" + "A" * 16
        wav = pcm_wav(b"\0" + base64.b64decode(encoded))
        text = self.report(wav)
        self.assertIsNotNone(SECRET_VALUE.search(text))
        self.assertEqual(read_report(self.path), text.encode())

    def test_known_secret_hidden_inside_valid_pcm_is_rejected(self):
        secret = "opaque_fixture_value"
        payload = secret.encode()
        payload += b"\0" * (len(payload) % 2)
        self.report(pcm_wav(payload))
        with self.assertRaisesRegex(ReviewSyncError, "секрет"):
            read_report(self.path, [secret])

    def test_unknown_key_pattern_in_decoded_pcm_is_rejected(self):
        key = ("sk-" + "Z" * 25).encode()
        self.report(pcm_wav(key))
        with self.assertRaisesRegex(ReviewSyncError, "секрет"):
            read_report(self.path)

    def test_visible_secret_outside_audio_still_rejected(self):
        self.report(pcm_wav(b"\1\0" * 20), suffix="sk-" + "Z" * 25)
        with self.assertRaisesRegex(ReviewSyncError, "секрет"):
            read_report(self.path)

    def test_trailing_data_and_extra_riff_attachment_rejected(self):
        wav = pcm_wav(b"\1\0" * 20)
        for content in (
            wav + b"extra",
            wav + b"JUNK" + struct.pack("<I", 4) + b"extra"[:4],
        ):
            if content.endswith(b"extr"):
                content = (
                    content[:4] + struct.pack("<I", len(content) - 8) + content[8:]
                )
            with self.subTest(length=len(content)):
                self.report(content)
                with self.assertRaises(ReviewSyncError):
                    read_report(self.path)

    def append_chunk(self, wav, kind, payload):
        content = (
            wav
            + kind
            + struct.pack("<I", len(payload))
            + payload
            + b"\0" * (len(payload) % 2)
        )
        return content[:4] + struct.pack("<I", len(content) - 8) + content[8:]

    def test_google_c2pa_is_preserved_and_scanned_for_secrets(self):
        wav = pcm_wav(b"\1\0" * 20)
        content = self.append_chunk(wav, b"C2PA", b"test provenance metadata")
        text = self.report(content)
        self.assertEqual(read_report(self.path), text.encode())
        for payload, secrets in (
            (b"opaque_fixture_value", ["opaque_fixture_value"]),
            (("sk-" + "Z" * 25).encode(), []),
        ):
            with self.subTest(payload_length=len(payload)):
                self.report(self.append_chunk(wav, b"C2PA", payload))
                with self.assertRaisesRegex(ReviewSyncError, "секрет"):
                    read_report(self.path, secrets)

    def test_invalid_c2pa_size_position_and_duplicate_are_rejected(self):
        wav = pcm_wav(b"\1\0" * 20)
        one = self.append_chunk(wav, b"C2PA", b"test")
        wrong_position = one[:12] + one[-12:] + one[12:-12]
        for content in (
            self.append_chunk(wav, b"C2PA", b""),
            self.append_chunk(wav, b"C2PA", b"x" * 65537),
            self.append_chunk(one, b"C2PA", b"test"),
            wrong_position,
        ):
            with self.subTest(length=len(content)):
                self.report(content)
                with self.assertRaises(ReviewSyncError):
                    read_report(self.path)


if __name__ == "__main__":
    unittest.main()
