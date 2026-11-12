import copy
import hashlib
import json
from pathlib import Path
import unittest

from agents.repairer import context_digest
from experiments.freeze import (make_protocol, select_task_ids, validate_freeze,
                                screening_report_results, frozen_file_digest)
from experiments.screening import review_digest


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


class FreezeTests(unittest.TestCase):
    def test_code_hashes_survive_git_line_endings_but_detect_code_changes(self):
        lf, crlf = b'def f():\n    return 1\n', b'def f():\r\n    return 1\r\n'
        self.assertEqual(frozen_file_digest(lf, 'lf'), frozen_file_digest(crlf, 'lf'))
        self.assertNotEqual(frozen_file_digest(lf, 'raw'), frozen_file_digest(crlf, 'raw'))
        self.assertNotEqual(frozen_file_digest(lf, 'lf'), frozen_file_digest(lf.replace(b'1', b'2'), 'lf'))
        with self.assertRaises(ValueError): frozen_file_digest(lf, 'ignore_changes')

    def test_original_completed_report_labels_are_preserved(self):
        for status in ('complete', 'screening_complete_with_quota_shortfall', 'second_pool_complete_with_quota_shortfall', 'third_pool_complete_with_quota_shortfall'):
            self.assertEqual(screening_report_results({'status': status, 'results': []}), [])
        for status in ('running', 'claimed', 'looks_complete'):
            with self.assertRaises(ValueError): screening_report_results({'status': status, 'results': []})

    def fixtures(self):
        runtime = json.loads((Path(__file__).parents[1]/'experiments/p08b-candidates-v6-public.json').read_text())['tasks'][0]['protocol']
        tasks, rows = [], []
        for project in ('click', 'dateutil', 'itsdangerous', 'jinja', 'markupsafe'):
            for kind in ('boundary', 'exception_handling', 'null_type'):
                for index in range(2):
                    identity = f'{project}_{kind}_{index}'
                    path, name = f'src/{project}/demo.py', f'f_{kind}_{index}'
                    correct, mutant = f'def {name}(): return 1\n', f'def {name}(): return 0\n'
                    oldtests = {'tests/test_old.py': 'def test_old(): assert True\n'}
                    holdout = {f'tests/p08_holdout/test_{identity}.py': 'def test_hold(): assert False\n'}
                    inventory = {'sources': {path: sha(correct)}, 'tests': {p: sha(v) for p, v in oldtests.items()}, 'support': {}, 'excluded': [], 'provenance': {}}
                    public = {'id': identity, 'project_id': project, 'origin': 'injected', 'target': {'path': path, 'qualified_name': name}, 'requirements': ['Return one'], 'requirement_sources': [], 'coverage_sources': [path], 'inventory': inventory, 'protocol': copy.deepcopy(runtime), 'owner': 'author', 'reviewer': 'reviewer'}
                    evaluation = {'defect_type': kind, 'reference_source_hashes': {path: sha(correct)}, 'mutant_source_hashes': {path: sha(mutant)}, 'mutation': {'old': '1', 'new': '0', 'line': 1}, 'diff': 'reviewed diff', 'holdout_tests': {p: {'content': v, 'sha256': sha(v)} for p, v in holdout.items()}, 'expected_failure': 'assertion', 'screening': {'status': 'pending', 'evidence': []}}
                    task = {'case': {'id': identity, 'sources': {path: mutant}, 'original_tests': oldtests, 'support': {}, 'target': (path, name), 'requirements': ['Return one'], 'coverage_sources': (path,)}, 'validation': {'reference_sources': {path: correct}, 'holdout_tests': holdout}, 'provenance': {'task': public, 'source_manifest': inventory}, 'evaluation': evaluation}
                    digest = review_digest(task)
                    row = {'candidate_id': identity, 'project_id': project, 'target': [path, name], 'defect_type': kind, 'status': 'eligible', 'review_input_sha256': digest, 'semantic_review': {'status': 'approved', 'reviewer': 'reviewer', 'notes': 'Independent contract review', 'input_sha256': digest}, 'image_id': runtime['image_id'], 'executions': []}
                    for repeat in (1, 2):
                        for stage in ('reference_original', 'reference_holdout', 'mutant_original', 'mutant_holdout'):
                            sources = task['validation']['reference_sources'] if stage.startswith('reference') else task['case']['sources']
                            tests = dict(oldtests)
                            if stage.endswith('holdout'): tests.update(holdout)
                            failed = stage == 'mutant_holdout'
                            run = {'run_id': stage+str(repeat), 'snapshot_digest': context_digest(tuple(sources.items()), tuple(tests.items())), 'exit_code': 1 if failed else 0, 'passed': 0 if failed else 1, 'failed': 1 if failed else 0, 'errors': 0, 'skipped': 0, 'failure': {'kind': 'test_assertion', 'summary': 'assertion'} if failed else None}
                            row['executions'].append({'stage': f'{stage}_{repeat}', 'result': {'run': run, 'cleanup_ok': True}})
                    tasks.append(task); rows.append(row)
        protocol = make_protocol([t['case']['id'] for t in tasks], source_commit='a'*40)
        return tasks, protocol, {'results': rows}

    def test_valid_freeze_is_pure_and_has_thirty_unique_tasks(self):
        tasks, protocol, screening = self.fixtures()
        before = copy.deepcopy((tasks, protocol, screening))
        result = validate_freeze(tasks, protocol, screening)
        self.assertEqual(result['task_count'], 30)
        self.assertEqual(result['planned_records'], 360)
        self.assertEqual((tasks, protocol, screening), before)

    def test_missing_task_duplicate_function_and_bad_quota_rejected(self):
        for mode in ('missing', 'duplicate_function', 'quota', 'pilot', 'duplicate_id'):
            with self.subTest(mode=mode):
                tasks, protocol, screening = self.fixtures()
                if mode == 'missing': tasks.pop()
                elif mode == 'duplicate_function': tasks[1]['case']['target'] = tasks[0]['case']['target']
                elif mode == 'quota': tasks[0]['evaluation']['defect_type'] = 'null_type'
                elif mode == 'pilot': tasks[-1]['case']['target'] = ('src/markupsafe/demo.py', 'escape_silent')
                else: tasks[1]['case']['id'] = tasks[0]['case']['id']
                with self.assertRaises(ValueError): validate_freeze(tasks, protocol, screening)

    def test_noneligible_stale_or_missing_review_and_input_hash_rejected(self):
        for mode in ('status', 'missing_review', 'self_review', 'review_hash', 'row_hash', 'source', 'oldtests', 'support', 'holdout'):
            with self.subTest(mode=mode):
                tasks, protocol, screening = self.fixtures(); row = screening['results'][0]
                if mode == 'status': row['status'] = 'unstable'
                elif mode == 'missing_review': row['semantic_review'] = None
                elif mode == 'self_review': row['semantic_review']['reviewer'] = 'author'
                elif mode == 'review_hash': row['semantic_review']['input_sha256'] = '0'*64
                elif mode == 'row_hash': row['review_input_sha256'] = '0'*64
                elif mode == 'source': tasks[0]['case']['sources'][next(iter(tasks[0]['case']['sources']))] += '# changed\n'
                elif mode == 'oldtests': tasks[0]['case']['original_tests'] = {}
                elif mode == 'support': tasks[0]['case']['support']['data.txt'] = b'new'
                else: tasks[0]['validation']['holdout_tests'][next(iter(tasks[0]['validation']['holdout_tests']))] += '# changed\n'
                with self.assertRaises(ValueError): validate_freeze(tasks, protocol, screening)

    def test_method_order_repeat_and_runtime_changes_rejected(self):
        for mode in ('method', 'duplicate_method', 'order', 'repeats', 'worker', 'seed', 'source_commit', 'unknown'):
            with self.subTest(mode=mode):
                tasks, protocol, screening = self.fixtures()
                if mode == 'method': protocol['methods'][0]['id'] = 'unknown'
                elif mode == 'duplicate_method': protocol['methods'][1] = protocol['methods'][0]
                elif mode == 'order': protocol['task_order'][1] = protocol['task_order'][0]
                elif mode == 'repeats': protocol['repetitions'] = 2
                elif mode == 'worker': protocol['runtime']['worker_sha256'] = '0'*64
                elif mode == 'seed': protocol['runtime']['seed'] = 42
                elif mode == 'source_commit': protocol['source_commit'] = 'not-a-commit'
                else: protocol['extra'] = 'unexpected'
                with self.assertRaises(ValueError): validate_freeze(tasks, protocol, screening)

    def test_missing_second_round_cleanup_failure_or_snapshot_drift_rejected(self):
        for mode in ('missing_round', 'cleanup', 'digest', 'original_failure', 'not_detected', 'changed_counts'):
            with self.subTest(mode=mode):
                tasks, protocol, screening = self.fixtures(); executions = screening['results'][0]['executions']
                if mode == 'missing_round': executions.pop()
                elif mode == 'cleanup': executions[0]['result']['cleanup_ok'] = False
                elif mode == 'digest': executions[0]['result']['run']['snapshot_digest'] = '0'*64
                elif mode == 'original_failure': executions[0]['result']['run']['failed'] = 1
                elif mode == 'not_detected': executions[3]['result']['run']['failed'] = 0
                else: executions[4]['result']['run']['passed'] += 1
                with self.assertRaises(ValueError): validate_freeze(tasks, protocol, screening)

    def test_distinct_run_artifact_links_are_not_behavior_instability(self):
        tasks, protocol, screening = self.fixtures()
        for row in screening['results']:
            for execution in row['executions']:
                failure = execution['result']['run']['failure']
                if failure:
                    failure['evidence'] = [execution['stage']+'/junit.xml']
        self.assertEqual(validate_freeze(tasks, protocol, screening)['task_count'], 30)

    def test_selection_is_fixed_and_does_not_use_model_performance(self):
        tasks, protocol, screening = self.fixtures()
        rows = screening['results']
        extra = copy.deepcopy(rows[0]); extra['candidate_id'] = 'zz_extra'; extra['target'][1] = 'zz_extra'
        rows.append(extra)
        selected = select_task_ids(rows)
        self.assertEqual(selected, tuple(protocol['task_order']))
        self.assertEqual(select_task_ids(list(reversed(rows))), selected)
        rows.append(copy.deepcopy(rows[0]))
        with self.assertRaises(ValueError): select_task_ids(rows)


if __name__ == '__main__':
    unittest.main()
