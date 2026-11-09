import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow.orchestrator import run_case, main, feedback_case
from execution.docker_runner import ExecutionResult
from workflow.state import TestRun, Failure, FailureKind
from models.adapter import ModelError


class OrchestratorTests(unittest.TestCase):
    def test_feedback_inherits_all_parent_generated_tests(self):
        parent = {'task_id': 'parent', 'status': 'verified',
                  'generation': {'files': [{'path': 'tests/generated/test_parent.py', 'content': 'def test_parent(): assert True'}]},
                  'repair': {'final_sources': [('app.py', 'def f(x): return x >= 1\n')],
                             'attempts': [{'visible_run': {'run': {'snapshot_digest': 'parent-snapshot'}}}]}}
        child = feedback_case(self.case(), parent)
        self.assertIn('test_old.py', child['original_tests'])
        self.assertIn('tests/generated/test_parent.py', child['original_tests'])
        self.assertEqual(child['parent_task_id'], 'parent')
        self.assertEqual(child['expected_parent_snapshot'], 'parent-snapshot')

    def test_feedback_parent_snapshot_mismatch_stops_before_generation(self):
        passed = TestRun('r', 'different', 0, 1, 0, 0)
        with tempfile.TemporaryDirectory() as folder:
            with patch('workflow.orchestrator.run_tests', return_value=ExecutionResult(passed, folder, True)):
                result = run_case(dict(self.case(), expected_parent_snapshot='parent-snapshot'), {}, client=None,
                                  image_id='unused', artifact_root=Path(folder))
        self.assertEqual(result['status'], 'parent_snapshot_mismatch')

    def test_feedback_failure_and_cancellation_export_current_report(self):
        for status in ('feedback_tests_failed', 'cancelled'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                export = root / 'export.json'
                export.write_text('{"stale": true}', encoding='utf-8')
                parent = {'case_id': 'age_threshold', 'task_id': 'parent', 'status': 'verified'}
                child = {'case_id': 'age_threshold-coverage-child', 'status': status}
                with patch('sys.argv', ['orchestrator', '--call', '--image-id', 'unused',
                           '--feedback', '--artifacts', str(root), '--export', str(export)]), \
                     patch('workflow.orchestrator.load_cases', return_value=(
                         [{'id': 'age_threshold'}], {'age_threshold': {}}, {})), \
                     patch('workflow.orchestrator.Config.from_env'), \
                     patch('workflow.orchestrator.ModelClient') as client, \
                     patch('workflow.orchestrator.feedback_case', return_value={'id': child['case_id']}), \
                     patch('workflow.orchestrator.run_case', side_effect=[parent, child]):
                    client.return_value.list_models.return_value = [client.return_value.config.model]
                    self.assertEqual(main(), 1)
                report = json.loads(export.read_text(encoding='utf-8'))
                self.assertEqual(report['feedback_child']['status'], status)
                self.assertEqual(report, json.loads((root / 'latest-summary.json').read_text(encoding='utf-8')))

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

    def test_cancel_after_model_response_is_recorded_before_other_stages(self):
        flag = {'cancelled': False}
        class Client:
            def complete(self, messages):
                flag['cancelled'] = True
                raise ModelError('environment', 'network failed')
        passed = TestRun('r', 'd', 0, 1, 0, 0)
        with tempfile.TemporaryDirectory() as folder:
            with patch('workflow.orchestrator.run_tests', return_value=ExecutionResult(passed, folder, True)) as execute:
                result = run_case(self.case(), {}, client=Client(), image_id='unused',
                                  artifact_root=Path(folder), cancelled=lambda: flag['cancelled'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(execute.call_count, 1)

    def test_bad_baseline_never_calls_model(self):
        failed = TestRun('r', 'd', 125, 0, 0, 0, failure=Failure(FailureKind.ENVIRONMENT, 'unavailable'))
        with tempfile.TemporaryDirectory() as folder:
            with patch('workflow.orchestrator.run_tests', return_value=ExecutionResult(failed, folder, True)):
                result = run_case(self.case(), {}, client=None, image_id='unused', artifact_root=Path(folder))
        self.assertEqual(result['status'], 'baseline_failed')
        self.assertEqual(result['events'][-1]['phase'], 'failed')

    def test_required_coverage_must_exist_before_model_call(self):
        passed = TestRun('r', 'd', 0, 1, 0, 0)
        with tempfile.TemporaryDirectory() as folder:
            with patch('workflow.orchestrator.run_tests', return_value=ExecutionResult(passed, folder, True)):
                result = run_case(dict(self.case(), require_coverage=True), {}, client=None,
                                  image_id='unused', artifact_root=Path(folder))
        self.assertEqual(result['status'], 'coverage_unavailable')

    def test_cancelled_batch_does_not_start_later_tasks_or_load_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / 'cancel.flag'
            flag.touch()
            with patch('sys.argv', ['orchestrator', '--call', '--image-id', 'unused',
                       '--cancel-file', str(flag), '--artifacts', folder]), \
                 patch('workflow.orchestrator.load_cases', return_value=(
                     [{'id': name} for name in ('age_threshold', 'take_first', 'sum_inclusive')],
                     {name: {} for name in ('age_threshold', 'take_first', 'sum_inclusive')}, {})), \
                 patch('workflow.orchestrator.Config.from_env') as config, \
                 patch('workflow.orchestrator.run_case', return_value={'case_id': 'age_threshold', 'status': 'cancelled'}) as execute:
                self.assertEqual(main(), 1)
                config.assert_not_called()
                self.assertEqual(execute.call_count, 1)


if __name__ == '__main__':
    unittest.main()
