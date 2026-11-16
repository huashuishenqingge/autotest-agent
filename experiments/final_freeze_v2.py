"""Verify frozen V2 inputs and reviewed qualification. Never launches experiments."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from agents.repairer import context_digest
from execution.proposals import checked_path
from experiments.formal_tasks_v2 import _canonical, load_formal_tasks
from experiments.freeze_v2 import make_candidate_protocol, check_candidate
from experiments.project_tasks import _fields
from experiments.qualification_v2 import STAGES, validate_review
from experiments.runtime_v2 import IMAGE
from experiments.screening import review_digest
from experiments.tasks import _read
from repair.fixtures import PROJECT_ROOT

EVIDENCE_HASHES = (
    ('evidence/p08b-v2-02-qualification.json', 'aaa9dabeef3bc7cdd43c0c7d461872e61ab65ce53376ec91e50a713ab06a7ad2'),
    ('evidence/p08b-v2-02-execution-index.json', 'd1e428b10d703c6fe5ac9e6e2e0ad1dd0ac9bdefcf9660a25fe4d315e921b1c1'),
    ('evidence/p08b-v2-02-semantic-reviews.json', 'dafc9454fda81dd9e4d71287ba1b1dde5c93f73ddce97ab8eebaf9bd5018a5f0'),
    ('evidence/p08b-v2-02-evidence-review.json', '4cf1983f2054ffa644e88d269bfa457bfe68162641e8d3ba2a91a07d8fc7559e'),
)
# Explicit path set is filled below at implementation time, never from a caller.
BOUND_PATHS = ('agents/__init__.py', 'agents/generator.py', 'agents/repairer.py', 'evaluation/__init__.py', 'evaluation/coverage.py', 'evidence/p08b-v2-02-code-review.json', 'evidence/p08b-v2-02-evidence-review.json', 'evidence/p08b-v2-02-execution-index.json', 'evidence/p08b-v2-02-qualification.json', 'evidence/p08b-v2-02-semantic-history.json', 'evidence/p08b-v2-02-semantic-reviews.json', 'evidence/p08b-v2-02-verification.json', 'execution/__init__.py', 'execution/container_worker.py', 'execution/docker_runner.py', 'execution/proposals.py', 'execution/requirements-p08.lock', 'execution/results.py', 'experiments/__init__.py', 'experiments/admission.py', 'experiments/campaign.py', 'experiments/context.py', 'experiments/final_freeze_v2.py', 'experiments/formal_tasks.py', 'experiments/formal_tasks_v2.py', 'experiments/freeze.py', 'experiments/freeze_v2.py', 'experiments/functions.py', 'experiments/p08b-tasks.json', 'experiments/p08b-v2-protocol.json', 'experiments/p08b-v2-tasks.json', 'experiments/pilot.py', 'experiments/preflight.py', 'experiments/project_tasks.py', 'experiments/projects.json', 'experiments/qualification_v2.py', 'experiments/runner.py', 'experiments/runtime_v2.py', 'experiments/scoped.py', 'experiments/screening.py', 'experiments/tasks.py', 'experiments/upstream.py', 'models/__init__.py', 'models/adapter.py', 'repair/__init__.py', 'repair/fixtures.py', 'repair/session.py', 'repository/__init__.py', 'repository/context.py', 'repository/parser.py', 'tests/test_campaign.py', 'tests/test_coverage.py', 'tests/test_docker_runner.py', 'tests/test_execution.py', 'tests/test_experiment_admission.py', 'tests/test_experiment_preflight.py', 'tests/test_experiment_runner.py', 'tests/test_experiment_tasks.py', 'tests/test_final_freeze_v2.py', 'tests/test_formal_tasks.py', 'tests/test_formal_tasks_v2.py', 'tests/test_freeze.py', 'tests/test_freeze_v2.py', 'tests/test_generation_example.py', 'tests/test_generator.py', 'tests/test_models.py', 'tests/test_orchestrator.py', 'tests/test_project_tasks.py', 'tests/test_qualification_v2.py', 'tests/test_repair_session.py', 'tests/test_repairer.py', 'tests/test_repository.py', 'tests/test_scoped.py', 'tests/test_screening.py', 'tests/test_state.py', 'tests/test_support_inventory.py', 'tests/test_upstream_inventory.py', 'tests/test_web.py', 'tests/test_worker_measurement.py', 'tests/test_workflow_journal.py', 'validation/p08b-evaluation.json', 'validation/p08b-v2-evaluation.json', 'workflow/__init__.py', 'workflow/journal.py', 'workflow/orchestrator.py', 'workflow/state.py')


def read_evidence():
    values = []
    for name, expected in EVIDENCE_HASHES:
        value = _read(checked_path(PROJECT_ROOT, name))
        if _canonical(value) != expected:
            raise ValueError('accepted V2 qualification evidence changed: '+name)
        values.append(value)
    return tuple(values)


def make_protocol(task_ids, *, source_commit):
    result = make_candidate_protocol(task_ids, source_commit=source_commit)
    result.update(schema='p08-protocol-v2', status='frozen_inputs')
    return result


def validate_protocol(protocol, task_ids):
    if (not isinstance(protocol, dict) or not isinstance(protocol.get('source_commit'), str)
            or not re.fullmatch('[a-f0-9]{40}', protocol['source_commit'])
            or _canonical(protocol) != _canonical(make_protocol(task_ids, source_commit=protocol['source_commit']))):
        raise ValueError('final V2 protocol differs from approved parameters')


def validate_qualification(tasks, report, index, reviews):
    try:
        return _validate_qualification(tasks, report, index, reviews)
    except (KeyError, TypeError, AttributeError, IndexError):
        raise ValueError('invalid V2 qualification evidence') from None


def _validate_qualification(tasks, report, index, reviews):
    ids = [t['case']['id'] for t in tasks]
    checks = check_candidate(tasks, make_candidate_protocol(ids, source_commit='0'*40))
    if (report['status'] != 'complete' or type(report['qualified']) is not int or report['qualified'] != 30
            or report['planned_executions'] != 120 or report['formal_ready'] is not False or report['model_calls'] != 0
            or index['image_id'] != IMAGE or len(report['results']) != 30 or len(index['records']) != 120
            or len(reviews) != 30 or [r['task_id'] for r in report['results']] != ids
            or [r['task_id'] for r in reviews] != ids):
        raise ValueError('complete thirty-task V2 qualification required')
    indexed = {(r['task_id'], r['stage']): r for r in index['records']}
    if set(indexed) != {(identity, stage) for identity in ids for stage in STAGES}:
        raise ValueError('missing or duplicated V2 execution index')
    run_ids = set()
    for task, row, review in zip(tasks, report['results'], reviews):
        validate_review(task, review)
        if (row['status'] != 'qualified' or row['image_id'] != IMAGE
                or row['project_id'] != task['provenance']['task']['project_id']
                or row['input_sha256'] != review_digest(task) or row['semantic_review'] != review
                or row['formal_ready'] is not False or row['model_calls'] != 0
                or [e['stage'] for e in row['executions']] != list(STAGES)):
            raise ValueError('stale or incomplete V2 task qualification')
        for entry in row['executions']:
            stage = entry['stage'];result = entry['result'];run = result['run']
            sources = task['validation']['reference_sources'] if stage.startswith('reference') else task['case']['sources']
            tests = dict(task['case']['original_tests'])
            if stage.endswith('holdout'): tests.update(task['validation']['holdout_tests'])
            support = tuple((p, hashlib.sha256(v).hexdigest()) for p,v in task['case']['support'].items())
            expected = context_digest(tuple(sources.items()), tuple(tests.items()), support)
            if (entry['valid'] is not True or result['cleanup_ok'] is not True
                    or run['snapshot_digest'] != expected or run['run_id'] in run_ids
                    or not isinstance(run['run_id'], str) or not run['run_id']
                    or any(type(run[k]) is not int or run[k] < 0 for k in ('passed','failed','errors','skipped'))
                    or entry['primary_source_count'] != len(task['case']['coverage_sources'])):
                raise ValueError('V2 execution identity, snapshot, counts or coverage invalid')
            run_ids.add(run['run_id'])
            if stage == 'mutant_holdout':
                prefixes = [p.removesuffix('.py').replace('/', '.') for p in task['validation']['holdout_tests']]
                if (run['exit_code'] != 1 or run['failed'] != 1 or run['errors'] != 0
                        or not run['failure'] or run['failure']['kind'] != 'test_assertion'
                        or len(entry['failure_nodes']) != 1
                        or not all(any(n['classname'] == p or n['classname'].startswith(p+'.') for p in prefixes) for n in entry['failure_nodes'])):
                    raise ValueError('targeted private assertion detection required')
            elif (run['exit_code'] != 0 or run['passed'] < 1 or run['failed'] or run['errors'] or run['failure'] is not None):
                raise ValueError('reference and original suites must pass')
            linked = indexed[(row['task_id'], stage)]
            values = {k: run[k] for k in ('run_id','snapshot_digest','passed','failed','errors','skipped')}
            values.update(valid=True, cleanup_ok=True)
            for key in ('manifest_sha256','junit_sha256','coverage_sha256'):
                value = entry[key]
                if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
                    raise ValueError('artifact digest required')
                values[key] = value
            if any(linked[k] != v or type(linked[k]) is not type(v) for k,v in values.items()):
                raise ValueError('V2 index differs from reviewed execution')
    return {'schema':'p08-v2-freeze-check-v1','task_count':checks['task_count'],
            'unique_functions':checks['unique_functions'],'qualified_tasks':30,
            'qualified_executions':len(run_ids),'planned_records':360,
            'task_checksums':checks['task_checksums'],'launch_authorized':False,'model_calls':0}


def file_digest(raw):
    return hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()


def verify_files(root, manifest):
    _fields(manifest, 'schema scope implementation_commit files validation')
    if manifest['schema'] != 'p08-v2-freeze-v1' or manifest['scope'] != 'frozen_inputs_no_launch':
        raise ValueError('unknown V2 freeze manifest')
    if not isinstance(manifest['implementation_commit'], str) or not re.fullmatch('[a-f0-9]{40}', manifest['implementation_commit']):
        raise ValueError('implementation Git commit required')
    files = manifest['files']
    if not isinstance(files, list) or [e.get('path') for e in files] != list(BOUND_PATHS):
        raise ValueError('complete ordered frozen file inventory required')
    for entry in files:
        _fields(entry, 'path normalization sha256')
        if entry['normalization'] != 'lf':
            raise ValueError('unknown V2 hash normalization')
        path = checked_path(root, entry['path'])
        if (not path.is_file() or path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400
                or file_digest(path.read_bytes()) != entry['sha256']):
            raise ValueError('frozen V2 file changed: '+entry['path'])


def git_blob(root, commit, name):
    if not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('fixed Git SHA required')
    kind = subprocess.check_output(['git','cat-file','-t',commit],cwd=root,stderr=subprocess.DEVNULL).strip()
    if kind != b'commit':
        raise ValueError('Git object must be a commit, not a tree or blob')
    return subprocess.check_output(['git','show',commit+':'+name],cwd=root,stderr=subprocess.DEVNULL)


def verify_receipt(root, receipt):
    _fields(receipt, 'schema freeze_commit files')
    if receipt['schema'] != 'p08-v2-freeze-receipt-v1':
        raise ValueError('unknown V2 freeze receipt')
    expected = sorted((*BOUND_PATHS, 'evidence/p08b-v2-freeze.json'))
    if not isinstance(receipt['files'], list) or [e.get('path') for e in receipt['files']] != expected:
        raise ValueError('complete V2 receipt inventory required')
    for entry in receipt['files']:
        _fields(entry, 'path lf_sha256 git_blob_sha256')
        raw = checked_path(root, entry['path']).read_bytes()
        blob = git_blob(root, receipt['freeze_commit'], entry['path'])
        if (file_digest(raw) != entry['lf_sha256'] or file_digest(blob) != entry['lf_sha256']
                or hashlib.sha256(blob).hexdigest() != entry['git_blob_sha256']):
            raise ValueError('V2 receipt differs from frozen Git files')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true', required=True)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args(argv)
    manifest = _read(PROJECT_ROOT/'evidence/p08b-v2-freeze.json')
    verify_files(PROJECT_ROOT, manifest)
    protocol = _read(PROJECT_ROOT/'experiments/p08b-v2-protocol.json')
    tasks = load_formal_tasks(PROJECT_ROOT/'experiments/p08b-v2-tasks.json',PROJECT_ROOT/'validation/p08b-v2-evaluation.json',
                             catalog_path=PROJECT_ROOT/'experiments/projects.json',reference_root=PROJECT_ROOT/'reference/p08',
                             resource_root=PROJECT_ROOT/'artifacts/p08a/image/wheels')
    validate_protocol(protocol, [t['case']['id'] for t in tasks])
    if protocol['source_commit'] != manifest['implementation_commit']:
        raise ValueError('protocol and implementation commit differ')
    for name in BOUND_PATHS:
        if name != 'experiments/p08b-v2-protocol.json':
            if file_digest(git_blob(PROJECT_ROOT, manifest['implementation_commit'], name)) != file_digest(checked_path(PROJECT_ROOT,name).read_bytes()):
                raise ValueError('implementation differs from committed version: '+name)
    report, index, reviews, audit = read_evidence()
    if audit['blocking_findings'] or audit['tasks'] != 30 or audit['unique_executions'] != 120:
        raise ValueError('independent qualification audit incomplete')
    validation = validate_qualification(tasks,report,index,reviews)
    if manifest['validation'] != validation:
        raise ValueError('frozen qualification checks differ')
    if args.receipt: verify_receipt(PROJECT_ROOT, _read(args.receipt))
    print(json.dumps(dict(validation, status='frozen_inputs_verified', receipt_verified=bool(args.receipt)),indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
