"""Replay the frozen REAL model proposal in Docker without calling any model."""
import argparse
import base64
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile

from agents.generator import parse_generation
from execution.docker_runner import run_tests
from execution.proposals import apply_proposal, checked_path
from repository.parser import parse_repository
from workflow.state import GenerateInput, Policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-id', required=True)
    args = parser.parse_args()
    evidence_path = Path(__file__).resolve().parents[1] / 'evidence/p04-deepseek-generation.json'
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='autotest-replay-') as folder:
        root = Path(folder).resolve()
        for path, encoded in evidence['fixture_files_base64'].items():
            target = checked_path(root, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(encoded, validate=True))
        snapshot = parse_repository(root, project_id='p04-generation')
        assert snapshot.repository.revision == evidence['base_revision']
        baseline = run_tests(root, snapshot.repository, image_id=args.image_id,
                             artifact_root=Path('artifacts/p04-frozen-replay'))
        assert baseline.run.successful and baseline.run.passed == 1 and baseline.cleanup_ok
        request = GenerateInput(evidence['task_id'], snapshot.repository, Policy(),
                                tuple(evidence['requirements']),
                                tuple((f.path, f.text) for f in snapshot.files if f.kind == 'source'))
        output = parse_generation(request, json.dumps(evidence['model_candidate']))
        apply_proposal(root, request, output)
        repo = replace(snapshot.repository, test_files=snapshot.repository.test_files + tuple(f.path for f in output.files))
        result = run_tests(root, repo, image_id=args.image_id,
                           artifact_root=Path('artifacts/p04-frozen-replay'))
        assert result.run.snapshot_digest == evidence['execution_digest']
        assert result.run.passed == 9 and result.run.failed == 1
        assert result.run.failure and result.run.failure.kind.value == 'test_assertion'
        assert result.cleanup_ok
        print(json.dumps(asdict(result), indent=2))
        print('PASS: real saved DeepSeek proposal reproduced the expected boundary failure; no model call.')


if __name__ == '__main__':
    main()
