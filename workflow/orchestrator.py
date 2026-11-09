"""P06A single-command generation, independent oracle check, detection and repair."""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import time
import uuid

from agents.generator import Generator
from agents.repairer import context_digest
from execution.docker_runner import run_tests
from execution.proposals import apply_proposal
from models.adapter import Budget, Completion, Config, ModelClient
from repair.fixtures import PROJECT_ROOT, load_cases, write_files
from repair.session import run_repair_session
from repository.context import build_context
from repository.parser import parse_repository
from workflow.journal import Journal
from workflow.state import FailureKind, Phase
from evaluation.coverage import summarize


class Cancelled(Exception):
    pass


class WorkflowReplayClient:
    """Replay saved proposals; only runtime identity fields are rebound."""
    def __init__(self, saved):
        if saved['status'] != 'verified':
            raise ValueError('only verified saved workflows may be replayed')
        self.saved = saved
        self.repairs = iter((saved.get('repair') or {}).get('attempts', []))

    def complete(self, messages):
        request = json.loads(messages[1]['content'])
        if 'sources' in request:
            payload = dict(next(self.repairs)['proposal'])
            payload.pop('failure', None)
            payload['based_on_run_id'] = request['based_on_run_id']
        else:
            original = self.saved['generation']
            payload = {'task_id': request['task_id'], 'base_revision': request['base_revision'],
                       'rationale': original['rationale'], 'files': [
                           {'path': f['path'], 'content': f['content']} for f in original['files']]}
        payload.update(task_id=request['task_id'], base_revision=request['base_revision'])
        return Completion(json.dumps(payload), 'replay-of-saved-P06-proposal', None)


def _source_hashes(result, source_files):
    directory = result['artifact_directory'] if isinstance(result, dict) else result.artifact_directory
    manifest = json.loads((Path(directory) / 'manifest.json').read_text(encoding='utf-8'))
    files = dict(manifest['files'])
    return {p: files[p] for p in source_files}


def feedback_case(case, parent):
    if parent['status'] != 'verified' or not parent.get('repair'):
        raise ValueError('feedback requires a verified repaired parent')
    inherited = dict(case['original_tests'])
    for file in parent['generation']['files']:
        if file['path'] in inherited:
            raise ValueError('parent generated tests conflict with original tests')
        inherited[file['path']] = file['content']
    return dict(case, id=case['id'] + '-coverage-child', goal='coverage',
                parent_task_id=parent['task_id'], sources=dict(parent['repair']['final_sources']),
                original_tests=inherited,
                expected_parent_snapshot=parent['repair']['attempts'][-1]['visible_run']['run']['snapshot_digest'])


def run_case(case, validation, *, client, image_id, artifact_root, cancelled=lambda: False):
    started = time.monotonic()
    task_id = uuid.uuid4().hex
    directory = Path(artifact_root).resolve() / task_id
    directory.mkdir(parents=True)
    journal = Journal(task_id)
    record = {'task_id': task_id, 'case_id': case['id'], 'status': 'failed', 'events': [],
              'baseline': None, 'generation': None, 'reference': None, 'generated_only': None,
              'full_before': None, 'repair': None, 'generation_completion': None}
    record['parent_task_id'] = case.get('parent_task_id')
    record['coverage'] = {}

    def checkpoint():
        if cancelled():
            raise Cancelled()

    def stop(status, kind=FailureKind.INTERNAL):
        record['status'] = status
        if journal.state.phase not in (Phase.FAILED, Phase.VERIFIED):
            journal.fail(status, kind)
        return record

    def execute(root, repo, label):
        checkpoint()
        secret = getattr(getattr(client, 'config', None), 'api_key', '')
        result = run_tests(root, repo, image_id=image_id, artifact_root=directory / label,
                           secrets=(secret,) if secret else ())
        coverage_path = Path(result.artifact_directory) / 'coverage.json'
        record['coverage'][label] = summarize(json.loads(coverage_path.read_text()), repo.source_files) if coverage_path.is_file() else None
        return result

    try:
        checkpoint()
        with tempfile.TemporaryDirectory(prefix='autotest-workflow-') as folder:
            root = Path(folder)
            # P05 manually supplied new tests are intentionally not copied here.
            write_files(root, case['sources'])
            write_files(root, case['original_tests'])
            snapshot = parse_repository(root, project_id=case['id'])
            if snapshot.issues:
                return stop('parse_failed', FailureKind.PARSE_ERROR)
            # Previously generated tests are excluded by the static parser's
            # directory heuristic, but remain part of the explicit approved
            # inventory and must never disappear from a feedback child's suite.
            revision = 'workflow-sha256:' + context_digest(tuple(case['sources'].items()), tuple(case['original_tests'].items()))
            repo = replace(snapshot.repository, revision=revision, test_files=tuple(case['original_tests']))
            journal.parsed(repo)
            baseline = execute(root, repo, 'baseline')
            record['baseline'] = asdict(baseline)
            if not baseline.run.successful or not baseline.cleanup_ok:
                return stop('baseline_failed', FailureKind.ENVIRONMENT)
            if case.get('expected_parent_snapshot') and baseline.run.snapshot_digest != case['expected_parent_snapshot']:
                return stop('parent_snapshot_mismatch', FailureKind.PATCH_CONFLICT)
            if case.get('require_coverage') and not record['coverage']['baseline']:
                return stop('coverage_unavailable', FailureKind.ENVIRONMENT)
            requirements = tuple(case['requirements'])
            if case.get('goal') == 'coverage':
                if not record['coverage']['baseline']:
                    return stop('coverage_unavailable', FailureKind.ENVIRONMENT)
                requirements += ('Coverage hints for the current source (behavior remains governed by independent requirements): '
                                 + json.dumps(record['coverage']['baseline']['files']),)
            bundle = build_context(snapshot, task_id=task_id, requirements=requirements,
                                   targets=tuple(case['sources']))
            request = replace(bundle.request, repository=repo)
            checkpoint()
            agent = Generator(client)
            output = agent.generate(request, related_tests=tuple(case['original_tests'].items()))
            record['generation_transport_metadata'] = getattr(client, 'last_metadata', None)
            record['generation'] = asdict(output)
            record['generation_diagnostic'] = agent.last_diagnostic
            record['generation_candidate'] = agent.last_safe_candidate
            completion = agent.last_completion
            if completion:
                record['generation_completion'] = {'model': completion.model, 'usage': completion.usage}
            checkpoint()
            if output.failure:
                return stop('generation_failed', output.failure.kind)
            journal.accept(request, output)
            apply_proposal(root, request, output)
            journal.generated_inventory(output)
            full_repo = journal.state.repository
            # Correct implementation is evaluator-only, never passed to Generator.
            with tempfile.TemporaryDirectory(prefix='autotest-oracle-') as oracle_folder:
                oracle = Path(oracle_folder)
                write_files(oracle, validation['reference_sources'])
                write_files(oracle, case['original_tests'])
                write_files(oracle, {f.path: f.content for f in output.files})
                reference = execute(oracle, full_repo, 'reference')
            record['reference'] = asdict(reference)
            if not reference.run.successful or not reference.cleanup_ok:
                return stop('invalid_test_oracle', FailureKind.TEST_GENERATION)
            if case.get('goal') == 'coverage':
                full = execute(root, full_repo, 'full-before')
                record['full_before'] = asdict(full)
                journal.tested(full.run)
                if not full.run.successful or not full.cleanup_ok:
                    return stop('feedback_tests_failed', FailureKind.TEST_GENERATION)
                if not record['coverage']['full-before']:
                    return stop('coverage_unavailable', FailureKind.ENVIRONMENT)
                with tempfile.TemporaryDirectory(prefix='autotest-feedback-validation-') as private_folder:
                    private_root = Path(private_folder)
                    write_files(private_root, case['sources'])
                    write_files(private_root, case['original_tests'])
                    write_files(private_root, {f.path: f.content for f in output.files})
                    write_files(private_root, validation['holdout_tests'])
                    private_repo = replace(full_repo, test_files=full_repo.test_files + tuple(validation['holdout_tests']))
                    independent = execute(private_root, private_repo, 'independent')
                record['independent_validation'] = asdict(independent)
                checkpoint()
                if not independent.run.successful or not independent.cleanup_ok:
                    return stop('independent_validation_failed')
                journal.verify(independent.run, visible_sources=_source_hashes(full, repo.source_files),
                               independent_sources=_source_hashes(independent, repo.source_files))
                record['status'] = 'verified'
                return record
            generated_repo = replace(full_repo, test_files=tuple(f.path for f in output.files))
            detected = execute(root, generated_repo, 'generated-only')
            record['generated_only'] = asdict(detected)
            if not detected.cleanup_ok or not detected.run.failure or detected.run.failure.kind != FailureKind.TEST_ASSERTION:
                return stop('not_detected' if detected.run.successful else 'generation_execution_failed',
                            FailureKind.TEST_GENERATION)
            full = execute(root, full_repo, 'full-before')
            record['full_before'] = asdict(full)
            journal.tested(full.run)
            if not full.cleanup_ok or not full.run.failure or full.run.failure.kind != FailureKind.TEST_ASSERTION:
                return stop('full_suite_not_repairable', FailureKind.ENVIRONMENT)

            def repair_event(event, *values):
                checkpoint()
                if event == 'current_failure':
                    journal.observed_failure(values[0])
                elif event == 'proposal':
                    journal.accept(*values)
                elif event == 'tested':
                    journal.tested(values[0])

            repair = run_repair_session(root, full_repo, original_tests=repo.test_files,
                requirements=tuple(case['requirements']), holdout_tests=tuple(validation['holdout_tests'].items()),
                client=client, image_id=image_id, artifact_root=directory / 'repair',
                task_id=task_id, event_hook=repair_event)
            record['repair'] = repair
            checkpoint()
            if repair['status'] != 'verified':
                return stop('repair_' + repair['status'])
            last_visible = repair['attempts'][-1]['visible_run']
            coverage_path = Path(last_visible['artifact_directory']) / 'coverage.json'
            record['coverage']['repaired-visible'] = summarize(json.loads(coverage_path.read_text()), repo.source_files) if coverage_path.exists() else None
            if case.get('require_coverage') and (not record['coverage']['full-before'] or not record['coverage']['repaired-visible']):
                return stop('coverage_unavailable', FailureKind.ENVIRONMENT)
            from workflow.state import TestRun
            independent = repair['independent_validation']['run']
            journal.verify(TestRun(independent['run_id'], independent['snapshot_digest'], 0,
                                   independent['passed'], 0, 0, independent['skipped']),
                           visible_sources=_source_hashes(last_visible, repo.source_files),
                           independent_sources=_source_hashes(repair['independent_validation'], repo.source_files))
            record['status'] = 'verified'
            return record
    except Cancelled:
        return stop('cancelled', FailureKind.BUDGET)
    except KeyboardInterrupt:
        return stop('cancelled', FailureKind.BUDGET)
    except (OSError, ValueError, TypeError):
        return stop('workflow_failed')
    finally:
        record['events'] = journal.events
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (directory / 'workflow.json').write_text(json.dumps(record, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--call', action='store_true')
    parser.add_argument('--image-id')
    parser.add_argument('--feedback', action='store_true', help='create one coverage child after successful parents')
    parser.add_argument('--case', action='append', choices=('age_threshold', 'take_first', 'sum_inclusive'))
    parser.add_argument('--replay', type=Path)
    parser.add_argument('--export', type=Path)
    parser.add_argument('--model', help='explicit authorized model override; does not change local .env')
    parser.add_argument('--cancel-file', type=Path, help='existence requests cancellation at stage boundaries')
    parser.add_argument('--artifacts', type=Path, default=Path('artifacts/p06-workflow'))
    args = parser.parse_args()
    cases, validation, manifest = load_cases()
    cases = [c for c in cases if not args.case or c['id'] in args.case]
    if args.call and args.replay:
        parser.error('--call and --replay are mutually exclusive')
    if not (args.call or args.replay):
        print(json.dumps({'cases': [c['id'] for c in cases], 'fixture_manifest': manifest,
                          'mode': 'preview; no model or container calls'}, indent=2))
        return 0
    if not args.image_id:
        parser.error('--call requires --image-id')
    saved = json.loads(args.replay.read_text()) if args.replay else None
    if saved and saved['fixture_manifest'] != manifest:
        raise ValueError('frozen fixture manifest differs')
    client = None
    cancelled = lambda: bool(args.cancel_file and args.cancel_file.exists())
    if args.call and not cancelled():
        config = Config.from_env(PROJECT_ROOT / '.env')
        if args.model:
            config = replace(config, model=args.model)
        client = ModelClient(config,
                             Budget(PROJECT_ROOT / 'artifacts/model-budget.json', max_attempts=None), thinking_mode='disabled')
        if client.config.model not in client.list_models():
            raise ValueError('model not authorized')
    report = {'schema': 1, 'origin': 'saved real workflow replay' if saved else 'real model',
              'fixture_manifest': manifest, 'cases': []}
    for case in cases:
        case = dict(case, require_coverage=args.feedback)
        saved_case = next((r for r in saved['cases'] if r['case_id'] == case['id']), None) if saved else None
        current_client = WorkflowReplayClient(saved_case) if saved else client
        result = run_case(case, validation[case['id']], client=current_client,
                          image_id=args.image_id, artifact_root=args.artifacts, cancelled=cancelled)
        if saved_case and result['status'] == 'verified':
            expected = saved_case['repair']['independent_validation']['run']['snapshot_digest']
            actual = result['repair']['independent_validation']['run']['snapshot_digest']
            if actual != expected:
                raise ValueError('replay independent snapshot mismatch')
        report['cases'].append(result)
        print(case['id'] + ': ' + result['status'], flush=True)
        args.artifacts.mkdir(parents=True, exist_ok=True)
        if client:
            client.ensure_safe(report)
        (args.artifacts / 'latest-summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        if result['status'] == 'cancelled':
            started = {row['case_id'] for row in report['cases']}
            report['not_started'] = [row['id'] for row in cases if row['id'] not in started]
            (args.artifacts / 'latest-summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            break
    if args.feedback and all(c['status'] == 'verified' for c in report['cases']):
        parent = report['cases'][0]
        if saved:
            saved_parent = next(r for r in saved['cases'] if r['case_id'] == parent['case_id'])
            if saved['feedback_child']['parent_task_id'] != saved_parent['task_id']:
                raise ValueError('saved feedback belongs to a different selected parent')
        child = feedback_case(cases[0], parent)
        feedback_client = WorkflowReplayClient(saved['feedback_child']) if saved else client
        result = run_case(child, validation[cases[0]['id']], client=feedback_client,
                          image_id=args.image_id, artifact_root=args.artifacts, cancelled=cancelled)
        report['feedback_child'] = result
        if saved and result['status'] == 'verified':
            if result['independent_validation']['run']['snapshot_digest'] != saved['feedback_child']['independent_validation']['run']['snapshot_digest']:
                raise ValueError('feedback replay independent snapshot mismatch')
        if client:
            client.ensure_safe(report)
        (args.artifacts / 'latest-summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(child['id'] + ': ' + result['status'], flush=True)
    if args.export:
        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(json.dumps(report, indent=2), encoding='utf-8')
    successful = all(c['status'] == 'verified' for c in report['cases'])
    if 'feedback_child' in report:
        successful = successful and report['feedback_child']['status'] == 'verified'
    return 0 if successful else 1


if __name__ == '__main__':
    raise SystemExit(main())
