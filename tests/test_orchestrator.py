import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow.orchestrator import run_case
from execution.docker_runner import ExecutionResult
from workflow.state import TestRun, Failure, FailureKind


class OrchestratorTests(unittest.TestCase):
    def case(self):
        return {'id': 'sample', 'sources': {'app.py': 'def f(x): return x > 1\n'},
                'original_tests': {'test_old.py': 'from app import f\ndef test_old(): assert f(2)\n'},
                'requirements': ['f(x) is true exactly when x >= 1.']}

    def test_cancelled_before_execution_never_calls_model(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('workflow.orchestrator.run_tests') as execute:
                result = run_case(self.case(), {}, client=None, image_id='unused',
                                  artifact_root=Path(folder), cancelled=lambda: True)
        self.assertEqual(result['status'], 'cancelled')
        execute.assert_not_called()

    def test_bad_baseline_never_calls_model(self):
        failed = TestRun('r', 'd', 125, 0, 0, 0, failure=Failure(FailureKind.ENVIRONMENT, 'unavailable'))
        with tempfile.TemporaryDirectory() as folder:
            with patch('workflow.orchestrator.run_tests', return_value=ExecutionResult(failed, folder, True)):
                result = run_case(self.case(), {}, client=None, image_id='unused', artifact_root=Path(folder))
        self.assertEqual(result['status'], 'baseline_failed')
        self.assertEqual(result['events'][-1]['phase'], 'failed')


if __name__ == '__main__':
    unittest.main()
