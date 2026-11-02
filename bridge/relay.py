"""Fixed-destination relay between a local Unix socket and the host proxy."""
import argparse
import errno
import os
import re
import signal
import select
import socket
import stat
import threading
import time

PROXY = ("127.0.0.1", 7890)
MAX_CONNECTIONS = 8
CONNECT_TIMEOUT = 5
IDLE_TIMEOUT = 60
TOTAL_TIMEOUT = 300
MAX_HEADER = 8192
ALLOWED_HOSTS = {"auth.openai.com", "chatgpt.com", "api.openai.com"}
ALLOWED_HEADERS = {"host", "connection", "proxy-connection", "user-agent"}


def remove_stale_socket(path):
    """Remove only an owned, unresponsive socket whose inode has not changed."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.geteuid():
        raise RuntimeError("Socket path is occupied by an unowned or non-socket file")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(CONNECT_TIMEOUT)
    try:
        probe.connect(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return
        if exc.errno != errno.ECONNREFUSED:
            raise RuntimeError("Could not establish whether socket is in use") from exc
    else:
        raise RuntimeError("Socket is already in use")
    finally:
        probe.close()
    try:
        after = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(after.st_mode) or after.st_uid != before.st_uid or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise RuntimeError("Socket path changed during verification")
    os.unlink(path)


def read_connect_request(conn):
    """Validate a bounded HTTP CONNECT header; preserve all bytes verbatim."""
    conn.settimeout(CONNECT_TIMEOUT)
    chunks = bytearray()
    deadline = time.monotonic() + CONNECT_TIMEOUT
    while b"\r\n\r\n" not in chunks:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("CONNECT header timed out")
        conn.settimeout(remaining)
        chunk = conn.recv(MAX_HEADER + 1 - len(chunks))
        if not chunk:
            raise ValueError("Incomplete CONNECT header")
        chunks.extend(chunk)
        header_end = chunks.find(b"\r\n\r\n")
        if (header_end < 0 and len(chunks) > MAX_HEADER) or header_end + 4 > MAX_HEADER:
            raise ValueError("CONNECT header too large")
    try:
        lines = bytes(chunks[:header_end]).decode("ascii").split("\r\n")
    except UnicodeError as exc:
        raise ValueError("Non-ASCII CONNECT header") from exc
    match = re.fullmatch(r"CONNECT ([a-z0-9.-]+):443 HTTP/1\.[01]", lines[0])
    if not match or match.group(1) not in ALLOWED_HOSTS:
        raise ValueError("CONNECT target is not allowed")
    for line in lines[1:]:
        if ":" not in line:
            raise ValueError("Invalid CONNECT header")
        name, value = line.split(":", 1)
        if name.lower() not in ALLOWED_HEADERS or not all(32 <= ord(c) <= 126 for c in value):
            raise ValueError("Invalid CONNECT header")
        if name.lower() == "host" and value.strip() != match.group(1) + ":443":
            raise ValueError("Host does not match CONNECT target")
    return bytes(chunks)


def forward(left, right, idle_timeout=IDLE_TIMEOUT, total_timeout=TOTAL_TIMEOUT):
    """Forward bytes in both directions, preserving TCP half-close semantics."""
    pair = {left: right, right: left}
    readable = set(pair)
    started = last_activity = time.monotonic()
    for conn in pair:
        conn.settimeout(CONNECT_TIMEOUT)
    try:
        while readable:
            now = time.monotonic()
            remaining = min(idle_timeout - (now - last_activity), total_timeout - (now - started))
            if remaining <= 0:
                break
            ready, _, _ = select.select(list(readable), [], [], remaining)
            if not ready:
                break
            for source in ready:
                data = source.recv(65536)
                target = pair[source]
                if not data:
                    readable.remove(source)
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                else:
                    target.sendall(data)
                    last_activity = time.monotonic()
    except (OSError, ValueError):
        pass
    finally:
        left.close()
        right.close()


def serve(mode, socket_path):
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    if mode == "host":
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        remove_stale_socket(socket_path)
        listener.bind(socket_path)
        os.chmod(socket_path, 0o600)
        created = os.lstat(socket_path)
    else:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(PROXY)
        created = None
    listener.listen(MAX_CONNECTIONS)

    def handle(incoming):
        outgoing = None
        try:
            if mode == "host":
                request = read_connect_request(incoming)
                outgoing = socket.create_connection(PROXY, timeout=CONNECT_TIMEOUT)
                outgoing.sendall(request)
            else:
                outgoing = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                outgoing.settimeout(CONNECT_TIMEOUT)
                outgoing.connect(socket_path)
            forward(incoming, outgoing)
        except (OSError, ValueError):
            incoming.close()
            if outgoing is not None:
                outgoing.close()
        finally:
            slots.release()

    try:
        while True:
            incoming, _ = listener.accept()
            if not slots.acquire(blocking=False):
                incoming.close()
                continue
            try:
                threading.Thread(target=handle, args=(incoming,), daemon=True).start()
            except Exception:
                slots.release()
                try:
                    incoming.close()
                except OSError:
                    pass
    finally:
        listener.close()
        if created is not None:
            try:
                current = os.lstat(socket_path)
                if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(socket_path)
            except FileNotFoundError:
                pass


def terminate_on_signal(signum, frame):
    raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("host", "client"))
    parser.add_argument("--socket", required=True, dest="socket_path")
    args = parser.parse_args()
    if not os.path.isabs(args.socket_path):
        parser.error("--socket must be an absolute path")
    signal.signal(signal.SIGTERM, terminate_on_signal)
    serve(args.mode, args.socket_path)


if __name__ == "__main__":
    main()
