import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_formal_tasks as fixtures_v1
from experiments.formal_tasks import load_formal_tasks as load_v1
from experiments.formal_tasks_v2 import load_formal_tasks, validate_documents, _baseline_documents
from experiments.runtime_v2 import expected_runtime, validate_runtime


class RuntimeV2Tests(unittest.TestCase):
    def test_runtime_is_fixed_and_checks_host_worker_and_lock(self):
        runtime = expected_runtime()
        validate_runtime(runtime)
        for key, value in [('image_id', 'sha256:'+'0'*64), ('worker_sha256', '0'*64),
                           ('dependency_lock_sha256', '0'*64), ('timeout_seconds', True),
                           ('temperature', 1), ('max_tokens', 2048.0), ('extra', 1)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_runtime(dict(runtime, **{key: value}))
        with patch('pathlib.Path.read_bytes', return_value=b'drift'):
            with self.assertRaises(ValueError): validate_runtime(runtime)

    def test_original_anchor_documents_match_fixed_content_hashes(self):
        public, private = _baseline_documents()
        self.assertEqual(public['schema'], 'p08-formal-v1')
        self.assertEqual(len(public['tasks']), 30)
        self.assertEqual(len(private['tasks']), 30)
        with patch('experiments.formal_tasks_v2._read', return_value={'schema': 'changed'}):
            with self.assertRaises(ValueError): _baseline_documents()


class FormalTasksV2Tests(unittest.TestCase):
    def fixtures(self, root):
        helper = fixtures_v1.FormalTaskTests()
        original = helper.setup_files(root)
        public, private = copy.deepcopy(original)
        public['schema'] = private['schema'] = 'p08-formal-v2'
        public['tasks'][0]['protocol'] = expected_runtime()
        return helper, original, public, private

    def load(self, root, helper, baseline, public, private, loader=load_formal_tasks):
        p, q = root/'public.json', root/'private.json'
        p.write_text(json.dumps(public), encoding='utf-8')
        q.write_text(json.dumps(private), encoding='utf-8')
        with patch('experiments.formal_tasks_v2._baseline_documents', return_value=baseline), \
             patch('experiments.formal_tasks_v2.load_projects', return_value=[helper.project]), \
             patch('experiments.formal_tasks_v2.build_reviewed_inventory', return_value=helper.inventory):
            return loader(p,q,catalog_path=root/'catalog.json',reference_root=root,resource_root=root/'wheels')

    def test_load_preserves_full_inventory_and_evaluator_separation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); helper, baseline, p, q=self.fixtures(root)
            row,=self.load(root,helper,baseline,p,q)
        self.assertEqual(len(row['case']['original_tests']),26)
        self.assertNotIn('PRIVATE_HOLDOUT_CANARY',repr(row['case']))
        self.assertNotIn('REFERENCE_ONLY_CANARY',repr(row['case']))
        self.assertEqual(row['validation']['reference_sources']['src/demo/__init__.py'],helper.source)

    def test_versions_and_runtime_cannot_be_mixed(self):
        for mode in ('v1','unknown','mixed','old_runtime','wrong_worker','type'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root=Path(folder); helper, baseline, p, q=self.fixtures(root)
                if mode=='v1': p['schema']=q['schema']='p08-formal-v1'
                elif mode=='unknown': p['schema']='p08-formal-v3'
                elif mode=='mixed': q['schema']='p08-formal-v1'
                elif mode=='old_runtime': p['tasks'][0]['protocol']=baseline[0]['tasks'][0]['protocol']
                elif mode=='wrong_worker': p['tasks'][0]['protocol']['worker_sha256']='0'*64
                else: p['tasks'][0]['protocol']['max_tokens']=2048.0
                with self.assertRaises(ValueError): self.load(root,helper,baseline,p,q)

    def test_unchanged_v1_loader_rejects_v2_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); helper, baseline, p, q=self.fixtures(root)
            with self.assertRaises(ValueError): self.load(root,helper,baseline,p,q,loader=load_v1)

    def test_cannot_reselect_or_change_requirements_tests_or_private_oracle(self):
        for mode in ('target','requirement','test_hash','mutation','holdout','order','unknown'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root=Path(folder); helper, baseline, p, q=self.fixtures(root)
                task=p['tasks'][0]; evaluation=q['tasks'][task['id']]
                if mode=='target': task['target']['qualified_name']='other'
                elif mode=='requirement': task['requirements']=['Changed']
                elif mode=='test_hash': task['inventory']['tests']['tests/conftest.py']='0'*64
                elif mode=='mutation': evaluation['mutation']['new']='pass'
                elif mode=='holdout': next(iter(evaluation['holdout_tests'].values()))['content']='pass'
                elif mode=='order': p['tasks'].append(copy.deepcopy(task))
                else: p['extra']=True
                with self.assertRaises(ValueError): self.load(root,helper,baseline,p,q)

    def test_rechecks_actual_source_inventory_and_documentation(self):
        for mode in ('source','fixture','support','doc'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root=Path(folder); helper, baseline, p, q=self.fixtures(root)
                if mode=='source': helper.inventory['sources']['src/demo/__init__.py']=b'def f(): return 9\n'
                elif mode=='fixture': del helper.inventory['tests']['tests/conftest.py']
                elif mode=='support': helper.inventory['support']['data/values.txt']=b'changed'
                else: (root/'demo/docs/api.md').write_text('changed',encoding='utf-8')
                with self.assertRaises(ValueError): self.load(root,helper,baseline,p,q)
