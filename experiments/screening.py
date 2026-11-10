"""No-model screening of reviewed injected candidates, never formal freezing."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import tempfile

from agents.repairer import context_digest
from execution.docker_runner import run_tests
from execution.proposals import checked_path
from experiments.formal_tasks import load_formal_tasks
from experiments.tasks import _read
from repair.fixtures import PROJECT_ROOT
from repair.session import _signature
from workflow.state import FailureKind, Repository

TYPES = ('boundary', 'null_type', 'exception_handling')


def review_digest(task):
    payload = {'public': task['provenance']['task'], 'evaluation': task['evaluation']}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def screen_candidate(task, *, image_id, artifact_root):
    case, validation = task['case'], task['validation']
    identity = case['id']
    if not isinstance(identity, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', identity):
        raise ValueError('invalid candidate identity')
    directory = Path(artifact_root).resolve()/identity
    directory.mkdir(parents=True, exist_ok=False)
    public = task['provenance']['task']
    record = {'candidate_id': identity, 'project_id': public['project_id'], 'target': list(case['target']),
              'defect_type': task['evaluation']['defect_type'], 'status': 'claimed', 'stage': 'review',
              'mutation': task['evaluation']['mutation'], 'requirements': list(case['requirements']),
              'requirement_sources': public['requirement_sources'], 'semantic_review': task.get('semantic_review'),
              'image_id': image_id, 'review_input_sha256': review_digest(task), 'executions': []}
    def save(status, reason):
        record.update(status=status, reason=reason)
        temporary = directory/'screening.tmp'
        temporary.write_text(json.dumps(record, indent=2), encoding='utf-8')
        temporary.replace(directory/'screening.json')
        return record
    save('claimed', 'candidate claimed; never automatically rerun')
    review = task.get('semantic_review') or {}
    current_review = (review.get('input_sha256') == record['review_input_sha256']
                      and review.get('reviewer') == public['reviewer'] and review.get('reviewer') != public['owner']
                      and isinstance(review.get('notes'), str) and bool(review['notes'].strip()))
    if current_review and review.get('status') == 'rejected':
        return save('semantic_rejected', 'independent reviewer rejected the oracle; retained without execution')
    if not current_review or review.get('status') != 'approved':
        return save('pending_review', 'independent semantic review missing or stale')
    if image_id != public['protocol']['image_id']:
        return save('environment_failed', 'image differs from reviewed protocol')
    if public['project_id'] == 'markupsafe' and case['target'][1] == 'escape_silent':
        return save('excluded_pilot', 'reserved P08A function')
    signatures = {}
    try:
        for repeat in (1, 2):
            for label in ('reference_original', 'reference_holdout', 'mutant_original', 'mutant_holdout'):
                stage = f'{label}_{repeat}'
                record['stage'] = stage
                save('running', 'executing immutable approved inputs')
                sources = validation['reference_sources'] if label.startswith('reference') else case['sources']
                tests = dict(case['original_tests'])
                if label.endswith('holdout'):
                    if {p.casefold() for p in tests} & {p.casefold() for p in validation['holdout_tests']}:
                        return save('invalid_mutation', 'visible and private inventories overlap')
                    tests.update(validation['holdout_tests'])
                support = dict(case.get('support', {}))
                digest = context_digest(tuple(sources.items()), tuple(tests.items()),
                                        tuple((p, hashlib.sha256(b).hexdigest()) for p, b in support.items()))
                with tempfile.TemporaryDirectory(prefix='autotest-p08-screen-') as folder:
                    root = Path(folder).resolve()
                    for path, data in {**sources, **tests, **support}.items():
                        file = checked_path(root, path)
                        file.parent.mkdir(parents=True, exist_ok=True)
                        file.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)
                    repo = Repository(public['project_id'], digest, tuple(sources), tuple(tests))
                    result = run_tests(root, repo, image_id=image_id, artifact_root=directory/stage,
                                       support_files=tuple(support),
                                       test_targets=tuple(validation['holdout_tests']) if label.endswith('holdout') else ())
                record['executions'].append({'stage': stage, 'result': asdict(result)})
                run = result.run
                if run.snapshot_digest != digest or not result.cleanup_ok:
                    return save('environment_failed', 'input digest mismatch or cleanup failed')
                signature = ((run.snapshot_digest, run.exit_code, run.passed, run.failed, run.errors, run.skipped)
                             if run.successful else _signature(result) if run.failure and run.failure.kind == FailureKind.TEST_ASSERTION else None)
                if repeat == 2 and (signature is None or signature != signatures[label]):
                    return save('unstable', 'second verification differs in inputs, counts or assertion identity')
                signatures[label] = signature
                if not run.successful:
                    if not run.failure or run.failure.kind != FailureKind.TEST_ASSERTION:
                        return save('environment_failed', 'collection/environment/timeout is not a semantic detection')
                    if label.startswith('reference'):
                        return save('reference_failed', 'correct version did not satisfy approved tests')
                    if label == 'mutant_original':
                        return save('detected_by_original', 'original regression detects mutation; excluded from survivor group')
                elif label == 'mutant_holdout':
                    return save('holdout_not_detected', 'private tests did not detect mutation; equivalence not established')
        return save('eligible', 'reviewed, original-regression surviving, privately detected and stable twice')
    except KeyboardInterrupt:
        save('interrupted', 'interrupted; preserve claim and do not resume this candidate silently')
        raise
    except (ValueError, OSError, TypeError, KeyError) as error:
        record['error_type'] = type(error).__name__
        return save('environment_failed', 'screening infrastructure failed; no semantic conclusion')


def quota_matrix(results, *, project_ids):
    matrix = []
    for project in project_ids:
        eligible = sorted((r for r in results if r['project_id'] == project and r['status'] == 'eligible'),
                          key=lambda r: (r['defect_type'], *r['target'], r['candidate_id']))
        options = {kind: sorted({tuple(r['target']) for r in eligible if r['defect_type'] == kind}) for kind in TYPES}
        # Maximum matching: six quota slots may not share a function, even across classes.
        assigned = {}
        def assign(slot, seen):
            for function in options[slot[0]]:
                if function in seen:
                    continue
                seen.add(function)
                if function not in assigned or assign(assigned[function], seen):
                    assigned[function] = slot
                    return True
            return False
        for kind in TYPES:
            for index in range(2):
                assign((kind, index), set())
        for kind in TYPES:
            count = sum(slot[0] == kind for slot in assigned.values())
            matrix.append({'project_id': project, 'defect_type': kind, 'required': 2,
                           'eligible_candidates': len(options[kind]), 'eligible_unique': count, 'shortfall': 2-count})
    return matrix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool', type=Path, default=PROJECT_ROOT/'validation/p08b-candidates.json')
    parser.add_argument('--project', action='append')
    parser.add_argument('--artifacts', type=Path, default=PROJECT_ROOT/'artifacts/p08b-screening')
    parser.add_argument('--export', type=Path, required=True)
    args = parser.parse_args()
    if args.export.exists():
        raise FileExistsError('preserve prior screening export')
    pool = _read(args.pool)
    if set(pool) != {'schema', 'public_manifest', 'evaluator_manifest', 'candidate_order', 'semantic_reviews'} or pool['schema'] != 'p08-candidate-pool-v1':
        raise ValueError('unknown candidate pool')
    tasks = load_formal_tasks(checked_path(PROJECT_ROOT, pool['public_manifest']), checked_path(PROJECT_ROOT, pool['evaluator_manifest']),
                              catalog_path=PROJECT_ROOT/'experiments/projects.json', reference_root=PROJECT_ROOT/'reference/p08',
                              resource_root=PROJECT_ROOT/'artifacts/p08a/image/wheels')
    tasks = sorted(tasks, key=lambda t: (t['provenance']['task']['project_id'], t['evaluation']['defect_type'],
                                        *t['case']['target'], t['case']['id']))
    if pool['candidate_order'] != [t['case']['id'] for t in tasks]:
        raise ValueError('candidate order differs from predeclared fixed order')
    projects = sorted({t['provenance']['task']['project_id'] for t in tasks})
    if args.project and not set(args.project).issubset(projects):
        raise ValueError('unknown project')
    report = {'schema': 1, 'scope': 'P08B T2 no-model candidate screening, not formal results',
              'pool_sha256': hashlib.sha256(args.pool.read_bytes()).hexdigest(), 'status': 'claimed', 'results': []}
    args.export.parent.mkdir(parents=True, exist_ok=True)
    with args.export.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    for task in tasks:
        project = task['provenance']['task']['project_id']
        if args.project and project not in args.project:
            continue
        task['semantic_review'] = pool['semantic_reviews'].get(task['case']['id'])
        row = screen_candidate(task, image_id=task['provenance']['task']['protocol']['image_id'], artifact_root=args.artifacts)
        report['results'].append(row)
        report['quota_matrix'] = quota_matrix(report['results'], project_ids=projects)
        report['status'] = 'running'
        args.export.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(row['candidate_id'] + ': ' + row['status'], flush=True)
    report['status'] = 'complete'
    report['freeze_recommended'] = all(c['shortfall'] == 0 for c in report['quota_matrix'])
    args.export.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
