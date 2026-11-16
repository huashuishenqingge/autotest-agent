import copy
import hashlib
import unittest

from evaluation.experiments import evaluate


class ExperimentMetricsTests(unittest.TestCase):
    def fixtures(self):
        good={'cleanup_ok':True,'run':{'exit_code':0,'passed':2,'failed':0,'errors':0,'failure':None}}
        bad={'cleanup_ok':True,'run':{'exit_code':1,'passed':1,'failed':1,'errors':0,'failure':{'kind':'test_assertion'}}}
        coverage=lambda n:{'lines':{'covered':n,'total':10},'branches':{'covered':n,'total':10}}
        records=[]
        for group,rounds in [('B0',0),('B1',1),('B2',2),('A0',2)]:
            detail={'rounds':[{'generation':{'files':[{'path':'tests/generated/test_a.py'}],'failure':None}} for _ in range(rounds)],'executions':[], 'generated_only':copy.deepcopy(bad) if rounds else None,'repair':{'attempts':[{'attempt':1}]} if group=='B1' else None}
            for n in range(1,rounds+1):
                detail['executions'] += [{'label':f'reference-{n}','result':copy.deepcopy(good)},{'label':f'round-{n}','result':copy.deepcopy(bad)}]
            records.append({'slot_id':group,'task_id':'task','project_id':'demo','defect_type':'boundary','group':group,'repeat':1,'status':'verified' if group=='B1' else 'baseline_survives' if group=='B0' else 'repair_not_reproducible','elapsed_seconds':2.0,'requests':[],'coverage':{'baseline':coverage(2),**({f'round-{rounds}':coverage(4)} if rounds else {})},'detail':detail})
        binding={'patch_id':'p','slot_id':'B1','task_id':'task','attempt':1,'review_key':'k','task_input_sha256':'1'*64,'diff_sha256':'2'*64,'before_source_sha256':'3'*64,'after_source_sha256':'4'*64,'is_last_attempt':True}
        review=dict(binding,status='confirmed',reviewer='independent',notes='Restores contract')
        return records,[binding],[review]

    def test_primary_denominator_keeps_failed_slots_and_zero_baseline(self):
        rows,b,v=self.fixtures();out=evaluate(rows,b,v,expected_per_group=1)
        self.assertEqual(out['groups']['B1']['semantic_confirmed'],1)
        self.assertEqual(out['groups']['B2']['semantic_rate'],0)
        self.assertEqual(out['groups']['B0']['detected'],0)
        self.assertEqual(out['groups']['B1']['generation_coverage']['line_delta_pp_mean'],20)
        self.assertIsNone(out['groups']['B0']['generation_coverage']['line_delta_pp_mean'])

    def test_uncertain_or_nonfinal_patch_never_counts_as_confirmed_success(self):
        for status in ('uncertain','rejected'):
            rows,b,v=self.fixtures();v[0]['status']=status
            self.assertEqual(evaluate(rows,b,v,expected_per_group=1)['groups']['B1']['semantic_confirmed'],0)
        rows,b,v=self.fixtures();b[0]['is_last_attempt']=v[0]['is_last_attempt']=False
        self.assertEqual(evaluate(rows,b,v,expected_per_group=1)['groups']['B1']['semantic_confirmed'],0)

    def test_missing_duplicate_stale_review_and_wrong_denominator_rejected(self):
        for mode in ('missing','duplicate','hash','denominator'):
            rows,b,v=self.fixtures()
            if mode=='missing': v=[]
            elif mode=='duplicate': v.append(copy.deepcopy(v[0]))
            elif mode=='hash': v[0]['after_source_sha256']='9'*64
            else: rows.pop()
            with self.subTest(mode=mode),self.assertRaises(ValueError): evaluate(rows,b,v,expected_per_group=1)

    def test_nonassertion_or_bad_reference_is_not_valid_detection(self):
        for mode in ('reference','exception'):
            rows,b,v=self.fixtures();detail=rows[1]['detail']
            if mode=='reference': detail['executions'][0]['result']['run']['exit_code']=1
            else: detail['generated_only']['run']['failure']['kind']='environment'
            self.assertEqual(evaluate(rows,b,v,expected_per_group=1)['groups']['B1']['detected'],0)

    def test_changed_coverage_denominator_is_reported_not_subtracted(self):
        rows,b,v=self.fixtures();rows[1]['coverage']['round-1']['lines']['total']=11
        out=evaluate(rows,b,v,expected_per_group=1)['groups']['B1']['generation_coverage']
        self.assertEqual(out['denominator_mismatches'],1)
        self.assertEqual(out['paired_records'],0)

    def test_unknown_usage_remains_unknown(self):
        rows,b,v=self.fixtures();rows[1]['requests']=[{'usage':None},{'usage':{'total_tokens':7}}]
        usage=evaluate(rows,b,v,expected_per_group=1)['groups']['B1']['usage']
        self.assertEqual(usage['known_total_tokens'],7)
        self.assertEqual(usage['unknown_requests'],1)
        self.assertIsNone(usage['total_tokens'])
