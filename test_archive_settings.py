"""Secret-export protections remain required after retiring the old TTS path."""

import tempfile
import unittest
from pathlib import Path

import build_review_archive as builder


class ArchiveSettingsTests(unittest.TestCase):
    def test_extended_example_is_safe(self):
        text = (builder.PROJECT_DIRECTORY / ".env.example").read_text()
        builder.validate_env_example(text)
        builder.validate_env_example(text + "\nFUTURE_API_KEY=your_api_key_here\nAUDIO_SAMPLE_RATE=24000\n")

    def test_non_placeholder_keys_are_rejected(self):
        for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "FUTURE_SECRET"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                builder.validate_env_example(f"{name}=not-a-placeholder")

    def test_known_key_formats_rejected_even_in_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "example.py"
            path.write_text("# " + "AIza" + "x" * 35)
            with self.assertRaises(ValueError):
                builder.read_safe_content(path)


if __name__ == "__main__":
    unittest.main()
