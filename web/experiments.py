"""Read-only, receipt-checked presentation of the completed V2 campaign."""
import csv
import hashlib
import io
import json
from collections import Counter
from urllib.parse import parse_qs, urlsplit
import re

from repair.fixtures import PROJECT_ROOT
from evaluation.experiments import evaluate


class DataUnavailable(ValueError):
    pass


class ExperimentData:
    def __init__(self, root):
        self.root = root
        try:
            self._load()
        except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
            raise DataUnavailable('实验数据缺失或校验未通过') from error

    def _read(self, name, expected=None):
        data = (self.root/name).read_bytes()
        if len(data) > 10_000_000:
            raise ValueError('oversized evidence')
        if expected and hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest() != expected:
            raise ValueError('receipt hash mismatch')
        return json.loads(data)

    def _load(self):
        original = self._read('evidence/p08b-v2-formal-receipt.json')
        analysis = self._read('evidence/p08b-v2-06-receipt.json')
        frozen = self._read('evidence/p08b-v2-freeze.json')
        hashes = {v['path']:v['lf_sha256'] for v in original['files'] + analysis['files']}
        hashes.update({v['path']:v['sha256'] for v in frozen['files']})
        def bound(path):
            return self._read(path, hashes[path])
        raw = bound('evidence/p08b-v2-formal-results.json')
        self.metrics = bound('evidence/p08b-v2-06-metrics.json')
        sidecar = bound('evidence/p08b-v2-06-semantic-reviews.json')
        inputs = bound('evidence/p08b-v2-06-metric-inputs.json')
        self.tasks = {t['id']:t for t in bound('experiments/p08b-v2-tasks.json')['tasks']}
        self.projects = {p['id']:p for p in bound('experiments/projects.json')['projects']}
        protocol = bound('experiments/p08b-v2-protocol.json')
        self.methods = {m['id']:m for m in protocol['methods']}
        self.configured_model = protocol['runtime']['model']
        executions = bound('evidence/p08b-v2-formal-executions.json')['runs']
        self.runs = {run['run_id']:run for run in executions}
        if len(self.runs) != len(executions):
            raise ValueError('duplicate execution identity')
        if raw['schema'] != 'p08-formal-raw-export-v1' or self.metrics['schema'] != 'p08-v2-metrics-v1':
            raise ValueError('unsupported experiment schema')
        computed = evaluate(inputs['records'], inputs['bindings'], sidecar['patches'])
        if any(computed[k] != self.metrics[k] for k in computed):
            raise ValueError('metrics mismatch')
        self.rows = raw['records']
        self.by_slot = {r['slot_id']:r for r in self.rows}
        if len(self.by_slot) != 360 or len(self.rows) != 360:
            raise ValueError('duplicate or incomplete slots')
        response_receipt = self._read('evidence/p07b-model-responses-receipt.json')
        if (response_receipt['schema'] != 'p07b-model-response-receipt-v1'
                or response_receipt['path'] != 'evidence/p07b-model-responses.json'
                or response_receipt['source_export_lf_sha256'] != hashes['evidence/p08b-v2-formal-results.json']):
            raise ValueError('unsupported response receipt')
        responses = self._read('evidence/p07b-model-responses.json',response_receipt['lf_sha256'])
        if (responses['schema'] != 'p07b-parsed-model-responses-v1'
                or responses['source_export_lf_sha256'] != response_receipt['source_export_lf_sha256']):
            raise ValueError('unsupported response export')
        self.responses = {r['slot_id']:r for r in responses['records']}
        if len(self.responses) != len(responses['records']) or set(self.responses) != set(self.by_slot):
            raise ValueError('incomplete or duplicate model response slots')
        for row in self.rows:
            saved = self.responses[row['slot_id']]
            if (saved['result_sha256'] != row['result_sha256']
                    or [g['round'] for g in saved['generation']] != [g['round'] for g in row['generation_rounds']]
                    or len(saved['repair']) != (row['repair'] or {}).get('attempts',0)):
                raise ValueError('stale model response association')
        if Counter(r['group'] for r in self.rows) != {g:90 for g in self.methods}:
            raise ValueError('wrong group denominator')
        self.tests, self.patches, self.reviews = {}, {}, {}
        for review in sidecar['patches']:
            key = (review['slot_id'], review['attempt'])
            if key in self.reviews:
                raise ValueError('duplicate patch review')
            self.reviews[key] = review
        for test in bound('evidence/p08b-v2-formal-generated-tests.json')['tests']:
            if test['slot_id'] not in self.by_slot:
                raise ValueError('unknown generated-test slot')
            self.tests.setdefault(test['slot_id'], []).append(test)
        used = set()
        for patch in bound('evidence/p08b-v2-formal-patches.json')['patches']:
            slot, attempt = patch['slot_id'], patch['attempt']
            key = (slot, attempt)
            review = self.reviews[key]
            row = self.by_slot[slot]
            if (key in used or review['task_id'] != row['task_id']
                    or review['project_id'] != row['project_id']
                    or hashlib.sha256(patch['patch_diff'].encode()).hexdigest() != review['diff_sha256']):
                raise ValueError('stale patch association')
            used.add(key)
            # Do not expose machine-local evidence_sources paths.
            self.patches.setdefault(slot, []).append({'attempt':attempt, 'diff':patch['patch_diff'],
                'rollback_ok':patch['rollback_ok'], 'review':review})
        if used != set(self.reviews):
            raise ValueError('missing applied patch')
        self.semantic = {}
        for row in self.rows:
            if [q['request_index'] for q in row['requests']] != list(range(1,len(row['requests'])+1)):
                raise ValueError('unordered request identities')
            for ref in row['execution_refs']:
                self._execution(row,ref['run_id'])
            final = [p['review'] for p in self.patches.get(row['slot_id'], []) if p['review']['is_last_attempt']]
            if row['status'] == 'verified' and len(final) != 1:
                raise ValueError('verified slot missing final review')
            self.semantic[row['slot_id']] = final[0]['status'] if row['status'] == 'verified' else 'not_verified'
        for g in self.methods:
            if sum(r['group']==g and self.semantic[r['slot_id']]=='confirmed' for r in self.rows) != self.metrics['groups'][g]['semantic_confirmed']:
                raise ValueError('semantic summary mismatch')

    def summary(self):
        return {'schema':1,'evidence':'saved-real-v2','projects_count':len(self.projects),
            'tasks_count':len(self.tasks),'records_count':len(self.rows),
            'projects':[{'id':p['id'],'tag':p['tag'],'commit':p['commit']} for p in self.projects.values()],
            'methods':self.methods,'groups':self.metrics['groups'],'by_project':self.metrics['by_project'],
            'defect_types':sorted({r['defect_type'] for r in self.rows}),
            'statuses':sorted({r['status'] for r in self.rows}),
            'notice':'V2历史真实实验结果，本次浏览未重新运行模型或容器。',
            'limits':['全部为人工注入缺陷，真实历史缺陷0；目标函数由任务给定。',
                '每组固定90条；三次重复不是90个独立缺陷，技术通过与语义确认分开。',
                '18条严格文本复现门槛限制保留；覆盖率仅有数据且分母相同的条件样本。',
                '包装层已知1015730 tokens、未知12次；补充传输36983后仍3次未知，总量未知。']}

    def _record(self, row):
        return {k:row[k] for k in ('slot_id','task_id','project_id','group','repeat','defect_type','status','elapsed_seconds')} | {'semantic':self.semantic[row['slot_id']]}

    def records(self, query):
        allowed = {'project','method','defect_type','status','semantic','limit','offset'}
        if set(query)-allowed:
            raise ValueError('unknown filter')
        limit, offset = int(query.get('limit','25')), int(query.get('offset','0'))
        if not 1 <= limit <= 100 or not 0 <= offset <= 360:
            raise ValueError('invalid pagination')
        choices = {'project':set(self.projects),'method':set(self.methods),
            'defect_type':{r['defect_type'] for r in self.rows}, 'status':{r['status'] for r in self.rows},
            'semantic':{'confirmed','rejected','uncertain','not_verified'}}
        for key, values in choices.items():
            if key in query and query[key] not in values:
                raise ValueError('invalid filter')
        mapping = {'project':'project_id','method':'group','defect_type':'defect_type','status':'status','semantic':'semantic'}
        rows = [self._record(r) for r in self.rows]
        rows = [r for r in rows if all(key not in query or r[field]==query[key] for key,field in mapping.items())]
        return {'records':rows[offset:offset+limit],'total':len(rows),'limit':limit,'offset':offset}

    def detail(self, slot):
        row = self.by_slot[slot]
        task = self.tasks[row['task_id']]
        requests = row['requests']
        known = sum(q['usage']['total_tokens'] for q in requests if q['usage'] is not None)
        unknown = sum(q['usage'] is None for q in requests)
        return {'record':self._record(row),'target':row['target'],
            'requirements':task['requirements'],'requirement_sources':task['requirement_sources'],
            'project':{k:self.projects[row['project_id']][k] for k in ('id','tag','commit')},
            'tests':self.tests.get(slot, []),'patches':self.patches.get(slot, []),
            'coverage':row['coverage'],'generation_rounds':row['generation_rounds'],
            'model_activity':self._model_activity(row),
            'saved_model_responses':self._saved_responses(row),
            'usage':{'requests':len(requests),'known_tokens':known,'unknown_requests':unknown,'total_tokens':None if unknown else known},
            'log_notice':'原始完整日志未包含在Git共享导出中；此处显示已保存的状态、测试与补丁，未重新执行。'}

    def _execution(self, row, run_id):
        run = self.runs[run_id]
        if '/'+row['slot_id']+'/' not in run['artifact_directory']:
            raise ValueError('execution belongs to another slot')
        return {k:run[k] for k in ('run_id','exit_code','passed','failed','errors','skipped','failure_kind','cleanup_ok')}

    def _saved_responses(self, row):
        saved = self.responses[row['slot_id']]
        repairs = []
        for attempt in saved['repair']:
            operations = {key:self._execution(row,attempt[key+'_id']) if attempt[key+'_id'] else None
                          for key in ('visible_run','regression_run')}
            repairs.append(attempt | operations)
        return {'format':'saved_parsed_json','byte_exact_reply_available':False,
            'notice':'以下是本机原始运行文件保存的模型回复解析后JSON，未重新生成。原始字符串格式及完整请求原文未保存。操作记录由程序产生，不是模型自行执行命令。',
            'generation':saved['generation'],'repair':repairs}

    def _model_activity(self, row):
        refs = {r['label']:r['run_id'] for r in row['execution_refs']}
        def execution(label):
            return self._execution(row,refs[label]) if label in refs else None
        generation = []
        for item in row['generation_rounds']:
            number = item['round']
            generation.append({'round':number,'status':item['status'], 'failure':item['failure'],
                'coverage_feedback':self.methods[row['group']]['coverage_feedback'] and number==2,
                'output_paths':item['generated_paths'],
                'reference_run':execution(f'reference-{number}'),
                'mutant_run':execution(f'round-{number}')})
        requests = []
        for item in row['requests']:
            index = item['request_index']
            is_generation = index <= len(generation)
            requests.append({'request_index':index,'role':'test_generation' if is_generation else 'source_repair',
                'round':index if is_generation else index-len(generation),
                'time':item['time'],'status':item['status'],'model':item.get('model'),
                'usage':item['usage'],'error_type':item.get('error_type'),
                'finish_reason':(item.get('transport_metadata') or {}).get('finish_reason')})
        repair = row['repair']
        repair_view = None
        if repair is not None:
            validation = repair.get('independent_validation')
            repair_view = {'status':repair['status'],'attempts':repair['attempts'],
                'independent_validation':self._execution(row,validation['run_id']) if validation else None}
        return {'configured_model':self.configured_model,'requests':requests,'generation':generation,
            'baseline':execution('baseline'),'generated_only':execution('generated-only'),
            'repair':repair_view,'full_prompt_available':False,'full_raw_response_available':False,
            'input_description_notice':'输入用途按冻结流程说明；共享导出不含逐次完整提示词或回复的原始字符串，保存的真实JSON见上方。以下用途说明不是请求原文。'}


def dataset():
    # Revalidate the fixed export files on every request. No stale successful
    # snapshot after collaborators replace or remove evidence while serving.
    return ExperimentData(PROJECT_ROOT)


def serve(request):
    """Return False for unrelated URLs; preserve the existing handler boundary."""
    url = urlsplit(request.path)
    if not url.path.startswith('/api/experiments/'):
        return False
    try:
        query = parse_qs(url.query, keep_blank_values=True, max_num_fields=7)
        if any(len(v)!=1 for v in query.values()):
            raise ValueError('duplicate query')
        query = {k:v[0] for k,v in query.items()}
        data = dataset()
        if url.path == '/api/experiments/summary' and not query:
            request.send(200,data.summary())
        elif url.path == '/api/experiments/records':
            request.send(200,data.records(query))
        elif url.path == '/api/experiments/download' and set(query) <= {'format'}:
            fmt = query.get('format','json')
            if fmt == 'json':
                request.send(200,data.summary(),download=True,filename='experiment-summary.json')
            elif fmt == 'csv':
                stream = io.StringIO(newline='')
                writer = csv.writer(stream)
                writer.writerow(['group','planned','detected','technical_verified','semantic_confirmed','semantic_rate'])
                for group, row in data.metrics['groups'].items():
                    writer.writerow([group]+[row[k] for k in ('planned','detected','technical_verified','semantic_confirmed','semantic_rate')])
                request.send(200,stream.getvalue().encode('utf-8-sig'),'text/csv; charset=utf-8',True,'experiment-summary.csv')
            else:
                raise ValueError('invalid format')
        else:
            match = re.fullmatch(r'/api/experiments/records/([a-f0-9]{32})(/download)?',url.path)
            if match and not query:
                request.send(200,data.detail(match[1]),download=bool(match[2]),filename='experiment-record.json')
            elif query:
                raise ValueError('unexpected query')
            else:
                request.send(404,{'error':'实验记录或地址不存在'})
    except DataUnavailable:
        request.send(503,{'error':'实验数据不可用，请检查仓库证据文件是否完整。'})
    except KeyError:
        request.send(404,{'error':'实验记录不存在'})
    except ValueError:
        request.send(400,{'error':'筛选或分页参数无效'})
    return True
