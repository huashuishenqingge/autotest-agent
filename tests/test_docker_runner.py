import tempfile
import unittest
import os
import stat
import json
import subprocess
from unittest.mock import patch
from pathlib import Path

from execution.docker_runner import stage_snapshot, docker_command, run_tests
from workflow.state import Repository, Policy, FailureKind


class DockerRunnerTests(unittest.TestCase):
    def test_cleanup_failure_cannot_verify_a_passing_report(self):
        # Simulated CLI evidence verifies control flow, not real Docker behavior.
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as artifacts:
            source = Path(root)
            (source / 'app.py').write_bytes(b'x=1')
            report = json.dumps({'exit_code': 0, 'junit': '<testsuite><testcase name="ok"/></testsuite>', 'log': 'ok'}).encode()
            responses = [subprocess.CompletedProcess([], 0, b'container', b''),
                         subprocess.CompletedProcess([], 0, report, b''),
                         subprocess.CompletedProcess([], 1, b'', b'cleanup failed')]
            with patch('execution.docker_runner.subprocess.run', side_effect=responses):
                result = run_tests(source, Repository('p', 'r', ('app.py',)),
                    image_id='sha256:'+'a'*64, artifact_root=Path(artifacts))
            self.assertFalse(result.cleanup_ok)
            self.assertFalse(result.run.successful)
            self.assertEqual(result.run.failure.kind, FailureKind.ENVIRONMENT)
            metadata = json.loads((Path(result.artifact_directory) / 'manifest.json').read_text())
            self.assertFalse(metadata['cleanup_ok'])
            self.assertEqual((source / 'app.py').read_bytes(), b'x=1')

    def test_sanitized_junit_remains_usable_for_failure_reproduction(self):
        from repair.session import _failure_evidence
        import xml.etree.ElementTree as ET
        for assignment in ('token=abc', 'password=x'):
            with self.subTest(assignment=assignment), tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as artifacts:
                source = Path(root)
                (source / 'app.py').write_bytes(b'x=1')
                xml = ('<testsuite><testcase classname="test_app" name="test_boundary">'
                       '<failure type="AssertionError" message="' + assignment + '">'
                       'AssertionError: assert False; key=a&amp;b</failure>'
                       '<system-out>' + assignment + '</system-out></testcase></testsuite>')
                report = json.dumps({'exit_code': 1, 'junit': xml, 'log': assignment}).encode()
                responses = [subprocess.CompletedProcess([], 0, b'container', b''),
                             subprocess.CompletedProcess([], 0, report, b''),
                             subprocess.CompletedProcess([], 0, b'', b'')]
                with patch('execution.docker_runner.subprocess.run', side_effect=responses):
                    result = run_tests(source, Repository('p', 'r', ('app.py',)),
                        image_id='sha256:'+'a'*64, artifact_root=Path(artifacts), secrets=('a&b',))
                saved = (Path(result.artifact_directory) / 'junit.xml').read_text(encoding='utf-8')
                tree = ET.fromstring(saved)
                self.assertEqual(result.run.failure.kind, FailureKind.TEST_ASSERTION)
                self.assertEqual(tree.find('.//failure').get('message'), assignment.split('=')[0] + '=[REDACTED]')
                self.assertNotIn('a&b', ''.join(tree.itertext()))
                self.assertNotIn(assignment, saved)
                self.assertEqual(len(_failure_evidence(result)), 1)

    def test_log_limit_preserves_truncation_marker(self):
        from execution.docker_runner import bounded_log
        text = bounded_log('x' * 200 + '\n[log truncated]', 64)
        self.assertLessEqual(len(text.encode('utf-8')), 64)
        self.assertTrue(text.endswith('[log truncated]'))

    def test_staging_freezes_bytes_and_rejects_missing_inventory(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as staging:
            root, dest = Path(source), Path(staging)
            (root / 'app.py').write_bytes(b'x=1\n')
            repo = Repository('p', 'baseline', ('app.py',), ('tests/test_app.py',))
            with self.assertRaises(ValueError):
                stage_snapshot(root, dest, repo)
            (root / 'tests').mkdir()
            (root / 'tests/test_app.py').write_bytes(b'def test_x(): assert True\n')
            digest, manifest = stage_snapshot(root, dest, repo)
            self.assertEqual(len(digest), 64)
            self.assertEqual(len(manifest), 2)
            (root / 'app.py').write_bytes(b'new')
            self.assertEqual((dest / 'app.py').read_bytes(), b'x=1\n')
            self.assertFalse((dest / '.env').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX mode bits require Linux')
    def test_snapshot_directory_readable_by_container_user(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as staging:
            root, dest = Path(source), Path(staging)
            (root / 'app.py').write_bytes(b'x=1')
            stage_snapshot(root, dest, Repository('p', 'r', ('app.py',)))
            self.assertTrue(stat.S_IMODE(dest.stat().st_mode) & stat.S_IXOTH)

    def test_command_is_fixed_and_restricted(self):
        command = docker_command(Path('snapshot').resolve(), 'autotest-abc', 'sha256:' + 'a'*64, Policy())
        self.assertIn('--read-only', command)
        self.assertIn('--network=none', command)
        self.assertIn('--cap-drop=ALL', command)
        self.assertIn('--user=65534:65534', command)
        self.assertIn('--pull=never', command)
        self.assertIn('--log-driver=none', command)
        self.assertNotIn('bash', command)
        self.assertTrue(any('readonly' in part for part in command))
        with self.assertRaises(ValueError):
            docker_command(Path('.'), 'autotest-abc', 'unreviewed:latest', Policy())

    def test_missing_docker_never_runs_on_host(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as artifacts:
            source = Path(root)
            (source / 'app.py').write_text('x=1', encoding='utf-8')
            repo = Repository('p', 'r', ('app.py',))
            result = run_tests(source, repo, image_id='sha256:'+'a'*64,
                               artifact_root=Path(artifacts), docker_executable='autotest-no-such-docker')
            self.assertEqual(result.run.failure.kind, FailureKind.ENVIRONMENT)
            self.assertFalse(result.run.successful)


if __name__ == '__main__':
    unittest.main()
