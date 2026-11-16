import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from web.experiments import ExperimentData, DataUnavailable
from web.server import Application, Store, handler

ROOT = Path(__file__).resolve().parents[1]


class ExperimentDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = ExperimentData(ROOT)

    def test_complete_summary_and_correct_method_meaning(self):
        s = self.data.summary()
        self.assertEqual((s['projects_count'], s['tasks_count'], s['records_count']), (5, 30, 360))
        self.assertTrue(s['methods']['B2']['coverage_feedback'])
        self.assertFalse(s['methods']['A0']['coverage_feedback'])
        self.assertEqual([s['groups'][g]['semantic_confirmed'] for g in ('B0','B1','B2','A0')], [0,35,15,15])

    def test_filter_pagination_and_rejected_final_patch(self):
        page = self.data.records({'project':'dateutil','method':'B2','semantic':'rejected','limit':'1'})
        self.assertEqual(page['total'], 1)
        row = page['records'][0]
        self.assertNotIn('tests', row)
        detail = self.data.detail(row['slot_id'])
        self.assertEqual(detail['record']['status'], 'verified')
        self.assertEqual(detail['record']['semantic'], 'rejected')
        self.assertEqual(len(detail['patches']), 3)
        self.assertTrue(detail['patches'][-1]['review']['is_last_attempt'])
        self.assertTrue(detail['tests'])
        self.assertEqual(self.data.records({'offset':'360'})['records'], [])

    def test_reject_bad_filters_and_unknown_slot(self):
        for query in ({'limit':'0'}, {'offset':'-1'}, {'method':'X'}, {'project':'../.env'}, {'path':'.env'}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.data.records(query)
        with self.assertRaises(KeyError):
            self.data.detail('0'*32)

    def test_missing_and_tampered_evidence_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(DataUnavailable):
                ExperimentData(Path(temp))
        original = Path.read_bytes
        def damaged(path):
            data = original(path)
            if path.name == 'p08b-v2-formal-results.json':
                value = json.loads(data)
                value['records'][1]['slot_id'] = value['records'][0]['slot_id']
                return json.dumps(value).encode()
            return data
        with patch.object(Path, 'read_bytes', damaged), self.assertRaises(DataUnavailable):
            ExperimentData(ROOT)

    def test_unknown_usage_is_preserved_and_details_are_portable(self):
        record = next(r for r in self.data.rows if any(q['usage'] is None for q in r['requests']))
        detail = self.data.detail(record['slot_id'])
        self.assertGreater(detail['usage']['unknown_requests'], 0)
        self.assertIsNone(detail['usage']['total_tokens'])
        self.assertNotIn('result_path', detail['record'])
        self.assertIn('未', detail['log_notice'])


class ExperimentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(Store(Path(self.tmp.name)/'tasks.db'))
        self.server = ThreadingHTTPServer(('127.0.0.1',0),handler(self.app))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.app.close(); self.tmp.cleanup()

    def get(self, path, headers=None):
        try:
            with urlopen(Request(self.base+path,headers=headers or {}),timeout=10) as response:
                return response.status,response.read(),response.headers
        except HTTPError as error:
            return error.code,error.read(),error.headers

    def test_page_api_download_and_original_tasks(self):
        self.assertEqual(self.get('/experiments')[0],200)
        self.assertEqual(self.get('/experiments.js')[0],200)
        status,body,_ = self.get('/api/experiments/summary')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['records_count'],360)
        _,body,_ = self.get('/api/experiments/records?method=B1&limit=1')
        slot = json.loads(body)['records'][0]['slot_id']
        status,body,headers = self.get('/api/experiments/records/'+slot+'/download')
        self.assertEqual(status,200)
        self.assertIn('attachment',headers['Content-Disposition'])
        self.assertEqual(json.loads(body)['record']['slot_id'],slot)
        status,body,headers = self.get('/api/experiments/download?format=csv')
        self.assertEqual(status,200)
        self.assertIn('text/csv',headers['Content-Type'])
        self.assertEqual(len(body.decode('utf-8-sig').splitlines()),5)
        self.assertEqual(self.get('/api/tasks')[0],200)

    def test_invalid_queries_and_origin_do_not_expose_evidence(self):
        for path in ('/api/experiments/records?limit=1&limit=2',
                     '/api/experiments/summary?path=.env',
                     '/api/experiments/download?format=exe'):
            self.assertEqual(self.get(path)[0],400)
        self.assertEqual(self.get('/api/experiments/records/'+'0'*32)[0],404)
        self.assertEqual(self.get('/api/experiments/summary',{'Origin':'https://example.com'})[0],403)
        self.assertEqual(self.get('/api/experiments/records?path=../../.env')[0],400)

    def test_changed_evidence_after_success_returns_unavailable(self):
        self.assertEqual(self.get('/api/experiments/summary')[0],200)
        original = Path.read_bytes
        def damaged(path):
            if path.name == 'p08b-v2-formal-results.json':
                return b'{}'
            return original(path)
        with patch.object(Path, 'read_bytes', damaged):
            self.assertEqual(self.get('/api/experiments/summary')[0],503)
