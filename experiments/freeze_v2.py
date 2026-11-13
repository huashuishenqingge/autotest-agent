"""Read-only V2 candidate gate. Does not qualify, freeze, schedule, or call models."""
import argparse
import copy
import json
from pathlib import Path
import re

from experiments.formal_tasks_v2 import load_formal_tasks, validate_documents, _canonical
from experiments.freeze import METHODS, PROJECTS, TYPES, _hashes
from experiments.project_tasks import _fields
from experiments.runtime_v2 import expected_runtime, validate_runtime
from experiments.screening import review_digest
from experiments.tasks import _read
from repair.fixtures import PROJECT_ROOT


def make_candidate_protocol(task_ids, *, source_commit):
    return {'schema': 'p08-protocol-v2-candidate', 'status': 'candidate_not_frozen',
            'source_commit': source_commit, 'selection_rule': 'unchanged_V1_thirty_tasks',
            'methods': copy.deepcopy(METHODS), 'repetitions': 3,
            'task_order': sorted(task_ids), 'execution_order': 'repeat_task_rotating_methods_v1',
            'runtime': expected_runtime(), 'http_timeout_seconds': 300, 'api_seed': None}


def _loaded_inputs(task):
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
        raise ValueError('loaded V2 case no longer matches provenance')
    if (_hashes(case['sources']) != evaluation['mutant_source_hashes']
            or _hashes(validation['reference_sources']) != evaluation['reference_source_hashes']
            or evaluation['reference_source_hashes'] != inventory['sources']
            or _hashes(case['original_tests']) != inventory['tests']
            or _hashes(case['support']) != inventory['support']):
        raise ValueError('V2 source, regression or support inventory changed')
    holdout = evaluation['holdout_tests']
    if (set(validation['holdout_tests']) != set(holdout)
            or any(holdout[p]['content'] != text or holdout[p]['sha256'] != _hashes({p: text})[p]
                   for p, text in validation['holdout_tests'].items())):
        raise ValueError('V2 private holdout inventory changed')
    validate_runtime(public['protocol'])


def check_candidate(tasks, protocol):
    try:
        return _check_candidate(tasks, protocol)
    except (KeyError, TypeError, AttributeError, IndexError, RecursionError):
        raise ValueError('invalid V2 candidate inputs') from None


def _check_candidate(tasks, protocol):
    if not isinstance(tasks, (list, tuple)) or len(tasks) != 30:
        raise ValueError('exactly thirty V2 candidates required')
    _fields(protocol, 'schema status source_commit selection_rule methods repetitions task_order execution_order runtime http_timeout_seconds api_seed')
    commit = protocol['source_commit']
    if not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('fixed candidate source commit required')
    ids = [t['case']['id'] for t in tasks]
    if len(set(ids)) != 30:
        raise ValueError('duplicate V2 candidate identity')
    expected = make_candidate_protocol(ids, source_commit=commit)
    if _canonical(protocol) != _canonical(expected):
        raise ValueError('V2 candidate protocol differs from approved parameters')
    validate_runtime(protocol['runtime'])
    public = {'schema': 'p08-formal-v2', 'tasks': [t['provenance']['task'] for t in tasks]}
    private = {'schema': 'p08-formal-v2', 'tasks': {t['case']['id']: t['evaluation'] for t in tasks}}
    validate_documents(public, private)
    functions, counts, checksums = set(), {}, []
    for task in tasks:
        _loaded_inputs(task)
        item, evaluation = task['provenance']['task'], task['evaluation']
        function = (item['project_id'], *task['case']['target'])
        if function in functions:
            raise ValueError('same function in multiple V2 tasks')
        functions.add(function)
        key = (item['project_id'], evaluation['defect_type'])
        counts[key] = counts.get(key, 0) + 1
        checksums.append({'task_id': task['case']['id'], 'input_sha256': review_digest(task)})
    if set(counts) != {(p, k) for p in PROJECTS for k in TYPES} or any(v != 2 for v in counts.values()):
        raise ValueError('V2 five-project category quotas changed')
    return {'schema': 'p08-v2-candidate-check-v1', 'status': 'candidate_inputs_consistent',
            'formal_ready': False, 'qualification': 'pending_V2_02',
            'semantic_review': 'pending_V2_02_03', 'task_count': 30, 'unique_functions': 30,
            'planned_records': 360, 'model_calls': 0,
            'protocol_sha256': _canonical(protocol), 'task_checksums': checksums}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-candidate', action='store_true', required=True)
    parser.add_argument('--public', type=Path, default=PROJECT_ROOT/'experiments/p08b-v2-tasks.json')
    parser.add_argument('--evaluation', type=Path, default=PROJECT_ROOT/'validation/p08b-v2-evaluation.json')
    parser.add_argument('--protocol', type=Path, default=PROJECT_ROOT/'experiments/p08b-v2-protocol-candidate.json')
    args = parser.parse_args(argv)
    tasks = load_formal_tasks(args.public, args.evaluation, catalog_path=PROJECT_ROOT/'experiments/projects.json',
                              reference_root=PROJECT_ROOT/'reference/p08', resource_root=PROJECT_ROOT/'artifacts/p08a/image/wheels')
    print(json.dumps(check_candidate(tasks, _read(args.protocol)), ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
