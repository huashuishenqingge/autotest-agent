import importlib.util
import json
from pathlib import Path
import runpy
import tempfile
import unittest

from execution.container_worker import pytest_arguments, export_coverage


class WorkerCollectionTests(unittest.TestCase):
    def test_all_entry_modes_have_the_same_explicit_root_and_keep_target_order(self):
        default = pytest_arguments([])
        targeted = pytest_arguments(['tests/test_a.py', 'tests/test_b.py'])
        self.assertIn('--rootdir=/workspace', default)
        self.assertIn('--rootdir=/workspace', targeted)
        self.assertEqual(default[:-1], targeted[:-2])
        self.assertEqual(default[-1], '/workspace')
        self.assertEqual(targeted[-2:], ['/workspace/tests/test_a.py', '/workspace/tests/test_b.py'])

    def test_invalid_or_duplicate_targets_still_fail_closed(self):
        for values in ('tests/test_x.py', ['../test_x.py'], ['/test_x.py'], ['tests/x.py\n'],
                       ['tests/a.py', 'tests/a.py'], ['tests/a.txt']):
            with self.subTest(values=values), self.assertRaises(ValueError):
                pytest_arguments(values)


@unittest.skipUnless(importlib.util.find_spec('coverage'), 'trusted coverage dependency tested in pinned Linux image')
class WorkerMeasurementTests(unittest.TestCase):
    def test_unimported_source_has_real_missing_lines_and_branches_without_execution(self):
        import coverage
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = root/'app.py'
            app.write_text('def used(flag):\n    if flag:\n        return 1\n    return 2\n', encoding='utf-8')
            # Match a src layout whose parent directory is not a Python package:
            # coverage's automatic unexecuted-file walk does not discover it.
            cold = root/'src/demo/_unused.py'
            cold.parent.mkdir(parents=True)
            cold.write_text('def cold(flag):\n    if flag:\n        return 1\n    return 2\nraise RuntimeError("must never be imported")\n', encoding='utf-8')
            data_file, output_file = root/'.coverage', root/'coverage.json'
            cov = coverage.Coverage(data_file=str(data_file), branch=True, source=[str(root)], config_file=False)
            with cov.collect():
                self.assertEqual(runpy.run_path(str(app))['used'](True), 1)
            cov.save()
            before_lines = cov.get_data().lines(str(app.resolve()))
            before_arcs = cov.get_data().arcs(str(app.resolve()))
            self.assertIsNone(cov.get_data().lines(str(cold.resolve())))
            report = export_coverage(root, data_file, output_file)
            cov.load()
            self.assertEqual(cov.get_data().lines(str(app.resolve())), before_lines)
            self.assertEqual(cov.get_data().arcs(str(app.resolve())), before_arcs)
            item = next(v for p, v in report['files'].items() if Path(p).resolve() == cold.resolve())
            self.assertEqual(item['executed_lines'], [])
            self.assertEqual(item['executed_branches'], [])
            self.assertIn(2, item['missing_lines'])
            self.assertGreaterEqual(len(item['missing_branches']), 2)
            self.assertEqual(json.loads(output_file.read_text(encoding='utf-8')), report)
            self.assertIn('src/demo/_unused.py', report['meta']['autotest_completion']['zero_recorded_files'])

    def test_missing_measurement_database_cannot_be_replaced_by_zero_statistics(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'app.py').write_text('x=1\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                export_coverage(root, root/'absent.coverage', root/'coverage.json')


if __name__ == '__main__':
    unittest.main()
