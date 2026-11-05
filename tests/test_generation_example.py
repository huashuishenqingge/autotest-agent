from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.generate_tests import generate_and_run
from execution.docker_runner import ExecutionResult
from models.adapter import Completion
from workflow.state import TestRun


class ExampleTests(unittest.TestCase):
    def test_generated_inventory_and_original_unchanged(self):
        class Client:
            def complete(self, messages):
                data = json.loads(messages[1]['content'])
                return Completion(json.dumps({'task_id': data['task_id'], 'base_revision': data['base_revision'],
                    'rationale': 'Requirement says addition.', 'files': [{'path': 'tests/generated/test_add.py',
                    'content': 'from app import add\ndef test_add(): assert add(0, 1) == 1\n'}]}), 'fake-model', None)
        seen = []
        def execute(root, repo, **kwargs):
            seen.append(repo)
            if len(seen) == 2:
                self.assertTrue((root / 'tests/generated/test_add.py').is_file())
            return ExecutionResult(TestRun('run' + str(len(seen)), 'digest', 0, len(repo.test_files), 0, 0), 'mock-artifacts', True)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'project'
            root.mkdir()
            (root / 'app.py').write_text('def add(a, b): return a+b\n')
            (root / 'test_app.py').write_text('from app import add\ndef test_old(): assert add(1, 2) == 3\n')
            with patch('examples.generate_tests.run_tests', side_effect=execute):
                result = generate_and_run(root, requirements=('addition',), targets=('app.py',),
                                          client=Client(), image_id='sha256:' + '0'*64,
                                          artifact_root=Path(folder) / 'evidence')
            self.assertIsNone(result['proposal']['failure'])
            self.assertIn('tests/generated/test_add.py', seen[1].test_files)
            self.assertEqual(seen[0].revision, seen[1].revision)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['app.py', 'test_app.py'])


if __name__ == '__main__':
    unittest.main()
