"""Run regression checks with public networking disabled, no provider credentials."""
import ipaddress
from pathlib import Path
import socket
import sys
import unittest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


def loopback(host):
    if host in {'localhost','',None}:return True
    try:return ipaddress.ip_address(host).is_loopback
    except ValueError:return False


if __name__=='__main__':
    original_connect=socket.socket.connect;original_dns=socket.getaddrinfo
    def connect(sock,address):
        if sock.family in {socket.AF_INET,socket.AF_INET6} and not loopback(address[0]):raise AssertionError('External network forbidden in CI')
        return original_connect(sock,address)
    def dns(host,*args,**kwargs):
        if not loopback(host):raise AssertionError('External DNS forbidden in CI')
        return original_dns(host,*args,**kwargs)
    socket.socket.connect=connect;socket.getaddrinfo=dns
    names=['test_web_adapter','test_web_progress','test_server_import','test_voice_authority','test_visual_sequence',
           'test_review_regressions','test_general_visual_repair','test_github_update']
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromNames(names))
    raise SystemExit(0 if result.wasSuccessful() else 1)
