import copy
import json
from pathlib import Path
import tempfile
import unittest

from experiments.tasks import load_pilots


class PilotTaskTests(unittest.TestCase):
    def data(self):
        return ({'schema': 1, 'scope': 'controlled-pilot-only', 'cases': [
            {'id': 'pilot_example', 'defect_type': 'null_type', 'requirements': ['f returns one.'],
             'sources': {'app.py': 'def f(): return 0\n'},
             'original_tests': {'tests/test_old.py': 'def test_old(): assert True\n'}}]},
            {'schema': 1, 'cases': {'pilot_example': {
                'reference_sources': {'app.py': 'def f(): return 1\n'},
                'holdout_tests': {'tests/test_private.py': 'from app import f\ndef test_private(): assert f() == 1\n'}}}})

    def load(self, visible, private):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / name for name in ('visible.json', 'private.json')]
            for path, data in zip(paths, (visible, private)):
                path.write_text(json.dumps(data), encoding='utf-8')
            return load_pilots(*paths)

    def test_pilot_scope_and_separate_validation_manifest(self):
        visible, private = self.data()
        cases, validation, manifest = self.load(visible, private)
        self.assertEqual(cases[0]['defect_type'], 'null_type')
        self.assertNotIn('reference_sources', cases[0])
        self.assertEqual(validation['pilot_example']['reference_sources'], private['cases']['pilot_example']['reference_sources'])
        self.assertEqual(set(manifest), {'visible', 'validation'})

    def test_unknown_schema_or_fields_are_rejected(self):
        for mutate in (lambda v, p: v.update(schema=2), lambda v, p: v['cases'][0].update(extra='ignored'),
                       lambda v, p: v.update(scope='formal'), lambda v, p: p['cases'].update(extra={})):
            visible, private = copy.deepcopy(self.data())
            mutate(visible, private)
            with self.assertRaises(ValueError):
                self.load(visible, private)

    def test_path_escape_private_overlap_and_mismatched_reference_rejected(self):
        for mutate in (lambda v, p: v['cases'][0]['sources'].update({'../escape.py': 'x=1'}),
                       lambda v, p: p['cases']['pilot_example']['holdout_tests'].update({'tests/test_old.py': 'x=1'}),
                       lambda v, p: p['cases']['pilot_example'].update(reference_sources={'other.py': 'x=1'})):
            visible, private = copy.deepcopy(self.data())
            mutate(visible, private)
            with self.assertRaises(ValueError):
                self.load(visible, private)
