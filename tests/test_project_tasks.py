import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.project_tasks import load_project_admission


class ProjectTaskTests(unittest.TestCase):
    def setup_files(self, root):
        code='def f(value):\n    return value\n'
        checkout=root/'reference/demo/src/demo';checkout.mkdir(parents=True)
        (checkout/'__init__.py').write_bytes(code.encode())
        visible={'schema':1,'scope':'P08A-admission-only','id':'demo_pilot','project_id':'demo','dependency_project_ids':[],
                 'target':{'path':'src/demo/__init__.py','qualified_name':'f'},'defect_type':'null_type','requirements':['f returns value'],
                 'original_tests':{'tests/test_old.py':'from demo import f\ndef test_old(): assert f(0)==0\n'},
                 'baseline_origin':'assistant fixture',
                 'support':{'data/value.txt':{'encoding':'utf8','content':'data','sha256':hashlib.sha256(b'data').hexdigest()}}}
        private={'schema':1,'id':'demo_pilot','mutation':{'old':'return value','new':'return 0'},'description':'controlled return defect',
                 'holdout_tests':{'tests/test_hidden.py':'from demo import f\ndef test_hidden_behaviour_canary(): assert f(1)==1\n'}}
        project={'id':'demo','package_root':'src/demo'}
        metadata={'modules':[{'path':'src/demo/__init__.py','sha256':hashlib.sha256(code.encode()).hexdigest()}],'licenses':{},'issues':[]}
        return visible,private,project,metadata

    def load(self,root,visible,private,project,metadata):
        v,p=root/'visible.json',root/'private.json'
        v.write_text(json.dumps(visible),encoding='utf-8');p.write_text(json.dumps(private),encoding='utf-8')
        with patch('experiments.project_tasks.load_projects',return_value=[project]), \
             patch('experiments.project_tasks.inspect_project',return_value=metadata):
            return load_project_admission(v,p,root/'catalog.json',root/'reference')

    def test_resolves_fixed_source_and_keeps_mutation_and_holdout_out_of_visible_case(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=self.setup_files(root)
            case,validation,provenance=self.load(root,*data)
        self.assertIn('return 0',case['sources']['src/demo/__init__.py'])
        self.assertIn('return value',validation['reference_sources']['src/demo/__init__.py'])
        self.assertNotIn('hidden_behaviour_canary',repr(case))
        self.assertNotIn('mutation',case)
        self.assertTrue(provenance['excluded_from_P08B'])

    def test_unknown_fields_scope_wrong_support_hash_and_nonprimary_target_rejected(self):
        for choice in ('unknown','scope','hash','target'):
            with self.subTest(choice=choice),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);visible,private,project,metadata=self.setup_files(root)
                if choice=='unknown':visible['reference_sources']={}
                elif choice=='scope':visible['scope']='formal'
                elif choice=='hash':visible['support']['data/value.txt']['sha256']='0'*64
                else:visible['target']['path']='src/other/__init__.py'
                with self.assertRaises(ValueError):
                    self.load(root,visible,private,project,metadata)
