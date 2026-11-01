"""The test harness must prevent accidental provider traffic."""
import socket
import unittest
from unittest.mock import patch

from run_tests import loopback, offline_network


class OfflineHarnessTests(unittest.TestCase):
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
