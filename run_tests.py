"""Запускает весь набор unittest с запретом настоящих сетевых подключений и DNS."""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch


def main() -> int:
    suite = unittest.defaultTestLoader.discover(".", pattern="test_*.py")
    # MockTransport и подставные API работают; случайный выход в сеть сразу провалит тест.
    with tempfile.TemporaryDirectory() as telemetry_directory, \
         patch("openai_meter.DEFAULT_DIRECTORY", Path(telemetry_directory)), \
         patch("billing_sync.refresh_after_report"), \
         patch("socket.socket.connect", side_effect=AssertionError("Real network forbidden")), \
         patch("socket.getaddrinfo", side_effect=AssertionError("Real DNS forbidden")):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
