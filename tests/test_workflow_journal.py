import unittest
from workflow.journal import Journal
from workflow.state import Repository, TestRun, Phase, GenerateInput, GenerateOutput, FileProposal, Policy


class JournalTests(unittest.TestCase):
    def test_success_requires_matching_independent_source_hashes(self):
        journal = Journal('task')
        repo = Repository('p', 'fixed', ('app.py',))
        journal.parsed(repo)
        request = GenerateInput('task', repo, Policy(), ('requirement',), (('app.py', 'x=1'),))
        output = GenerateOutput('task', 'fixed', (FileProposal('tests/generated/test_a.py', 'def test_a(): assert True'),))
        journal.accept(request, output)
        journal.generated_inventory(output)
        journal.tested(TestRun('visible', 'digest', 0, 1, 0, 0))
        independent = TestRun('private', 'different', 0, 2, 0, 0)
        with self.assertRaises(ValueError):
            journal.verify(independent)
        with self.assertRaises(ValueError):
            journal.verify(independent, visible_sources={'app.py': 'a'*64}, independent_sources={'app.py': 'b'*64})
        journal.verify(independent, visible_sources={'app.py': 'a'*64}, independent_sources={'app.py': 'a'*64})
        self.assertEqual(journal.state.phase, Phase.VERIFIED)

    def test_independent_validation_is_required(self):
        journal = Journal('task')
        journal.parsed(Repository('p', 'fixed', ('app.py',)))
        with self.assertRaises(ValueError):
            journal.verify(TestRun('private', 'different', 0, 1, 0, 0))
        self.assertEqual(journal.state.phase, Phase.PARSED)

    def test_failure_is_terminal_and_preserves_history(self):
        journal = Journal('task')
        journal.fail('cancelled')
        self.assertEqual(journal.state.phase, Phase.FAILED)
        self.assertEqual(len(journal.events), 2)
        with self.assertRaises(ValueError):
            journal.parsed(Repository('p', 'fixed', ('app.py',)))


if __name__ == '__main__':
    unittest.main()
