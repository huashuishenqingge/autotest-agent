import difflib
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.repairer import RepairContext, context_digest
from experiments.context import ScopedGenerator
from experiments.formal_tasks import load_formal_tasks
from experiments.scoped import ScopedRepairer
from models.adapter import Completion
from workflow.state import (Failure, FailureKind, GenerateInput, Policy, RepairInput,
                            Repository, TestRun)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FormalTaskTests(unittest.TestCase):
    def setup_files(self, root):
        (root/'demo/docs').mkdir(parents=True)
        doc=b'API contract\nReturn the documented value.\n'
        (root/'demo/docs/api.md').write_bytes(doc)
        self.project={'id':'demo','package_root':'src/demo','commit':'fixed'}
        self.source='def f(value):\n    return "REFERENCE_ONLY_CANARY"\n'
        self.mutant=self.source.replace('"REFERENCE_ONLY_CANARY"','"bad"')
        tests={f'tests/test_old_{n}.py':b'def test_old(): assert True\n' for n in range(25)}
        tests['tests/conftest.py']=b'FIXTURE=1\n'
        self.inventory={'sources':{'src/demo/__init__.py':self.source.encode()},'tests':tests,
                        'support':{'data/values.txt':b'visible data'},'coverage_sources':('src/demo/__init__.py',),
                        'excluded':[],'provenance':{'project':self.project,'dependencies':[],'licenses':{}}}
        snapshot={kind:{p:sha(b) for p,b in self.inventory[kind].items()} for kind in ('sources','tests','support')}
        snapshot.update(excluded=[],provenance=self.inventory['provenance'])
        project_root=Path(__file__).resolve().parents[1]
        protocol={'image_id':'sha256:7227650ec06570ea9a532bfb669d0888a4a92f7f179d2d442878f353b27ecd66',
                  'dependency_lock_sha256':sha((project_root/'execution/requirements-p08.lock').read_bytes()),
                  'worker_sha256':sha((project_root/'execution/container_worker.py').read_bytes().replace(b'\r\n',b'\n')),
                  'context_version':'p08-context-v1','repair_version':'p08-scoped-repair-v1',
                  'model':'deepseek-v4-flash-ascend','temperature':0.2,'thinking_mode':'disabled',
                  'max_tokens':2048,'input_characters':16000,'timeout_seconds':60}
        public={'schema':'p08-formal-v1','tasks':[{'id':'demo_candidate','project_id':'demo','origin':'injected',
                'target':{'path':'src/demo/__init__.py','qualified_name':'f','line':1,'end_line':2},
                'requirements':['Return the documented value.'],
                'requirement_sources':[{'path':'docs/api.md','start_line':2,'end_line':2,'sha256':sha(doc),'excerpt':'Return the documented value.'}],
                'coverage_sources':['src/demo/__init__.py'],'inventory':snapshot,'protocol':protocol,'owner':'A','reviewer':'B'}]}
        holdout='def test_PRIVATE_HOLDOUT_CANARY(): assert True\n'
        diff=''.join(difflib.unified_diff(self.source.splitlines(keepends=True),self.mutant.splitlines(keepends=True),fromfile='a/src/demo/__init__.py',tofile='b/src/demo/__init__.py'))
        private={'schema':'p08-formal-v1','tasks':{'demo_candidate':{
                 'defect_type':'null_type','reference_source_hashes':snapshot['sources'],
                 'mutant_source_hashes':{'src/demo/__init__.py':sha(self.mutant.encode())},
                 'mutation':{'old':'return "REFERENCE_ONLY_CANARY"','new':'return "bad"','line':2},
                 'diff':diff,'holdout_tests':{'tests/p08_holdout/test_demo_candidate.py':{'content':holdout,'sha256':sha(holdout.encode())}},
                 'expected_failure':'PRIVATE_EXPECTATION_CANARY','screening':{'status':'pending','evidence':[]}}}}
        return public,private

    def load(self,root,public,private):
        p,q=root/'public.json',root/'private.json'
        p.write_text(json.dumps(public),encoding='utf-8');q.write_text(json.dumps(private),encoding='utf-8')
        with patch('experiments.formal_tasks.load_projects',return_value=[self.project]), \
             patch('experiments.formal_tasks.build_reviewed_inventory',return_value=self.inventory):
            return load_formal_tasks(p,q,catalog_path=root/'catalog.json',reference_root=root,resource_root=root/'wheels')

    def test_loads_more_than_twenty_tests_and_separates_evaluation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);public,private=self.setup_files(root)
            row,=self.load(root,public,private)
        self.assertEqual(len(row['case']['original_tests']),26)
        self.assertEqual(row['case']['sources']['src/demo/__init__.py'],self.mutant)
        self.assertEqual(row['validation']['reference_sources']['src/demo/__init__.py'],self.source)
        self.assertNotIn('PRIVATE_EXPECTATION_CANARY',repr(row['case']))
        self.assertNotIn('REFERENCE_ONLY_CANARY',repr(row['case']))

    def test_refuses_inventory_drift_protocol_docs_target_private_hash_and_namespaces(self):
        for choice in ('schema','unknown','missing_fixture','support','target','pilot','coverage','protocol','docs','line','diff','holdout_hash','overlap','identity','namespace'):
            with self.subTest(choice=choice),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);p,q=self.setup_files(root);t=p['tasks'][0];e=q['tasks']['demo_candidate']
                if choice=='schema':p['schema']='pilot'
                elif choice=='unknown':t['reference_sources']={}
                elif choice=='missing_fixture':del t['inventory']['tests']['tests/conftest.py']
                elif choice=='support':self.inventory['support']['data/values.txt']=b'changed'
                elif choice=='target':t['target']['path']='src/dependency/__init__.py'
                elif choice=='pilot':t['project_id']='markupsafe';self.project['id']='markupsafe';t['target']['qualified_name']='escape_silent'
                elif choice=='coverage':t['coverage_sources']=[]
                elif choice=='protocol':t['protocol']['max_tokens']=9999
                elif choice=='docs':t['requirement_sources'][0]['excerpt']='fabricated'
                elif choice=='line':e['mutation']['line']=1
                elif choice=='diff':e['diff']='made up'
                elif choice=='holdout_hash':next(iter(e['holdout_tests'].values()))['sha256']='0'*64
                elif choice=='overlap':self.inventory['tests']['tests/p08_holdout/test_demo_candidate.py']=b'pass'
                elif choice=='identity':q['tasks']['other']=q['tasks'].pop('demo_candidate')
                elif choice=='namespace':self.inventory['tests']['tests/generated/test_prior.py']=b'pass'
                with self.assertRaises(ValueError):self.load(root,p,q)

    def test_actual_generation_and_repair_messages_do_not_contain_private_canaries(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p,q=self.setup_files(root);row,=self.load(root,p,q)
        case=row['case'];sources=tuple(case['sources'].items());tests=tuple(case['original_tests'].items())
        repo=Repository('demo','fixed',tuple(case['sources']),tuple(case['original_tests']))
        class Client:
            messages=[]
            def complete(self,messages):
                self.messages.append(messages)
                return Completion('{}','fake no network',None)
        client=Client()
        generation=GenerateInput('t',repo,Policy(),tuple(case['requirements']),sources)
        ScopedGenerator(client,case['target'],tests).generate(generation)
        hashes=tuple((path,sha(data.encode())) for path,data in sources)
        support=tuple((path,sha(data)) for path,data in case['support'].items())
        failed=TestRun('r',context_digest(sources,tests,support),1,0,1,0,failure=Failure(FailureKind.TEST_ASSERTION,'assertion'))
        request=RepairInput('t',repo,Policy(),failed,1,sources,'assertion')
        context=RepairContext(tuple(case['requirements']),hashes,tests,support)
        ScopedRepairer(client,context,case['target'],failure_evidence=(('test','test_f','assertion','AssertionError'),)).repair(request)
        self.assertEqual(len(client.messages),2)
        for canary in ('REFERENCE_ONLY_CANARY','PRIVATE_HOLDOUT_CANARY','PRIVATE_EXPECTATION_CANARY','reference_source_hashes','holdout_tests'):
            self.assertNotIn(canary,json.dumps(client.messages))

    def test_duplicate_json_keys_and_duplicate_task_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p,q=self.setup_files(root)
            p['tasks'].append(p['tasks'][0])
            with self.assertRaises(ValueError):self.load(root,p,q)
            (root/'duplicate.json').write_text('{"schema":"p08-formal-v1","schema":"p08-formal-v1","tasks":[]}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'duplicate JSON key'):
                load_formal_tasks(root/'duplicate.json',root/'private.json',catalog_path=root/'catalog.json',reference_root=root,resource_root=root)
