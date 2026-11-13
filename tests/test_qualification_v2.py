from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_freeze_v2 as fixture_module
from agents.repairer import context_digest
from execution.docker_runner import ExecutionResult
from execution.results import classify_result
from experiments.qualification_v2 import qualify
from experiments.runtime_v2 import IMAGE
from experiments.screening import review_digest


class QualificationV2Tests(unittest.TestCase):
    def task(self):
        tasks,_,_=fixture_module.FreezeV2Tests().fixtures()
        task=tasks[0]
        review={'schema':'p08-v2-semantic-review-v1','task_id':task['case']['id'],
                'input_sha256':review_digest(task),'reviewer':'independent_v2',
                'status':'approved','notes':'Reference contract and private assertion independently checked.'}
        return task,review

    def execute(self,folder,task,review,mode=None):
        calls=[]
        def runner(root,repo,*,artifact_root,**kwargs):
            stage=Path(artifact_root).name;calls.append(stage)
            self.assertEqual(kwargs['image_id'],IMAGE)
            self.assertTrue(set(task['case']['original_tests']).issubset(repo.test_files))
            self.assertEqual(set(kwargs['test_targets']),{p for p in repo.test_files if Path(p).name.startswith('test_')})
            self.assertEqual(any('p08_holdout' in p for p in repo.test_files),stage.endswith('holdout'))
            directory=Path(artifact_root);directory.mkdir(parents=True)
            sources=tuple((p,(root/p).read_text()) for p in repo.source_files)
            tests=tuple((p,(root/p).read_text()) for p in repo.test_files)
            digest=context_digest(sources,tests)
            failed=stage=='mutant_holdout' and mode!='no_detection'
            module=next(iter(task['validation']['holdout_tests'])).removesuffix('.py').replace('/','.')
            if mode=='wrong_node': module='tests.test_old'
            body='<failure message="AssertionError: assert 0 == 1">AssertionError</failure>' if failed else ''
            if mode=='misleading_exception' and failed:
                body='<failure type="ValueError" message="must assert input">ValueError: must assert input</failure>'
            if mode=='didnotraise' and failed:
                body='<failure message="Failed: DID NOT RAISE ValueError">with pytest.raises(ValueError):</failure>'
            if mode=='collection' and stage=='mutant_holdout': body='<error>ImportError</error>'
            xml=f'<testsuite><testcase classname="{module}" name="test_hold">{body}</testcase></testsuite>'.encode()
            (directory/'junit.xml').write_bytes(xml)
            files={p:{'executed_lines':[1],'missing_lines':[],'executed_branches':[],'missing_branches':[]} for p in repo.source_files}
            if mode=='coverage': files={}
            (directory/'coverage.json').write_text(json.dumps({'files':files}))
            entries=sorted((p,hashlib.sha256((root/p).read_bytes()).hexdigest()) for p in repo.source_files+repo.test_files)
            (directory/'manifest.json').write_text(json.dumps({'image_id':IMAGE,'cleanup_ok':mode!='cleanup','files':entries}))
            run=classify_result(stage,digest,1 if body else 0,xml)
            if mode=='digest': run=replace(run,snapshot_digest='0'*64)
            return ExecutionResult(run,str(directory),mode!='cleanup')
        with patch('experiments.qualification_v2.run_tests',side_effect=runner):
            result=qualify(task,review,artifact_root=Path(folder))
        return result,calls

    def test_four_complete_suite_runs_qualify_without_model_calls(self):
        for mode in (None,'didnotraise'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                task,review=self.task();result,calls=self.execute(folder,task,review,mode)
                self.assertEqual(calls,['reference_original','reference_holdout','mutant_original','mutant_holdout'])
                self.assertEqual(result['status'],'qualified')
                self.assertEqual(result['model_calls'],0)
                self.assertIs(result['formal_ready'],False)

    def test_missing_stale_rejected_or_self_review_cannot_execute(self):
        for mode in ('digest','rejected','owner','schema','unknown'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                task,review=self.task()
                if mode=='digest': review['input_sha256']='0'*64
                elif mode=='rejected': review['status']='rejected'
                elif mode=='owner': review['reviewer']=task['provenance']['task']['owner']
                elif mode=='schema': review['schema']='p08-v1-review'
                else: review['extra']=True
                with patch('experiments.qualification_v2.run_tests') as runner,self.assertRaises(ValueError):
                    qualify(task,review,artifact_root=Path(folder))
                runner.assert_not_called()

    def test_bad_evidence_retains_all_four_runs_and_cannot_qualify(self):
        for mode in ('no_detection','wrong_node','collection','coverage','digest','cleanup','misleading_exception'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                task,review=self.task();result,calls=self.execute(folder,task,review,mode)
                self.assertEqual(len(calls),4)
                self.assertEqual(result['status'],'qualification_failed')

    def test_same_directory_is_never_silently_reexecuted(self):
        task,review=self.task()
        with tempfile.TemporaryDirectory() as folder:
            self.execute(folder,task,review)
            with patch('experiments.qualification_v2.run_tests') as runner,self.assertRaises(FileExistsError):
                qualify(task,review,artifact_root=Path(folder))
            runner.assert_not_called()
