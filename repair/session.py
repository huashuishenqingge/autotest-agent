"""Three-round repair on an owned copy; withheld validation never feeds the model."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

from agents.repairer import RepairContext, Repairer, PROMPT_VERSION
from execution.docker_runner import run_tests, stage_snapshot
from execution.proposals import ApplyError, apply_proposal, checked_path, rollback
from workflow.state import FailureKind, Policy, RepairInput


def _failure_evidence(result):
    path = Path(result.artifact_directory) / 'junit.xml'
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError('missing or excessive failure evidence')
    xml = path.read_bytes()
    if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
        raise ValueError('unsafe failure evidence')
    try:
        tree = ET.fromstring(xml)
    except ET.ParseError:
        raise ValueError('invalid failure evidence') from None
    failures = []
    for case in tree.iter('testcase'):
        failure = case.find('failure')
        if failure is not None:
            if not case.get('name'):
                raise ValueError('failure testcase identity required')
            failures.append((case.get('classname', ''), case.get('name'),
                             ' '.join(failure.get('message', '').split()),
                             ' '.join(''.join(failure.itertext()).split())))
    if len(failures) != result.run.failed or not failures:
        raise ValueError('failure evidence count mismatch')
    return tuple(sorted(failures))


def _signature(result):
    run = result.run
    return (run.snapshot_digest, run.exit_code, run.passed, run.failed, run.errors, run.skipped,
            run.failure.kind if run.failure else None, result.cleanup_ok, _failure_evidence(result))


def run_repair_session(root, repository, *, original_tests, requirements, holdout_tests,
                       client, image_id, artifact_root, policy=None):
    policy = policy or Policy()
    root = Path(root).resolve(strict=True)
    if not original_tests or not set(original_tests).issubset(repository.test_files):
        raise ValueError('explicit nonempty original test inventory required')
    holdout_tests = tuple(holdout_tests)
    known = {p.casefold() for p in repository.source_files + repository.test_files}
    if (not holdout_tests or len({p.casefold() for p, _ in holdout_tests}) != len(holdout_tests)
            or any(p.casefold() in known for p, _ in holdout_tests)):
        raise ValueError('nonoverlapping independent test inventory required')
    task_id = uuid.uuid4().hex
    directory = Path(artifact_root).resolve() / task_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {'task_id': task_id, 'project_id': repository.project_id, 'base_revision': repository.revision,
              'prompt_version': PROMPT_VERSION, 'status': 'failed', 'attempts': [],
              'baseline': None, 'initial_failure': None, 'reproduction': None,
              'independent_validation': None, 'final_sources': None, 'elapsed_seconds': None}
    started = time.monotonic()
    secret = getattr(getattr(client, 'config', None), 'api_key', '')
    secrets = (secret,) if secret else ()
    try:
        with tempfile.TemporaryDirectory(prefix='autotest-repair-') as folder:
            working = Path(folder).resolve()
            stage_snapshot(root, working, repository)
            # Stage input is read-only. Only approved source files in this owned
            # copy become writable for atomic replacement, never the user's root.
            for path in repository.source_files:
                checked_path(working, path).chmod(0o600)
            for path, _ in holdout_tests:
                checked_path(working, path)  # validate before any model call

            def execute(repo):
                return run_tests(working, repo, image_id=image_id, artifact_root=directory / 'execution',
                                 policy=policy, secrets=secrets)

            baseline_repo = replace(repository, test_files=tuple(original_tests))
            baseline = execute(baseline_repo)
            record['baseline'] = asdict(baseline)
            if not baseline.run.successful or not baseline.cleanup_ok:
                record['status'] = 'baseline_failed'
                return record
            failed = execute(repository)
            record['initial_failure'] = asdict(failed)
            if not failed.run.failure or failed.run.failure.kind != FailureKind.TEST_ASSERTION or not failed.cleanup_ok:
                record['status'] = 'not_a_repairable_assertion'
                return record
            reproduced = execute(repository)
            record['reproduction'] = asdict(reproduced)
            initial_signature = _signature(failed)
            if initial_signature != _signature(reproduced):
                record['status'] = 'not_reproducible'
                return record
            failed = reproduced
            for attempt in range(1, min(3, policy.max_fix_attempts) + 1):
                sources = tuple((p, checked_path(working, p).read_bytes().decode('utf-8')) for p in repository.source_files)
                hashes = tuple((p, hashlib.sha256(checked_path(working, p).read_bytes()).hexdigest()) for p in repository.source_files)
                tests = tuple((p, checked_path(working, p).read_bytes().decode('utf-8')) for p in repository.test_files)
                context = RepairContext(tuple(requirements), hashes, tests)
                log_path = Path(failed.artifact_directory) / 'log.txt'
                log = log_path.read_text(encoding='utf-8')
                request = RepairInput(task_id, repository, policy, failed.run, attempt, sources, log)
                agent = Repairer(client, context)
                attempt_started = time.monotonic()
                output = agent.repair(request)
                completion = agent.last_completion
                item = {'attempt': attempt, 'based_on_run_id': failed.run.run_id,
                        'input_digest': failed.run.snapshot_digest, 'source_hashes': hashes,
                        'proposal': asdict(output), 'model_candidate': agent.last_safe_candidate,
                        'completion': None if completion is None else {
                            'model': completion.model, 'usage': completion.usage,
                            'usage_available': completion.usage is not None},
                        'transport_metadata': getattr(client, 'last_metadata', None),
                        'patch_diff': None, 'visible_run': None, 'original_regression_run': None,
                        'rollback_ok': None, 'refreshed_failure': None,
                        'elapsed_seconds': round(time.monotonic() - attempt_started, 3)}
                record['attempts'].append(item)
                if output.failure:
                    record['status'] = output.failure.kind.value
                    return record
                receipt = apply_proposal(working, request, output)
                item['patch_diff'] = receipt.diff
                (directory / f'attempt-{attempt}.diff').write_text(receipt.diff, encoding='utf-8')
                visible = execute(repository)
                item['visible_run'] = asdict(visible)
                if visible.run.successful and visible.cleanup_ok:
                    # Add private tests only to a separate evaluation copy.
                    with tempfile.TemporaryDirectory(prefix='autotest-validation-') as validation_folder:
                        validation = Path(validation_folder).resolve()
                        stage_snapshot(working, validation, repository)
                        for path, content in holdout_tests:
                            target = checked_path(validation, path)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(content.encode('utf-8'))
                        independent_repo = replace(repository, test_files=repository.test_files + tuple(p for p, _ in holdout_tests))
                        independent = run_tests(validation, independent_repo, image_id=image_id,
                            artifact_root=directory / 'independent', policy=policy, secrets=secrets)
                    record['independent_validation'] = asdict(independent)
                    if independent.run.successful and independent.cleanup_ok:
                        record['final_sources'] = tuple((p, checked_path(working, p).read_bytes().decode('utf-8'))
                                                        for p in repository.source_files)
                        record['status'] = 'verified'
                        return record
                    rollback(working, receipt)
                    item['rollback_ok'] = True
                    record['status'] = 'independent_validation_failed'
                    return record  # no private failure feedback to a model
                original = execute(baseline_repo)
                item['original_regression_run'] = asdict(original)
                item['classification'] = 'regression' if not original.run.successful else (
                    visible.run.failure.kind.value if visible.run.failure else 'environment')
                rollback(working, receipt)
                item['rollback_ok'] = True
                if (not visible.cleanup_ok or not original.cleanup_ok or not visible.run.failure
                        or visible.run.failure.kind != FailureKind.TEST_ASSERTION):
                    record['status'] = 'patch_execution_failed'
                    return record
                # Never diagnose a reverted patch using its old failure run.
                failed = execute(repository)
                item['refreshed_failure'] = asdict(failed)
                if (not failed.cleanup_ok or not failed.run.failure
                        or failed.run.failure.kind != FailureKind.TEST_ASSERTION):
                    record['status'] = 'restored_state_not_repairable'
                    return record
                if _signature(failed) != initial_signature:
                    record['status'] = 'not_reproducible'
                    return record
            record['status'] = 'budget_exhausted'
    except ApplyError as error:
        record['status'] = error.kind.value
    except (OSError, ValueError, UnicodeError):
        record['status'] = 'internal'
    finally:
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (directory / 'repair.json').write_text(json.dumps(record, ensure_ascii=True, indent=2), encoding='utf-8')
    return record
