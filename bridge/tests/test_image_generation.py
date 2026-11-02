import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('image_generation', ROOT / 'image_generation.py')
image_generation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_generation)

THREAD = '01a19746-c1a6-79f0-835e-6dbbcb4adecc'
OTHER_THREAD = '01a19746-c1a6-79f0-835e-6dbbcb4adecd'
FILENAME = 'exec-406d230d-89cf-4f18-8fc1-815f1742d83e.png'
SAFE_OPEN = os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd


def events(root, thread=THREAD, claimed_thread=None, filename=FILENAME):
    claimed = claimed_thread or thread
    return '\n'.join(json.dumps(e) for e in [
        {'type': 'thread.started', 'thread_id': thread},
        {'type': 'item.completed', 'item': {'type': 'agent_message',
         'text': f'File: `{root.as_posix()}/{claimed}/{filename}`'}},
        {'type': 'turn.completed'},
    ])


def png_bytes(size=(4, 4)):
    output = io.BytesIO()
    Image.new('RGB', size, 'blue').save(output, 'PNG')
    return output.getvalue()


class EventParsingTests(unittest.TestCase):
    def test_diagnostics_only_contain_counts(self):
        sample = events(Path('/auth/generated_images')) + '\nnot-json'
        result = image_generation.image_event_diagnostics(sample)
        self.assertEqual(result, {'threads': 1, 'completed': 1, 'failed': 0, 'messages': 1,
                                  'image_items': 0, 'path_mentions': 1, 'invalid': 1})
        self.assertNotIn(THREAD, str(result))
        deep = '[' * 2000 + '0' + ']' * 2000
        self.assertEqual(image_generation.image_event_diagnostics(deep)['invalid'], 1)
        self.assertIsNone(image_generation.parse_image_reference(deep, Path('/auth/generated_images')))

    def test_path_must_match_current_thread_and_filename(self):
        root = Path('/auth/generated_images')
        self.assertEqual(image_generation.parse_image_reference(events(root), root), (THREAD, FILENAME))
        self.assertIsNone(image_generation.parse_image_reference(events(root, claimed_thread=OTHER_THREAD), root))
        self.assertIsNone(image_generation.parse_image_reference(events(root, filename='../secret.png'), root))

    def test_missing_or_ambiguous_evidence_fails_closed(self):
        root = Path('/auth/generated_images')
        valid = events(root)
        self.assertIsNone(image_generation.parse_image_reference(valid.replace('thread.started', 'thread.missing'), root))
        self.assertIsNone(image_generation.parse_image_reference(valid + '\nnot-json', root))
        self.assertIsNone(image_generation.parse_image_reference(valid + '\n' + valid, root))
        self.assertIsNone(image_generation.parse_image_reference(valid.replace('turn.completed', 'turn.failed'), root))
        second = 'exec-406d230d-89cf-4f18-8fc1-815f1742d83f.png'
        ambiguous = valid.replace('File:', f'File: `{root.as_posix()}/{THREAD}/{second}` and')
        self.assertIsNone(image_generation.parse_image_reference(ambiguous, root))
        self.assertIsNone(image_generation.parse_image_reference(valid.replace('.png`', '.png/secret`'), root))

    def test_invalid_cli_output_and_unsafe_platform_fail_closed(self):
        self.assertIsNone(image_generation.read_generated_image('{}', Path('/auth/generated_images')))
        with patch.object(image_generation, '_safe_open_supported', return_value=False):
            self.assertIsNone(image_generation.read_generated_image(events(Path('/auth/generated_images')), Path('/auth/generated_images')))


@unittest.skipUnless(SAFE_OPEN, 'atomic no-follow directory traversal unavailable')
class ImageReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'generated_images'
        (self.root / THREAD).mkdir(parents=True)
        self.path = self.root / THREAD / FILENAME

    def test_valid_image_is_returned_exactly(self):
        data = png_bytes()
        self.path.write_bytes(data)
        self.assertEqual(image_generation.read_generated_image(events(self.root), self.root), data)

    def test_completed_thread_with_no_path_can_return_only_unique_generated_file(self):
        data = png_bytes()
        self.path.write_bytes(data)
        no_path = events(self.root).replace(f'File: `{self.root.as_posix()}/{THREAD}/{FILENAME}`', 'Image generated')
        self.assertEqual(image_generation.read_generated_image(no_path, self.root), data)
        second = self.path.parent / 'exec-406d230d-89cf-4f18-8fc1-815f1742d83f.png'
        second.write_bytes(data)
        self.assertIsNone(image_generation.read_generated_image(no_path, self.root))
        self.assertIsNone(image_generation.read_generated_image(no_path.replace('turn.completed', 'turn.failed'), self.root))

    def test_no_path_fallback_rejects_symlink_directory_and_files(self):
        target = Path(self.temp.name) / 'external.png'
        target.write_bytes(png_bytes())
        self.path.symlink_to(target)
        no_path = events(self.root).replace(f'File: `{self.root.as_posix()}/{THREAD}/{FILENAME}`', 'Image generated')
        self.assertIsNone(image_generation.read_generated_image(no_path, self.root))

    def test_no_path_fallback_rejects_concurrent_added_file(self):
        self.path.write_bytes(png_bytes())
        no_path = events(self.root).replace(f'File: `{self.root.as_posix()}/{THREAD}/{FILENAME}`', 'Image generated')
        original_read = os.read
        def changing_read(fd, size):
            data = original_read(fd, size)
            if data:
                (self.path.parent / 'extra.png').write_bytes(b'extra')
            return data
        with patch.object(image_generation.os, 'read', side_effect=changing_read):
            self.assertIsNone(image_generation.read_generated_image(no_path, self.root))

    def test_no_path_fallback_rejects_replaced_directory(self):
        self.path.write_bytes(png_bytes())
        no_path = events(self.root).replace(f'File: `{self.root.as_posix()}/{THREAD}/{FILENAME}`', 'Image generated')
        original_read = os.read
        changed = False
        def changing_read(fd, size):
            nonlocal changed
            data = original_read(fd, size)
            if data and not changed:
                changed = True
                self.path.parent.rename(self.root / 'old-thread')
                (self.root / THREAD).mkdir()
            return data
        with patch.object(image_generation.os, 'read', side_effect=changing_read):
            self.assertIsNone(image_generation.read_generated_image(no_path, self.root))

    def test_symlink_file_and_thread_directory_are_rejected(self):
        target = Path(self.temp.name) / 'external.png'
        target.write_bytes(png_bytes())
        self.path.symlink_to(target)
        self.assertIsNone(image_generation.read_generated_image(events(self.root), self.root))
        self.path.unlink()
        (self.root / THREAD).rmdir()
        (self.root / THREAD).symlink_to(target.parent, target_is_directory=True)
        self.assertIsNone(image_generation.read_generated_image(events(self.root), self.root))

    def test_invalid_format_oversize_and_too_many_pixels_are_rejected(self):
        self.path.write_bytes(b'not an image')
        self.assertIsNone(image_generation.read_generated_image(events(self.root), self.root))
        self.path.write_bytes(b'x' * (image_generation.MAX_IMAGE_BYTES + 1))
        self.assertIsNone(image_generation.read_generated_image(events(self.root), self.root))
        self.path.write_bytes(png_bytes((8, 8)))
        with patch.object(image_generation, 'MAX_IMAGE_PIXELS', 32):
            self.assertIsNone(image_generation.read_generated_image(events(self.root), self.root))


if __name__ == '__main__':
    unittest.main()
