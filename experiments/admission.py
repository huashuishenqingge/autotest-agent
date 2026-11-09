"""One nonformal MarkupSafe scope/feedback admission case, excluded from P08B."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import uuid

from experiments.project_tasks import load_project_admission
from experiments.runner import run_experiment
from models.adapter import Budget, Config, ModelClient
from repair.fixtures import PROJECT_ROOT


def load_case():
    return load_project_admission(
        PROJECT_ROOT / 'fixtures/p08a-project-admission.json',
        PROJECT_ROOT / 'validation/p08a-project-admission.json',
        PROJECT_ROOT / 'experiments/projects.json',
        PROJECT_ROOT / 'reference/p08')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--call', action='store_true')
    p.add_argument('--replay', type=Path)
    p.add_argument('--image-id')
    p.add_argument('--group', choices=('B0','B1','B2','A0'), action='append')
    p.add_argument('--model', default='deepseek-v4-flash-ascend')
    p.add_argument('--artifacts', type=Path, default=PROJECT_ROOT / 'artifacts/p08a/admission')
    p.add_argument('--export', type=Path, default=PROJECT_ROOT / 'artifacts/p08a/admission-summary.json')
    p.add_argument('--cancel-file', type=Path)
    args=p.parse_args()
    if args.call and args.replay:
        p.error('--call and --replay are mutually exclusive')
    if (args.call or args.replay) and args.export.exists():
        raise FileExistsError('preserve campaign identity; choose an explicitly new export to run again')
    case,validation,provenance=load_case()
    if not args.call and not args.replay:
        print(json.dumps(provenance,indent=2))
        return 0
    if not args.image_id:
        p.error('--image-id required')
    cancelled=lambda: bool(args.cancel_file and args.cancel_file.exists())
    client=None
    groups=args.group or ['B0','B1','B2','A0']
    saved=json.loads(args.replay.read_text(encoding='utf-8')) if args.replay else None
    if saved and saved['provenance'] != provenance:
        raise ValueError('saved admission inputs differ')
    if saved and any(g['image_id'] != args.image_id for g in saved['groups'] if g['group'] in groups):
        raise ValueError('replay requires the same reviewed image')
    report={'schema':1,'scope':'nonformal P08A admission','origin':'saved proposal replay' if saved else 'new real model',
            'campaign_id':uuid.uuid4().hex,'status':'claimed','provenance':provenance,'groups':[]}
    args.export.parent.mkdir(parents=True,exist_ok=True)
    with args.export.open('x',encoding='utf-8') as stream:
        json.dump(report,stream,indent=2)
    if args.call and any(g != 'B0' for g in groups) and not cancelled():
        try:
            client=ModelClient(replace(Config.from_env(PROJECT_ROOT/'.env'),model=args.model),
                               Budget(PROJECT_ROOT/'artifacts/model-budget.json',max_attempts=None),thinking_mode='disabled')
            if client.config.model not in client.list_models():
                raise ValueError('model not authorized')
        except (ValueError,OSError):
            report['status']='configuration_failed'
            args.export.write_text(json.dumps(report,indent=2),encoding='utf-8')
            raise
    class ReplayClient:
        def __init__(self,row):
            self.generations=iter(row['rounds'])
            self.repairs=iter((row.get('repair') or {}).get('attempts',[]))
        def complete(self,messages):
            from models.adapter import Completion
            request=json.loads(messages[1]['content'])
            if 'test_prefix' in request:
                output=next(self.generations)['generation']
                data={'task_id':request['task_id'],'base_revision':request['base_revision'],
                      'rationale':output['rationale'],'files':[{'path':f['path'],'content':f['content']} for f in output['files']]}
            else:
                data=dict(next(self.repairs)['model_candidate'])
                data.update(task_id=request['task_id'],base_revision=request['base_revision'],based_on_run_id=request['based_on_run_id'])
            return Completion(json.dumps(data),'replay saved admission proposal',None)
    for group in groups:
        if saved:
            old=next(g for g in saved['groups'] if g['group']==group)
            if old['status'] not in ('verified','baseline_survives'):
                raise ValueError('only verified saved admission groups may be replayed')
            current=ReplayClient(old)
        else:
            current=client
        result=run_experiment(case,validation,group=group,client=current,image_id=args.image_id,
                              artifact_root=args.artifacts,cancelled=cancelled)
        report['groups'].append(result)
        report['status']=result['status']
        if saved and group!='B0' and result['status']=='verified':
            if old['repair']['independent_validation']['run']['snapshot_digest'] != result['repair']['independent_validation']['run']['snapshot_digest']:
                raise ValueError('replay source/test/support digest mismatch')
        if client:
            client.ensure_safe(report)
        args.export.parent.mkdir(parents=True,exist_ok=True)
        args.export.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(group+': '+result['status'],flush=True)
        if result['status']=='cancelled':
            break
    success=len(report['groups'])==len(groups) and all(g['status'] in ('verified','baseline_survives') for g in report['groups'])
    report['status']='completed' if success else 'cancelled' if report['groups'] and report['groups'][-1]['status']=='cancelled' else 'completed_with_failures'
    args.export.write_text(json.dumps(report,indent=2),encoding='utf-8')
    return 0 if success else 1


if __name__=='__main__':
    raise SystemExit(main())
