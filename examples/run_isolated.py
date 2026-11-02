"""Run an explicitly reviewed Python inventory in Docker; never on host."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from execution.docker_runner import run_tests
from repository.parser import parse_repository
from workflow.state import Policy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--image-id', required=True)
    parser.add_argument('--artifacts', type=Path, default=Path('artifacts'))
    parser.add_argument('--timeout', type=int, default=60)
    parser.add_argument('--approve-parser-inventory', action='store_true',
                        help='explicitly accept parser source/test list for this small Python-only demo')
    args = parser.parse_args()
    snapshot = parse_repository(args.root, project_id='isolated-demo')
    if not args.approve_parser_inventory:
        print(json.dumps(asdict(snapshot.repository), indent=2))
        parser.error('review inventory, then explicitly pass --approve-parser-inventory')
    result = run_tests(args.root, snapshot.repository, image_id=args.image_id,
                       artifact_root=args.artifacts, policy=Policy(timeout_seconds=args.timeout))
    print(json.dumps(asdict(result), indent=2))
    return 0 if result.run.successful else 1


if __name__ == '__main__':
    raise SystemExit(main())
