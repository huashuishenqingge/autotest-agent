from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.repairer import context_digest
from execution.docker_runner import ExecutionResult
from execution.results import classify_result
from experiments.screening import review_digest, screen_candidate, quota_matrix


class ScreeningTests(unittest.TestCase):
    def task(self):
        task={'case':{'id':'demo','sources':{'src/demo.py':'def f(): return 0\n'},
                     'original_tests':{'tests/test_old.py':'def test_old(): assert True\n'},
                     'support':{},'target':('src/demo.py','f'),'requirements':['f returns one']},
              'validation':{'reference_sources':{'src/demo.py':'def f(): return 1\n'},
                            'holdout_tests':{'tests/p08_holdout/test_demo.py':'def test_hold(): assert True\n'}},
              'provenance':{'task':{'project_id':'click','owner':'author','reviewer':'reviewer',
                                   'target':{'path':'src/demo.py','qualified_name':'f'},'requirement_sources':[],
                                   'protocol':{'image_id':'reviewed'}}},
              'evaluation':{'defect_type':'boundary','mutation':{},'expected_failure':'assertion'}}
        task['semantic_review']={'status':'approved','reviewer':'reviewer','notes':'independent requirements checked',
                                 'input_sha256':review_digest(task)}
        return task

    def execute(self, folder, task, failures=None):
        failures=failures or {}
        calls=[]
        def runner(root, repo, *, artifact_root, **kwargs):
            stage=Path(artifact_root).name;calls.append(stage)
            if '_original_' in stage:
                self.assertFalse(any(p.startswith('tests/p08_holdout/') for p in repo.test_files))
            else:
                self.assertEqual(tuple(kwargs['test_targets']),tuple(task['validation']['holdout_tests']))
                self.assertTrue(set(task['case']['original_tests']).issubset(repo.test_files))
            directory=Path(artifact_root);directory.mkdir(parents=True)
            sources=tuple((p,(root/p).read_bytes().decode()) for p in repo.source_files)
            tests=tuple((p,(root/p).read_bytes().decode()) for p in repo.test_files)
            digest=context_digest(sources,tests)
            mode=failures.get(stage,'assert' if stage.startswith('mutant_holdout') else 'pass')
            xml={'assert':b'<testsuite><testcase classname="test_hold" name="test_hold"><failure message="assert 0 == 1">AssertionError: assert 0 == 1</failure></testcase></testsuite>',
                 'other':b'<testsuite><testcase classname="test_hold" name="different"><failure message="assert 0 == 1">AssertionError</failure></testcase></testsuite>',
                 'env':b'<testsuite><testcase name="test_hold"><error>ImportError</error></testcase></testsuite>',
                 'pass':b'<testsuite><testcase name="test_ok"/></testsuite>'}[mode]
            (directory/'junit.xml').write_bytes(xml)
            run=classify_result(stage,digest,0 if mode=='pass' else 1,xml)
            return ExecutionResult(run,str(directory),True)
        with patch('experiments.screening.run_tests',side_effect=runner):
            result=screen_candidate(task,image_id='reviewed',artifact_root=Path(folder))
        return result,calls

    def test_survivor_requires_two_complete_stable_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            result,calls=self.execute(folder,self.task())
        self.assertEqual(result['status'],'eligible')
        self.assertEqual(len(calls),8)

    def test_original_detection_environment_and_no_holdout_detection_are_distinct(self):
        for stage,mode,expected in [('mutant_original_1','assert','detected_by_original'),
                                    ('mutant_holdout_1','env','environment_failed'),
                                    ('mutant_holdout_1','pass','holdout_not_detected'),
                                    ('reference_original_1','assert','reference_failed')]:
            with self.subTest(expected=expected),tempfile.TemporaryDirectory() as folder:
                result,_=self.execute(folder,self.task(),{stage:mode})
                self.assertEqual(result['status'],expected)

    def test_second_pass_mismatch_is_unstable_and_same_identity_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            task=self.task();result,_=self.execute(folder,task,{'mutant_holdout_2':'other'})
            self.assertEqual(result['status'],'unstable')
            with self.assertRaises(FileExistsError):self.execute(folder,task)

    def test_missing_or_stale_semantic_review_never_executes(self):
        with tempfile.TemporaryDirectory() as folder,patch('experiments.screening.run_tests') as runner:
            task=self.task();task['semantic_review']['input_sha256']='0'*64
            result=screen_candidate(task,image_id='reviewed',artifact_root=Path(folder))
            self.assertEqual(result['status'],'pending_review');runner.assert_not_called()

    def test_explicit_semantic_rejection_is_preserved_without_execution(self):
        with tempfile.TemporaryDirectory() as folder,patch('experiments.screening.run_tests') as runner:
            task=self.task();task['semantic_review'].update(status='rejected',notes='Strict rejection of invalid input is not a stable API promise.')
            result=screen_candidate(task,image_id='reviewed',artifact_root=Path(folder))
            self.assertEqual(result['status'],'semantic_rejected')
            runner.assert_not_called()

    def test_quota_counts_unique_functions_once_even_across_mutations_and_classes(self):
        base={'status':'eligible','project_id':'click','defect_type':'boundary','target':['src/demo.py','f'],'candidate_id':'a'}
        duplicate=dict(base,candidate_id='b',defect_type='null_type')
        matrix=quota_matrix([base,duplicate],project_ids=('click',))
        self.assertEqual(sum(cell['eligible_unique'] for cell in matrix),1)
        self.assertTrue(all(cell['required']==2 for cell in matrix))

    def test_execution_digest_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            xml=b'<testsuite><testcase name="ok"/></testsuite>'
            result=ExecutionResult(classify_result('run','wrong-input',0,xml),folder,True)
            with patch('experiments.screening.run_tests',return_value=result):
                screened=screen_candidate(self.task(),image_id='reviewed',artifact_root=Path(folder))
            self.assertEqual(screened['status'],'environment_failed')
            self.assertEqual(len(screened['executions']),1)
