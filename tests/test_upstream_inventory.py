import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.upstream import build_reviewed_inventory, inventory_manifest


class InventoryTests(unittest.TestCase):
    def build(self, root, *, bad_source=False):
        project = {'id': 'demo', 'package_root': 'src/demo', 'commit': 'fixed'}
        metadata = {'issues': [], 'licenses': {}, 'modules': [{'path': 'src/demo/__init__.py',
                    'sha256': hashlib.sha256(b'def f(): pass\n').hexdigest()}]}
        if bad_source:
            metadata['modules'][0]['sha256'] = '0' * 64
        registry = {'schema': 1, 'projects': {'demo': {}}}
        with patch('experiments.upstream.inspect_project', return_value=metadata), \
             patch('experiments.upstream._read', return_value=registry):
            return build_reviewed_inventory(project, reference_root=root, resource_root=root/'wheels')

    def setup_files(self, root):
        (root/'demo/src/demo').mkdir(parents=True)
        (root/'demo/tests').mkdir()
        (root/'demo/src/demo/__init__.py').write_bytes(b'def f(): pass\n')
        (root/'demo/tests/conftest.py').write_bytes(b'FIXTURE = 1\n')
        (root/'demo/tests/test_api.py').write_bytes(b'def test_ok(): assert True\n')

    def test_keeps_all_fixtures_and_adds_origin_without_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.setup_files(root)
            inventory=self.build(root)
        self.assertEqual(inventory['tests']['tests/conftest.py'],b'FIXTURE = 1\n')
        self.assertIn('tests/test_p08_working_copy_origin.py',inventory['tests'])
        self.assertEqual(inventory['coverage_sources'],('src/demo/__init__.py',))

    def test_changed_source_unknown_resource_and_origin_overwrite_rejected(self):
        for choice in ('source','resource','origin'):
            with self.subTest(choice=choice),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);self.setup_files(root)
                if choice=='resource':(root/'demo/tests/unknown.txt').write_bytes(b'not approved')
                if choice=='origin':(root/'demo/tests/test_p08_working_copy_origin.py').write_bytes(b'pass')
                with self.assertRaises(ValueError):self.build(root,bad_source=choice=='source')

    def test_budget_case_conflicts_and_disguised_code_are_rejected(self):
        for choice in ('count','file_size','total_size','case','magic','extension'):
            with self.subTest(choice=choice):
                inventory={'sources':{'src/demo.py':b'pass'},'tests':{},'support':{},'excluded':[],'provenance':{}}
                if choice=='count':inventory['tests']={f'tests/test_{n}.py':b'pass' for n in range(2000)}
                if choice=='file_size':inventory['support']['data.txt']=b'x'*1_000_001
                if choice=='total_size':inventory['support']={f'data/{n}.txt':b'x'*1_000_000 for n in range(10)}
                if choice=='case':inventory['tests']['SRC/DEMO.py']=b'pass'
                if choice=='magic':inventory['support']['data.txt']=b'MZcode'
                if choice=='extension':inventory['support']['data.PY']=b'pass'
                with self.assertRaises(ValueError):inventory_manifest(inventory)

    def test_relative_reference_root_is_resolved_before_path_checks(self):
        # Windows requires the same drive; POSIX containers have a read-only cwd.
        with tempfile.TemporaryDirectory(dir=Path.cwd() if os.name == 'nt' else None) as folder:
            root=Path(folder);self.setup_files(root)
            inventory=self.build(Path(os.path.relpath(root,Path.cwd())))
        self.assertIn('src/demo/__init__.py',inventory['sources'])
