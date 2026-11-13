import copy
import unittest
from unittest.mock import patch

from tests import test_freeze as fixtures_v1
from experiments.freeze_v2 import make_candidate_protocol, check_candidate
from experiments.runtime_v2 import expected_runtime


class FreezeV2Tests(unittest.TestCase):
    def fixtures(self):
        tasks, _, _ = fixtures_v1.FreezeTests().fixtures()
        public={'schema':'p08-formal-v1','tasks':[]}
        private={'schema':'p08-formal-v1','tasks':{}}
        for row in tasks:
            public['tasks'].append(copy.deepcopy(row['provenance']['task']))
            private['tasks'][row['case']['id']]=copy.deepcopy(row['evaluation'])
            row['provenance']['task']['protocol']=expected_runtime()
        protocol=make_candidate_protocol([r['case']['id'] for r in tasks],source_commit='a'*40)
        return tasks,protocol,(public,private)

    def check(self,tasks,protocol,baseline):
        with patch('experiments.formal_tasks_v2._baseline_documents',return_value=baseline):
            return check_candidate(tasks,protocol)

    def test_candidate_gate_is_pure_and_does_not_assert_qualification_or_freeze(self):
        tasks,protocol,baseline=self.fixtures();before=copy.deepcopy((tasks,protocol))
        result=self.check(tasks,protocol,baseline)
        self.assertEqual(result['task_count'],30)
        self.assertEqual(result['planned_records'],360)
        self.assertIs(result['formal_ready'],False)
        self.assertEqual(result['qualification'],'pending_V2_02')
        self.assertEqual(result['semantic_review'],'pending_V2_02_03')
        self.assertEqual((tasks,protocol),before)

    def test_changed_method_order_runtime_commit_schema_or_task_count_rejected(self):
        for mode in ('method','repeat','order','runtime','commit','schema','field','missing','duplicate','source','holdout','case_requirement'):
            with self.subTest(mode=mode):
                tasks,p,b=self.fixtures()
                if mode=='method': p['methods'][0]['repair_attempts']=1
                elif mode=='repeat': p['repetitions']=True
                elif mode=='order': p['task_order'].reverse()
                elif mode=='runtime': p['runtime']['image_id']='sha256:'+'0'*64
                elif mode=='commit': p['source_commit']='main'
                elif mode=='schema': p['schema']='p08-protocol-v1'
                elif mode=='field': p['formal_ready']=True
                elif mode=='missing': tasks.pop()
                elif mode=='duplicate': tasks[1]=copy.deepcopy(tasks[0])
                elif mode=='source': tasks[0]['case']['sources'][next(iter(tasks[0]['case']['sources']))]='pass'
                elif mode=='holdout': tasks[0]['validation']['holdout_tests']={}
                else: tasks[0]['case']['requirements']=['Changed']
                with self.assertRaises(ValueError): self.check(tasks,p,b)
