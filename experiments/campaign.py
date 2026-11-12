"""Immutable schedules and durable per-slot claims. No automatic request retry."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from execution.proposals import checked_path
from experiments.formal_tasks import load_formal_tasks
from experiments.freeze import (frozen_file_digest, validate_freeze, screening_report_results)
from experiments.runner import run_experiment
from experiments.tasks import _read
from models.adapter import ModelClient, ModelError
from repair.fixtures import PROJECT_ROOT

FORMAL_SCOPE = 'P08B frozen formal experiment'
GROUPS = ('B0', 'B1', 'B2', 'A0')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def _file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _time():
    return datetime.now(timezone.utc).isoformat()


def _unlinked(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            if part.is_symlink() or getattr(part.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('linked campaign/output path rejected')
    return path.resolve()


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


def _mkdir_durable(path):
    path = _unlinked(path)
    if path.is_dir(): return
    _mkdir_durable(path.parent)
    path.mkdir(exist_ok=True)
    _sync_directory(path.parent)


def _write_json(path, value, *, exclusive=False):
    """Fsync file before replace; all campaign mutations hold the OS lease."""
    path = _unlinked(path)
    if exclusive and path.exists(): raise FileExistsError('immutable campaign record exists')
    _mkdir_durable(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.campaign-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if Path(temporary).exists(): Path(temporary).unlink()


@contextmanager
def _campaign_lock(root):
    """Kernel releases this lease on process exit; never steals a live lease."""
    root = _unlinked(root)
    _mkdir_durable(root)
    path = checked_path(root.resolve(), '.campaign.lock')
    with path.open('a+b') as stream:
        if path.stat().st_size == 0: stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('campaign already running or lease unavailable') from None
        try: yield
        finally:
            stream.seek(0)
            if os.name == 'nt': msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def build_schedule(task_ids):
    ids = tuple(sorted(task_ids))
    if (not 1 <= len(ids) <= 500 or len(set(ids)) != len(ids)
            or any(not isinstance(i, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', i) for i in ids)):
        raise ValueError('unique bounded task IDs required')
    result = []
    for repeat in (1, 2, 3):
        for index, identity in enumerate(ids):
            offset = (index + repeat - 1) % 4
            for group in GROUPS[offset:] + GROUPS[:offset]:
                result.append({'index': len(result), 'task_index': index, 'task_id': identity,
                               'group': group, 'repeat': repeat, 'key': f'{identity}:{group}:{repeat}'})
    return tuple(result)


def _admit(tasks, protocol, image_id):
    """T3 inputs stay unchanged; only the approved T4 runner delta is overlaid."""
    freeze = _read(PROJECT_ROOT/'evidence/p08b-freeze.json')
    receipt = _read(PROJECT_ROOT/'evidence/p08b-freeze-receipt.json')
    runtime = _read(PROJECT_ROOT/'experiments/p08b-campaign-runtime.json')
    if (set(runtime) != {'schema', 'freeze_commit', 'freeze_manifest_sha256', 'overrides', 'additions'}
            or runtime['schema'] != 'p08-campaign-runtime-v1'
            or runtime['freeze_commit'] != receipt['freeze_commit']
            or runtime['freeze_manifest_sha256'] != _file_sha(PROJECT_ROOT/'evidence/p08b-freeze.json')
            or runtime['freeze_manifest_sha256'] != receipt['freeze_manifest_working_sha256']
            or set(runtime['overrides']) != {'experiments/runner.py'}
            or set(runtime['additions']) != {'experiments/campaign.py', 'tests/test_campaign.py'}):
        raise ValueError('campaign runtime does not match accepted T3 freeze')
    for entry in freeze['files']:
        expected = entry['sha256']; override = runtime['overrides'].get(entry['path'])
        if override is not None:
            if set(override) != {'base_sha256', 'sha256', 'reason'} or override['base_sha256'] != expected:
                raise ValueError('runtime override does not bind its frozen base')
            expected = override['sha256']
        path = checked_path(PROJECT_ROOT, entry['path'])
        if frozen_file_digest(path.read_bytes(), entry['normalization']) != expected:
            raise ValueError('frozen source, data or implementation changed: '+entry['path'])
    for path, expected in runtime['additions'].items():
        if frozen_file_digest(checked_path(PROJECT_ROOT, path).read_bytes(), 'lf') != expected:
            raise ValueError('campaign implementation changed')
    rows = []
    for entry in freeze['screening_reports']:
        path = checked_path(PROJECT_ROOT, entry['path'])
        if _file_sha(path) != entry['sha256']: raise ValueError('screening evidence changed')
        rows.extend(screening_report_results(_read(path)))
    checks = validate_freeze(tasks, protocol, {'results': rows})
    if checks != freeze['validation'] or image_id != protocol['runtime']['image_id']:
        raise ValueError('formal tasks or image changed')
    return {'scope': FORMAL_SCOPE, 'freeze_commit': receipt['freeze_commit'],
            'freeze_sha256': runtime['freeze_manifest_sha256'], 'runtime_sha256': _digest(runtime),
            'task_input_sha256': _digest(checks['task_checksums'])}


def _safe(client, value):
    checker = getattr(client, 'ensure_safe', None)
    if checker: checker(value)


class _SlotClient:
    def __init__(self, client, directory, limit, plan_sha256, slot_id):
        self.client, self.directory, self.limit = client, directory, limit
        self.plan_sha256, self.slot_id, self.attempts = plan_sha256, slot_id, 0

    def __getattr__(self, name):
        return getattr(self.client, name)

    def complete(self, messages):
        if self.attempts >= self.limit:
            raise ModelError('budget', 'Fixed slot model request cap reached')
        self.attempts += 1
        base = self.directory/'requests'/f'{self.attempts:02}'
        record = {'plan_sha256': self.plan_sha256, 'slot_id': self.slot_id,
                  'request_index': self.attempts, 'status': 'claimed', 'time': _time()}
        _write_json(base.with_suffix('.claim.json'), record, exclusive=True)
        try:
            completion = self.client.complete(messages)
        except Exception as error:
            _write_json(base.with_suffix('.return.json'), dict(record, status='raised',
                        error_type=type(error).__name__, usage=None), exclusive=True)
            raise
        returned = dict(record, status='returned', model=completion.model, usage=completion.usage,
                        transport_metadata=getattr(self.client, 'last_metadata', None))
        _safe(self.client, returned)
        _write_json(base.with_suffix('.return.json'), returned, exclusive=True)
        return completion


def _requests(directory, plan_sha256, slot_id):
    results = []
    for claim in sorted((directory/'requests').glob('*.claim.json')):
        row = _read(claim)
        if row['plan_sha256'] != plan_sha256 or row['slot_id'] != slot_id:
            raise ValueError('request record belongs to another slot')
        returned = claim.with_name(claim.name.replace('.claim.json', '.return.json'))
        if returned.exists():
            result = _read(returned)
            if any(result[k] != row[k] for k in ('plan_sha256', 'slot_id', 'request_index')):
                raise ValueError('request return identity mismatch')
            results.append(result)
        else: results.append(dict(row, status='unknown', usage=None))
    return results


def _slot_state(root, slot, plan_sha256):
    directory = checked_path(root, 'slots/'+slot['slot_id'])
    claim, outcome = directory/'claim.json', directory/'outcome.json'
    state = dict(slot, state='pending', status=None, requests=[])
    if outcome.exists() and not claim.exists(): raise ValueError('outcome has no durable claim')
    if not claim.exists(): return state
    record = _read(claim)
    if record['plan_sha256'] != plan_sha256 or record['slot_id'] != slot['slot_id']:
        raise ValueError('slot claim does not match immutable plan')
    state['requests'] = _requests(directory, plan_sha256, slot['slot_id'])
    if outcome.exists():
        result = _read(outcome)
        if result['plan_sha256'] != plan_sha256 or result['slot_id'] != slot['slot_id']:
            raise ValueError('slot outcome does not match immutable plan')
        if result['request_records_sha256'] != _digest(state['requests']):
            raise ValueError('durable request/usage records changed; never resend')
        for path_key, hash_key in (('result_path', 'result_sha256'), ('raw_result_path', 'raw_result_sha256')):
            if result.get(path_key) is not None:
                path = checked_path(root, result[path_key])
                if not path.is_file() or _file_sha(path) != result[hash_key]:
                    raise ValueError('durable result missing or changed; never resend')
        state.update(state='finished', status=result['status'], result_path=result['result_path'])
    else:
        interruption = directory/'interrupted.json'
        if not interruption.exists():
            _write_json(interruption, {'plan_sha256': plan_sha256, 'slot_id': slot['slot_id'],
                                      'status': 'interrupted_unknown', 'time': _time(),
                                      'reason': 'Claim exists without durable outcome; no automatic resend.'}, exclusive=True)
        state.update(state='interrupted_unknown', status='interrupted_unknown')
    return state


def _report(plan, states, *, cancelled=False):
    requests = [r for state in states for r in state['requests']]
    known = [r['usage']['total_tokens'] for r in requests if isinstance(r.get('usage'), dict)
             and type(r['usage'].get('total_tokens')) is int and r['usage']['total_tokens'] >= 0]
    unknown = len(requests)-len(known)
    counts = {name: sum(s['state'] == name for s in states) for name in ('pending', 'finished', 'interrupted_unknown')}
    return {'schema': 'p08-campaign-v1', 'scope': plan['proof']['scope'], 'plan_sha256': _digest(plan),
            'attempt_kind': plan['attempt']['kind'], 'attempt': plan['attempt'],
            'freeze_commit': plan['proof']['freeze_commit'], 'status': 'cancelled' if cancelled else
            'incomplete' if counts['pending'] or counts['interrupted_unknown'] else 'complete',
            'counts': counts, 'planned_records': len(states), 'model_request_attempts': len(requests),
            'usage': {'known_total_tokens': sum(known), 'unknown_requests': unknown,
                      'total_tokens': None if unknown else sum(known)}, 'slots': states}


def run_campaign(tasks, protocol, *, client, image_id, artifact_root, export_path, cancelled=lambda: False):
    proof = _admit(tasks, protocol, image_id)
    return _execute_campaign(tasks, protocol, proof=proof, client=client, image_id=image_id,
                             artifact_root=artifact_root, export_path=export_path, cancelled=cancelled)


def _plan(tasks, protocol, proof, attempt):
    binding = _digest({'proof': proof, 'protocol': protocol})
    schedule = build_schedule(tuple(t['case']['id'] for t in tasks))
    if attempt['kind'] == 'supplemental':
        schedule = tuple(s for s in schedule if s['key'] in attempt['slot_keys'])
        binding = _digest({'primary_binding': binding, 'attempt': attempt})
    slots = [dict(s, slot_id=_digest({'binding': binding, 'key': s['key']})[:32]) for s in schedule]
    return {'schema': 'p08-campaign-plan-v1', 'proof': proof, 'protocol': protocol, 'schedule': slots, 'attempt': attempt}


def run_supplemental_attempt(tasks, protocol, *, client, image_id, base_artifact_root,
                             attempt_id, slot_keys, reason, cancelled=lambda: False):
    """Explicit additional attempt; never substitutes any primary repetition."""
    if (not isinstance(attempt_id, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,47}', attempt_id)
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000
            or not slot_keys or len(set(slot_keys)) != len(slot_keys)):
        raise ValueError('explicit unique attempt ID, reason and slot keys required')
    proof = _admit(tasks, protocol, image_id)
    base = _unlinked(base_artifact_root)
    if not base.is_dir(): raise ValueError('primary campaign must already exist')
    expected = _plan(tasks, protocol, proof, {'kind': 'primary', 'id': 'primary'})
    with _campaign_lock(base):
        original = _read(checked_path(base, 'plan.json'))
        if original != expected: raise ValueError('primary plan differs from current frozen inputs')
        states = {s['key']: _slot_state(base, s, _digest(original)) for s in original['schedule']}
        if any(k not in states or states[k]['state'] == 'pending' for k in slot_keys):
            raise ValueError('pending slots must resume normally, not count as supplemental attempts')
    attempt = {'kind': 'supplemental', 'id': attempt_id, 'reason': reason,
               'base_plan_sha256': _digest(original), 'slot_keys': sorted(slot_keys)}
    directory = base/'supplemental'/attempt_id
    return _execute_campaign(tasks, protocol, proof=proof, client=client, image_id=image_id,
                             artifact_root=directory, export_path=directory/'export.json',
                             cancelled=cancelled, attempt=attempt)


def _execute_campaign(tasks, protocol, *, proof, client, image_id, artifact_root, export_path,
                      cancelled, attempt=None):
    attempt = attempt or {'kind': 'primary', 'id': 'primary'}
    if proof['scope'] == FORMAL_SCOPE and (not isinstance(client, ModelClient)
            or client.config.model != protocol['runtime']['model'] or client.thinking_mode != 'disabled'):
        raise ValueError('formal campaign requires the frozen model adapter and thinking mode')
    root, export_path = _unlinked(artifact_root), _unlinked(export_path)
    binding = _digest({'proof': proof, 'protocol': protocol})
    if proof['scope'] == FORMAL_SCOPE:
        canonical = (PROJECT_ROOT/'artifacts/p08b-campaign'/binding[:32]).resolve()
        if attempt['kind'] == 'supplemental': canonical = canonical/'supplemental'/attempt['id']
        if root != canonical or export_path != canonical/'export.json':
            raise ValueError('formal campaign requires its canonical root and export path')
    if root in export_path.parents and export_path != root/'export.json':
        raise ValueError('export cannot replace campaign control or result records')
    plan = _plan(tasks, protocol, proof, attempt)
    slots = plan['schedule']
    plan_sha256 = _digest(plan)
    _safe(client, plan)  # In particular, explicit replay reasons must be safe before persistence.
    by_id = {t['case']['id']: t for t in tasks}
    with _campaign_lock(root):
        plan_path = checked_path(root, 'plan.json')
        if plan_path.exists():
            if _read(plan_path) != plan: raise ValueError('campaign plan changed; existing claims cannot be reused')
        else: _write_json(plan_path, plan, exclusive=True)
        if export_path.exists():
            old_export = _read(export_path)
            if old_export.get('schema') != 'p08-campaign-v1' or old_export.get('plan_sha256') != plan_sha256:
                raise ValueError('export path contains unrelated data; refusing to overwrite')
        states = [_slot_state(root, slot, plan_sha256) for slot in slots]
        def publish(stopped=False):
            report = _report(plan, states, cancelled=stopped)
            _safe(client, report)
            _write_json(export_path, report)
            return report
        publish()
        stopped = False
        for index, slot in enumerate(slots):
            if states[index]['state'] != 'pending': continue
            if cancelled(): stopped = True; break
            directory = checked_path(root, 'slots/'+slot['slot_id'])
            _write_json(directory/'claim.json', {'plan_sha256': plan_sha256, 'slot_id': slot['slot_id'],
                                               'slot': slot, 'time': _time()}, exclusive=True)
            task = by_id[slot['task_id']]
            model = None if slot['group'] == 'B0' else _SlotClient(client, directory,
                         2 if slot['group'] == 'B1' else 5, plan_sha256, slot['slot_id'])
            try:
                result = run_experiment(task['case'], task['validation'], group=slot['group'], client=model,
                                        image_id=image_id, artifact_root=root/'runs', run_id=slot['slot_id'],
                                        scope=proof['scope'], cancelled=cancelled)
                if (result['run_id'] != slot['slot_id'] or result['case_id'] != slot['task_id']
                        or result['group'] != slot['group'] or result['scope'] != proof['scope']):
                    raise ValueError('runner returned a different slot identity')
            except Exception as error:
                result = {'scope': proof['scope'], 'run_id': slot['slot_id'], 'case_id': slot['task_id'],
                          'group': slot['group'], 'status': 'campaign_execution_failed', 'error_type': type(error).__name__}
            _safe(client, result)
            result_path = f'results/{slot["slot_id"]}.json'
            _write_json(checked_path(root, result_path), result, exclusive=True)
            raw_path = f'runs/{slot["slot_id"]}/experiment.json'
            raw = checked_path(root, raw_path)
            outcome = {'plan_sha256': plan_sha256, 'slot_id': slot['slot_id'], 'status': result['status'],
                       'request_records_sha256': _digest(_requests(directory, plan_sha256, slot['slot_id'])),
                       'result_path': result_path, 'result_sha256': _file_sha(root/result_path),
                       'raw_result_path': raw_path if raw.is_file() else None,
                       'raw_result_sha256': _file_sha(raw) if raw.is_file() else None, 'time': _time()}
            _write_json(directory/'outcome.json', outcome, exclusive=True)
            states[index] = _slot_state(root, slot, plan_sha256)
            stopped = result['status'] == 'cancelled' or cancelled()
            publish(stopped)
            if stopped: break
        return publish(stopped)


def main():
    parser = argparse.ArgumentParser(description='Read-only formal campaign input verification; does not run models.')
    parser.add_argument('--verify', action='store_true', required=True)
    parser.parse_args()
    tasks = load_formal_tasks(PROJECT_ROOT/'experiments/p08b-tasks.json', PROJECT_ROOT/'validation/p08b-evaluation.json',
                              catalog_path=PROJECT_ROOT/'experiments/projects.json', reference_root=PROJECT_ROOT/'reference/p08',
                              resource_root=PROJECT_ROOT/'artifacts/p08a/image/wheels')
    protocol = _read(PROJECT_ROOT/'experiments/p08b-protocol.json')
    _admit(tasks, protocol, protocol['runtime']['image_id'])
    print('Verified frozen tasks, protocol, evidence and T4 runtime delta; 360 slots; no model calls.')
    return 0


if __name__ == '__main__': raise SystemExit(main())
