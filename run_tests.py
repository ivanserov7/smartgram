"""Run pipeline and broker regressions without the separately installed MCP SDK."""

import ipaddress
import os
import socket
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent


class OfflineTestLoader(unittest.TestLoader):
    """Keep SDK protocol tests in their mandatory, isolated CI environment."""

    def _match_path(self, path, full_path, pattern):
        # Exclude before import: the pipeline environment must not require the SDK.
        return path != "test_mcp_http.py" and super()._match_path(path, full_path, pattern)


def loopback(host):
    """Allow only literal loopback addresses and the local test server name."""
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host in {"localhost", "", None}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@contextmanager
def offline_network():
    """Local HTTP fixtures work; accidental provider calls fail immediately."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_dns = socket.getaddrinfo

    def check(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6} and not loopback(address[0]):
            raise AssertionError("External network forbidden in tests")

    def connect(sock, address):
        check(sock, address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(sock, address)
        return original_connect_ex(sock, address)

    def dns(host, *args, **kwargs):
        if not loopback(host):
            raise AssertionError("External DNS forbidden in tests")
        return original_dns(host, *args, **kwargs)

    with patch("socket.socket.connect", connect), \
         patch("socket.socket.connect_ex", connect_ex), \
         patch("socket.getaddrinfo", dns):
        yield


def main():
    """One discovery-based suite for developers, CI and release checks."""
    # HTTP fixture behavior must not depend on a developer's VPN/proxy setup.
    proxy_keys = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "gemini_http_proxy"}
    credential_keys = {"OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPGRAM_API_KEY", "PEXELS_API_KEY"}
    environment = {key: value for key, value in os.environ.items()
                   if key.lower() not in proxy_keys and key not in credential_keys}
    with tempfile.TemporaryDirectory() as telemetry_directory, \
         patch.dict(os.environ, environment, clear=True), \
         patch("openai_meter.DEFAULT_DIRECTORY", Path(telemetry_directory)), \
         offline_network():
        suite = OfflineTestLoader().discover(str(ROOT), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
