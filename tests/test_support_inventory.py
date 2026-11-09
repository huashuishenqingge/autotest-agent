import hashlib
from pathlib import Path
import tempfile
import unittest

from agents.repairer import context_digest
from execution.docker_runner import stage_snapshot, docker_command, run_tests
from workflow.state import Repository, Policy


class SupportInventoryTests(unittest.TestCase):
    def test_binary_resource_is_readonly_and_changes_exact_snapshot(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as dest:
            root = Path(folder)
            (root/'app.py').write_bytes(b'x=1\n')
            (root/'data.bin').write_bytes(b'\x00\xffdata')
            repo = Repository('p', 'baseline', ('app.py',))
            old_dest, new_dest = Path(dest)/'old', Path(dest)/'new'
            old_dest.mkdir()
            new_dest.mkdir()
            empty, _ = stage_snapshot(root, old_dest, repo)
            self.assertEqual(empty, context_digest((('app.py', 'x=1\n'),), ()))
            digest, manifest = stage_snapshot(root, new_dest, repo, support_files=('data.bin',))
            self.assertEqual((new_dest/'data.bin').read_bytes(), b'\x00\xffdata')
            support_hash = hashlib.sha256(b'\x00\xffdata').hexdigest()
            self.assertEqual(digest, context_digest((('app.py', 'x=1\n'),), (), (('data.bin', support_hash),)))
            self.assertNotEqual(digest, empty)
            self.assertIn(('data.bin', support_hash), manifest)

    def test_resources_reject_code_missing_duplicate_or_overlapping_paths(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as dest:
            root = Path(folder)
            (root/'app.py').write_bytes(b'x=1')
            for name in ('helper.py', 'lib.so', 'launch.ps1', 'fake.dat'):
                (root/name).write_bytes(b'MZexecutable' if name == 'fake.dat' else b'data')
            for paths in (('helper.py',), ('lib.so',), ('launch.ps1',), ('fake.dat',),
                          ('missing.txt',), ('app.py',), ('data.txt', 'DATA.txt'), ('../secret.txt',)):
                with self.subTest(paths=paths), self.assertRaises(ValueError):
                    stage_snapshot(root, Path(dest), Repository('p', 'r', ('app.py',)), support_files=paths)

    def test_support_hashes_cannot_overlap_source_or_be_invalid(self):
        for values in ((('app.py', 'a'*64),), (('data.bin', 'bad'),),
                       (('data.bin', 'a'*64), ('DATA.bin', 'b'*64))):
            with self.subTest(values=values), self.assertRaises(ValueError):
                context_digest((('app.py', 'x=1'),), (), values)

    def test_collection_targets_are_fixed_file_arguments_not_commands(self):
        import json
        command=docker_command(Path('.').resolve(),'autotest-test','sha256:'+'a'*64,Policy(),
                               test_targets=('tests/generated/test_f.py',))
        self.assertEqual(json.loads(command[-1]),['tests/generated/test_f.py'])
        with self.assertRaises(ValueError):
            docker_command(Path('.').resolve(),'autotest-test','sha256:'+'a'*64,Policy(),test_targets=('../escape.py',))

    def test_targeted_collection_does_not_accept_legacy_worker_success(self):
        import json
        import subprocess
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as dest:
            root=Path(folder)
            (root/'app.py').write_bytes(b'x=1')
            (root/'test_app.py').write_bytes(b'def test_x(): assert True')
            report=json.dumps({'exit_code':0,'junit':'<testsuite><testcase name="all"/></testsuite>','log':'ok'}).encode()
            responses=[subprocess.CompletedProcess([],0,b'container',b''),
                       subprocess.CompletedProcess([],0,report,b''),subprocess.CompletedProcess([],0,b'',b'')]
            with patch('execution.docker_runner.subprocess.run',side_effect=responses):
                result=run_tests(root,Repository('p','r',('app.py',),('test_app.py',)),image_id='sha256:'+'a'*64,
                                 artifact_root=Path(dest),test_targets=('test_app.py',))
            self.assertFalse(result.run.successful)
