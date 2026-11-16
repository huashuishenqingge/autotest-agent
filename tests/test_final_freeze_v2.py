import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_freeze_v2 as fixture_module
from agents.repairer import context_digest
from experiments.screening import review_digest
from experiments.qualification_v2 import STAGES
from experiments.runtime_v2 import IMAGE
from experiments.formal_tasks_v2 import _canonical
from experiments.final_freeze_v2 import (make_protocol, validate_qualification, verify_files,
                                          validate_protocol, read_evidence)


class FinalFreezeV2Tests(unittest.TestCase):
    def test_tree_object_cannot_be_used_as_commit(self):
        from experiments.final_freeze_v2 import git_blob
        with patch('experiments.final_freeze_v2.subprocess.check_output',return_value=b'tree\n'):
            with self.assertRaises(ValueError): git_blob(Path('.'),'a'*40,'a.py')

    def test_accepted_evidence_anchor_is_fixed(self):
        documents=read_evidence()
        self.assertEqual(documents[0]['qualified'],30)
        with patch('experiments.final_freeze_v2._read',return_value={'changed':True}):
            with self.assertRaises(ValueError): read_evidence()

    def fixtures(self):
        tasks,_,baseline=fixture_module.FreezeV2Tests().fixtures()
        report={'results':[],'qualified':30,'status':'complete','planned_executions':120,'formal_ready':False,'model_calls':0}
        index={'image_id':IMAGE,'records':[]};reviews=[]
        for n,t in enumerate(tasks):
            digest=review_digest(t)
            review={'schema':'p08-v2-semantic-review-v1','task_id':t['case']['id'],'input_sha256':digest,'reviewer':'independent','status':'approved','notes':'Independent contract check'}
            reviews.append(review)
            row={'task_id':t['case']['id'],'project_id':t['provenance']['task']['project_id'],'input_sha256':digest,'semantic_review':review,'image_id':IMAGE,'formal_ready':False,'model_calls':0,'status':'qualified','executions':[]}
            for stage in STAGES:
                src=t['validation']['reference_sources'] if stage.startswith('reference') else t['case']['sources']
                tests=dict(t['case']['original_tests'])
                if stage.endswith('holdout'): tests.update(t['validation']['holdout_tests'])
                snap=context_digest(tuple(src.items()),tuple(tests.items()))
                fail=stage=='mutant_holdout';run_id=f'run_{n}_{stage}'
                run={'run_id':run_id,'snapshot_digest':snap,'exit_code':1 if fail else 0,'passed':1,'failed':int(fail),'errors':0,'skipped':0,'failure':{'kind':'test_assertion'} if fail else None}
                e={'stage':stage,'valid':True,'result':{'run':run,'cleanup_ok':True},'primary_source_count':1,
                   'failure_nodes':[{'classname':f"tests.p08_holdout.test_{t['case']['id']}",'name':'test_hold'}] if fail else [],
                   'manifest_sha256':'1'*64,'junit_sha256':'2'*64,'coverage_sha256':'3'*64}
                row['executions'].append(e)
                index['records'].append({'task_id':row['task_id'],'stage':stage,'valid':True,'run_id':run_id,'snapshot_digest':snap,'passed':run['passed'],'failed':run['failed'],'errors':0,'skipped':0,'cleanup_ok':True,'manifest_sha256':'1'*64,'junit_sha256':'2'*64,'coverage_sha256':'3'*64})
            report['results'].append(row)
        return tasks,report,index,reviews,baseline

    def check(self,tasks,report,index,reviews,baseline):
        with patch('experiments.formal_tasks_v2._baseline_documents',return_value=baseline):
            return validate_qualification(tasks,report,index,reviews)

    def test_all_thirty_current_reviews_and_120_unique_qualified_runs_required(self):
        tasks,r,i,v,b=self.fixtures()
        result=self.check(tasks,r,i,v,b)
        self.assertEqual(result['qualified_tasks'],30)
        self.assertEqual(result['qualified_executions'],120)
        self.assertIs(result['launch_authorized'],False)

    def test_bad_or_stale_qualification_review_and_index_rejected(self):
        for mode in ('missing','status','stale','duplicate','snapshot','coverage','cleanup','failure_node','index','original_failure','unknown_stage'):
            with self.subTest(mode=mode):
                tasks,r,i,v,b=self.fixtures();row=r['results'][0];entry=row['executions'][-1]
                if mode=='missing': r['results'].pop()
                elif mode=='status': row['status']='claimed'
                elif mode=='stale': v[0]['input_sha256']='0'*64
                elif mode=='duplicate': entry['result']['run']['run_id']=row['executions'][0]['result']['run']['run_id']
                elif mode=='snapshot': entry['result']['run']['snapshot_digest']='0'*64
                elif mode=='coverage': entry['primary_source_count']=0
                elif mode=='cleanup': entry['result']['cleanup_ok']=False
                elif mode=='failure_node': entry['failure_nodes'][0]['classname']='tests.test_old'
                elif mode=='index': i['records'][-1]['manifest_sha256']='9'*64
                elif mode=='original_failure': row['executions'][0]['result']['run']['failed']=1
                else: entry['stage']='mutant_holdout_2'
                with self.assertRaises(ValueError): self.check(tasks,r,i,v,b)

    def test_final_protocol_refuses_candidate_v1_method_runtime_and_noncommit(self):
        ids=['task_a','task_b'];p=make_protocol(ids,source_commit='a'*40)
        validate_protocol(p,ids)
        for mode in ('candidate','v1','method','runtime','commit','unknown'):
            q=copy.deepcopy(p)
            if mode=='candidate': q['schema']='p08-protocol-v2-candidate'
            elif mode=='v1': q['schema']='p08-protocol-v1'
            elif mode=='method': q['methods'][0]['generation_rounds']=True
            elif mode=='runtime': q['runtime']['image_id']='sha256:'+'0'*64
            elif mode=='commit': q['source_commit']='main'
            else: q['launch_authorized']=True
            with self.subTest(mode=mode),self.assertRaises(ValueError): validate_protocol(q,ids)

    def test_manifest_cannot_omit_files_change_hashes_or_normalization(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'a.py').write_bytes(b'x=1\r\n')
            entry={'path':'a.py','normalization':'lf','sha256':hashlib.sha256(b'x=1\n').hexdigest()}
            manifest={'schema':'p08-v2-freeze-v1','scope':'frozen_inputs_no_launch','implementation_commit':'a'*40,'files':[entry],'validation':{}}
            with patch('experiments.final_freeze_v2.BOUND_PATHS',('a.py',)):
                verify_files(root,manifest)
                for mode in ('missing','hash','normalization','field'):
                    m=copy.deepcopy(manifest)
                    if mode=='missing': m['files']=[]
                    elif mode=='hash': m['files'][0]['sha256']='0'*64
                    elif mode=='normalization': m['files'][0]['normalization']='skip'
                    else: m['files'][0]['optional']=True
                    with self.subTest(mode=mode),self.assertRaises(ValueError): verify_files(root,m)
