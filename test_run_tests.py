"""The test harness must prevent accidental provider traffic."""
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_tests import OfflineTestLoader, loopback, offline_network


class OfflineHarnessTests(unittest.TestCase):
    def test_isolated_sdk_module_is_not_imported_but_other_tests_are_discovered(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sys, "path", sys.path.copy()), patch.dict(sys.modules):
            root = Path(folder)
            (root / "test_mcp_http.py").write_text("raise ImportError('MCP SDK is deliberately absent')\n")
            (root / "test_offline_discovery_fixture.py").write_text(
                "import unittest\nclass Regression(unittest.TestCase):\n"
                "    def test_regression(self): self.assertTrue(True)\n"
            )
            loader = OfflineTestLoader()
            suite = loader.discover(str(root))
            result = unittest.TestResult()
            suite.run(result)
            self.assertEqual(result.testsRun, 1)
            self.assertTrue(result.wasSuccessful(), result.errors)
            self.assertEqual(loader.errors, [])

    def test_unexpected_import_errors_still_fail_the_base_suite(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sys, "path", sys.path.copy()), patch.dict(sys.modules):
            (Path(folder) / "test_broken_discovery_fixture.py").write_text(
                "raise ImportError('A real regression must fail CI')\n"
            )
            suite = OfflineTestLoader().discover(folder)
            result = unittest.TestResult()
            suite.run(result)
            self.assertFalse(result.wasSuccessful())
            self.assertEqual(len(result.errors), 1)
            self.assertIn("A real regression must fail CI", result.errors[0][1])

    def test_only_loopback_names_and_addresses_are_allowed(self):
        for value in ['localhost', '127.0.0.1', '::1', b'127.0.0.1']:
            self.assertTrue(loopback(value))
        for value in ['api.openai.com', '192.168.1.1', '8.8.8.8', 'localhost.example.com']:
            self.assertFalse(loopback(value))

    def test_external_dns_is_blocked_before_resolution(self):
        with patch('socket.getaddrinfo') as resolver, offline_network():
            with self.assertRaisesRegex(AssertionError, 'External DNS'):
                socket.getaddrinfo('api.openai.com', 443)
            resolver.assert_not_called()

    def test_connect_and_connect_ex_are_blocked_before_network(self):
        for name in ['connect', 'connect_ex']:
            with patch('socket.socket.' + name) as connect, offline_network():
                with socket.socket() as sock:
                    with self.assertRaisesRegex(AssertionError, 'External network'):
                        getattr(sock, name)(('8.8.8.8', 443))
                connect.assert_not_called()

    def test_loopback_fixture_is_allowed(self):
        with patch('socket.socket.connect', return_value=None) as connect, offline_network():
            with socket.socket() as sock:
                sock.connect(('127.0.0.1', 12345))
            connect.assert_called_once()
