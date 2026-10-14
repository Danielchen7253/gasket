import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('gate', Path(__file__).with_name('gate.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [1000.0]
        self.state = Path(self.tmp.name) / 'state.sqlite'
        self.gate = m.Gate(self.state, ['test', 'other'], lambda: self.now[0])

    def test_default_closed_and_unknown_project(self):
        self.assertEqual(self.gate.check('test', '192.0.2.1', 'GET', '/')[0], False)
        self.assertEqual(self.gate.check('unknown', '192.0.2.1', 'GET', '/')[0], False)

    def test_owner_approval_expires_and_restart_does_not_extend_it(self):
        self.gate.approve('test', 1, 'Owner requested preview')
        self.assertTrue(self.gate.check('test', '192.0.2.1', 'GET', '/')[0])
        restarted = m.Gate(self.state, ['test'], lambda: self.now[0])
        self.assertTrue(restarted.active('test'))
        self.now[0] += 61
        self.assertFalse(restarted.check('test', '192.0.2.1', 'GET', '/')[0])

    def test_repeat_ignores_cache_busting_query_and_ban_survives_restart(self):
        self.gate.approve('test', 10, 'test')
        for i in range(20):
            self.assertTrue(self.gate.check('test', '192.0.2.1', 'GET', '/api/data?nonce=' + str(i))[0])
        self.assertEqual(self.gate.check('test', '192.0.2.1', 'GET', '/api/%64ata?nonce=21'), (False, 'repeated-request'))
        restarted = m.Gate(self.state, ['test'], lambda: self.now[0])
        self.now[0] += 120
        self.assertFalse(restarted.check('test', '192.0.2.1', 'GET', '/another')[0])
        self.assertTrue(restarted.check('test', '192.0.2.2', 'GET', '/another')[0])
        restarted.approve('test', 10, 'owner renews')
        self.assertFalse(restarted.check('test', '192.0.2.1', 'GET', '/another')[0])
        restarted.unblock('test', '192.0.2.1', 'owner verified source')
        self.assertTrue(restarted.check('test', '192.0.2.1', 'GET', '/another')[0])

    def test_different_assets_and_normal_refresh_allowed(self):
        self.gate.approve('test', 10, 'test')
        for i in range(50):
            self.assertTrue(self.gate.check('test', '192.0.2.1', 'GET', '/assets/' + str(i))[0])
        self.assertTrue(self.gate.check('test', '192.0.2.1', 'GET', '/')[0])
        self.assertTrue(self.gate.check('test', '192.0.2.1', 'GET', '/')[0])

    def test_source_rate_across_paths(self):
        self.gate.approve('test', 10, 'test')
        for i in range(120):
            self.assertTrue(self.gate.check('test', '192.0.2.1', 'GET', '/path/' + str(i))[0])
        self.assertEqual(self.gate.check('test', '192.0.2.1', 'GET', '/other'), (False, 'source-rate'))

    def test_distributed_traffic_closes_project_and_isolates_others(self):
        self.gate.approve('test', 10, 'test')
        self.gate.approve('other', 10, 'test')
        for i in range(600):
            ip = '198.18.' + str(i // 250) + '.' + str(i % 250 + 1)
            self.assertTrue(self.gate.check('test', ip, 'GET', '/')[0])
        self.assertEqual(self.gate.check('test', '203.0.113.1', 'GET', '/'), (False, 'traffic-spike'))
        self.now[0] += 120
        self.assertFalse(self.gate.active('test'))
        self.assertTrue(self.gate.active('other'))

    def test_parallel_checks_are_atomic(self):
        self.gate.approve('test', 10, 'test')
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            decisions = list(pool.map(lambda _: self.gate.check('test', '192.0.2.1', 'GET', '/')[0], range(40)))
        self.assertEqual(sum(decisions), 20)

    def test_bandwidth_trips_and_stays_closed(self):
        self.gate.approve('test', 10, 'test')
        self.assertTrue(self.gate.transfer('test', m.BYTE_LIMIT))
        self.assertFalse(self.gate.transfer('test', 1))
        self.now[0] += 61
        self.assertFalse(self.gate.active('test'))


class Origin(BaseHTTPRequestHandler):
    calls = 0
    def do_GET(self):
        Origin.calls += 1
        body = json.dumps({'path': self.path, 'xff': self.headers.get('X-Forwarded-For')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gate = m.Gate(Path(self.tmp.name) / 'state.sqlite', ['test'])
        Origin.calls = 0
        self.origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        self.server = m.Server(('127.0.0.1', 0), self.gate, 'test', urlsplit('http://127.0.0.1:' + str(self.origin.server_port)))
        for server in [self.origin, self.server]:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def get(self, path='/', headers=None, data=None):
        request = Request(self.url + path, headers=headers or {}, data=data)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except HTTPError as response:
            return response.code, response.read(), response.headers

    def test_locked_page_assets_api_and_webhook_never_reach_origin(self):
        for path in ['/', '/assets/main.js', '/api/data', '/.netlify/functions/api', '/webhook']:
            self.assertEqual(self.get(path)[0], 403)
        self.assertEqual(Origin.calls, 0)

    def test_approved_forwarding_and_immediate_revocation(self):
        self.gate.approve('test', 10, 'owner approved')
        status, body, headers = self.get('/api/data?nonce=1')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['path'], '/api/data?nonce=1')
        self.assertEqual(headers['Cache-Control'], 'private, no-store')
        self.gate.close('test', 'owner closed')
        self.assertEqual(self.get()[0], 403)
        self.assertEqual(Origin.calls, 1)

    def test_spoofed_forwarded_ip_cannot_reset_repeat_counter(self):
        self.gate.approve('test', 10, 'test')
        for i in range(20):
            result = self.get('/api', {'X-Forwarded-For': '192.0.2.' + str(i + 1), 'X-Real-IP': '192.0.2.' + str(i + 1)})
            self.assertEqual(result[0], 200)
            self.assertEqual(json.loads(result[1])['xff'], '127.0.0.1')
        self.assertEqual(self.get('/api')[0], 403)
        self.assertEqual(Origin.calls, 20)

    def test_authorizer_is_authenticated_and_defaults_closed(self):
        data = json.dumps({'project': 'test', 'ip': '192.0.2.1', 'method': 'GET', 'path': '/'}).encode()
        self.assertEqual(self.get('/_traffic_lock/authorize', data=data)[0], 403)
        with patch.dict(os.environ, {'GATE_AUTHORIZATION_KEY': 'x' * 32}):
            headers = {'Authorization': 'Bearer ' + 'x' * 32}
            self.assertEqual(self.get('/_traffic_lock/authorize', headers, data)[0], 403)
            self.gate.approve('test', 10, 'test')
            self.assertEqual(self.get('/_traffic_lock/authorize', headers, data)[0], 200)
        self.assertEqual(Origin.calls, 0)

    def test_broken_database_fails_closed(self):
        self.gate.approve('test', 10, 'test')
        with self.gate.connect() as db:
            db.execute('DROP TABLE projects')
        self.assertEqual(self.get()[0], 503)
        self.assertEqual(Origin.calls, 0)


if __name__ == '__main__':
    unittest.main()
