from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from models.adapter import Completion
from repair.session import run_repair_session
from agents.repairer import context_digest
from execution.docker_runner import ExecutionResult
from workflow.state import Repository, TestRun, Failure, FailureKind


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'original'
        self.root.mkdir()
        (self.root / 'app.py').write_bytes(b'def f(x): return x > 1\n')
        (self.root / 'test_old.py').write_bytes(b'from app import f\ndef test_old(): assert f(2)\n')
        (self.root / 'test_new.py').write_bytes(b'from app import f\ndef test_new(): assert f(1)\n')
        self.repo = Repository('case', 'fixed', ('app.py',), ('test_old.py', 'test_new.py'))
        self.calls = []
        self.sequence = 0

    def runner(self, root, repo, *, image_id, artifact_root, **kwargs):
        self.sequence += 1
        files = tuple((p, (root / p).read_bytes().decode('utf-8')) for p in repo.source_files + repo.test_files)
        passing = repo.test_files == ('test_old.py',) or '>=' in (root / 'app.py').read_text()
        failure = None if passing else Failure(FailureKind.TEST_ASSERTION, 'assertion')
        run = TestRun('r' + str(self.sequence), context_digest(files, ()), 0 if passing else 1,
                      len(repo.test_files) if passing else 1, 0 if passing else 1, 0, failure=failure)
        directory = Path(artifact_root) / run.run_id
        directory.mkdir(parents=True)
        (directory / 'log.txt').write_text('visible assertion')
        return ExecutionResult(run, str(directory), True)

    def client(self, *, wrong=False):
        outer = self
        class Client:
            def complete(self, messages):
                data = json.loads(messages[1]['content'])
                outer.calls.append(data)
                source = data['sources'][0]
                content = 'def f(x): return x > 0\n' if wrong else 'def f(x): return x >= 1\n'
                return Completion(json.dumps({'task_id': data['task_id'], 'base_revision': data['base_revision'],
                    'based_on_run_id': data['based_on_run_id'], 'diagnosis': 'app.py:1 excludes endpoint; include it.',
                    'files': [{'path': 'app.py', 'content': content, 'expected_sha256': source['sha256']}]}), 'fake', None)
        return Client()

    def run_session(self, client):
        return run_repair_session(self.root, self.repo, original_tests=('test_old.py',),
            requirements=('f returns true for x >= 1',), holdout_tests=(('validation/test_private.py',
            'from app import f\ndef test_private(): assert not f(0)\n'),), client=client,
            image_id='sha256:' + '0'*64, artifact_root=Path(self.temp.name) / 'artifacts')

    def test_success_and_no_holdout_in_model_context(self):
        before = (self.root / 'app.py').read_bytes()
        with patch('repair.session.run_tests', side_effect=self.runner):
            result = self.run_session(self.client())
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(len(result['attempts']), 1)
        self.assertNotIn('test_private', json.dumps(self.calls))
        self.assertEqual((self.root / 'app.py').read_bytes(), before)

    def test_three_failed_patches_rollback_and_refresh_failure_ids(self):
        with patch('repair.session.run_tests', side_effect=self.runner):
            result = self.run_session(self.client(wrong=True))
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len({c['based_on_run_id'] for c in self.calls}), 3)
        self.assertTrue(all(a['rollback_ok'] for a in result['attempts']))
        self.assertEqual(len({c['sources'][0]['sha256'] for c in self.calls}), 1)

    def test_holdout_failure_stops_without_model_feedback(self):
        def execute(root, repo, **kwargs):
            result = self.runner(root, repo, **kwargs)
            if 'validation/test_private.py' in repo.test_files:
                failed = TestRun('private-fail', result.run.snapshot_digest, 1, 2, 1, 0,
                                 failure=Failure(FailureKind.TEST_ASSERTION, 'private detail'))
                return replace(result, run=failed)
            return result
        with patch('repair.session.run_tests', side_effect=execute):
            result = self.run_session(self.client())
        self.assertEqual(result['status'], 'independent_validation_failed')
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(result['attempts'][0]['rollback_ok'])

    def test_invalid_output_stops_after_one_request(self):
        class Client:
            calls = 0
            def complete(self, messages):
                self.calls += 1
                return Completion('{}', 'fake', None)
        client = Client()
        with patch('repair.session.run_tests', side_effect=self.runner):
            result = self.run_session(client)
        self.assertEqual(result['status'], 'invalid_output')
        self.assertEqual(client.calls, 1)
        self.assertIsNone(result['attempts'][0]['patch_diff'])

    def test_regression_is_classified_and_rolled_back(self):
        def execute(root, repo, **kwargs):
            result = self.runner(root, repo, **kwargs)
            if repo.test_files == ('test_old.py',) and 'x > 0' in (root / 'app.py').read_text():
                run = TestRun(result.run.run_id, result.run.snapshot_digest, 1, 0, 1, 0,
                              failure=Failure(FailureKind.TEST_ASSERTION, 'regression'))
                return replace(result, run=run)
            return result
        with patch('repair.session.run_tests', side_effect=execute):
            result = self.run_session(self.client(wrong=True))
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertTrue(all(a['classification'] == 'regression' and a['rollback_ok'] for a in result['attempts']))


if __name__ == '__main__':
    unittest.main()
