"""Rebuild V2-06 offline evidence from authenticated local V2-05 artifacts.

Run: python -m evaluation.report_v2 --root PATH
Requires the ignored original campaign folder, not just the Git exports.
"""
import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET

from evaluation.experiments import evaluate


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(root, path):
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError('evidence path escapes root')
    return json.loads(candidate.read_text(encoding='utf-8'))


def write(root, name, value):
    (root / 'evidence' / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def junit(campaign, slot, result):
    matches = list((campaign/'runs'/slot).rglob(result['run']['run_id']+'/junit.xml'))
    if len(matches) != 1:
        raise ValueError('missing or ambiguous JUnit evidence')
    data = matches[0].read_bytes()
    if len(data) > 2_000_000 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('unsafe JUnit evidence')
    return ET.fromstring(data)


def signature_details(tree):
    return sorted((t.get('classname',''), t.get('name'),
        ' '.join(f.get('message','').split()), ' '.join(''.join(f.itertext()).split()))
        for t in tree.iter('testcase') for f in t if f.tag == 'failure')


def rebuild(root):
    receipt = read(root, 'evidence/p08b-v2-formal-receipt.json')
    for item in receipt['files']:
        data = (root / item['path']).read_bytes().replace(b'\r\n', b'\n')
        if digest(data) != item['lf_sha256']:
            raise ValueError('original receipt mismatch: ' + item['path'])
    export = read(root, 'evidence/p08b-v2-formal-results.json')
    sidecar = read(root, 'evidence/p08b-v2-06-semantic-reviews.json')
    rows = export['records']
    for row in rows:
        data = (root / row['result_path']).read_bytes()
        if digest(data) != row['result_sha256']:
            raise ValueError('raw result hash mismatch')
        row['detail'] = json.loads(data)
        if (row['detail']['run_id'] != row['slot_id'] or row['detail']['group'] != row['group']
                or row['detail']['status'] != row['status']):
            raise ValueError('raw slot identity mismatch')
    slots = {r['slot_id']: r for r in rows}
    # Rebuild every binding from frozen task, actual applied diff and full proposal.
    from experiments.formal_tasks_v2 import load_formal_tasks, _canonical
    from experiments.functions import locate_function, replace_function
    from experiments.screening import review_digest
    tasks = load_formal_tasks(root/'experiments/p08b-v2-tasks.json',
        root/'validation/p08b-v2-evaluation.json', catalog_path=root/'experiments/projects.json',
        reference_root=root/'reference/p08', resource_root=root/'artifacts/p08a/image/wheels')
    tasks = {t['case']['id']: t for t in tasks}
    for row in rows:
        detail = row['detail']
        if set(detail['coverage_sources']) != set(tasks[row['task_id']]['case']['coverage_sources']):
            raise ValueError('coverage source inventory differs from frozen task')
        for value in detail['coverage'].values():
            if set(value['files']) != set(detail['coverage_sources']):
                raise ValueError('coverage missing primary source file')
    bindings = []
    for patch in read(root, 'evidence/p08b-v2-formal-patches.json')['patches']:
        row = slots[patch['slot_id']]
        task = tasks[row['task_id']]
        path, name = task['case']['target']
        before = task['case']['sources'][path]
        repair = row['detail']['repair']
        attempt = next(a for a in repair['attempts'] if a['attempt'] == patch['attempt'])
        files = attempt['proposal']['files']
        if len(files) != 1 or files[0]['path'] != path:
            raise ValueError('proposal outside target')
        after = files[0]['content']
        if (files[0]['expected_sha256'] != digest(before.encode())
                or replace_function(before, name, locate_function(after, name)['code']) != after):
            raise ValueError('proposal changed unrelated source')
        hashes = {'task_input_sha256': review_digest(task),
                  'diff_sha256': digest(patch['patch_diff'].encode()),
                  'before_source_sha256': digest(before.encode()),
                  'after_source_sha256': digest(after.encode())}
        last = patch['attempt'] == repair['attempts'][-1]['attempt']
        if row['status'] == 'verified' and last and dict(repair['final_sources'])[path] != after:
            raise ValueError('last proposal not final applied source')
        bindings.append(dict(patch_id=f"{row['slot_id']}:{patch['attempt']}:{hashes['diff_sha256'][:16]}",
            slot_id=row['slot_id'], task_id=row['task_id'], attempt=patch['attempt'],
            review_key=_canonical(hashes), is_last_attempt=last, **hashes))
    reviews = sidecar['patches']
    metrics = evaluate(rows, bindings, reviews)
    metrics.update(schema='p08-v2-metrics-v1', data_commit=receipt['data_commit'],
        original_receipt_sha256=digest((root/'evidence/p08b-v2-formal-receipt.json').read_bytes()),
        semantic_sidecar_lf_sha256=digest((root/'evidence/p08b-v2-06-semantic-reviews.json').read_bytes().replace(b'\r\n',b'\n')),
        semantic_patch_counts=dict(Counter(p['status'] for p in reviews)),
        semantic_group_counts=dict(Counter(g['status'] for g in sidecar['groups'])))
    reconciliation = []
    failures = []
    for row in rows:
        detail = row['detail']
        unknown = [q for q in row['requests'] if q.get('usage') is None]
        candidates = [(n, g) for n, g in enumerate(detail['rounds'], 1)
                      if g.get('completion') is None and g.get('transport_metadata')]
        for request in unknown:
            # One unknown in slot + one failed generation event, ordered round index
            # equals request index (all generation requests precede repair requests).
            matched = (len(unknown) == len(candidates) == 1
                       and candidates[0][0] == request['request_index'])
            meta = candidates[0][1]['transport_metadata'] if matched else None
            usage = meta.get('usage') if meta else None
            valid = usage and all(isinstance(usage.get(k), int) and usage[k] >= 0
                for k in ('prompt_tokens','completion_tokens','total_tokens'))
            valid = valid and usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens']
            reconciliation.append({'slot_id': row['slot_id'], 'task_id': row['task_id'],
                'group': row['group'], 'request_index': request['request_index'],
                'round': candidates[0][0] if matched else None,
                'matched': bool(valid), 'transport_metadata': meta,
                'supplemental_tokens': usage['total_tokens'] if valid else None})
        if row['status'] in ('verified', 'baseline_survives'):
            continue
        item = {'slot_id': row['slot_id'], 'task_id': row['task_id'], 'group': row['group'],
                'status': row['status'], 'result_sha256': row['result_sha256']}
        if row['status'] == 'generation_failed':
            generation = detail['rounds'][-1]
            item.update(category=(generation['generation']['failure'] or {}).get('message'),
                        finish_reason=(generation.get('transport_metadata') or {}).get('finish_reason'))
        else:
            execution = detail['executions'][-1]
            run = execution['result']['run']
            item.update(category=(run.get('failure') or {}).get('kind', 'no_failure'),
                        last_label=execution['label'], exit_code=run['exit_code'])
            # Find portable canonical run directory by run ID, never trust stale
            # absolute artifact_directory from another machine.
            run_id = run['run_id']
            campaign = (root/row['result_path']).parent.parent
            xml = junit(campaign, row['slot_id'], execution['result'])
            item['junit_failures'] = [dict(classname=t.get('classname'), name=t.get('name'),
                    kind=child.tag, message=(child.get('message') or '')[:1000])
                    for t in xml.iter('testcase') for child in t if child.tag in ('failure', 'error')]
            if row['status'] == 'repair_not_reproducible':
                reproduction = detail['repair'].get('reproduction')
                item['repair_reproduction'] = reproduction['run'] if reproduction else None
                initial = detail['repair']['initial_failure']
                if reproduction:
                    a = signature_details(junit(campaign, row['slot_id'], initial))
                    b = signature_details(junit(campaign, row['slot_id'], reproduction))
                    keys = ('snapshot_digest','exit_code','passed','failed','errors','skipped')
                    item['reproduction_comparison'] = {
                        'run_fields_changed': [k for k in keys if initial['run'][k] != reproduction['run'][k]],
                        'failed_test_identities_equal': [v[:2] for v in a] == [v[:2] for v in b],
                        'normalized_messages_equal': [v[:3] for v in a] == [v[:3] for v in b],
                        'normalized_tracebacks_equal': a == b,
                        'equal_after_address_mask_diagnostic': re.sub(r'0x[0-9a-fA-F]+', '<address>', json.dumps(a)) == re.sub(r'0x[0-9a-fA-F]+', '<address>', json.dumps(b)),
                        'initial_failure_count': len(a), 'reproduced_failure_count': len(b),
                        'first_different_evidence': next(({'initial':x,'reproduced':y} for x,y in zip(a,b) if x!=y), None)}
        failures.append(item)
    known = sum(g['usage']['known_total_tokens'] for g in metrics['groups'].values())
    recovered = sum(x['supplemental_tokens'] or 0 for x in reconciliation)
    unresolved = sum(not x['matched'] for x in reconciliation)
    usage = {'original_wrapper_known_tokens': known, 'original_wrapper_unknown_requests': len(reconciliation),
             'original_total_tokens': None if reconciliation else known,
             'supplemental_transport_tokens': recovered, 'unresolved_requests': unresolved,
             'transport_reconciled_total_tokens': None if unresolved else known + recovered,
             'limits': 'Provider transport-reported usage, not invoice or exact billed cost; wrapper aggregate unchanged. Successful-round metadata is not added again.',
             'records': reconciliation}
    metrics['failure_counts'] = dict(Counter(f['status'] for f in failures))
    metrics['repaired_coverage_records'] = [{'slot_id':r['slot_id'], 'group':r['group'],
        'status':r['status'], 'lines':r['coverage']['repaired-visible']['lines'],
        'branches':r['coverage']['repaired-visible']['branches']}
        for r in rows if 'repaired-visible' in r['coverage']]
    write(root, 'p08b-v2-06-metrics.json', metrics)
    write(root, 'p08b-v2-06-usage-reconciliation.json', usage)
    write(root, 'p08b-v2-06-failure-analysis.json', {'records': failures,
        'categories': dict(Counter(f['status']+':'+str(f['category']) for f in failures))})
    # Compact, portable, authenticated derivation inputs let collaborators rerun
    # pure metrics without shipping the ignored upstream execution snapshots.
    normalized = []
    for row in rows:
        item = {k: row[k] for k in ('slot_id','task_id','group','repeat','project_id','defect_type',
                                   'status','elapsed_seconds','requests','coverage','result_sha256')}
        d = row['detail']
        item['detail'] = {'executions': [{'label':e['label'], 'result':{
            'cleanup_ok':e['result']['cleanup_ok'], 'run':e['result']['run']}} for e in d['executions']],
            'generated_only': None if not d.get('generated_only') else {
                'cleanup_ok': d['generated_only']['cleanup_ok'], 'run':d['generated_only']['run']},
            'rounds':[{'generation':{'files':[{'path':f['path']} for f in g['generation']['files']],
                                    'failure':g['generation']['failure']}} for g in d['rounds']],
            'repair': None if d.get('repair') is None else {'attempts':[
                {'attempt':a['attempt']} for a in d['repair']['attempts']]}}
        normalized.append(item)
    write(root, 'p08b-v2-06-metric-inputs.json', {'scope':'Derived metrics only; not original execution snapshots',
        'records': normalized, 'bindings': bindings})
    with (root/'evidence/p08b-v2-06-summary.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        fields = ['group','planned','detected','technical_verified','semantic_confirmed','semantic_rate',
                  'repair_session_entered','slots_with_applied_patch','applied_patch_attempts']
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for group, values in metrics['groups'].items():
            writer.writerow(dict(group=group, **values))
    print(json.dumps({'groups': {g: {k: v[k] for k in ('detected','technical_verified','semantic_confirmed')}
        for g,v in metrics['groups'].items()}, 'usage': {k:v for k,v in usage.items() if k!='records'},
        'patches': metrics['semantic_patch_counts']}, ensure_ascii=False))
    return metrics


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--portable', action='store_true', help='Compare pure metrics using Git-shared derived inputs; does not re-audit raw execution')
    args = parser.parse_args()
    if args.portable:
        inputs = read(args.root, 'evidence/p08b-v2-06-metric-inputs.json')
        reviews = read(args.root, 'evidence/p08b-v2-06-semantic-reviews.json')['patches']
        actual = evaluate(inputs['records'], inputs['bindings'], reviews)
        stored = read(args.root, 'evidence/p08b-v2-06-metrics.json')
        if any(actual[k] != stored[k] for k in actual):
            raise ValueError('portable metrics differ')
        print('Portable metrics match; original snapshots not re-audited.')
    else:
        rebuild(args.root.resolve())
