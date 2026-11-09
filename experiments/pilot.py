"""Run only explicitly reviewed controlled pilots; never P08B formal samples."""
import argparse
from dataclasses import replace
import json
from pathlib import Path

from experiments.tasks import load_pilots
from models.adapter import Budget, Config, ModelClient
from repair.fixtures import PROJECT_ROOT
from workflow.orchestrator import run_case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--call', action='store_true')
    parser.add_argument('--image-id')
    parser.add_argument('--model', default='deepseek-v4-flash-ascend')
    parser.add_argument('--artifacts', type=Path, default=PROJECT_ROOT / 'artifacts/p08a/pilots')
    parser.add_argument('--export', type=Path, default=PROJECT_ROOT / 'artifacts/p08a/pilots-summary.json')
    parser.add_argument('--cancel-file', type=Path)
    args = parser.parse_args()
    cases, validation, manifest = load_pilots(PROJECT_ROOT / 'fixtures/p08a-controlled.json',
                                             PROJECT_ROOT / 'validation/p08a-controlled.json')
    if not args.call:
        print(json.dumps({'scope': 'controlled-pilot-only', 'cases': [c['id'] for c in cases], 'manifest': manifest}))
        return 0
    if not args.image_id:
        parser.error('--call requires --image-id')
    if args.export.exists():
        raise FileExistsError('preserve prior pilot evidence; select an explicitly new export')
    args.export.parent.mkdir(parents=True, exist_ok=True)
    with args.export.open('x',encoding='utf-8') as stream:
        json.dump({'schema':1,'status':'claimed','scope':'controlled-pilot-only'},stream)
    cancelled = lambda: bool(args.cancel_file and args.cancel_file.exists())
    client = None
    if not cancelled():
        client = ModelClient(replace(Config.from_env(PROJECT_ROOT / '.env'), model=args.model),
                             Budget(PROJECT_ROOT / 'artifacts/model-budget.json', max_attempts=None), thinking_mode='disabled')
        if client.config.model not in client.list_models():
            raise ValueError('model not authorized')
    report = {'schema': 1, 'scope': 'controlled-pilot-only, excluded from P08B',
              'manifest': manifest, 'image_id': args.image_id, 'cases': []}
    args.export.parent.mkdir(parents=True, exist_ok=True)
    for case in cases:
        result = run_case(dict(case, require_coverage=True), validation[case['id']], client=client,
                          image_id=args.image_id, artifact_root=args.artifacts, cancelled=cancelled)
        report['cases'].append(result)
        if client:
            client.ensure_safe(report)
        args.export.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(case['id'] + ': ' + result['status'], flush=True)
        if result['status'] == 'cancelled':
            break
    return 0 if len(report['cases']) == len(cases) and all(c['status'] == 'verified' for c in report['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
