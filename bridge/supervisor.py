"""Run the local proxy relay and chat bridge in one container lifecycle."""
import os
import signal
import subprocess
import sys
import threading
import time

STOP_TIMEOUT = 5


def stop_children(children):
    for child in children:
        if child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + STOP_TIMEOUT
    for child in children:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            child.wait()


def supervise(stop_event, spawn=subprocess.Popen):
    root = os.path.dirname(os.path.abspath(__file__))
    relay_env = os.environ.copy()
    relay_env.pop("BRIDGE_TOKEN", None)
    children = []
    try:
        children.append(spawn([sys.executable, os.path.join(root, "relay.py"), "client", "--socket", "/proxy/proxy.sock"], env=relay_env))
        children.append(spawn([sys.executable, os.path.join(root, "bridge.py")]))
        while not stop_event.wait(0.2):
            if any(child.poll() is not None for child in children):
                return 1
        return 0
    except OSError:
        return 1
    finally:
        stop_children(children)


def main():
    stop_event = threading.Event()

    def stop(_signal, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return supervise(stop_event)


if __name__ == "__main__":
    raise SystemExit(main())
