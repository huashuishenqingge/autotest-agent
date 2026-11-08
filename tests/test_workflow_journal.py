import unittest
from workflow.journal import Journal
from workflow.state import Repository, TestRun, Phase


class JournalTests(unittest.TestCase):
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
