import errno
import socket
import stat
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import relay


class FakeConnection:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False
        self.shutdowns = []

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        return self.chunks.pop(0)

    def sendall(self, data):
        self.sent.append(data)

    def shutdown(self, direction):
        self.shutdowns.append(direction)

    def close(self):
        self.closed = True


class RelayTests(unittest.TestCase):
    def test_forward_both_directions_and_half_close(self):
        left = FakeConnection([b"hello", b""])
        right = FakeConnection([b"world", b""])
        with patch("relay.select.select", side_effect=[([left], [], []), ([right], [], []), ([left], [], []), ([right], [], [])]):
            relay.forward(left, right)
        self.assertEqual(right.sent, [b"hello"])
        self.assertEqual(left.sent, [b"world"])
        self.assertEqual(left.shutdowns, [socket.SHUT_WR])
        self.assertTrue(left.closed and right.closed)

    def test_idle_timeout_closes_connections(self):
        left, right = FakeConnection([]), FakeConnection([])
        with patch("relay.select.select", return_value=([], [], [])):
            relay.forward(left, right)
        self.assertTrue(left.closed and right.closed)

    def test_allowed_connect_targets_preserve_request(self):
        for host in relay.ALLOWED_HOSTS:
            request = f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode()
            with self.subTest(host=host):
                self.assertEqual(relay.read_connect_request(FakeConnection([request])), request)

    def test_disallowed_connect_requests(self):
        cases = [b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n", b"CONNECT chatgpt.com:80 HTTP/1.1\r\n\r\n", b"CONNECT chatgpt.com.evil.test:443 HTTP/1.1\r\n\r\n", b"CONNECT CHATGPT.COM:443 HTTP/1.1\r\n\r\n", b"GET https://chatgpt.com/ HTTP/1.1\r\n\r\n", b"CONNECT chatgpt.com:443 HTTP/1.1\r\nProxy-Authorization: token\r\n\r\n", b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n", b"CONNECT chatgpt.com:443 HTTP/1.1\r\nX-Forwarded-Host: other\r\n\r\n", b"a" * (relay.MAX_HEADER + 1)]
        for request in cases:
            with self.subTest(request=request[:60]), self.assertRaises(ValueError):
                relay.read_connect_request(FakeConnection([request]))

    def test_stale_socket_only_if_owned_and_unresponsive(self):
        info = SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=123, st_dev=1, st_ino=2)
        probe = MagicMock()
        probe.connect.side_effect = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        with patch("relay.os.lstat", side_effect=[info, info]), patch("relay.os.geteuid", return_value=123, create=True), patch("relay.socket.AF_UNIX", 1, create=True), patch("relay.socket.socket", return_value=probe), patch("relay.os.unlink") as unlink:
            relay.remove_stale_socket("/tmp/x")
            unlink.assert_called_once_with("/tmp/x")

    def test_never_remove_regular_file_or_live_socket(self):
        regular = SimpleNamespace(st_mode=stat.S_IFREG, st_uid=123, st_dev=1, st_ino=2)
        owned = SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=123, st_dev=1, st_ino=2)
        for info in (regular, owned):
            with self.subTest(mode=info.st_mode), patch("relay.os.lstat", return_value=info), patch("relay.os.geteuid", return_value=123, create=True), patch("relay.socket.AF_UNIX", 1, create=True), patch("relay.socket.socket"), patch("relay.os.unlink") as unlink, self.assertRaises(RuntimeError):
                relay.remove_stale_socket("/tmp/x")
            unlink.assert_not_called()

    def test_thread_start_failure_releases_slot_and_closes_socket(self):
        incoming = MagicMock()
        listener = MagicMock()
        listener.accept.side_effect = [(incoming, ("127.0.0.1", 12345)), KeyboardInterrupt]

        with patch("relay.socket.socket", return_value=listener), \
                patch("relay.threading.Thread.start", side_effect=RuntimeError("can't start new thread")):
            try:
                relay.serve("client", "/dummy/path.sock")
            except KeyboardInterrupt:
                pass

        incoming.close.assert_called_once()
        listener.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
