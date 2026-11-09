import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from web.server import Application, Store, handler, real_runner


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'tasks.db')
        self.app = Application(self.store)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()

    def request(self, path, body=None, headers=None):
        h = {'Content-Type': 'application/json', 'X-Task-Token': self.app.token}
        h.update(headers or {})
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except HTTPError as e:
            return e.code, e.read(), e.headers

    def test_submit_query_download_and_persistence(self):
        status, body, _ = self.request('/api/tasks', {'case_id': self.app.cases[0]})
        self.assertEqual(status, 202)
        task = json.loads(body)
        status, body, headers = self.request('/api/tasks/' + task['id'] + '/download')
        self.assertEqual(status, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual(json.loads(body)['mode'], 'demo')
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get(task['id'])['status'], 'interrupted')

    def test_reject_foreign_origin_token_and_host(self):
        for h in ({'Origin': 'https://attacker.example'}, {'X-Task-Token': 'wrong'}, {'Host': 'attacker.example'}):
            self.assertEqual(self.request('/api/tasks', {'case_id': self.app.cases[0]}, h)[0], 403)
        self.assertEqual(self.store.list(), [])

    def test_schema_and_path_rejection(self):
        for body in ({'case_id': '../secret'}, {'case_id': self.app.cases[0], 'api_key': 'secret'}, [], None):
            if body is not None:
                self.assertEqual(self.request('/api/tasks', body)[0], 400)
        for path in ('/.env', '/api/tasks/../../.env', '/static/../server.py', '/api/tasks/' + 'a'*32):
            self.assertEqual(self.request(path)[0], 404)

    def test_cancel_queued_and_terminal_idempotence(self):
        task = self.app.submit({'case_id': self.app.cases[0]})
        self.assertEqual(self.request('/api/tasks/' + task['id'] + '/cancel', {})[0], 200)
        self.assertEqual(self.store.get(task['id'])['status'], 'cancelled')
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.cancel(task['id'])['status'], 'cancelled')

    def test_demo_finishes_and_identifies_synthetic_evidence(self):
        task = self.app.submit({'case_id': self.app.cases[0]})
        self.app.start()
        deadline = time.monotonic() + 4
        while self.store.get(task['id'])['status'] not in {'succeeded', 'failed'} and time.monotonic() < deadline:
            time.sleep(.03)
        result = self.store.get(task['id'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['result']['evidence'], 'simulated')
        self.assertIsNone(result['result']['coverage'])

    def test_running_cancel_and_failure_redaction(self):
        ready = threading.Event()
        def run(task, cancelled):
            ready.set()
            while not cancelled():
                time.sleep(.01)
            raise ValueError('private-secret')
        self.app.runner = run
        task = self.app.submit({'case_id': self.app.cases[0]})
        self.app.start()
        self.assertTrue(ready.wait(2))
        self.store.cancel(task['id'])
        deadline = time.monotonic() + 2
        while self.store.get(task['id'])['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.02)
        result = self.store.get(task['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertNotIn('private-secret', json.dumps(result))

    def test_queue_bound(self):
        for _ in range(10):
            self.app.submit({'case_id': self.app.cases[0]})
        with self.assertRaises(ValueError):
            self.app.submit({'case_id': self.app.cases[0]})

    def test_static_and_info(self):
        for path in ('/', '/app.js', '/style.css', '/api/info'):
            status, body, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn('nosniff', headers['X-Content-Type-Options'])
        info = json.loads(self.request('/api/info')[1])
        self.assertEqual(info['mode'], 'demo')
        self.assertEqual(len(info['cases']), 3)

    def test_real_adapter_reuses_orchestrator_and_safety_check(self):
        # Adapter wiring only, no paid model or Docker execution.
        with patch('models.adapter.Config.from_env'), patch('models.adapter.ModelClient') as client, patch('workflow.orchestrator.run_case') as run:
            run.return_value = {'status': 'verified', 'events': []}
            callback = lambda: False
            result = real_runner('sha256:'+'a'*64, Path(self.tmp.name))(
                {'case_id': self.app.cases[0]}, callback)
            self.assertEqual(result['evidence'], 'real')
            self.assertIs(run.call_args.kwargs['cancelled'], callback)
            client.return_value.ensure_safe.assert_called_once_with(result)


if __name__ == '__main__':
    unittest.main()
