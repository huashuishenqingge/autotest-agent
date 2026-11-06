"""Opt-in model generation on an approved Python-only inventory, never host tests."""
import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

from agents.generator import Generator, PROMPT_VERSION, make_messages
from execution.docker_runner import run_tests, stage_snapshot
from execution.proposals import apply_proposal
from models.adapter import Budget, Config, ModelClient, ModelError
from repository.context import build_context
from repository.parser import parse_repository
from workflow.state import Policy

DEMO_REQUIREMENTS = (
    'is_adult(age) accepts integer ages. Ages 18 and above return True; ages 0 through 17 return False.',
    'Negative ages raise ValueError. Compare booleans with concrete expected values.',
)


def write_demo(root):
    (root / 'app.py').write_text('def is_adult(age):\n    if age < 0:\n        raise ValueError("negative age")\n    return age > 18\n', encoding='utf-8')
    (root / 'test_app.py').write_text('from app import is_adult\ndef test_existing():\n    assert is_adult(20) is True\n', encoding='utf-8')


def prepare(root, requirements, targets):
    snapshot = parse_repository(root, project_id='p04-generation')
    if snapshot.issues:
        raise ValueError('Parser reported excluded or invalid inputs; review before generation')
    bundle = build_context(snapshot, task_id=uuid.uuid4().hex, requirements=tuple(requirements), targets=tuple(targets))
    return snapshot, bundle


def generate_and_run(root, *, requirements, targets, client, image_id, artifact_root, reference_tests=()):
    snapshot, bundle = prepare(root, requirements, targets)
    related = tuple(item for item in bundle.related_tests if item[0] in reference_tests)
    if set(reference_tests) != {p for p, _ in related}:
        raise ValueError('Reference tests must be present in the selected bounded context')
    messages = make_messages(bundle.request, related)
    if sum(len(m['content']) for m in messages) > 16000:
        raise ValueError('Full prompt exceeds approved character budget')
    directory = Path(artifact_root).resolve() / bundle.request.task_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {'task_id': bundle.request.task_id, 'prompt_version': PROMPT_VERSION,
              'base_revision': snapshot.repository.revision, 'proposal': None,
              'completion': None, 'baseline': None, 'execution': None, 'format_diagnostic': None,
              'model_candidate': None,
              'prompt_sha256': hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest(),
              'targets': list(targets), 'reference_tests': list(reference_tests)}
    try:
        # All application happens in an owned disposable copy. No writes to root.
        with tempfile.TemporaryDirectory(prefix='autotest-generation-') as folder:
            working = Path(folder)
            _, entries = stage_snapshot(Path(root), working, snapshot.repository)
            if dict(entries) != {f.path: f.sha256 for f in snapshot.files}:
                raise ValueError('Source changed after context snapshot; start a new task')
            record['baseline'] = asdict(run_tests(working, snapshot.repository, image_id=image_id,
                                                  artifact_root=directory / 'execution'))
            if (record['baseline']['run']['exit_code'] != 0 or not record['baseline']['cleanup_ok']
                    or record['baseline']['run']['passed'] < 1):
                raise ValueError('Original tests do not pass; generation was not called')
            generator = Generator(client)
            output = generator.generate(bundle.request, related_tests=related)
            record['format_diagnostic'] = generator.last_diagnostic
            record['model_candidate'] = generator.last_safe_candidate
            record['proposal'] = asdict(output)
            completion = generator.last_completion
            if completion:
                record['completion'] = {'model': completion.model, 'usage': completion.usage,
                                         'usage_available': completion.usage is not None}
            if output.failure:
                return record
            receipt = apply_proposal(working, bundle.request, output)
            (directory / 'proposal.diff').write_text(receipt.diff, encoding='utf-8')
            repo = replace(snapshot.repository, test_files=snapshot.repository.test_files + tuple(f.path for f in output.files))
            record['execution'] = asdict(run_tests(working, repo, image_id=image_id,
                                                   artifact_root=directory / 'execution', policy=Policy()))
    finally:
        # Transport rejects known credential echoes. Only proposal and metadata are
        # persisted, never Authorization, raw HTTP errors, or reasoning_content.
        (directory / 'generation.json').write_text(json.dumps(record, ensure_ascii=True, indent=2), encoding='utf-8')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, nargs='?')
    parser.add_argument('--demo', action='store_true', help='use fixed age-boundary fixture')
    parser.add_argument('--requirements-file', type=Path)
    parser.add_argument('--target', action='append', default=[])
    parser.add_argument('--reference-test', action='append', default=[])
    parser.add_argument('--image-id')
    parser.add_argument('--list-models', action='store_true')
    parser.add_argument('--call', action='store_true', help='explicitly authorize one generation attempt')
    parser.add_argument('--approve-parser-inventory', action='store_true')
    args = parser.parse_args()
    try:
        # Fixed project-wide ledger: changing artifact folders cannot reset calls.
        ledger = Path(__file__).resolve().parents[1] / 'artifacts' / 'model-budget.json'
        if args.list_models:
            config = Config.from_env(ledger.parent.parent / '.env')
            print(json.dumps(ModelClient(config, Budget(ledger, max_attempts=None)).list_models(), ensure_ascii=True))
            return 0
        if args.demo and args.root:
            parser.error('--demo and root are mutually exclusive')
        if not args.demo and (not args.root or not args.requirements_file or not args.target):
            parser.error('provide root, --requirements-file and --target, or use --demo')
        with tempfile.TemporaryDirectory(prefix='autotest-demo-') as folder:
            root = Path(folder) if args.demo else args.root
            if args.demo:
                write_demo(root)
            requirements = DEMO_REQUIREMENTS if args.demo else (args.requirements_file.read_text(encoding='utf-8'),)
            targets = ('app.py',) if args.demo else tuple(args.target)
            snapshot, bundle = prepare(root, requirements, targets)
            print(json.dumps({'inventory': asdict(snapshot.repository), 'requirements': requirements,
                              'targets': targets, 'prompt_version': PROMPT_VERSION}, ensure_ascii=True, indent=2))
            if not args.call:
                print('Preview only: no model request, no container execution.')
                return 0
            if not args.approve_parser_inventory or not args.image_id:
                parser.error('real generation requires --approve-parser-inventory and --image-id')
            config = Config.from_env(ledger.parent.parent / '.env')
            client = ModelClient(config, Budget(ledger, max_attempts=None))
            if config.model not in client.list_models():
                raise ValueError('Configured model is not in this API key authorized model list')
            result = generate_and_run(root, requirements=requirements, targets=targets, client=client,
                                      image_id=args.image_id, artifact_root=ledger.parent / 'p04-generation',
                                      reference_tests=tuple(args.reference_test))
            print(json.dumps({'task_id': result['task_id'], 'completion': result['completion'],
                              'generation_failed': bool(result['proposal'] and result['proposal']['failure']),
                              'execution': result['execution']}, ensure_ascii=True, indent=2))
            # An expected assertion failure still yields a nonzero test exit code;
            # human review decides whether its oracle matches requirements.
            return 0 if result['execution'] and result['execution']['run']['exit_code'] == 0 else 1
    except ModelError as error:
        print(f'{error.kind}: {error}')
        return 1
    except (ValueError, OSError):
        print('Configuration or input is invalid; check environment, inventory and requirements. No host tests were run.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
