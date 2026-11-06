"""Frozen visible inputs and separately loaded evaluator-only validation data."""
import hashlib
import json
from pathlib import Path

from agents.repairer import context_digest
from execution.proposals import checked_path
from workflow.state import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_cases():
    visible = PROJECT_ROOT / 'fixtures/p05-boundary.json'
    private = PROJECT_ROOT / 'validation/p05-holdout.json'
    cases = json.loads(visible.read_text(encoding='utf-8'))['cases']
    validation = json.loads(private.read_text(encoding='utf-8'))['cases']
    # Hash canonical JSON so checkout newline conversion does not change the
    # manifest; embedded source/test strings (including CRLF) remain exact.
    manifest = {p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
                    json.dumps(json.loads(p.read_text(encoding='utf-8')), sort_keys=True,
                               ensure_ascii=True, separators=(',', ':')).encode('utf-8')).hexdigest()
                for p in (visible, private)}
    return cases, validation, manifest


def write_files(root, files):
    root = Path(root).resolve(strict=True)
    for path, content in files.items():
        target = checked_path(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode('utf-8'))


def visible_repository(case):
    tests = {**case['original_tests'], **case['new_tests']}
    digest = context_digest(tuple(case['sources'].items()), tuple(tests.items()))
    return Repository(case['id'], 'frozen-sha256:' + digest, tuple(case['sources']), tuple(tests))
