"""P06A single-command generation, independent oracle check, detection and repair."""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import uuid

from agents.generator import Generator
from execution.docker_runner import run_tests
from execution.proposals import apply_proposal
from models.adapter import Budget, Config, ModelClient
from repair.fixtures import PROJECT_ROOT, load_cases, write_files
from repair.session import run_repair_session
from repository.context import build_context
from repository.parser import parse_repository
from workflow.journal import Journal
from workflow.state import FailureKind, Phase


class Cancelled(Exception):
    pass


def run_case(case, validation, *, client, image_id, artifact_root, cancelled=lambda: False):
    task_id = uuid.uuid4().hex
    directory = Path(artifact_root).resolve() / task_id
    directory.mkdir(parents=True)
    journal = Journal(task_id)
    record = {'task_id': task_id, 'case_id': case['id'], 'status': 'failed', 'events': [],
              'baseline': None, 'generation': None, 'reference': None, 'generated_only': None,
              'full_before': None, 'repair': None, 'generation_completion': None}

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
        return run_tests(root, repo, image_id=image_id, artifact_root=directory / label,
                         secrets=(secret,) if secret else ())

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
            bundle = build_context(snapshot, task_id=task_id, requirements=tuple(case['requirements']),
                                   targets=tuple(case['sources']))
            repo = snapshot.repository
            journal.parsed(repo)
            baseline = execute(root, repo, 'baseline')
            record['baseline'] = asdict(baseline)
            if not baseline.run.successful or not baseline.cleanup_ok:
                return stop('baseline_failed', FailureKind.ENVIRONMENT)
            checkpoint()
            agent = Generator(client)
            output = agent.generate(bundle.request, related_tests=bundle.related_tests)
            record['generation'] = asdict(output)
            record['generation_diagnostic'] = agent.last_diagnostic
            completion = agent.last_completion
            if completion:
                record['generation_completion'] = {'model': completion.model, 'usage': completion.usage}
            if output.failure:
                return stop('generation_failed', output.failure.kind)
            journal.accept(bundle.request, output)
            apply_proposal(root, bundle.request, output)
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
            if repair['status'] != 'verified':
                return stop('repair_' + repair['status'])
            from workflow.state import TestRun
            independent = repair['independent_validation']['run']
            journal.verify(TestRun(independent['run_id'], independent['snapshot_digest'], 0,
                                   independent['passed'], 0, 0, independent['skipped']))
            record['status'] = 'verified'
            return record
    except Cancelled:
        return stop('cancelled', FailureKind.BUDGET)
    except (OSError, ValueError, TypeError):
        return stop('workflow_failed')
    finally:
        record['events'] = journal.events
        (directory / 'workflow.json').write_text(json.dumps(record, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--call', action='store_true')
    parser.add_argument('--image-id')
    parser.add_argument('--artifacts', type=Path, default=Path('artifacts/p06-workflow'))
    args = parser.parse_args()
    cases, validation, manifest = load_cases()
    if not args.call:
        print(json.dumps({'cases': [c['id'] for c in cases], 'fixture_manifest': manifest,
                          'mode': 'preview; no model or container calls'}, indent=2))
        return 0
    if not args.image_id:
        parser.error('--call requires --image-id')
    client = ModelClient(Config.from_env(PROJECT_ROOT / '.env'),
                         Budget(PROJECT_ROOT / 'artifacts/model-budget.json', max_attempts=None), thinking_mode='disabled')
    if client.config.model not in client.list_models():
        raise ValueError('model not authorized')
    report = {'schema': 1, 'fixture_manifest': manifest, 'cases': []}
    for case in cases:
        result = run_case(case, validation[case['id']], client=client,
                          image_id=args.image_id, artifact_root=args.artifacts)
        report['cases'].append(result)
        print(case['id'] + ': ' + result['status'], flush=True)
        args.artifacts.mkdir(parents=True, exist_ok=True)
        client.ensure_safe(report)
        (args.artifacts / 'latest-summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if all(c['status'] == 'verified' for c in report['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
