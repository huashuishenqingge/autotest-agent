import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution.proposals import apply_proposal, rollback, ApplyError
from execution.results import classify_result
from workflow.state import (Repository, Policy, GenerateInput, GenerateOutput,
    FileProposal, RepairInput, RepairOutput, Failure, FailureKind, TestRun)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'app.py').write_bytes(b'def add(a,b): return a-b\n')
        self.repo = Repository('p', 'baseline', ('app.py',))
        self.request = GenerateInput('t', self.repo, Policy(), ('addition',), ())

    def test_create_conflict_and_rollback(self):
        output = GenerateOutput('t', 'baseline', (FileProposal('tests/generated/test_app.py', 'assert True\n'),))
        receipt = apply_proposal(self.root, self.request, output)
        self.assertTrue((self.root / output.files[0].path).is_file())
        self.assertIn('+assert True', receipt.diff)
        with self.assertRaises(ApplyError) as caught:
            apply_proposal(self.root, self.request, output)
        self.assertEqual(caught.exception.kind, FailureKind.PATCH_CONFLICT)
        rollback(self.root, receipt)
        self.assertFalse((self.root / output.files[0].path).exists())

    def repair_request(self):
        run = TestRun('r', 'snapshot', 1, 0, 1, 0, failure=Failure(FailureKind.TEST_ASSERTION, 'assertion'))
        return RepairInput('t', self.repo, Policy(), run, 1, (), 'assertion')

    def test_repair_hash_and_stale_rollback(self):
        before = (self.root / 'app.py').read_bytes()
        request = self.repair_request()
        stale = RepairOutput('t', 'baseline', 'r', (FileProposal('app.py', 'x=1', '0'*64),))
        with self.assertRaises(ApplyError):
            apply_proposal(self.root, request, stale)
        output = RepairOutput('t', 'baseline', 'r', (FileProposal('app.py', 'def add(a,b): return a+b\n', sha(before)),))
        receipt = apply_proposal(self.root, request, output)
        rollback(self.root, receipt)
        self.assertEqual((self.root / 'app.py').read_bytes(), before)
        receipt = apply_proposal(self.root, request, output)
        (self.root / 'app.py').write_bytes(b'new change')
        with self.assertRaises(ApplyError):
            rollback(self.root, receipt)
        self.assertEqual((self.root / 'app.py').read_bytes(), b'new change')

    def test_all_preconditions_checked_before_writing(self):
        (self.root / 'tests').mkdir()
        (self.root / 'tests/generated').mkdir()
        (self.root / 'tests/generated/test_b.py').write_bytes(b'existing')
        output = GenerateOutput('t', 'baseline', (
            FileProposal('tests/generated/test_a.py', 'assert True'),
            FileProposal('tests/generated/test_b.py', 'assert True')))
        with self.assertRaises(ApplyError):
            apply_proposal(self.root, self.request, output)
        self.assertFalse((self.root / output.files[0].path).exists())

    def test_partial_write_failure_restores_previous_files(self):
        output = GenerateOutput('t', 'baseline', (
            FileProposal('tests/generated/test_a.py', 'assert True'),
            FileProposal('tests/generated/test_b.py', 'assert True')))
        import execution.proposals as module
        real = module._replace_file
        counter = 0
        def fail_second(*args):
            nonlocal counter
            counter += 1
            if counter == 2:
                raise OSError('injected write failure')
            return real(*args)
        with patch.object(module, '_replace_file', side_effect=fail_second):
            with self.assertRaises(ApplyError):
                apply_proposal(self.root, self.request, output)
        self.assertFalse((self.root / output.files[0].path).exists())
        self.assertFalse((self.root / output.files[1].path).exists())

    def test_protected_scope_and_symlink_parent(self):
        with self.assertRaises(ValueError):
            apply_proposal(self.root, self.request, GenerateOutput('t', 'baseline', (FileProposal('app.py','x'),)))
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / 'tests').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('symlink permission unavailable')
            with self.assertRaises(ApplyError):
                apply_proposal(self.root, self.request, GenerateOutput('t', 'baseline', (FileProposal('tests/generated/test_x.py','x'),)))


class ResultTests(unittest.TestCase):
    def result(self, code, xml, **kwargs):
        return classify_result('run', 'digest', code, xml.encode(), **kwargs)

    def test_pass_empty_and_malformed(self):
        run = self.result(0, '<testsuites><testsuite><testcase name="a"/></testsuite></testsuites>')
        self.assertTrue(run.successful)
        self.assertFalse(self.result(0, '<testsuite/>').successful)
        self.assertEqual(self.result(0, 'not xml').failure.kind, FailureKind.INVALID_OUTPUT)

    def test_assertion_collection_environment_timeout(self):
        assertion = '<testsuite><testcase><failure type="AssertionError">assert 1 == 2</failure></testcase></testsuite>'
        self.assertEqual(self.result(1, assertion).failure.kind, FailureKind.TEST_ASSERTION)
        for code, xml, expected in (
            (2, '<testsuite><testcase><error>ImportError</error></testcase></testsuite>', FailureKind.TEST_COLLECTION),
            (1, '<testsuite><testcase><failure type="ValueError">ValueError: bad</failure></testcase></testsuite>', FailureKind.ENVIRONMENT),
            (125, '', FailureKind.ENVIRONMENT),
        ):
            self.assertEqual(self.result(code, xml).failure.kind, expected)
        self.assertEqual(self.result(124, '', timed_out=True).failure.kind, FailureKind.TIMEOUT)

    def test_inconsistent_counts_do_not_verify(self):
        xml = '<testsuite><testcase><failure type="AssertionError"/></testcase></testsuite>'
        self.assertFalse(self.result(0, xml).successful)


if __name__ == '__main__':
    unittest.main()
