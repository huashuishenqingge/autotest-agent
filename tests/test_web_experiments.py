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

    def test_model_role_links_real_calls_outputs_and_validation(self):
        detail = self.data.detail('007d44248ba3f62f4da61ead5e3ba954')
        activity = detail['model_activity']
        self.assertEqual([r['role'] for r in activity['requests']], ['test_generation','source_repair'])
        self.assertEqual(activity['requests'][0]['model'], 'deepseek-v4-flash-ascend')
        self.assertEqual(activity['baseline']['failed'], 0)
        generation = activity['generation'][0]
        self.assertEqual(generation['reference_run']['exit_code'], 0)
        self.assertGreater(generation['mutant_run']['failed'], 0)
        self.assertEqual(generation['output_paths'], [t['path'] for t in detail['tests']])
        self.assertEqual(activity['repair']['independent_validation']['failed'], 0)
        self.assertFalse(activity['full_prompt_available'])

    def test_baseline_has_no_model_activity_and_failure_is_not_invented_success(self):
        baseline = next(r for r in self.data.rows if r['group']=='B0')
        activity = self.data.detail(baseline['slot_id'])['model_activity']
        self.assertEqual(activity['requests'], [])
        self.assertEqual(activity['generation'], [])
        self.assertIsNone(activity['repair'])
        failed = next(r for r in self.data.rows if r['status']=='generation_failed' and any(q['usage'] is None for q in r['requests']))
        activity = self.data.detail(failed['slot_id'])['model_activity']
        self.assertIsNone(activity['requests'][0]['usage'])
        self.assertIsNone(activity['generation'][0]['reference_run'])
        self.assertEqual(activity['generation'][0]['output_paths'], [])

    def test_feedback_role_uses_frozen_method_definition(self):
        for method, expected in [('B2', True), ('A0', False)]:
            item = next(r for r in self.data.rows if r['group']==method and len(r['generation_rounds'])==2)
            activity = self.data.detail(item['slot_id'])['model_activity']
            self.assertEqual(activity['generation'][1]['coverage_feedback'], expected)


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
