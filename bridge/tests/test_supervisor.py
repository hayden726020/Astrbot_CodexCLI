import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import supervisor


class Child:
    def __init__(self, code=None, resist=False):
        self.code = code
        self.resist = resist
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        if not self.resist:
            self.code = -15

    def kill(self):
        self.killed = True
        self.code = -9

    def wait(self, timeout=None):
        self.waited = True
        if self.code is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.code


class SupervisorTests(unittest.TestCase):
    def test_child_exit_terminates_other_and_returns_failure(self):
        relay, bridge = Child(code=0), Child()
        calls = []
        def spawn(argv, **kwargs):
            calls.append((argv, kwargs))
            return (relay, bridge)[len(calls) - 1]
        with patch.dict(os.environ, {"BRIDGE_TOKEN": "secret"}):
            status = supervisor.supervise(threading.Event(), spawn)
        self.assertEqual(status, 1)
        self.assertTrue(bridge.terminated and bridge.waited and relay.waited)
        self.assertEqual(calls[0][0][0], sys.executable)
        self.assertEqual(calls[0][0][-2:], ["--socket", "/proxy/proxy.sock"])
        self.assertNotIn("BRIDGE_TOKEN", calls[0][1]["env"])
        self.assertTrue(calls[1][0][-1].endswith("bridge.py"))

    def test_stop_signal_cleans_both_and_kills_resistant_child(self):
        first, second = Child(resist=True), Child()
        children = iter((first, second))
        event = threading.Event()
        event.set()
        self.assertEqual(supervisor.supervise(event, lambda *a, **kw: next(children)), 0)
        self.assertTrue(first.terminated and first.killed and first.waited)
        self.assertTrue(second.terminated and second.waited)

    def test_start_failure_cleans_existing_child(self):
        child = Child()
        count = 0
        def spawn(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("failure")
            return child
        self.assertEqual(supervisor.supervise(threading.Event(), spawn), 1)
        self.assertTrue(child.terminated and child.waited)

    def test_main_signal_sets_stop_event(self):
        handlers = {}
        def register(sig, handler):
            handlers[sig] = handler
        def exercise(event):
            handlers[supervisor.signal.SIGTERM](supervisor.signal.SIGTERM, None)
            self.assertTrue(event.is_set())
            return 0
        with patch("supervisor.signal.signal", side_effect=register), patch("supervisor.supervise", side_effect=exercise):
            self.assertEqual(supervisor.main(), 0)


if __name__ == "__main__":
    unittest.main()
