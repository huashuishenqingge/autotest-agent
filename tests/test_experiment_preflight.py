import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiments.preflight import inspect_project, load_projects, main


class PreflightTests(unittest.TestCase):
    def git_ok(self, command, **kwargs):
        output = self.spec()['url'] if 'config' in command else 'a'*40+'\n' if 'rev-parse' in command else ''
        return subprocess.CompletedProcess(command, 0, output, '')

    def spec(self):
        return dict(id='demo', url='https://github.com/example/demo.git', tag='1.0',
                    commit='a' * 40, package_root='src/demo',
                    license_files=['LICENSE'], config_files=['pyproject.toml'])

    def prepare(self, root):
        (root / 'src/demo').mkdir(parents=True)
        (root / 'src/demo/__init__.py').write_text('def small(x):\n    return x\n', encoding='utf-8')
        (root / 'LICENSE').write_text('test fixture license', encoding='utf-8')
        (root / 'pyproject.toml').write_text('[project]\nname="demo"\n', encoding='utf-8')
        (root / 'tests').mkdir()
        (root / 'tests/conftest.py').write_text('raise RuntimeError("must never import")\n', encoding='utf-8')
        (root / 'tests/input.txt').write_text('fixture', encoding='utf-8')

    def test_inspection_hashes_and_reports_limits_without_executing_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.prepare(root)
            (root / 'src/demo/large.py').write_text('#' + 'x' * 17000 + '\ndef large(): return 1\n', encoding='utf-8')
            with patch('experiments.preflight.subprocess.run', side_effect=self.git_ok):
                report = inspect_project(root, self.spec())
            self.assertTrue(report['commit_verified'])
            self.assertTrue(report['package_exceeds_prompt_alone'])
            self.assertEqual(report['functions'], 2)
            self.assertEqual(report['modules_over_prompt_alone'], ['src/demo/large.py'])
            self.assertIn('tests/conftest.py', report['fixture_modules'])
            self.assertIn('tests/input.txt', report['non_python_test_files'])
            self.assertGreater(report['repair_inputs_lower_bound_characters'], report['package_characters'])
            self.assertEqual(len(report['licenses']['LICENSE']['sha256']), 64)
            self.assertEqual(report['execution_status'], 'not_run')

    def test_wrong_commit_rejected_before_inspection(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('experiments.preflight.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'b'*40+'\n', '')):
                with self.assertRaisesRegex(ValueError, 'commit'):
                    inspect_project(Path(folder), self.spec())

    def test_manifest_rejects_unknown_fields_traversal_and_duplicate_projects(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'projects.json'
            for rows in ([dict(self.spec(), extra=True)], [dict(self.spec(), package_root='../demo')],
                         [self.spec(), self.spec()]):
                path.write_text(json.dumps({'schema': 1, 'projects': rows}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_projects(path)
            path.write_text(json.dumps({'schema': 1, 'projects': [self.spec()]}), encoding='utf-8')
            self.assertEqual(load_projects(path)[0]['id'], 'demo')

    def test_linked_config_rejected_without_reading_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.prepare(root)
            target = root / 'pyproject.toml'
            with patch('experiments.preflight.subprocess.run', side_effect=self.git_ok), \
                 patch('pathlib.Path.is_symlink', side_effect=lambda: True):
                with self.assertRaises(ValueError):
                    inspect_project(root, self.spec())

    def test_cli_accepts_relative_reference_directory(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            checkout = root / 'demo'
            checkout.mkdir()
            self.prepare(checkout)
            catalog = root / 'projects.json'
            catalog.write_text(json.dumps({'schema': 1, 'projects': [self.spec()]}), encoding='utf-8')
            export = root / 'report.json'
            with patch('sys.argv', ['preflight', '--reference', os.path.relpath(root),
                       '--catalog', str(catalog), '--export', str(export)]), \
                 patch('experiments.preflight.subprocess.run', side_effect=self.git_ok):
                main()
            self.assertEqual(json.loads(export.read_text())['projects'][0]['project']['id'], 'demo')

    def test_dirty_checkout_is_rejected_even_with_correct_head(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.prepare(root)
            responses = [subprocess.CompletedProcess([], 0, 'a'*40+'\n', ''),
                         subprocess.CompletedProcess([], 0, ' M src/demo/__init__.py\n', '')]
            with patch('experiments.preflight.subprocess.run', side_effect=responses):
                with self.assertRaisesRegex(ValueError, 'modified'):
                    inspect_project(root, self.spec())

    def test_wrong_origin_or_tag_rejected_even_with_correct_head(self):
        for field in ('origin', 'tag'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.prepare(root)
                def wrong(command, **kwargs):
                    if field == 'origin' and 'config' in command:
                        return subprocess.CompletedProcess(command, 0, 'https://github.com/other/repo.git', '')
                    if field == 'tag' and any('refs/tags/' in p for p in command):
                        return subprocess.CompletedProcess(command, 0, 'b'*40, '')
                    return self.git_ok(command, **kwargs)
                with patch('experiments.preflight.subprocess.run', side_effect=wrong):
                    with self.assertRaises(ValueError):
                        inspect_project(root, self.spec())


if __name__ == '__main__':
    unittest.main()
