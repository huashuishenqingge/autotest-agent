import copy
import unittest
from pathlib import Path

from web.experiments import ExperimentData
from web.explanations import explain, TASK_GUIDES


class ExplanationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=ExperimentData(Path(__file__).resolve().parents[1])

    def test_all_frozen_tasks_have_checked_chinese_guides(self):
        self.assertEqual(set(TASK_GUIDES),set(self.data.tasks))
        for guide in TASK_GUIDES.values():
            self.assertTrue(all(guide))

    def test_hour_zero_explanation_does_not_change_original_response(self):
        detail=self.data.detail('007d44248ba3f62f4da61ead5e3ba954')
        before=copy.deepcopy(detail['saved_model_responses'])
        guide=explain(detail)
        self.assertIn('0点',guide['expected_behavior'])
        self.assertIn('正确参考版本',guide['generation'][0]['validation'])
        self.assertIn('1个',guide['generation'][0]['output'])
        self.assertIn('if value is not None',guide['repair'][0]['change'])
        self.assertEqual(before,detail['saved_model_responses'])
        self.assertIn('不是模型原文',guide['notice'])

    def test_rejected_patch_is_explained_as_failure_even_when_tests_pass(self):
        detail=self.data.detail('af0ef4975a4f736274587bce689b1458')
        guide=explain(detail)
        self.assertIn('不能算修复成功',guide['conclusion'])
        self.assertIn('漏掉',guide['repair'][-1]['semantic'])
        self.assertIn('0',guide['repair'][-1]['semantic'])

    def test_baseline_missing_reply_and_no_repair_attempt_remain_honest(self):
        for status in ('baseline_survives','generation_failed','repair_not_reproducible'):
            record=next(r for r in self.data.rows if r['status']==status)
            detail=self.data.detail(record['slot_id'])
            guide=explain(detail)
            if status=='baseline_survives':
                self.assertIn('没有调用模型',guide['conclusion'])
                self.assertEqual(guide['generation'],[])
            elif status=='generation_failed':
                self.assertIn('未完成',guide['conclusion'])
            else:
                self.assertEqual(guide['repair'],[])
                self.assertIn('没有请求模型修复',guide['conclusion'])
