import unittest
from evaluation.coverage import summarize


class CoverageTests(unittest.TestCase):
    def test_only_approved_source_counts_and_missing_lines_are_preserved(self):
        report = {'files': {'app.py': {'executed_lines': [1, 2], 'missing_lines': [3],
            'executed_branches': [[2, 3]], 'missing_branches': [[2, -1]]},
            'test_app.py': {'executed_lines': [1], 'missing_lines': []}}}
        result = summarize(report, ('app.py',))
        self.assertEqual(result['lines'], {'covered': 2, 'total': 3})
        self.assertEqual(result['branches'], {'covered': 1, 'total': 2})
        self.assertEqual(result['files']['app.py']['missing_lines'], [3])
        self.assertNotIn('test_app.py', result['files'])

    def test_missing_source_or_conflicting_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            summarize({'files': {}}, ('app.py',))
        with self.assertRaises(ValueError):
            summarize({'files': {'app.py': {'executed_lines': [1], 'missing_lines': [1]}}}, ('app.py',))


if __name__ == '__main__':
    unittest.main()
