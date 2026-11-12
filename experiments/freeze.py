"""Read-only admission gate for thirty screened tasks. Never calls a model."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from agents.repairer import context_digest
from execution.proposals import checked_path
from experiments.formal_tasks import IMAGE, _protocol, load_formal_tasks
from experiments.project_tasks import _fields
from experiments.screening import review_digest
from experiments.tasks import _read
from repair.fixtures import PROJECT_ROOT

PROJECTS = ('click', 'dateutil', 'itsdangerous', 'jinja', 'markupsafe')
TYPES = ('boundary', 'exception_handling', 'null_type')
METHODS = [
    {'id': 'B0', 'generation_rounds': 0, 'repair_attempts': 0, 'coverage_feedback': False},
    {'id': 'B1', 'generation_rounds': 1, 'repair_attempts': 1, 'coverage_feedback': False},
    {'id': 'B2', 'generation_rounds': 2, 'repair_attempts': 3, 'coverage_feedback': True},
    {'id': 'A0', 'generation_rounds': 2, 'repair_attempts': 3, 'coverage_feedback': False},
]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def frozen_file_digest(data, normalization):
    if normalization not in ('raw', 'lf'):
        raise ValueError('unknown frozen file hash normalization')
    return _sha(data.replace(b'\r\n', b'\n') if normalization == 'lf' else data)


def _canonical(value):
    return _sha(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode())


def _hashes(files):
    return {p: _sha(v.encode('utf-8') if isinstance(v, str) else v) for p, v in files.items()}


def make_protocol(task_ids, *, source_commit):
    runtime = {'image_id': IMAGE,
               'dependency_lock_sha256': _sha((PROJECT_ROOT/'execution/requirements-p08.lock').read_bytes()),
               'worker_sha256': _sha((PROJECT_ROOT/'execution/container_worker.py').read_bytes().replace(b'\r\n', b'\n')),
               'context_version': 'p08-context-v1', 'repair_version': 'p08-scoped-repair-v1',
               'model': 'deepseek-v4-flash-ascend', 'temperature': 0.2, 'thinking_mode': 'disabled',
               'max_tokens': 2048, 'input_characters': 16000, 'timeout_seconds': 60}
    return {'schema': 'p08-protocol-v1', 'source_commit': source_commit,
            'selection_rule': 'project_type_path_function_candidate_first_two_distinct',
            'methods': json.loads(json.dumps(METHODS)), 'repetitions': 3,
            'task_order': sorted(task_ids), 'execution_order': 'repeat_task_rotating_methods_v1',
            'runtime': runtime, 'http_timeout_seconds': 300, 'api_seed': None}


def select_task_ids(results):
    """Apply the declared ordering to all pools, never a model success metric."""
    ids, seen, counts, selected = set(), set(), {}, []
    for row in results:
        identity = row['candidate_id']
        if not isinstance(identity, str) or identity in ids:
            raise ValueError('duplicate or invalid screening identity')
        ids.add(identity)
    eligible = [r for r in results if r['status'] == 'eligible']
    eligible.sort(key=lambda r: (r['project_id'], r['defect_type'], *r['target'], r['candidate_id']))
    for row in eligible:
        project, kind = row['project_id'], row['defect_type']
        target = row['target']
        if project not in PROJECTS or kind not in TYPES or not isinstance(target, (tuple, list)) or len(target) != 2:
            raise ValueError('unknown project, type or function in screening')
        function = (project, *target)
        slot = (project, kind)
        if counts.get(slot, 0) < 2 and function not in seen:
            selected.append(row['candidate_id']); seen.add(function)
            counts[slot] = counts.get(slot, 0) + 1
    if any(counts.get((p, k)) != 2 for p in PROJECTS for k in TYPES):
        raise ValueError('screened different-function quotas not satisfied')
    return tuple(sorted(selected))


def screening_report_results(report):
    # The first three accepted exports retain their original descriptive labels.
    if (report.get('status') not in ('complete', 'screening_complete_with_quota_shortfall',
                                   'second_pool_complete_with_quota_shortfall',
                                   'third_pool_complete_with_quota_shortfall')
            or not isinstance(report.get('results'), list)):
        raise ValueError('incomplete screening report')
    return report['results']


def _inputs(task):
    case, validation = task['case'], task['validation']
    public, evaluation = task['provenance']['task'], task['evaluation']
    _fields(case, 'id sources original_tests support target requirements coverage_sources')
    _fields(validation, 'reference_sources holdout_tests')
    inventory = public['inventory']
    target = (public['target']['path'], public['target']['qualified_name'])
    if (case['id'] != public['id'] or tuple(case['target']) != target
            or case['requirements'] != public['requirements'] or public['origin'] != 'injected'
            or inventory != task['provenance']['source_manifest']
            or tuple(case['coverage_sources']) != tuple(public['coverage_sources'])):
        raise ValueError('loaded case no longer matches public provenance')
    if public['project_id'] == 'markupsafe' and target[1] == 'escape_silent':
        raise ValueError('pilot target is not a formal task')
    if (_hashes(case['sources']) != evaluation['mutant_source_hashes']
            or _hashes(validation['reference_sources']) != evaluation['reference_source_hashes']
            or evaluation['reference_source_hashes'] != inventory['sources']
            or _hashes(case['original_tests']) != inventory['tests']
            or _hashes(case['support']) != inventory['support']):
        raise ValueError('source, regression or support inventory changed')
    holdout = evaluation['holdout_tests']
    if (set(validation['holdout_tests']) != set(holdout)
            or any(holdout[p]['content'] != text or holdout[p]['sha256'] != _sha(text.encode())
                   for p, text in validation['holdout_tests'].items())):
        raise ValueError('private holdout inventory changed')
    _protocol(public['protocol'])


def _screening(task, row):
    public, case = task['provenance']['task'], task['case']
    digest = review_digest(task)
    review = row.get('semantic_review') or {}
    if (row['status'] != 'eligible' or row['project_id'] != public['project_id']
            or tuple(row['target']) != tuple(case['target'])
            or row['defect_type'] != task['evaluation']['defect_type']
            or row['image_id'] != public['protocol']['image_id']
            or row['review_input_sha256'] != digest
            or review.get('status') != 'approved' or review.get('input_sha256') != digest
            or review.get('reviewer') != public['reviewer']
            or public['reviewer'].strip().casefold() == public['owner'].strip().casefold()
            or not isinstance(review.get('notes'), str) or not review['notes'].strip()):
        raise ValueError('screening or independent semantic review missing or stale')
    labels = ('reference_original', 'reference_holdout', 'mutant_original', 'mutant_holdout')
    executions = row['executions']
    if [e['stage'] for e in executions] != [f'{label}_{repeat}' for repeat in (1, 2) for label in labels]:
        raise ValueError('two complete ordered screening rounds required')
    signatures = {}
    for execution in executions:
        label, repeat = execution['stage'].rsplit('_', 1)
        result, tests = execution['result'], dict(case['original_tests'])
        sources = task['validation']['reference_sources'] if label.startswith('reference') else case['sources']
        if label.endswith('holdout'): tests.update(task['validation']['holdout_tests'])
        expected = context_digest(tuple(sources.items()), tuple(tests.items()), tuple(_hashes(case['support']).items()))
        run = result['run']
        if (result['cleanup_ok'] is not True or run['snapshot_digest'] != expected
                or any(type(run[k]) is not int or run[k] < 0 for k in ('passed', 'failed', 'errors', 'skipped'))):
            raise ValueError('screening snapshot, cleanup or counts invalid')
        if label == 'mutant_holdout':
            if (run['exit_code'] != 1 or run['failed'] < 1 or run['errors'] != 0
                    or not run['failure'] or run['failure']['kind'] != 'test_assertion'):
                raise ValueError('private assertion detection required')
        elif (run['exit_code'] != 0 or run['failed'] != 0 or run['errors'] != 0
              or run['passed'] < 1 or run['failure'] is not None):
            raise ValueError('correct version and original regressions must pass')
        # Evidence links contain distinct run IDs. Match the original screening
        # signature's failure kind; complete JUnit bodies are bound separately.
        signature = tuple(run[k] for k in ('snapshot_digest', 'exit_code', 'passed', 'failed', 'errors', 'skipped')) + (run['failure']['kind'] if run['failure'] else None,)
        if repeat == '2' and signatures[label] != signature:
            raise ValueError('screening round signatures changed')
        signatures[label] = signature


def validate_freeze(tasks, protocol, screening):
    """Validate T1-loaded tasks against all six immutable T2 reports.

    Report hashes and full JUnit identities are separately reviewed/bound in the
    freeze evidence; this pure gate verifies declared runs without local artifacts.
    Return only checksums/counts, never a model input or private test content.
    """
    try:
        return _validate(tasks, protocol, screening)
    except (KeyError, TypeError, AttributeError, IndexError):
        raise ValueError('invalid freeze inputs') from None


def _validate(tasks, protocol, screening):
    if not isinstance(tasks, (tuple, list)) or len(tasks) != 30:
        raise ValueError('exactly thirty loaded tasks required')
    _fields(protocol, 'schema source_commit selection_rule methods repetitions task_order execution_order runtime http_timeout_seconds api_seed')
    if not isinstance(protocol['source_commit'], str) or not re.fullmatch('[a-f0-9]{40}', protocol['source_commit']):
        raise ValueError('fixed source Git commit required')
    identities = [t['case']['id'] for t in tasks]
    if len(set(identities)) != 30:
        raise ValueError('duplicate formal task identity')
    expected_protocol = make_protocol(identities, source_commit=protocol['source_commit'])
    if _canonical(protocol) != _canonical(expected_protocol):
        raise ValueError('method, order or runtime protocol changed')
    selected = select_task_ids(screening['results'])
    if tuple(sorted(identities)) != selected:
        raise ValueError('tasks differ from fixed first-two selection')
    rows = {r['candidate_id']: r for r in screening['results']}
    functions, counts, checksums = set(), {}, []
    for task in sorted(tasks, key=lambda t: t['case']['id']):
        _inputs(task)
        public, evaluation = task['provenance']['task'], task['evaluation']
        function = (public['project_id'], *task['case']['target'])
        if function in functions:
            raise ValueError('same function cannot fill another task or category')
        functions.add(function)
        slot = (public['project_id'], evaluation['defect_type'])
        counts[slot] = counts.get(slot, 0) + 1
        _screening(task, rows[task['case']['id']])
        checksums.append({'task_id': task['case']['id'], 'input_sha256': review_digest(task),
                          'inventory_sha256': _canonical(public['inventory'])})
    if any(counts.get((p, k)) != 2 for p in PROJECTS for k in TYPES):
        raise ValueError('five projects each require two tasks per defect type')
    return {'schema': 'p08-freeze-check-v1', 'task_count': 30, 'unique_functions': 30,
            'planned_records': 360, 'model_calls': 0, 'protocol_sha256': _canonical(protocol),
            'task_checksums': checksums,
            'quota_matrix': [{'project_id': p, 'defect_type': k, 'count': counts[p, k]} for p in PROJECTS for k in TYPES]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public', type=Path, default=PROJECT_ROOT/'experiments/p08b-tasks.json')
    parser.add_argument('--evaluation', type=Path, default=PROJECT_ROOT/'validation/p08b-evaluation.json')
    parser.add_argument('--protocol', type=Path, default=PROJECT_ROOT/'experiments/p08b-protocol.json')
    args = parser.parse_args()
    evidence = _read(PROJECT_ROOT/'evidence/p08b-freeze.json')
    for entry in evidence['files']:
        path = checked_path(PROJECT_ROOT, entry['path'])
        if frozen_file_digest(path.read_bytes(), entry['normalization']) != entry['sha256']:
            raise ValueError('frozen input or implementation file changed: '+entry['path'])
    rows = []
    for entry in evidence['screening_reports']:
        path = checked_path(PROJECT_ROOT, entry['path'])
        if _sha(path.read_bytes()) != entry['sha256']:
            raise ValueError('screening report changed')
        rows.extend(screening_report_results(_read(path)))
    tasks = load_formal_tasks(args.public, args.evaluation, catalog_path=PROJECT_ROOT/'experiments/projects.json',
                              reference_root=PROJECT_ROOT/'reference/p08', resource_root=PROJECT_ROOT/'artifacts/p08a/image/wheels')
    checks = validate_freeze(tasks, _read(args.protocol), {'results': rows})
    if checks != evidence['validation']:
        raise ValueError('freeze validation differs from recorded evidence')
    print('Validated 30 frozen tasks, 15 quotas and protocol; no model calls.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
