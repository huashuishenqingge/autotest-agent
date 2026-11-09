import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.admission import main
from models.adapter import ModelError


class AdmissionSafetyTests(unittest.TestCase):
    def test_existing_campaign_is_rejected_before_credentials_or_source_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            export=Path(folder)/'existing.json'
            export.write_text('{"preserve":true}')
            with patch('sys.argv',['admission','--call','--image-id','unused','--export',str(export)]), \
                 patch('experiments.admission.load_case',return_value=({}, {}, {})) as load, \
                 patch('experiments.admission.Config.from_env',side_effect=AssertionError('must not read credentials')):
                with self.assertRaises(FileExistsError):
                    main()
            load.assert_not_called()
            self.assertTrue(json.loads(export.read_text())['preserve'])

    def test_campaign_is_claimed_before_credentials_can_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            export=Path(folder)/'new.json'
            def unavailable(*args):
                self.assertTrue(export.exists())
                raise ModelError('environment','synthetic unavailable configuration')
            with patch('sys.argv',['admission','--call','--image-id','unused','--export',str(export)]), \
                 patch('experiments.admission.load_case',return_value=({}, {}, {})), \
                 patch('experiments.admission.Config.from_env',side_effect=unavailable):
                with self.assertRaises(ModelError):
                    main()
            self.assertEqual(json.loads(export.read_text())['groups'],[])

    def test_failed_group_is_not_masked_by_later_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);saved=root/'saved.json';export=root/'report.json'
            repair={'independent_validation':{'run':{'snapshot_digest':'same'}}}
            saved.write_text(json.dumps({'provenance':{},'groups':[
                {'group':g,'status':'verified','image_id':'image','rounds':[],'repair':repair} for g in ('B1','B2')]}))
            with patch('sys.argv',['admission','--replay',str(saved),'--image-id','image','--group','B1','--group','B2','--export',str(export)]), \
                 patch('experiments.admission.load_case',return_value=({}, {}, {})), \
                 patch('experiments.admission.run_experiment',side_effect=[
                     {'group':'B1','status':'generation_failed','repair':None},
                     {'group':'B2','status':'verified','repair':repair}]):
                self.assertEqual(main(),1)
            self.assertEqual(json.loads(export.read_text())['status'],'completed_with_failures')
