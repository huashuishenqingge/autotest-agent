"""Synthetic model output + REAL Docker integration; consumes no model quota."""
import argparse
import json
from pathlib import Path
import tempfile

from examples.generate_tests import DEMO_REQUIREMENTS, generate_and_run, write_demo
from models.adapter import Completion


class FixtureClient:
    def complete(self, messages):
        request = json.loads(messages[1]['content'])
        proposal = {'task_id': request['task_id'], 'base_revision': request['base_revision'],
                    'rationale': 'Independent requirements: 18 is included; 0 is not adult; negative ages raise.',
                    'files': [{'path': 'tests/generated/test_age.py', 'content': '''import pytest
from app import is_adult

def test_normal_and_invalid():
    assert is_adult(0) is False
    with pytest.raises(ValueError):
        is_adult(-1)

def test_boundary():
    assert is_adult(18) is True
'''}]}
        return Completion(json.dumps(proposal), 'synthetic-fixture-only', None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-id', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        write_demo(root)
        before = {p.name: p.read_bytes() for p in root.iterdir()}
        record = generate_and_run(root, requirements=DEMO_REQUIREMENTS, targets=('app.py',),
                                  client=FixtureClient(), image_id=args.image_id,
                                  artifact_root=Path('artifacts/p04-synthetic-integration'))
        assert record['baseline']['run']['passed'] == 1
        assert record['baseline']['run']['exit_code'] == 0
        assert record['proposal']['failure'] is None
        full = record['execution']
        assert full['run']['passed'] == 2 and full['run']['failed'] == 1
        assert full['run']['failure']['kind'] == 'test_assertion'
        assert full['cleanup_ok']
        assert before == {p.name: p.read_bytes() for p in root.iterdir()}
        print('PASS: synthetic proposal + real Docker baseline/full tests; age=18 defect exposed.')
        print('No real model was called. task_id=' + record['task_id'])


if __name__ == '__main__':
    main()
