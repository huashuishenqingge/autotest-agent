"""P05 repair acceptance on three frozen tasks, or replay saved real patches."""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile

from execution.docker_runner import run_tests
from models.adapter import Budget, Completion, Config, ModelClient
from repair.fixtures import load_cases, write_files, visible_repository, PROJECT_ROOT
from repair.session import run_repair_session


class ReplayClient:
    """Rebind only runtime IDs; source content/hash remains the saved real proposal."""
    def __init__(self, attempts):
        self.attempts = iter(attempts)

    def complete(self, messages):
        request = json.loads(messages[1]['content'])
        saved = next(self.attempts)
        payload = dict(saved['proposal'])
        payload.pop('failure', None)
        payload.update(task_id=request['task_id'], base_revision=request['base_revision'],
                       based_on_run_id=request['based_on_run_id'])
        return Completion(json.dumps(payload), 'replay-of-saved-real-proposal', None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-id')
    parser.add_argument('--call', action='store_true')
    parser.add_argument('--validate-fixtures', action='store_true')
    parser.add_argument('--case', choices=('age_threshold', 'take_first', 'sum_inclusive'), action='append')
    parser.add_argument('--replay', type=Path)
    parser.add_argument('--export', type=Path)
    parser.add_argument('--artifacts', type=Path, default=Path('artifacts/p05-repair'))
    args = parser.parse_args()
    if args.call and args.replay:
        parser.error('--call and --replay are mutually exclusive')
    cases, validation, manifest = load_cases()
    selected = [c for c in cases if not args.case or c['id'] in args.case]
    print(json.dumps({'fixture_manifest': manifest, 'cases': [
        {'id': c['id'], 'requirements': c['requirements'], 'visible_test_origin': c['visible_test_origin'],
         'repository': asdict(visible_repository(c))} for c in selected]}, indent=2), flush=True)
    if not (args.call or args.replay or args.validate_fixtures):
        print('Preview only: no model calls or container execution.')
        return 0
    if not args.image_id:
        parser.error('execution requires --image-id')
    client = None
    saved = None
    if args.call:
        config = Config.from_env(PROJECT_ROOT / '.env')
        client = ModelClient(config, Budget(PROJECT_ROOT / 'artifacts/model-budget.json', max_attempts=None))
        if config.model not in client.list_models():
            raise ValueError('Configured model is not authorized')
    if args.replay:
        saved = json.loads(args.replay.read_text(encoding='utf-8'))
        if saved['fixture_manifest'] != manifest:
            raise ValueError('Frozen fixtures differ from saved evidence')
    report = {'schema': 1, 'origin': 'saved real model replay' if saved else 'real model' if args.call else 'fixture validation',
              'fixture_manifest': manifest, 'cases': []}
    for case in selected:
        name = case['id']
        independent = validation[name]
        repo = visible_repository(case)
        with tempfile.TemporaryDirectory(prefix='autotest-reference-') as folder:
            reference_root = Path(folder)
            write_files(reference_root, independent['reference_sources'])
            write_files(reference_root, case['original_tests'])
            write_files(reference_root, case['new_tests'])
            write_files(reference_root, independent['holdout_tests'])
            reference_repo = replace(repo, test_files=repo.test_files + tuple(independent['holdout_tests']))
            reference = run_tests(reference_root, reference_repo, image_id=args.image_id,
                                   artifact_root=args.artifacts / 'references')
        if not reference.run.successful or not reference.cleanup_ok:
            raise ValueError('Frozen tests do not pass the trusted reference implementation')
        print(f'{name}: trusted reference {reference.run.passed} passed', flush=True)
        if args.validate_fixtures and not (args.call or args.replay):
            report['cases'].append({'case_id': name, 'reference': asdict(reference)})
            continue
        current_client = client
        saved_case = None
        if saved:
            saved_case = next(row for row in saved['cases'] if row['case_id'] == name)
            current_client = ReplayClient(saved_case['repair']['attempts'])
        with tempfile.TemporaryDirectory(prefix='autotest-frozen-input-') as folder:
            root = Path(folder)
            write_files(root, case['sources'])
            write_files(root, case['original_tests'])
            write_files(root, case['new_tests'])
            before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            result = run_repair_session(root, repo, original_tests=tuple(case['original_tests']),
                requirements=tuple(case['requirements']), holdout_tests=tuple(independent['holdout_tests'].items()),
                client=current_client, image_id=args.image_id, artifact_root=args.artifacts / name)
            after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            if before != after:
                raise ValueError('Original frozen fixture was changed')
        if saved_case and result['status'] == 'verified':
            if (result['independent_validation']['run']['snapshot_digest'] !=
                    saved_case['repair']['independent_validation']['run']['snapshot_digest']):
                raise ValueError('Replay execution snapshot differs from real repair')
        report['cases'].append({'case_id': name, 'visible_test_origin': case['visible_test_origin'],
                                'reference': asdict(reference), 'repair': result})
        print(f'{name}: {result["status"]}; attempts={len(result["attempts"])}; task_id={result["task_id"]}', flush=True)
    args.artifacts.mkdir(parents=True, exist_ok=True)
    (args.artifacts / 'latest-summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if args.export:
        if client:
            client.ensure_safe(report)
        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if all(row.get('repair', {}).get('status', 'verified') == 'verified' for row in report['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
