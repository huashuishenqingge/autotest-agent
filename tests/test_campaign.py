import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from experiments.campaign import (build_schedule, run_campaign, run_supplemental_attempt,
                                  _write_json, _campaign_lock, _admit)
from experiments.freeze import make_protocol
from models.adapter import Completion, ModelError, ModelClient


class PowerLoss(BaseException):
    pass


class Client:
    def __init__(self, usage=None):
        self.calls = 0
        self.usage = usage

    def complete(self, messages):
        self.calls += 1
        return Completion('{}', 'offline-test-model', self.usage)


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tasks = ({'case': {'id': 'synthetic'}, 'validation': {}},)
        self.protocol = make_protocol(['synthetic'], source_commit='a'*40)
        self.proof = {'scope': 'P08B scheduler synthetic validation only', 'freeze_sha256': 'b'*64,
                      'runtime_sha256': 'c'*64, 'freeze_commit': 'a'*40, 'task_input_sha256': 'd'*64}
        self.runner_calls = []

    def test_admission_checks_explicit_runtime_delta_and_frozen_bytes(self):
        for mode in ('valid','runner_drift','scheduler_drift','base_drift','extra_override','freeze_drift'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);sha=lambda b:hashlib.sha256(b).hexdigest()
                def write(path,data):
                    p=root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
                write('experiments/runner.py',b'new scope implementation\n')
                write('experiments/campaign.py',b'scheduler\n')
                write('tests/test_campaign.py',b'checks\n')
                freeze={'files':[{'path':'experiments/runner.py','sha256':sha(b'old runner\n'),'normalization':'lf'}],
                        'screening_reports':[],'validation':{'ok':True}}
                write('evidence/p08b-freeze.json',json.dumps(freeze).encode())
                fsha=sha((root/'evidence/p08b-freeze.json').read_bytes())
                receipt={'freeze_commit':'a'*40,'freeze_manifest_working_sha256':fsha}
                runtime={'schema':'p08-campaign-runtime-v1','freeze_commit':'a'*40,'freeze_manifest_sha256':fsha,
                    'overrides':{'experiments/runner.py':{'base_sha256':sha(b'old runner\n'),'sha256':sha(b'new scope implementation\n'),'reason':'scope only'}},
                    'additions':{'experiments/campaign.py':sha(b'scheduler\n'),'tests/test_campaign.py':sha(b'checks\n')}}
                if mode=='runner_drift':write('experiments/runner.py',b'changed\n')
                elif mode=='scheduler_drift':write('experiments/campaign.py',b'changed\n')
                elif mode=='base_drift':runtime['overrides']['experiments/runner.py']['base_sha256']='0'*64
                elif mode=='extra_override':runtime['overrides']['models/adapter.py']={}
                elif mode=='freeze_drift':write('evidence/p08b-freeze.json',json.dumps(dict(freeze,unknown=1)).encode())
                write('evidence/p08b-freeze-receipt.json',json.dumps(receipt).encode())
                write('experiments/p08b-campaign-runtime.json',json.dumps(runtime).encode())
                with patch('experiments.campaign.PROJECT_ROOT',root),patch('experiments.campaign.validate_freeze',return_value={'ok':True,'task_checksums':[]}):
                    # Supply the matching checksum shape; this test isolates runtime admission.
                    freeze['validation']={'ok':True,'task_checksums':[]}
                    if mode!='freeze_drift':
                        write('evidence/p08b-freeze.json',json.dumps(freeze).encode());fsha=sha((root/'evidence/p08b-freeze.json').read_bytes())
                        receipt['freeze_manifest_working_sha256']=fsha;runtime['freeze_manifest_sha256']=fsha
                        write('evidence/p08b-freeze-receipt.json',json.dumps(receipt).encode());write('experiments/p08b-campaign-runtime.json',json.dumps(runtime).encode())
                    if mode=='valid':self.assertEqual(_admit((),self.protocol,self.protocol['runtime']['image_id'])['scope'],'P08B frozen formal experiment')
                    else:
                        with self.assertRaises(ValueError):_admit((),self.protocol,self.protocol['runtime']['image_id'])

    def fake_runner(self, case, validation, *, group, client, artifact_root, run_id, scope, **kwargs):
        self.runner_calls.append(group)
        path = Path(artifact_root)/run_id
        path.mkdir(parents=True, exist_ok=False)
        if group == 'B0':
            self.assertIsNone(client)
        else:
            client.complete([{'role': 'user', 'content': 'offline'}])
        report = {'scope': scope, 'case_id': case['id'], 'group': group, 'run_id': run_id,
                  'status': 'baseline_survives' if group == 'B0' else 'verified'}
        (path/'experiment.json').write_text(json.dumps(report), encoding='utf-8')
        return report

    def execute(self, folder, client, **kwargs):
        with patch('experiments.campaign._admit', return_value=copy.deepcopy(self.proof)), \
             patch('experiments.campaign.run_experiment', side_effect=kwargs.pop('runner', self.fake_runner)):
            return run_campaign(self.tasks, self.protocol, client=client, image_id=self.protocol['runtime']['image_id'],
                                artifact_root=Path(folder)/'campaign', export_path=kwargs.pop('export_path',Path(folder)/'export.json'), **kwargs)

    def test_export_cannot_overwrite_plan_or_unrelated_file(self):
        for mode in ('plan', 'unrelated'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                target=Path(folder)/'campaign'/'plan.json' if mode=='plan' else Path(folder)/'export.json'
                if mode=='unrelated':target.write_text('{"unrelated":"keep"}')
                with self.assertRaises(ValueError):self.execute(folder,Client(),export_path=target)
                self.assertEqual(self.runner_calls,[])
                if mode=='unrelated':self.assertEqual(json.loads(target.read_text()),{'unrelated':'keep'})

    def test_formal_campaign_requires_its_single_canonical_directory(self):
        self.proof['scope']='P08B frozen formal experiment'
        client=object.__new__(ModelClient)
        client.config=SimpleNamespace(model=self.protocol['runtime']['model'],api_key='offline-unused-key')
        client.thinking_mode='disabled'
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):self.execute(folder,client)
            self.assertFalse((Path(folder)/'campaign').exists())

    def test_request_return_write_failure_preserves_unknown_usage_without_resend(self):
        client=Client({'total_tokens':9})
        def fail(path,value,**kwargs):
            if Path(path).name.endswith('.return.json'):raise OSError('returned usage not durable')
            return _write_json(path,value,**kwargs)
        with tempfile.TemporaryDirectory() as folder:
            with patch('experiments.campaign._write_json',side_effect=fail):result=self.execute(folder,client)
            again=self.execute(folder,client)
        self.assertEqual(client.calls,9)
        self.assertEqual(result['usage']['unknown_requests'],9)
        self.assertIsNone(again['usage']['total_tokens'])

    def test_missing_slot_outcome_after_return_is_unknown_not_retried(self):
        client=Client()
        def fail(path,value,**kwargs):
            if Path(path).name=='outcome.json':raise OSError('outcome not durable')
            return _write_json(path,value,**kwargs)
        with tempfile.TemporaryDirectory() as folder:
            with patch('experiments.campaign._write_json',side_effect=fail):
                with self.assertRaises(OSError):self.execute(folder,client)
            result=self.execute(folder,client)
        self.assertEqual(result['counts']['interrupted_unknown'],1)
        self.assertEqual(len(self.runner_calls),12)

    def test_os_lease_releases_after_real_process_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            code='from pathlib import Path; from experiments.campaign import _campaign_lock; import os,sys\nwith _campaign_lock(Path(sys.argv[1])): os._exit(17)'
            child=subprocess.run([sys.executable,'-c',code,folder],capture_output=True)
            self.assertEqual(child.returncode,17)
            with _campaign_lock(Path(folder)):pass

    def test_explicit_supplement_is_separate_and_never_replaces_primary(self):
        client=Client()
        with tempfile.TemporaryDirectory() as folder:
            first=self.execute(folder,client)
            base=Path(folder)/'campaign';old_export=(Path(folder)/'export.json').read_bytes()
            key=next(s['key'] for s in first['slots'] if s['group']=='B1')
            with patch('experiments.campaign._admit',return_value=self.proof),patch('experiments.campaign.run_experiment',side_effect=self.fake_runner):
                for _ in range(2):
                    result=run_supplemental_attempt(self.tasks,self.protocol,client=client,image_id=self.protocol['runtime']['image_id'],
                        base_artifact_root=base,attempt_id='manual_1',slot_keys=(key,),reason='Explicit offline replay check')
                with self.assertRaises(ValueError):
                    run_supplemental_attempt(self.tasks,self.protocol,client=client,image_id=self.protocol['runtime']['image_id'],
                        base_artifact_root=base,attempt_id='manual_1',slot_keys=(key,),reason='changed reason')
            self.assertEqual((Path(folder)/'export.json').read_bytes(),old_export)
            self.assertEqual(client.calls,10)
            self.assertEqual(result['attempt_kind'],'supplemental')
            self.assertEqual(result['planned_records'],1)
            self.assertEqual(first['planned_records'],12)

    def test_durable_request_usage_cannot_change_after_slot_finished(self):
        with tempfile.TemporaryDirectory() as folder:
            client=Client({'total_tokens':7});self.execute(folder,client)
            path=next((Path(folder)/'campaign'/'slots').glob('*/requests/*.return.json'))
            data=json.loads(path.read_text());data['usage']['total_tokens']=999;path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):self.execute(folder,client)
            self.assertEqual(client.calls,9)

    def test_sensitive_supplement_reason_is_rejected_before_writing_plan(self):
        client=Client()
        def safe(value):
            if 'private-marker' in json.dumps(value):raise ValueError('sensitive data rejected')
        client.ensure_safe=safe
        with tempfile.TemporaryDirectory() as folder:
            original=self.execute(folder,client);base=Path(folder)/'campaign'
            with patch('experiments.campaign._admit',return_value=self.proof):
                with self.assertRaises(ValueError):
                    run_supplemental_attempt(self.tasks,self.protocol,client=client,image_id=self.protocol['runtime']['image_id'],
                        base_artifact_root=base,attempt_id='sensitive',slot_keys=(original['slots'][0]['key'],),reason='private-marker')
            self.assertFalse((base/'supplemental'/'sensitive'/'plan.json').exists())

    def test_schedule_is_exact_unique_balanced_and_rotates_in_fixed_order(self):
        ids = tuple(f'task_{i:02}' for i in range(30))
        schedule = build_schedule(tuple(reversed(ids)))
        self.assertEqual(len(schedule), 360)
        self.assertEqual(len({r['key'] for r in schedule}), 360)
        self.assertEqual([r['group'] for r in schedule[:4]], ['B0', 'B1', 'B2', 'A0'])
        self.assertEqual([r['group'] for r in schedule[4:8]], ['B1', 'B2', 'A0', 'B0'])
        self.assertEqual([r['group'] for r in schedule[120:124]], ['B1', 'B2', 'A0', 'B0'])
        self.assertEqual(schedule, build_schedule(ids))
        for identity in ids:
            self.assertEqual(len([r for r in schedule if r['task_id'] == identity]), 12)
        for bad in ((), ('same', 'same'), ('../escape',)):
            with self.assertRaises(ValueError): build_schedule(bad)

    def test_completion_and_resume_never_repeat_calls_and_keep_unknown_usage(self):
        client = Client()
        with tempfile.TemporaryDirectory() as folder:
            first = self.execute(folder, client)
            again = self.execute(folder, client)
        self.assertEqual(client.calls, 9)
        self.assertEqual(len(self.runner_calls), 12)
        self.assertEqual(first['status'], 'complete')
        self.assertEqual(again['counts']['finished'], 12)
        self.assertIsNone(again['usage']['total_tokens'])
        self.assertEqual(again['usage']['unknown_requests'], 9)
        self.assertEqual(again['model_request_attempts'], 9)

    def test_claim_before_execution_crash_is_unknown_and_never_resent(self):
        def crash(*args, **kwargs): raise PowerLoss()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(PowerLoss): self.execute(folder, Client(), runner=crash)
            client = Client()
            result = self.execute(folder, client)
        self.assertEqual(result['counts']['interrupted_unknown'], 1)
        self.assertEqual(len(self.runner_calls), 11)
        self.assertEqual(client.calls, 9)

    def test_returned_request_without_slot_result_stays_unknown_on_recovery(self):
        client = Client({'total_tokens': 7})
        def crash(case, validation, **kwargs):
            if kwargs['group'] == 'B0': return self.fake_runner(case, validation, **kwargs)
            kwargs['client'].complete([{'role': 'user', 'content': 'offline'}])
            raise PowerLoss()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(PowerLoss): self.execute(folder, client, runner=crash)
            self.assertEqual(client.calls, 1)
            result = self.execute(folder, client)
        self.assertEqual(result['counts']['interrupted_unknown'], 1)
        self.assertEqual(client.calls, 9)
        self.assertEqual(result['usage']['known_total_tokens'], 63)

    def test_claim_not_durable_and_export_failure_prevent_requests(self):
        for kind in ('claim', 'export'):
            client = Client()
            def fail(path, value, **kwargs):
                if Path(path).name == ('claim.json' if kind == 'claim' else 'export.json'):
                    raise OSError('offline fault injection')
                return _write_json(path, value, **kwargs)
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                with patch('experiments.campaign._write_json', side_effect=fail):
                    with self.assertRaises(OSError): self.execute(folder, client)
                self.assertEqual(client.calls, 0)

    def test_export_failure_after_durable_outcome_does_not_repeat_finished_slot(self):
        client = Client(); exports = [0]
        def fail(path, value, **kwargs):
            if Path(path).name == 'export.json':
                exports[0] += 1
                if exports[0] == 2: raise OSError('export failure after first result')
            return _write_json(path, value, **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            with patch('experiments.campaign._write_json', side_effect=fail):
                with self.assertRaises(OSError): self.execute(folder, client)
            result = self.execute(folder, client)
        self.assertEqual(len(self.runner_calls), 12)
        self.assertEqual(result['counts']['finished'], 12)

    def test_cancel_before_claim_and_after_return_preserves_pending_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client()
            result = self.execute(folder, client, cancelled=lambda: True)
            self.assertEqual(result['counts']['pending'], 12)
            self.assertEqual(client.calls, 0)
            resumed = self.execute(folder, client, cancelled=lambda: len(self.runner_calls) >= 2)
            self.assertEqual(resumed['counts']['finished'], 2)
            self.assertEqual(resumed['counts']['pending'], 10)
            self.assertEqual(client.calls, 1)

    def test_protocol_or_proof_drift_duplicate_start_and_result_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(); self.execute(folder, client)
            self.proof['runtime_sha256'] = 'e'*64
            with self.assertRaises(ValueError): self.execute(folder, client)
            self.proof['runtime_sha256'] = 'c'*64
            with _campaign_lock(Path(folder)/'campaign'):
                with self.assertRaises(ValueError): self.execute(folder, client)
            path = next((Path(folder)/'campaign'/'runs').glob('*/experiment.json'))
            path.write_text('{}')
            with self.assertRaises(ValueError): self.execute(folder, client)
            self.assertEqual(client.calls, 9)

    def test_each_slot_caps_model_requests_without_retries(self):
        client = Client({'total_tokens': 1})
        def excess(case, validation, **kwargs):
            group = kwargs['group']
            if group == 'B0': return self.fake_runner(case, validation, **kwargs)
            limit = 2 if group == 'B1' else 5
            for _ in range(limit): kwargs['client'].complete([{'role': 'user', 'content': 'offline'}])
            with self.assertRaises(ModelError): kwargs['client'].complete([{'role': 'user', 'content': 'offline'}])
            path = Path(kwargs['artifact_root'])/kwargs['run_id'];path.mkdir(parents=True)
            result = {'scope': kwargs['scope'], 'case_id': case['id'], 'group': group, 'run_id': kwargs['run_id'], 'status': 'repair_exhausted'}
            (path/'experiment.json').write_text(json.dumps(result))
            return result
        with tempfile.TemporaryDirectory() as folder:
            result = self.execute(folder, client, runner=excess)
        self.assertEqual(client.calls, 36)
        self.assertEqual(result['model_request_attempts'], 36)
        self.assertEqual(result['usage']['total_tokens'], 36)


if __name__ == '__main__': unittest.main()
