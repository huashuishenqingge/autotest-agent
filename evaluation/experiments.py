"""Offline descriptive metrics. No execution, model calls or evidence mutation."""
from collections import Counter
from math import isfinite
from statistics import mean, median

GROUPS = ('B0', 'B1', 'B2', 'A0')
BINDING_FIELDS = ('slot_id', 'task_id', 'attempt', 'review_key',
                  'task_input_sha256', 'diff_sha256', 'before_source_sha256',
                  'after_source_sha256', 'is_last_attempt')


def _run(result, assertion=False):
    if not result or result.get('cleanup_ok') is not True:
        return False
    run = result.get('run', {})
    if assertion:
        return (run.get('exit_code') == 1 and run.get('failed', 0) > 0
                and run.get('errors') == 0
                and (run.get('failure') or {}).get('kind') == 'test_assertion')
    return (run.get('exit_code') == 0 and run.get('passed', 0) > 0
            and run.get('failed') == 0 and run.get('errors') == 0)


def _detected(row):
    detail = row['detail']
    executions = {e['label']: e['result'] for e in detail['executions']}
    if row['group'] == 'B0':
        return _run(executions.get('baseline'), True)
    required = 1 if row['group'] == 'B1' else 2
    rounds = detail.get('rounds', [])
    if len(rounds) != required:
        return False
    for number, item in enumerate(rounds, 1):
        generation = item.get('generation') or {}
        if (not generation.get('files') or generation.get('failure') is not None
                or not _run(executions.get(f'reference-{number}'))):
            return False
    return (_run(detail.get('generated_only'), True)
            and _run(executions.get(f'round-{required}'), True))


def _coverage(rows):
    lines, branches = [], []
    missing = mismatches = 0
    for row in rows:
        values = row.get('coverage', {})
        labels = [k for k in values if k.startswith('round-') and k[6:].isdigit()]
        if 'baseline' not in values or not labels:
            missing += 1
            continue
        before = values['baseline']
        after = values[max(labels, key=lambda k: int(k[6:]))]
        if any(before[k]['total'] != after[k]['total'] for k in ('lines', 'branches')):
            mismatches += 1
            continue
        for kind, output in (('lines', lines), ('branches', branches)):
            total = before[kind]['total']
            if total:
                output.append(100 * (after[kind]['covered'] - before[kind]['covered']) / total)
    return {'paired_records': len(lines), 'branch_paired_records': len(branches),
            'missing_records': missing, 'denominator_mismatches': mismatches,
            'line_delta_pp_mean': mean(lines) if lines else None,
            'branch_delta_pp_mean': mean(branches) if branches else None}


def _summarize(rows, confirmed, bindings):
    slots = {r['slot_id'] for r in rows}
    requests = [q for r in rows for q in r['requests']]
    unknown = sum(q.get('usage') is None for q in requests)
    known = sum(q['usage']['total_tokens'] for q in requests if q.get('usage') is not None)
    durations = [r['elapsed_seconds'] for r in rows
                 if isinstance(r.get('elapsed_seconds'), (int, float))
                 and isfinite(r['elapsed_seconds']) and r['elapsed_seconds'] >= 0]
    n = len(rows)
    technical = sum(r['status'] == 'verified' for r in rows)
    semantic = len(slots & confirmed)
    entered = sum(r['detail'].get('repair') is not None for r in rows)
    applied = [b for b in bindings if b['slot_id'] in slots]
    return {'planned': n, 'status_counts': dict(Counter(r['status'] for r in rows)),
            'technical_verified': technical, 'technical_rate': technical / n,
            'semantic_confirmed': semantic, 'semantic_rate': semantic / n,
            'detected': sum(_detected(r) for r in rows),
            'repair_session_entered': entered,
            'slots_with_applied_patch': len({b['slot_id'] for b in applied}),
            'applied_patch_attempts': len(applied),
            'semantic_given_repair_session': semantic / entered if entered else None,
            'generation_coverage': _coverage(rows),
            'elapsed': {'samples': len(durations), 'total_seconds': sum(durations),
                        'mean_seconds': mean(durations) if durations else None,
                        'median_seconds': median(durations) if durations else None},
            'usage': {'request_attempts': len(requests), 'known_total_tokens': known,
                      'unknown_requests': unknown, 'total_tokens': None if unknown else known}}


def evaluate(records, bindings, reviews, *, expected_per_group=90):
    """Require complete groups and one hash-matched review per applied patch.

    The caller authenticates source/result hashes before attaching ``detail``.
    Conditional and coverage denominators are explicit; missing data stays missing.
    """
    slots = {r['slot_id']: r for r in records}
    if len(slots) != len(records):
        raise ValueError('duplicate slot')
    counts = Counter(r['group'] for r in records)
    if counts != Counter({g: expected_per_group for g in GROUPS}):
        raise ValueError('incomplete fixed group denominator')
    identities = [(r['task_id'], r['group'], r['repeat']) for r in records]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate task/group/repeat')
    by_patch = {r['patch_id']: r for r in reviews}
    if (len(by_patch) != len(reviews) or len({b['patch_id'] for b in bindings}) != len(bindings)
            or set(by_patch) != {b['patch_id'] for b in bindings}):
        raise ValueError('missing, extra or duplicate patch review')
    confirmed = set()
    for binding in bindings:
        review = by_patch[binding['patch_id']]
        if any(review.get(k) != binding[k] for k in BINDING_FIELDS):
            raise ValueError('stale patch review binding')
        row = slots.get(binding['slot_id'])
        if not row or row['task_id'] != binding['task_id']:
            raise ValueError('unknown patch slot/task')
        if review.get('status') not in ('confirmed', 'rejected', 'uncertain'):
            raise ValueError('unresolved patch review')
        if not review.get('reviewer') or not review.get('notes'):
            raise ValueError('review requires attribution and rationale')
        if (row['status'] == 'verified' and binding['is_last_attempt']
                and review['status'] == 'confirmed'):
            confirmed.add(row['slot_id'])
    output = {'groups': {g: _summarize([r for r in records if r['group'] == g], confirmed, bindings)
                         for g in GROUPS}, 'by_project': {}, 'by_defect_type': {}}
    for field, key in (('project_id', 'by_project'), ('defect_type', 'by_defect_type')):
        for value in sorted({r[field] for r in records}):
            output[key][value] = {g: _summarize([r for r in records if r[field] == value and r['group'] == g], confirmed, bindings)
                                  for g in GROUPS if any(r[field] == value and r['group'] == g for r in records)}
    output['tasks'] = [{'task_id': task, 'semantic_confirmed': {
        g: sum(r['slot_id'] in confirmed for r in records if r['task_id'] == task and r['group'] == g)
        for g in GROUPS}} for task in sorted({r['task_id'] for r in records})]
    differences = [t['semantic_confirmed']['A0'] - t['semantic_confirmed']['B2'] for t in output['tasks']]
    output['a0_vs_b2_tasks'] = {'a0_more': sum(d > 0 for d in differences),
                               'equal': differences.count(0), 'b2_more': sum(d < 0 for d in differences)}
    return output
