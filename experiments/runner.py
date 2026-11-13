"""Admission experiment runner: two generated suites precede source repair.

Round journals are immutable handoff evidence, not P07 standalone task records.
Only the final round can claim VERIFIED for the complete experiment result.
"""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from agents.repairer import context_digest
from evaluation.coverage import summarize
from execution.docker_runner import run_tests
from execution.proposals import apply_proposal, checked_path
from experiments.context import ScopedGenerator
from experiments.scoped import ScopedRepairer, locate_function
from repair.fixtures import write_files
from repair.session import run_repair_session
from workflow.journal import Journal
from workflow.orchestrator import Cancelled, _source_hashes
from workflow.state import FailureKind, GenerateInput, Policy, Repository, TestRun, Phase


def _write_support(root, files):
    for path, data in files.items():
        target = checked_path(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(data, (str, bytes)):
            raise ValueError('support must contain exact data bytes or UTF-8 text')
        target.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)


def run_experiment(case, validation, *, group, client, image_id, artifact_root,
                   cancelled=lambda: False, run_id=None, scope='P08A nonformal admission only'):
    if group not in ('B0', 'B1', 'B2', 'A0'):
        raise ValueError('unknown method')
    if scope not in ('P08A nonformal admission only', 'P08B frozen formal experiment',
                     'P08B scheduler synthetic validation only', 'P08B v2 nonformal validity replay'):
        raise ValueError('unknown experiment scope')
    run_id = run_id or uuid.uuid4().hex
    if not isinstance(run_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', run_id):
        raise ValueError('invalid immutable experiment identity')
    directory = Path(artifact_root).resolve() / run_id
    directory.mkdir(parents=True, exist_ok=False)  # Never silently rerun paid work.
    started = time.monotonic()
    report = {'schema': 1, 'scope': scope, 'run_id': run_id, 'case_id': case['id'],
              'group': group, 'status': 'failed', 'rounds': [], 'executions': [], 'coverage': {},
              'repair': None, 'generated_only': None, 'image_id': image_id}
    report['stage'] = 'input_validation'
    policy = Policy(max_fix_attempts=1 if group == 'B1' else 3)
    support = dict(case.get('support', {}))
    support_paths = tuple(support)
    journal = None
    def checkpoint():
        if cancelled():
            raise Cancelled()
    def stop(status, kind=FailureKind.INTERNAL):
        report['status'] = status
        if journal and journal.state.phase not in (Phase.VERIFIED, Phase.FAILED):
            journal.fail(status, kind)
        return report
    def execute(root, repo, label, *, test_targets=()):
        checkpoint()
        report['stage'] = label
        secret = getattr(getattr(client, 'config', None), 'api_key', '')
        result = run_tests(root, repo, image_id=image_id, artifact_root=directory / label,
                           support_files=support_paths, test_targets=test_targets, policy=policy, secrets=(secret,) if secret else ())
        report['executions'].append({'label': label, 'result': asdict(result)})
        coverage = Path(result.artifact_directory) / 'coverage.json'
        if coverage.is_file():
            report['coverage'][label] = summarize(json.loads(coverage.read_text()), case.get('coverage_sources', repo.source_files))
        return result
    def oracle(repo, tests, label):
        with tempfile.TemporaryDirectory(prefix='autotest-p08-reference-') as folder:
            root = Path(folder).resolve()
            write_files(root, validation['reference_sources'])
            write_files(root, tests)
            _write_support(root, support)
            return execute(root, repo, label)
    try:
        checkpoint()
        sources, original = dict(case['sources']), dict(case['original_tests'])
        if not sources or not original or set(sources) != set(validation['reference_sources']):
            raise ValueError('complete original/reference source inventory required')
        paths = list(sources) + list(original) + list(support) + list(validation['holdout_tests'])
        if len(set(p.casefold() for p in paths)) != len(paths):
            raise ValueError('overlapping source/test/support/private inventory')
        if case['target'][0] not in sources:
            raise ValueError('scope must identify approved source')
        locate_function(sources[case['target'][0]], case['target'][1])
        support_hashes = tuple((p, hashlib.sha256(v.encode() if isinstance(v, str) else v).hexdigest()) for p, v in support.items())
        revision = 'experiment-sha256:' + context_digest(tuple(sources.items()), tuple(original.items()), support_hashes)
        repo = Repository(case['id'], revision, tuple(sources), tuple(original), (case['target'][1],))
        report['input_revision'] = revision
        report['support_hashes'] = support_hashes
        report['target'] = case['target']
        report['coverage_sources'] = case.get('coverage_sources', repo.source_files)
        report['stage'] = 'workspace_allocation'
        with tempfile.TemporaryDirectory(prefix='autotest-p08-experiment-') as folder:
            report['stage'] = 'workspace_staging'
            root = Path(folder).resolve()
            write_files(root, sources)
            write_files(root, original)
            _write_support(root, support)
            baseline_targets = tuple(p for p in repo.test_files if Path(p).name.startswith('test_') or Path(p).name.endswith('_test.py'))
            if not baseline_targets:
                raise ValueError('explicit nonempty baseline collection required')
            baseline = execute(root, repo, 'baseline', test_targets=baseline_targets)
            if not baseline.cleanup_ok:
                return stop('baseline_environment', FailureKind.ENVIRONMENT)
            if group == 'B0':
                report['status'] = ('baseline_survives' if baseline.run.successful else 'baseline_detected'
                                    if baseline.run.failure and baseline.run.failure.kind == FailureKind.TEST_ASSERTION else 'baseline_environment')
                return report
            if not baseline.run.successful or 'baseline' not in report['coverage']:
                return stop('baseline_environment', FailureKind.ENVIRONMENT)
            reference = oracle(repo, original, 'reference-baseline')
            if not reference.run.successful or not reference.cleanup_ok:
                return stop('reference_environment', FailureKind.ENVIRONMENT)
            all_tests = dict(original)
            generated = {}
            parent = None
            previous_run = None
            for round_index in range(1, 2 if group == 'B1' else 3):
                checkpoint()
                if previous_run:
                    actual_sources = tuple((p, checked_path(root, p).read_bytes().decode('utf-8')) for p in repo.source_files)
                    actual_tests = tuple((p, checked_path(root, p).read_bytes().decode('utf-8')) for p in all_tests)
                    if context_digest(actual_sources, actual_tests, support_hashes) != previous_run.snapshot_digest:
                        return stop('inherited_snapshot_mismatch', FailureKind.PATCH_CONFLICT)
                task_id = uuid.uuid4().hex
                journal = Journal(task_id, policy=policy)
                journal.parsed(repo)
                entry = {'round': round_index, 'task_id': task_id, 'parent_task_id': parent,
                         'inherited_snapshot': None if previous_run is None else previous_run.snapshot_digest,
                         'events': journal.events, 'status': 'generating'}
                if report['rounds']:
                    report['rounds'][-1]['handoff_to'] = task_id
                    report['rounds'][-1]['status'] = 'handed_off'
                report['rounds'].append(entry)
                hint = report['coverage'].get('round-1') if group == 'B2' and round_index == 2 else None
                request = GenerateInput(task_id, repo, policy, tuple(case['requirements']), tuple(sources.items()))
                agent = ScopedGenerator(client, case['target'], tuple(all_tests.items()), coverage_hint=hint,
                                        generated_paths=tuple(generated))
                output = agent.generate(request)
                entry.update(generation=asdict(output), context_audit=agent.context_audit,
                             model_candidate=agent.last_safe_candidate,
                             transport_metadata=getattr(client, 'last_metadata', None),
                             completion=None if agent.last_completion is None else {'model': agent.last_completion.model, 'usage': agent.last_completion.usage})
                checkpoint()
                if output.failure:
                    entry['status'] = 'generation_failed'
                    return stop('context_budget' if output.failure.message.startswith('context_budget:') else 'generation_failed', output.failure.kind)
                journal.accept(request, output)
                apply_proposal(root, request, output)
                journal.generated_inventory(output)
                for file in output.files:
                    generated[file.path] = file.content
                    all_tests[file.path] = file.content
                repo = journal.state.repository
                reference = oracle(repo, all_tests, f'reference-{round_index}')
                entry['reference'] = asdict(reference)
                if not reference.run.successful or not reference.cleanup_ok:
                    return stop('invalid_test_oracle', FailureKind.TEST_GENERATION)
                full = execute(root, repo, f'round-{round_index}')
                entry['full_run'] = asdict(full)
                journal.tested(full.run)
                if not full.cleanup_ok or (not full.run.successful and (not full.run.failure or full.run.failure.kind != FailureKind.TEST_ASSERTION)):
                    return stop('generation_execution_failed', FailureKind.ENVIRONMENT)
                if f'round-{round_index}' not in report['coverage']:
                    return stop('coverage_unavailable', FailureKind.ENVIRONMENT)
                previous_run, parent = full.run, task_id
            detected = execute(root, repo, 'generated-only', test_targets=tuple(generated))
            report['generated_only'] = asdict(detected)
            if (not detected.cleanup_ok or not detected.run.failure or detected.run.failure.kind != FailureKind.TEST_ASSERTION):
                return stop('not_detected' if detected.run.successful else 'generated_execution_failed', FailureKind.TEST_GENERATION)
            if not previous_run.failure or previous_run.failure.kind != FailureKind.TEST_ASSERTION:
                return stop('full_suite_not_repairable')
            def repair_event(event, *values):
                if event == 'current_failure':
                    journal.observed_failure(values[0])
                elif event == 'proposal':
                    journal.accept(*values)
                elif event == 'tested':
                    journal.tested(values[0])
            factory = lambda model, context, **kwargs: ScopedRepairer(model, context, case['target'], **kwargs)
            repair = run_repair_session(root, repo, original_tests=tuple(original), requirements=tuple(case['requirements']),
                holdout_tests=tuple(validation['holdout_tests'].items()), client=client, image_id=image_id,
                artifact_root=directory / 'repair', policy=policy, task_id=parent, event_hook=repair_event,
                support_files=support_paths, repairer_factory=factory, cancelled=cancelled)
            report['repair'] = repair
            checkpoint()
            if repair['status'] != 'verified':
                return stop('repair_' + repair['status'])
            visible, independent = repair['attempts'][-1]['visible_run'], repair['independent_validation']
            run = independent['run']
            coverage = Path(visible['artifact_directory']) / 'coverage.json'
            if not coverage.is_file():
                raise ValueError('final coverage unavailable')
            report['coverage']['repaired-visible'] = summarize(json.loads(coverage.read_text()), case.get('coverage_sources', repo.source_files))
            journal.verify(TestRun(run['run_id'], run['snapshot_digest'], run['exit_code'], run['passed'], run['failed'], run['errors'], run['skipped']),
                           visible_sources=_source_hashes(visible, repo.source_files), independent_sources=_source_hashes(independent, repo.source_files))
            report['rounds'][-1]['status'] = 'verified'
            report['status'] = 'verified'
            return report
    except (Cancelled, KeyboardInterrupt):
        return stop('cancelled', FailureKind.BUDGET)
    except (OSError, ValueError, TypeError, SyntaxError, RecursionError) as error:
        report['error'] = {'type': type(error).__name__,
                           'errno': getattr(error, 'errno', None) if isinstance(error, OSError) else None,
                           'winerror': getattr(error, 'winerror', None) if isinstance(error, OSError) else None}
        return stop('experiment_failed')
    finally:
        report['elapsed_seconds'] = round(time.monotonic()-started, 3)
        safe = getattr(client, 'ensure_safe', None)
        if safe:
            safe(report)
        (directory / 'experiment.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
