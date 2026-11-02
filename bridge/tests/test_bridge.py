import base64
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('codex_bridge', ROOT / 'bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def image_url():
    output = io.BytesIO()
    Image.new('RGB', (8, 8), (0, 255, 0)).save(output, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')


def initialize_budget(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript('''
        CREATE TABLE image_budget_meta (id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, version INTEGER NOT NULL);
        CREATE TABLE image_budget_state (id INTEGER PRIMARY KEY, day TEXT NOT NULL, used INTEGER NOT NULL);
        INSERT INTO image_budget_meta VALUES (1, 'ext-image-budget', 1);
        INSERT INTO image_budget_state VALUES (1, '2000-01-01', 0);
        ''')
        conn.commit()
    finally:
        conn.close()


@contextmanager
def simulated_budget_open(path):
    """Exercise SQLite transactions on Windows; POSIX descriptor safety is separate."""
    conn = sqlite3.connect(Path(path).as_uri() + '?mode=rw', uri=True, timeout=10, isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


class BridgeTests(unittest.TestCase):
    def test_image_http_is_opt_in_independently_authenticated_and_bounded(self):
        def post(body, auth='Bearer test-image-token-0123456789abcdef'):
            raw = json.dumps(body).encode()
            handler = bridge.Handler.__new__(bridge.Handler)
            handler.server = SimpleNamespace(token='test-chat-token-0123456789abcdef0')
            handler.path = '/v1/ext/image'
            handler.headers = {'Authorization': auth, 'Content-Length': str(len(raw))}
            handler.rfile = io.BytesIO(raw)
            result = []
            handler.reply = lambda status, payload: result.append((status, payload))
            handler.reply_image = lambda payload: result.append((200, payload))
            handler.do_POST()
            return result[0]
        token = 'test-image-token-0123456789abcdef'
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(post({'prompt': 'bird'})[0], 404)
            with patch.dict(os.environ, {'BRIDGE_IMAGE_ENABLED': 'true'}):
                self.assertEqual(post({'prompt': 'bird'})[0], 404)
                with patch.dict(os.environ, {'BRIDGE_IMAGE_TOKEN': 'short'}):
                    self.assertEqual(post({'prompt': 'bird'})[0], 404)
                with patch.dict(os.environ, {'BRIDGE_IMAGE_TOKEN': 'test-chat-token-0123456789abcdef0'}):
                    self.assertEqual(post({'prompt': 'bird'})[0], 404)
                with patch.dict(os.environ, {'BRIDGE_IMAGE_TOKEN': token}):
                    self.assertEqual(post({'prompt': 'bird'}, 'Bearer test-chat-token-0123456789abcdef0')[0], 401)
                    self.assertEqual(post({'prompt': ''})[0], 400)
                    self.assertEqual(post({'prompt': 'a' * 501})[0], 400)
                    self.assertEqual(post({'prompt': 'bird', 'path': '/etc/passwd'})[0], 400)
                    with patch.object(bridge, 'reserve_image_budget', return_value=True) as reserve, patch.object(bridge, 'call_image', return_value=b'png') as call:
                        self.assertEqual(post({'prompt': 'bird'}), (200, b'png'))
                        reserve.assert_called_once()
                        call.assert_called_once_with('bird')
                    for reservation, status in [(False, 429), (None, 503)]:
                        with patch.object(bridge, 'reserve_image_budget', return_value=reservation), patch.object(bridge, 'call_image') as call:
                            self.assertEqual(post({'prompt': 'bird'})[0], status)
                            call.assert_not_called()
                with patch.dict(os.environ, {'BRIDGE_IMAGE_TOKEN': token, 'BRIDGE_WEB_SEARCH_TOKEN': token}):
                    self.assertEqual(post({'prompt': 'bird'})[0], 404)

    def test_image_cli_keeps_restrictions_and_fails_closed_without_valid_file(self):
        captured = {}
        def fake_run(args, prompt, **kwargs):
            captured.update(args=args, prompt=prompt, **kwargs)
            return 'fake events'
        with patch.object(bridge, 'run_process', fake_run), patch.object(bridge, 'read_generated_image', return_value=b'picture'):
            self.assertEqual(bridge.call_image('bird'), b'picture')
        self.assertIn('read-only', captured['args'])
        self.assertIn('--ignore-user-config', captured['args'])
        self.assertIn('shell_tool', captured['args'])
        self.assertIn('plugins', captured['args'])
        self.assertIn('multi_agent', captured['args'])
        self.assertIn('image_generation', captured['args'])
        self.assertEqual(captured['timeout'], bridge.IMAGE_TIMEOUT)
        self.assertIn('bird', captured['prompt'])
        with patch.object(bridge, 'run_process', return_value='fake events'), patch.object(bridge, 'read_generated_image', return_value=None):
            with self.assertRaises(bridge.UpstreamFailure):
                bridge.call_image('bird')
        with patch.object(bridge, 'run_process', side_effect=OSError('failed')):
            with self.assertRaises(bridge.UpstreamFailure):
                bridge.call_image('bird')

    def test_image_budget_is_persistent_atomic_and_global(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            db = str(Path(directory).resolve() / 'budget.sqlite3')
            initialize_budget(db)
            with patch.dict(os.environ, {'BRIDGE_IMAGE_BUDGET_DB': db}, clear=True), patch.object(bridge, 'open_budget_database', simulated_budget_open):
                with ThreadPoolExecutor(max_workers=12) as pool:
                    outcomes = list(pool.map(lambda _: bridge.reserve_image_budget(), range(30)))
                self.assertEqual(outcomes.count(True), 20)
                self.assertEqual(outcomes.count(False), 10)
                self.assertFalse(bridge.reserve_image_budget())
                self.assertFalse(bridge.reserve_image_budget())

    def test_image_budget_fails_closed_without_explicit_usable_persistent_path(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(bridge.reserve_image_budget())
            with patch.dict(os.environ, {'BRIDGE_IMAGE_BUDGET_DB': 'relative.sqlite3'}):
                self.assertIsNone(bridge.reserve_image_budget())

    def test_missing_uninitialized_or_invalid_budget_db_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            db = str(Path(directory).resolve() / 'budget.sqlite3')
            with patch.dict(os.environ, {'BRIDGE_IMAGE_BUDGET_DB': db}, clear=True), patch.object(bridge, 'open_budget_database', simulated_budget_open):
                self.assertIsNone(bridge.reserve_image_budget())
                self.assertFalse(Path(db).exists())
                sqlite3.connect(db).close()
                self.assertIsNone(bridge.reserve_image_budget())
                initialize_budget(db)
                conn = sqlite3.connect(db)
                conn.execute('DELETE FROM image_budget_state')
                conn.commit()
                conn.close()
                self.assertIsNone(bridge.reserve_image_budget())
                conn = sqlite3.connect(db)
                conn.execute("INSERT INTO image_budget_state VALUES (1, '2000-01-01', 0)")
                conn.execute('UPDATE image_budget_meta SET version = 99')
                conn.commit()
                conn.close()
                self.assertIsNone(bridge.reserve_image_budget())
            with patch.dict(os.environ, {'BRIDGE_IMAGE_BUDGET_DB': str(ROOT / 'tests' / 'missing-folder' / 'db.sqlite3')}):
                self.assertIsNone(bridge.reserve_image_budget())

    def test_reserved_image_budget_is_not_refunded(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            db = str(Path(directory).resolve() / 'budget.sqlite3')
            initialize_budget(db)
            with patch.dict(os.environ, {'BRIDGE_IMAGE_BUDGET_DB': db}, clear=True), patch.object(bridge, 'open_budget_database', simulated_budget_open):
                self.assertTrue(bridge.reserve_image_budget())
                self.assertEqual(sum(bridge.reserve_image_budget() is True for _ in range(20)), 19)

    def test_search_requires_real_search_results_and_uses_restricted_cli(self):
        captured = {}
        events = [
            {'type': 'item.completed', 'item': {'type': 'web_search', 'action': {'type': 'search'}, 'results': [
                {'type': 'text_result', 'title': 'Example', 'url': 'https://example.org/page', 'snippet': '真实来源摘要'},
                {'type': 'text_result', 'title': 'Bad', 'url': 'file:///etc/passwd'}]}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '搜索结果摘要'}},
        ]
        def fake_run(args, prompt, **kwargs):
            captured['args'], captured['prompt'] = args, prompt
            return '\n'.join(map(json.dumps, events))
        with patch.object(bridge, 'run_process', fake_run):
            result = bridge.call_search('测试关键词')
        self.assertEqual(result['text'], '真实来源摘要')
        self.assertEqual(result['sources'], [{'title': 'Example', 'url': 'https://example.org/page'}])
        self.assertIn('--search', captured['args'])
        self.assertIn('code_mode_host_grpc', captured['args'])
        self.assertIn('read-only', captured['args'])
        self.assertIn('shell_tool', captured['args'])
        self.assertIn('image_generation', captured['args'])
        self.assertEqual(captured['args'][captured['args'].index('image_generation') - 1], '--disable')
        self.assertIn('测试关键词', captured['prompt'])
        with patch.object(bridge, 'run_process', return_value=json.dumps(events[-1])):
            with self.assertRaises(bridge.UpstreamFailure):
                bridge.call_search('测试关键词')
        no_sources = [dict(events[0], item=dict(events[0]['item'], results=[])), events[-1]]
        with patch.object(bridge, 'run_process', return_value='\n'.join(map(json.dumps, no_sources))):
            with self.assertRaises(bridge.UpstreamFailure):
                bridge.call_search('测试关键词')
        with patch.object(bridge, 'run_process', return_value='not json'):
            with self.assertRaises(bridge.UpstreamFailure):
                bridge.call_search('测试关键词')
        with patch.object(bridge, 'run_process', side_effect=bridge.UpstreamFailure('timeout')):
            with self.assertRaises(bridge.UpstreamFailure):
                bridge.call_search('测试关键词')

    def test_search_http_is_authenticated_opt_in_and_validated(self):
        def post(body, auth='Bearer test-search-token-0123456789abcdef'):
            raw = json.dumps(body).encode()
            handler = bridge.Handler.__new__(bridge.Handler)
            handler.server = SimpleNamespace(token='test-token')
            handler.path = '/v1/ext/search'
            handler.headers = {'Authorization': auth, 'Content-Length': str(len(raw))}
            handler.rfile = io.BytesIO(raw)
            result = []
            handler.reply = lambda status, payload: result.append((status, payload))
            handler.do_POST()
            return result[0]
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(post({'query': '天气'}, 'bad')[0], 404)
            self.assertEqual(post({'query': '天气'})[0], 404)
            with patch.dict(os.environ, {'BRIDGE_WEB_SEARCH_ENABLED': 'true'}):
                self.assertEqual(post({'query': '天气'})[0], 404)
                with patch.dict(os.environ, {'BRIDGE_WEB_SEARCH_TOKEN': 'test-search-token-0123456789abcdef'}):
                    self.assertEqual(post({'query': '天气'}, 'Bearer test-token')[0], 401)
                    self.assertEqual(post({'query': ''})[0], 400)
                    self.assertEqual(post({'query': 'a' * 501})[0], 400)
                    self.assertEqual(post({'query': 'a\nb'})[0], 400)
                    self.assertEqual(post({'query': 1})[0], 400)
                    self.assertEqual(post({'query': 'ok', 'extra': True})[0], 400)
                    expected = {'text': '结果', 'sources': [{'title': 'x', 'url': 'https://example.org/'}]}
                    with patch.object(bridge, 'allow_search', return_value=True):
                        with patch.object(bridge, 'call_search', return_value=expected):
                            self.assertEqual(post({'query': '天气'}), (200, expected))
                        with patch.object(bridge, 'call_search', side_effect=bridge.UpstreamFailure('failed')):
                            self.assertEqual(post({'query': '天气'})[0], 502)
                    with patch.object(bridge, 'allow_search', return_value=False), patch.object(bridge, 'call_search') as call:
                        self.assertEqual(post({'query': '天气'})[0], 429)
                        call.assert_not_called()

    def test_search_hourly_limit_is_reserved_and_expires(self):
        with patch.object(bridge, 'SEARCH_HITS', bridge.deque()), patch.dict(os.environ, {'BRIDGE_WEB_SEARCH_RATE_PER_HOUR': '2'}):
            with patch.object(bridge.time, 'monotonic', return_value=100.0):
                self.assertTrue(bridge.allow_search())
                self.assertTrue(bridge.allow_search())
                self.assertFalse(bridge.allow_search())
            with patch.object(bridge.time, 'monotonic', return_value=3701.0):
                self.assertTrue(bridge.allow_search())
        with patch.dict(os.environ, {'BRIDGE_WEB_SEARCH_RATE_PER_HOUR': '0'}):
            self.assertFalse(bridge.allow_search())
        with patch.dict(os.environ, {'BRIDGE_WEB_SEARCH_RATE_PER_HOUR': 'bad'}):
            self.assertFalse(bridge.allow_search())

    def test_search_rejects_equal_or_short_token(self):
        self.assertFalse(bridge.search_token_valid('a' * 32, 'a' * 32))
        self.assertFalse(bridge.search_token_valid('short', 'other'))
        self.assertTrue(bridge.search_token_valid('b' * 32, 'a' * 32))

    def test_accepts_bounded_user_images_and_rejects_remote_or_tool_inputs(self):
        request = bridge.validate({
            'model': bridge.MODEL,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': '看图'},
                {'type': 'image_url', 'image_url': {'url': image_url()}},
            ]}],
        })
        self.assertEqual(len(request.images), 1)
        self.assertNotIn('base64', json.dumps(request.messages))
        with self.assertRaises(bridge.BadRequest):
            bridge.validate({'messages': [{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}},
            ]}]})
        with self.assertRaises(bridge.BadRequest):
            bridge.validate({'messages': [{'role': 'assistant', 'content': [
                {'type': 'image_url', 'image_url': {'url': image_url()}},
            ]}]})
        with self.assertRaises(bridge.BadRequest):
            bridge.validate({'tools': [{'type': 'function'}], 'messages': [{'role': 'user', 'content': 'x'}]})

    def test_more_images_than_the_limit_degrade_instead_of_failing(self):
        request = bridge.validate({'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '收图'},
        ] + [{'type': 'image_url', 'image_url': {'url': image_url()}} for _ in range(5)]}]})
        self.assertEqual(len(request.images), bridge.MAX_IMAGES)
        self.assertEqual(request.dropped_images, 5 - bridge.MAX_IMAGES)
        content = json.dumps(request.messages, ensure_ascii=False)
        self.assertIn(f"dropped: over the {bridge.MAX_IMAGES}-image limit", content)
        self.assertNotIn('base64', content)

    def test_multiple_images_are_private_temp_files_and_enable_only_view_image(self):
        request = bridge.validate({'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '比较这两张'},
            {'type': 'image_url', 'image_url': {'url': image_url()}},
            {'type': 'image_url', 'image_url': {'url': image_url()}},
        ]}]})
        observed = {}

        def fake_run(args, prompt, **kwargs):
            observed['args'] = args
            observed['prompt'] = prompt
            observed['paths'] = [args[index + 1] for index, item in enumerate(args) if item == '--image']
            for path in observed['paths']:
                self.assertTrue(Path(path).is_file())
            return json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '两张绿色图片'}})

        with patch.object(bridge, 'run_process', fake_run), \
                patch.dict(os.environ, {'BRIDGE_TMPDIR': str(ROOT / 'tests')}):
            self.assertEqual(bridge.call_codex(request), '两张绿色图片')
        self.assertIn('view_image', observed['args'])
        self.assertEqual(len(observed['paths']), 2)
        self.assertEqual(observed['args'][-2:], ['--', '-'])
        self.assertIn('untrusted', observed['prompt'].lower())
        for path in observed['paths']:
            self.assertFalse(Path(path).exists())

    def test_call_codex_joins_multi_agent_messages_and_uses_double_dash(self):
        request = bridge.validate({'messages': [{'role': 'user', 'content': 'hello'}]})
        observed = {}

        def fake_run(args, prompt, **kwargs):
            observed['args'] = args
            return "\n".join([
                json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '第一段'}}),
                json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '第二段'}}),
            ])

        with patch.object(bridge, 'run_process', fake_run):
            result = bridge.call_codex(request)
        self.assertEqual(result, '第一段\n第二段')
        self.assertEqual(observed['args'][-2:], ['--', '-'])

    def test_historical_images_are_context_for_latest_user_request(self):
        messages = [{'role': 'user', 'content': '[image 1]'},
                    {'role': 'user', 'content': '画一只坐在窗边的猫'}]
        prompt = bridge.make_prompt(messages, [Path('/tmp/example.jpg')])
        self.assertIn('Respond to the last user message', prompt)
        self.assertIn('earlier images as context only when relevant', prompt)
        self.assertNotIn('Inspect the attached images in order before answering', prompt)

    def test_text_requests_keep_view_image_disabled(self):
        request = bridge.validate({'messages': [{'role': 'user', 'content': '你好'}]})
        observed = {}

        def fake_run(args, prompt, **kwargs):
            observed['args'] = args
            return json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '你好'}})

        with patch.object(bridge, 'run_process', fake_run):
            bridge.call_codex(request)
        self.assertIn('view_image', observed['args'])
        self.assertEqual(observed['args'][observed['args'].index('image_generation') - 1], '--disable')
        self.assertNotIn('--search', observed['args'])


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd,
                     'POSIX directory descriptor protection unavailable')
class BudgetPathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).absolute()
        if any(self.directory.is_relative_to(Path(p)) for p in ('/tmp', '/var/tmp', '/run', '/dev/shm')):
            self.skipTest('test workspace is on a disallowed temporary path')
        self.directory.chmod(0o700)
        self.db = self.directory / 'budget.sqlite3'
        initialize_budget(self.db)
        self.db.chmod(0o600)

    def test_real_preinitialized_file_opens_but_missing_file_is_not_created(self):
        with bridge.open_budget_database(str(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT used FROM image_budget_state').fetchone(), (0,))
        missing = self.directory / 'missing.sqlite3'
        with self.assertRaises(OSError):
            with bridge.open_budget_database(str(missing)):
                pass
        self.assertFalse(missing.exists())

    def test_symlink_unsafe_permissions_and_wrong_owner_fail_closed(self):
        link = self.directory / 'link.sqlite3'
        link.symlink_to(self.db)
        with self.assertRaises(OSError):
            with bridge.open_budget_database(str(link)):
                pass
        parent_link = self.directory / 'linked-dir'
        parent_link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(OSError):
            with bridge.open_budget_database(str(parent_link / self.db.name)):
                pass
        self.directory.chmod(0o755)
        with self.assertRaises(OSError):
            with bridge.open_budget_database(str(self.db)):
                pass
        self.directory.chmod(0o700)
        self.db.chmod(0o644)
        with self.assertRaises(OSError):
            with bridge.open_budget_database(str(self.db)):
                pass
        self.db.chmod(0o600)
        with patch.object(bridge.os, 'geteuid', return_value=os.geteuid() + 1):
            with self.assertRaises(OSError):
                with bridge.open_budget_database(str(self.db)):
                    pass


if __name__ == '__main__':
    unittest.main()
