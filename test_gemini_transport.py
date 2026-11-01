import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from gemini_transport import gemini_proxy_options
from voice_errors import VoiceoverError


class GeminiTransportTests(unittest.TestCase):
    def test_safe_default_template_remains_valid_for_source_exports(self):
        from build_review_archive import validate_env_example
        template = Path(__file__).with_name('.env.example').read_text()
        validate_env_example(template)

    def test_env_file_and_process_override_do_not_mutate_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("GEMINI_HTTP_PROXY=http://127.0.0.1:17890\n")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(gemini_proxy_options(path)["proxy"], "http://127.0.0.1:17890")
                self.assertNotIn("GEMINI_HTTP_PROXY", os.environ)
            with patch.dict(os.environ, {"GEMINI_HTTP_PROXY": ""}, clear=True):
                self.assertEqual(gemini_proxy_options(path), {})

    def test_rejects_subscription_and_remote_proxy_without_disclosing_them(self):
        for value in ("vless://secret@example.com:443", "https://example.com/secret",
                      "http://user:secret@127.0.0.1:17890", "http://127.0.0.1:99999",
                      "http://127.0.0.1:17890?secret", "http://example.com:8080"):
            with self.subTest(value=value), patch.dict(os.environ, {"GEMINI_HTTP_PROXY": value}):
                with self.assertRaises(VoiceoverError) as error:
                    gemini_proxy_options()
                self.assertNotIn(value, str(error.exception))
                self.assertNotIn("secret", str(error.exception))

    def test_https_uses_connect_despite_no_proxy_and_never_resolves_target(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5)
            port = listener.getsockname()[1]
            requests, errors = [], []

            def serve():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(5)
                        requests.append(connection.recv(4096))
                        connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            original = socket.getaddrinfo
            targets = []

            def resolve(host, *args, **kwargs):
                targets.append(host)
                if host not in ("127.0.0.1", b"127.0.0.1"):
                    raise AssertionError("Unexpected direct DNS resolution")
                return original(host, *args, **kwargs)

            with patch.dict(os.environ, {"GEMINI_HTTP_PROXY": f"http://127.0.0.1:{port}",
                                         "NO_PROXY": "*", "HTTPS_PROXY": "http://unused.invalid:1"}):
                with patch("socket.getaddrinfo", side_effect=resolve):
                    with httpx.Client(**gemini_proxy_options(), timeout=2) as client:
                        with self.assertRaises(httpx.ProxyError):
                            client.get("https://gemini-proxy-test.invalid/")
            thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(requests), 1)
            self.assertTrue(requests[0].startswith(b"CONNECT gemini-proxy-test.invalid:443 HTTP/1.1"))
            self.assertTrue(targets)


if __name__ == "__main__":
    unittest.main()
