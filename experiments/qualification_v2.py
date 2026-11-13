"""One four-stage V2 qualification pass, no model calls and no formal freeze."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from agents.repairer import context_digest
from evaluation.coverage import summarize
from execution.docker_runner import run_tests
from execution.proposals import checked_path
from execution.results import classify_result
from experiments.campaign import _unlinked
from experiments.formal_tasks_v2 import load_formal_tasks
from experiments.freeze_v2 import _loaded_inputs, check_candidate
from experiments.project_tasks import _fields
from experiments.runtime_v2 import IMAGE
from experiments.screening import review_digest
from experiments.tasks import _read
from repair.fixtures import PROJECT_ROOT
from workflow.state import FailureKind, Repository

STAGES = ('reference_original', 'reference_holdout', 'mutant_original', 'mutant_holdout')


def validate_review(task, review):
    _fields(review, 'schema task_id input_sha256 reviewer status notes')
    owner = task['provenance']['task']['owner']
    if (review['schema'] != 'p08-v2-semantic-review-v1' or review['status'] != 'approved'
            or review['task_id'] != task['case']['id'] or review['input_sha256'] != review_digest(task)
            or not isinstance(review['reviewer'], str) or not review['reviewer'].strip()
            or review['reviewer'].strip().casefold() == owner.strip().casefold()
            or not isinstance(review['notes'], str) or not review['notes'].strip()):
        raise ValueError('current independent V2 semantic approval required')
    _loaded_inputs(task)


def _save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=True, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)


def _assertion_failure(node):
    """Recognize pytest's assertion summaries, not arbitrary substring matches."""
    kind = node.get('type', '')
    message = node.get('message', '').lstrip()
    if kind not in ('', 'AssertionError', 'builtins.AssertionError', 'Failed', '_pytest.outcomes.Failed'):
        return False
    if kind in ('AssertionError', 'builtins.AssertionError'):
        return True
    if kind == '' and (message.startswith('assert ') or re.match(r'^(?:builtins\.)?AssertionError(?::|$)', message)):
        return True
    return (message.startswith('Failed: DID NOT RAISE ') and 'pytest.raises' in ''.join(node.itertext()))


def _evidence(result, expected_files, digest, primary_sources, holdout_paths, stage):
    directory = Path(result.artifact_directory)
    run = result.run
    manifest = _read(directory/'manifest.json')
    if (not result.cleanup_ok or manifest['cleanup_ok'] is not True or manifest['image_id'] != IMAGE
            or manifest['files'] != [list(p) for p in expected_files] or run.snapshot_digest != digest):
        raise ValueError('qualification snapshot, image or cleanup mismatch')
    raw = (directory/'junit.xml').read_bytes()
    observed = classify_result(run.run_id, digest, run.exit_code, raw)
    fields = ('exit_code', 'passed', 'failed', 'errors', 'skipped')
    if any(getattr(run, k) != getattr(observed, k) for k in fields):
        raise ValueError('qualification JUnit counts mismatch')
    if len(raw) > 2_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('unsafe qualification JUnit')
    nodes = list(ET.fromstring(raw).iter('testcase'))
    failed = [{'classname': n.get('classname', ''), 'name': n.get('name', '')}
              for n in nodes if n.find('failure') is not None]
    coverage = summarize(_read(directory/'coverage.json'), primary_sources)
    prefixes = [p.removesuffix('.py').replace('/', '.') for p in holdout_paths]
    if stage == 'mutant_holdout':
        valid = (observed.failure is not None and observed.failure.kind == FailureKind.TEST_ASSERTION
                 and run.exit_code == 1 and run.failed > 0 and run.errors == 0
                 and len(failed) == run.failed
                 and all(_assertion_failure(n.find('failure')) for n in nodes if n.find('failure') is not None)
                 and all(any(n['classname'] == p or n['classname'].startswith(p+'.') for p in prefixes) for n in failed))
    else:
        valid = observed.successful
    identities = [(n.get('classname'), n.get('name')) for n in nodes]
    return {'valid': bool(valid), 'failure_nodes': failed,
            'junit_sha256': hashlib.sha256(raw).hexdigest(),
            'junit_identities_sha256': hashlib.sha256(json.dumps(identities).encode()).hexdigest(),
            'manifest_sha256': hashlib.sha256((directory/'manifest.json').read_bytes()).hexdigest(),
            'coverage_sha256': hashlib.sha256((directory/'coverage.json').read_bytes()).hexdigest(),
            'coverage': {k: coverage[k] for k in ('lines', 'branches')}, 'primary_source_count': len(coverage['files'])}


def qualify(task, review, *, artifact_root):
    validate_review(task, review)
    case, validation = task['case'], task['validation']
    identity = case['id']
    if not isinstance(identity, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', identity):
        raise ValueError('invalid V2 qualification identity')
    directory = _unlinked(Path(artifact_root)/identity)
    directory.mkdir(parents=True, exist_ok=False)
    record = {'schema': 'p08-v2-qualification-task-v1', 'scope': 'V2-02 qualification only, not formal results',
              'task_id': identity, 'project_id': task['provenance']['task']['project_id'],
              'input_sha256': review_digest(task), 'image_id': IMAGE, 'semantic_review': review,
              'status': 'claimed', 'formal_ready': False, 'model_calls': 0, 'executions': []}
    output = directory/'qualification.json'
    _save(output, record)
    support = dict(case['support'])
    for stage in STAGES:
        record['stage'] = stage
        _save(output, record)
        entry = {'stage': stage, 'valid': False}
        try:
            sources = validation['reference_sources'] if stage.startswith('reference') else case['sources']
            tests = dict(case['original_tests'])
            if stage.endswith('holdout'):
                if set(tests) & set(validation['holdout_tests']):
                    raise ValueError('overlapping original/private tests')
                tests.update(validation['holdout_tests'])
            all_files = {**sources, **tests, **support}
            entries = sorted((p, hashlib.sha256(v.encode() if isinstance(v, str) else v).hexdigest()) for p, v in all_files.items())
            digest = context_digest(tuple(sources.items()), tuple(tests.items()), tuple((p, hashlib.sha256(v).hexdigest()) for p, v in support.items()))
            with tempfile.TemporaryDirectory(prefix='autotest-v2-qualification-') as folder:
                root = Path(folder).resolve()
                for p, data in all_files.items():
                    target = checked_path(root, p); target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data.encode() if isinstance(data, str) else data)
                repo = Repository(task['provenance']['task']['project_id'], digest, tuple(sources), tuple(tests))
                targets = tuple(p for p in tests if Path(p).name.startswith('test_') or Path(p).name.endswith('_test.py'))
                if not targets:
                    raise ValueError('nonempty explicit test collection required')
                result = run_tests(root, repo, image_id=IMAGE, artifact_root=directory/stage,
                                   support_files=tuple(support), test_targets=targets)
            entry['result'] = asdict(result)
            entry.update(_evidence(result, entries, digest, case['coverage_sources'], validation['holdout_tests'], stage))
        except (ValueError, OSError, TypeError, KeyError, ET.ParseError) as error:
            entry['error_type'] = type(error).__name__
        record['executions'].append(entry)
        _save(output, record)
    record['status'] = 'qualified' if all(e['valid'] for e in record['executions']) else 'qualification_failed'
    _save(output, record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    parser.add_argument('--reviews', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--export', type=Path, required=True)
    args = parser.parse_args(argv)
    tasks = load_formal_tasks(PROJECT_ROOT/'experiments/p08b-v2-tasks.json', PROJECT_ROOT/'validation/p08b-v2-evaluation.json',
                              catalog_path=PROJECT_ROOT/'experiments/projects.json', reference_root=PROJECT_ROOT/'reference/p08',
                              resource_root=PROJECT_ROOT/'artifacts/p08a/image/wheels')
    check_candidate(tasks, _read(PROJECT_ROOT/'experiments/p08b-v2-protocol-candidate.json'))
    reviews = _read(args.reviews)
    if not isinstance(reviews, list) or len(reviews) != 30 or {r['task_id'] for r in reviews} != {t['case']['id'] for t in tasks}:
        raise ValueError('exactly thirty unique current semantic reviews required')
    indexed = {r['task_id']: r for r in reviews}
    for task in tasks: validate_review(task, indexed[task['case']['id']])
    output = _unlinked(args.export)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema': 'p08-v2-qualification-batch-v1', 'scope': 'V2-02 no-model qualification, not formal results',
              'status': 'running', 'planned_executions': 120, 'formal_ready': False, 'model_calls': 0, 'results': []}
    with output.open('x', encoding='utf-8') as stream: json.dump(report, stream)
    for task in tasks:
        result = qualify(task, indexed[task['case']['id']], artifact_root=args.artifacts)
        report['results'].append(result)
        _save(output, report)
        print(len(report['results']), result['task_id'], result['status'], flush=True)
    report['status'] = 'complete'
    report['qualified'] = sum(r['status'] == 'qualified' for r in report['results'])
    _save(output, report)
    return 0 if report['qualified'] == 30 else 1


if __name__ == '__main__':
    raise SystemExit(main())
