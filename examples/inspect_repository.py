"""Read-only repository/context example: python -m examples.inspect_repository ."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from repository.context import build_context
from repository.parser import parse_repository


def main():
    parser = argparse.ArgumentParser(description='Inspect Python source without executing it')
    parser.add_argument('root', type=Path)
    parser.add_argument('--project-id', default='inspection')
    parser.add_argument('--target', action='append', default=[])
    parser.add_argument('--requirement', action='append', default=[])
    args = parser.parse_args()
    snapshot = parse_repository(args.root, project_id=args.project_id)
    result = {
        'repository': asdict(snapshot.repository),
        'symbols': [asdict(s) for s in snapshot.symbols],
        'issues': [asdict(i) for i in snapshot.issues],
        'files': [{k: v for k, v in asdict(f).items() if k != 'text'} for f in snapshot.files],
    }
    if args.target:
        context = build_context(snapshot, task_id='inspection-task', requirements=tuple(args.requirement), targets=tuple(args.target))
        result['context'] = {
            'source_paths': [p for p, _ in context.request.source_texts],
            'test_paths': [p for p, _ in context.related_tests],
            'spans': context.spans,
            'omitted_paths': context.omitted_paths,
        }
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
