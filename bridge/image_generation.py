"""Safely identify and read an image produced by one isolated Codex run.

No process is started here. The caller must use an isolated Codex invocation
with shell disabled and must not treat assistant text alone as file authority.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import struct
import warnings
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

MAX_EVENTS_CHARS = 1_048_576
MAX_IMAGE_BYTES = 5_000_000
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_EDGE = 4096
UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'


def image_event_diagnostics(events: str) -> dict[str, int]:
    """Return only aggregate event counts; never include message text or paths."""
    counts = {'threads': 0, 'completed': 0, 'failed': 0, 'messages': 0,
              'image_items': 0, 'path_mentions': 0, 'invalid': 0}
    if not isinstance(events, str) or len(events) > MAX_EVENTS_CHARS:
        counts['invalid'] = 1
        return counts
    for line in events.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError('invalid event')
            kind = event.get('type')
            if kind == 'thread.started': counts['threads'] += 1
            if kind == 'turn.completed': counts['completed'] += 1
            if kind == 'turn.failed': counts['failed'] += 1
            item = event.get('item')
            if kind == 'item.completed' and isinstance(item, dict):
                if item.get('type') == 'image_generation': counts['image_items'] += 1
                if item.get('type') == 'agent_message':
                    counts['messages'] += 1
                    text = item.get('text')
                    if isinstance(text, str): counts['path_mentions'] += text.count('/auth/generated_images/')
        except (ValueError, TypeError, RecursionError):
            counts['invalid'] += 1
    return counts


def parse_image_reference(events: str, generated_root: Path) -> tuple[str, str] | None:
    """Return the single image name bound to this completed CLI thread, or None."""
    if not isinstance(events, str) or len(events) > MAX_EVENTS_CHARS:
        return None
    root = generated_root.as_posix()
    if not root.startswith('/') or any(part in ('.', '..') for part in root.split('/')):
        return None
    thread_ids: list[str] = []
    messages: list[str] = []
    completed = 0
    try:
        for line in events.splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                return None
            kind = event.get('type')
            if kind == 'thread.started':
                thread_ids.append(event.get('thread_id'))
            elif kind == 'turn.completed':
                completed += 1
            elif kind == 'turn.failed':
                return None
            elif kind == 'item.completed':
                item = event.get('item')
                if isinstance(item, dict) and item.get('type') == 'agent_message':
                    text = item.get('text')
                    if not isinstance(text, str):
                        return None
                    messages.append(text)
    except (ValueError, TypeError, RecursionError):
        return None
    if len(thread_ids) != 1 or not isinstance(thread_ids[0], str) or not re.fullmatch(UUID, thread_ids[0]) or completed != 1:
        return None
    thread = thread_ids[0]
    pattern = re.compile(
        r'(?<![\w./-])' + re.escape(root.rstrip('/')) + '/' + re.escape(thread)
        + r'/(exec-' + UUID + r'\.png)(?![\w./-])'
    )
    found = [match.group(1) for message in messages for match in pattern.finditer(message)]
    if len(found) != 1:
        return None
    return thread, found[0]


def _safe_open_supported() -> bool:
    return os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY') and os.open in os.supports_dir_fd


def _read_no_symlinks(root: Path, thread: str, filename: str) -> bytes:
    """Traverse from / with no symlinks using directory descriptors."""
    parts = PurePosixPath(root.as_posix()).parts
    if not parts or parts[0] != '/' or any(part in ('.', '..') for part in parts[1:]):
        raise OSError('invalid root')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    descriptors: list[int] = []
    try:
        parent = os.open('/', flags)
        descriptors.append(parent)
        for part in (*parts[1:], thread):
            parent = os.open(part, flags, dir_fd=parent)
            descriptors.append(parent)
        image_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
        descriptors.append(image_fd)
        info = os.fstat(image_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > MAX_IMAGE_BYTES:
            raise OSError('invalid image file')
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(image_fd, min(65536, MAX_IMAGE_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise OSError('image too large')
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _unique_completed_thread_image(events: str, root: Path) -> bytes | None:
    """Read a sole output through pinned, checked directory and file handles."""
    if not isinstance(events, str) or len(events) > MAX_EVENTS_CHARS:
        return None
    threads, completed, messages = [], 0, 0
    try:
        for line in events.splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                return None
            kind = event.get('type')
            if kind == 'thread.started':
                threads.append(event.get('thread_id'))
            elif kind == 'turn.completed':
                completed += 1
            elif kind == 'turn.failed':
                return None
            elif kind == 'item.completed':
                item = event.get('item')
                if isinstance(item, dict) and item.get('type') == 'agent_message':
                    messages += 1
                    message = item.get('text')
                    if not isinstance(message, str) or root.as_posix().rstrip('/') + '/' in message:
                        return None
        if len(threads) != 1 or not isinstance(threads[0], str) or not re.fullmatch(UUID, threads[0]) or completed != 1 or messages == 0:
            return None
        parts = PurePosixPath(root.as_posix()).parts
        if not parts or parts[0] != '/' or any(part in ('.', '..') for part in parts[1:]):
            return None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        descriptors = []
        try:
            parent = os.open('/', flags)
            descriptors.append(parent)
            for part in parts[1:]:
                parent = os.open(part, flags, dir_fd=parent)
                descriptors.append(parent)
            root_fd = parent
            root_info = os.fstat(root_fd)
            if root_info.st_uid != os.geteuid() or stat.S_IMODE(root_info.st_mode) & 0o022:
                return None
            parent = os.open(threads[0], flags, dir_fd=root_fd)
            descriptors.append(parent)
            dir_info = os.fstat(parent)
            if dir_info.st_uid != os.geteuid() or stat.S_IMODE(dir_info.st_mode) & 0o022:
                return None
            names = os.listdir(parent)
            if len(names) != 1 or not re.fullmatch(r'exec-' + UUID + r'\.png', names[0]):
                return None
            fd = os.open(names[0], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
            descriptors.append(fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o022 or not 0 < info.st_size <= MAX_IMAGE_BYTES):
                return None
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, min(65536, MAX_IMAGE_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    return None
                chunks.append(chunk)
            after = os.fstat(fd)
            current_file = os.stat(names[0], dir_fd=parent, follow_symlinks=False)
            current_dir = os.stat(threads[0], dir_fd=root_fd, follow_symlinks=False)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
            if (identity(info) != identity(after) or identity(info) != identity(current_file)
                    or (dir_info.st_dev, dir_info.st_ino) != (current_dir.st_dev, current_dir.st_ino)
                    or os.listdir(parent) != names):
                return None
            return b''.join(chunks)
        finally:
            for fd in reversed(descriptors):
                os.close(fd)
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def read_generated_image(events: str, generated_root: Path = Path('/auth/generated_images')) -> bytes | None:
    """Read only a bounded, verified PNG belonging to this completed run."""
    if not _safe_open_supported():
        return None
    try:
        reference = parse_image_reference(events, generated_root)
        data = _read_no_symlinks(generated_root, *reference) if reference is not None else _unique_completed_thread_image(events, generated_root)
        if data is None:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if (image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1
                        or width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_EDGE
                        or width * height > MAX_IMAGE_PIXELS):
                    return None
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        return data
    except (OSError, ValueError, SyntaxError, EOFError, OverflowError, struct.error,
            UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        return None
