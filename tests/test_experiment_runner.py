import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.repairer import context_digest
from execution.docker_runner import ExecutionResult
from experiments.runner import run_experiment
from models.adapter import Completion
from workflow.state import TestRun, Failure, FailureKind


class ExperimentRunnerTests(unittest.TestCase):
    def case(self):
        return {'id': 'nonformal', 'sources': {'app.py':'def f(x):\n    return x > 1\n'},
                'original_tests': {'tests/test_old.py':'from app import f\ndef test_old(): assert f(2)\n'},
                'requirements': ['f(x) returns x >= 1.'], 'target': ('app.py','f'),
                'support': {'data/info.txt': b'reviewed data'}, 'coverage_sources': ('app.py',)}

    def validation(self):
        return {'reference_sources': {'app.py':'def f(x):\n    return x >= 1\n'},
                'holdout_tests': {'tests/test_private.py':'from app import f\ndef test_private(): assert not f(0)\n'}}

    def runner(self, root, repo, *, artifact_root, **kwargs):
        self.sequence += 1
        sources = tuple((p,(root/p).read_bytes().decode()) for p in repo.source_files)
        tests = tuple((p,(root/p).read_bytes().decode()) for p in repo.test_files)
        support = tuple((p,hashlib.sha256((root/p).read_bytes()).hexdigest()) for p in kwargs.get('support_files', ()))
        passing = '>=' in dict(sources)['app.py'] or not any('generated/' in p for p in repo.test_files)
        run = TestRun('run'+str(self.sequence),context_digest(sources,tests,support),0 if passing else 1,
                      len(tests) if passing else len(tests)-1,0 if passing else 1,0,
                      failure=None if passing else Failure(FailureKind.TEST_ASSERTION,'assertion'))
        directory=Path(artifact_root)/run.run_id
        directory.mkdir(parents=True)
        (directory/'log.txt').write_text('visible assertion')
        (directory/'junit.xml').write_text('<testsuite><testcase classname="test_g" name="test_g"><failure message="assert f(1)">AssertionError</failure></testcase></testsuite>')
        entries=[(p,hashlib.sha256(t.encode()).hexdigest()) for p,t in sources+tests]+list(support)
        (directory/'manifest.json').write_text(json.dumps({'files':entries}))
        (directory/'coverage.json').write_text(json.dumps({'files':{'app.py':{'executed_lines':[1,2], 'missing_lines':[], 'executed_branches':[], 'missing_branches':[]}}}))
        self.inventories.append(repo.test_files)
        return ExecutionResult(run,str(directory),True)

    def client(self, *, invalid_second=False):
        outer=self
        class Client:
            generation=0
            def complete(self,messages):
                p=json.loads(messages[1]['content'])
                outer.prompts.append(p)
                if 'test_prefix' in p:
                    self.generation+=1
                    if invalid_second and self.generation==2:
                        return Completion('{}','fake',None)
                    out={'task_id':p['task_id'],'base_revision':p['base_revision'],'rationale':'endpoint requires True',
                         'files':[{'path':f'tests/generated/test_round{self.generation}.py','content':f'from app import f\ndef test_g{self.generation}(): assert f(1)\n'}]}
                else:
                    out={'task_id':p['task_id'],'base_revision':p['base_revision'],'based_on_run_id':p['based_on_run_id'],
                         'diagnosis':'app.py:2 excludes endpoint','replacements':[{'path':'app.py','qualified_name':'f','content':'def f(x):\n    return x >= 1'}]}
                return Completion(json.dumps(out),'fake',None)
        return Client()

    def setUp(self):
        self.sequence=0
        self.inventories=[]
        self.prompts=[]

    def execute(self, group, folder, **kwargs):
        with patch('experiments.runner.run_tests',side_effect=self.runner), patch('repair.session.run_tests',side_effect=self.runner):
            return run_experiment(self.case(),self.validation(),group=group,image_id='unused',artifact_root=Path(folder),**kwargs)

    def test_two_rounds_accumulate_and_only_b2_receives_coverage(self):
        for group in ('B2','A0'):
            self.setUp()
            with tempfile.TemporaryDirectory() as folder:
                result=self.execute(group,folder,client=self.client())
            self.assertEqual(result['status'],'verified')
            self.assertEqual(len(result['rounds']),2)
            self.assertEqual(len(self.prompts),3)
            self.assertEqual('coverage_hint' in self.prompts[1],group=='B2')
            self.assertNotIn('coverage_hint',self.prompts[0])
            self.assertNotIn('test_private',json.dumps(self.prompts))
            self.assertTrue(any('tests/generated/test_round1.py' in p and 'tests/generated/test_round2.py' in p for p in self.inventories))
            self.assertEqual(result['rounds'][1]['parent_task_id'],result['rounds'][0]['task_id'])

    def test_single_round_and_invalid_second_do_not_trigger_extra_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            result=self.execute('B1',folder,client=self.client())
        self.assertEqual(result['status'],'verified')
        self.assertEqual(len(self.prompts),2)
        self.setUp()
        with tempfile.TemporaryDirectory() as folder:
            result=self.execute('B2',folder,client=self.client(invalid_second=True))
        self.assertEqual(result['status'],'generation_failed')
        self.assertEqual(len(self.prompts),2)
        self.assertIsNone(result['repair'])
        self.assertEqual(len(result['rounds']),2)

    def test_cancel_and_existing_run_id_never_call_model(self):
        with tempfile.TemporaryDirectory() as folder:
            result=self.execute('B2',folder,client=None,cancelled=lambda:True,run_id='owned')
            self.assertEqual(result['status'],'cancelled')
            with self.assertRaises(FileExistsError):
                self.execute('B2',folder,client=None,run_id='owned')
        self.assertEqual(self.prompts,[])

    def test_generated_collection_keeps_original_fixture_inventory(self):
        case=self.case()
        case['original_tests']['tests/conftest.py']='import pytest\n@pytest.fixture\ndef endpoint(): return 1\n'
        def execute(root,repo,**kwargs):
            if Path(kwargs['artifact_root']).name=='generated-only':
                self.assertIn('tests/conftest.py',repo.test_files)
                self.assertEqual(kwargs['test_targets'],('tests/generated/test_round1.py',))
            return self.runner(root,repo,**kwargs)
        with tempfile.TemporaryDirectory() as folder, \
             patch('experiments.runner.run_tests',side_effect=execute), \
             patch('repair.session.run_tests',side_effect=self.runner):
            result=run_experiment(case,self.validation(),group='B1',client=self.client(),image_id='unused',artifact_root=Path(folder))
        self.assertEqual(result['status'],'verified')

    def test_cancellation_after_repair_response_keeps_attempt_and_usage_link(self):
        flag={'cancelled':False}
        original=self.client()
        class Client:
            def complete(self,messages):
                result=original.complete(messages)
                if 'replacements' in result.content:
                    flag['cancelled']=True
                return result
        with tempfile.TemporaryDirectory() as folder:
            result=self.execute('B1',folder,client=Client(),cancelled=lambda:flag['cancelled'])
        self.assertEqual(result['status'],'cancelled')
        self.assertIsNotNone(result['repair'])
        self.assertEqual(result['repair']['status'],'cancelled')
        self.assertEqual(len(result['repair']['attempts']),1)
        self.assertIsNotNone(result['repair']['attempts'][0]['completion'])
        self.assertIsNone(result['repair']['attempts'][0]['patch_diff'])

    def test_host_io_failure_records_safe_type_without_private_message(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch('experiments.runner.tempfile.TemporaryDirectory',side_effect=PermissionError(13,'private-location-not-for-output')):
            result=run_experiment(self.case(),self.validation(),group='B1',client=None,image_id='unused',artifact_root=Path(folder))
        self.assertEqual(result['status'],'experiment_failed')
        self.assertEqual(result['error']['type'],'PermissionError')
        self.assertEqual(result['error']['errno'],13)
        self.assertNotIn('private-location-not-for-output',json.dumps(result))

    def test_scope_is_explicit_and_legacy_default_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            old=self.execute('B0',folder,client=None)
            new=self.execute('B0',folder,client=None,scope='P08B frozen formal experiment')
            self.assertEqual(old['scope'],'P08A nonformal admission only')
            self.assertEqual(new['scope'],'P08B frozen formal experiment')
            with self.assertRaises(ValueError):
                self.execute('B0',folder,client=None,scope='unreviewed')

    def test_wrong_valid_repairs_respect_one_or_three_attempt_caps(self):
        for group,expected in [('B1',2),('B2',5),('A0',5)]:
            self.setUp();original=self.client()
            class WrongClient:
                def complete(self,messages):
                    response=original.complete(messages)
                    if 'replacements' in response.content:
                        response=Completion(response.content.replace('return x >= 1','return x > 2'),'fake',None)
                    return response
            with self.subTest(group=group),tempfile.TemporaryDirectory() as folder:
                result=self.execute(group,folder,client=WrongClient())
            self.assertNotEqual(result['status'],'verified')
            self.assertEqual(len(self.prompts),expected)
            self.assertEqual(len(result['repair']['attempts']),1 if group=='B1' else 3)
