"""Deterministic p08-context-v1 excerpts; execution inventory remains complete."""
import ast
from dataclasses import asdict
import hashlib
import json

from agents.generator import SYSTEM as GENERATION_SYSTEM, parse_generation
from experiments.functions import locate_function
from models.adapter import ModelError, strict_json
from workflow.state import Failure, FailureKind, GenerateOutput

VERSION = 'p08-context-v1'


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


def _segment(text, node, decorators=False):
    lines = text.encode('utf-8').splitlines(keepends=True)
    start_line = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])]) if decorators else node.lineno
    column = node.col_offset
    start = sum(map(len, lines[:start_line-1])) + column
    end = sum(map(len, lines[:node.end_lineno-1])) + node.end_col_offset
    return text.encode('utf-8')[start:end].decode('utf-8'), start_line


def _hint(value):
    if value is None:
        return None
    result = {'files': {}, 'truncated': False}
    for path, item in sorted(value.get('files', value).items()):
        target = {'missing_lines': [], 'missing_branches': []}
        result['files'][path] = target
        if len(encoded(result)) > 1500:
            del result['files'][path]
            result['truncated'] = True
            break
        for key in ('missing_lines', 'missing_branches'):
            for entry in sorted(item.get(key, [])):
                target[key].append(entry)
                if len(encoded(result)) > 1450:
                    target[key].pop()
                    result['truncated'] = True
                    break
    return result


def build_messages(request, requirements, target, tests, system, *, failure_evidence=(),
                   coverage_hint=None, generated_paths=(), repair=False):
    sources = dict(request.source_texts)
    path, name = target
    if path not in request.repository.source_files or path not in sources:
        raise ValueError('target not in source inventory')
    if len(sources[path].encode('utf-8')) > request.policy.max_file_bytes:
        raise ModelError('budget', 'context_budget: target module exceeds full-file proposal cap')
    scope = locate_function(sources[path], name)
    mapping = {'path': path, 'qualified_name': name, 'line': scope['line'], 'end_line': scope['end_line'],
               'classes': scope['classes'], 'decorators': scope['decorators']}
    if not requirements or any(not isinstance(r, str) or not r.strip() for r in requirements):
        raise ValueError('independent requirements required')
    if len(encoded(requirements)) > 2000 or len(encoded(scope['code'])) > 6000 or len(encoded(mapping)) > 1500:
        raise ModelError('budget', 'context_budget: mandatory target or requirements exceed cap')
    payload = {'task_id': request.task_id, 'base_revision': request.repository.revision,
               'requirements': requirements, 'context_version': VERSION, 'target': mapping,
               'target_definition': scope['code'], 'source_sha256': hashlib.sha256(sources[path].encode()).hexdigest(),
               'imports': [], 'related_symbols': [], 'related_tests': []}
    if repair:
        payload.update(based_on_run_id=request.failed_run.run_id, snapshot_digest=request.failed_run.snapshot_digest,
                       attempt=request.attempt)
        if not failure_evidence:
            raise ValueError('current failure identity required')
        failure = encoded(sorted(failure_evidence)[0])
        payload['failure_excerpt'] = failure if len(failure) <= 2000 else failure[:1400] + '\n[truncated]\n' + failure[-400:]
    else:
        payload.update(test_prefix=request.policy.test_prefix,
                       existing_generated_paths=[p for p in request.repository.test_files if p.startswith(request.policy.test_prefix)])
    audit = {'version': VERSION, 'selected': [dict(mapping, kind='target')], 'omitted': [],
             'source_sha256': payload['source_sha256']}
    def messages(data):
        return [{'role': 'system', 'content': system}, {'role': 'user', 'content': encoded(data)}]
    def length(data):
        return sum(len(m['content']) for m in messages(data))
    if length(payload) + 1700 > 16000:
        raise ModelError('budget', 'context_budget: mandatory prompt exceeds cap')
    def add(field, fragment, cap):
        size = len(encoded(payload[field] + [fragment]))
        payload[field].append(fragment)
        if size > cap or length(payload) + 1700 > 16000:
            payload[field].pop()
            audit['omitted'].append({'path': fragment['path'], 'line': fragment['line'], 'kind': field, 'reason': 'budget'})
        else:
            audit['selected'].append({'path': fragment['path'], 'line': fragment['line'], 'kind': field})
    module = scope['tree']
    for node in module.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            text, line = _segment(sources[path], node)
            add('imports', {'path': path, 'line': line, 'content': text}, 1000)
    references = {n.id for n in ast.walk(scope['node']) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in references and node is not scope['node']:
            signature = (node.name + '(' + ast.unparse(node.args) + ')') if hasattr(node, 'args') else ('class ' + node.name)
            doc = (ast.get_docstring(node) or '').split('\n\n')[0]
            add('related_symbols', {'path': path, 'line': node.lineno, 'content': signature + '\n' + doc}, 1500)
    target_module = path.removeprefix('src/').removesuffix('.py').replace('/', '.').removesuffix('.__init__')
    candidates = []
    for test_path, text in sorted(tests):
        if test_path not in request.repository.test_files:
            raise ValueError('unapproved visible context')
        tree = ast.parse(text)
        imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        prefix = '\n'.join(ast.get_source_segment(text, n) for n in imports)
        imports_target = any(target_module in (ast.get_source_segment(text, n) or '') for n in imports)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                calls = [n.func for n in ast.walk(node) if isinstance(n, ast.Call)]
                explicit = any((isinstance(f, ast.Name) and f.id == name.split('.')[-1]) or
                               (isinstance(f, ast.Attribute) and f.attr == name.split('.')[-1]) for f in calls)
                snippet, line = _segment(text, node, True)
                rank = (0 if test_path in generated_paths else 1, 0 if explicit else 1 if imports_target else 2, test_path, line)
                candidates.append((rank, {'path': test_path, 'line': line, 'content': prefix + '\n' + snippet}))
    for _, fragment in sorted(candidates, key=lambda item: item[0]):
        add('related_tests', fragment, 2500)
    for p in sorted(sources):
        audit['omitted'].append({'path': p, 'kind': 'remaining_module_body', 'reason': 'scope_only'})
    hint = _hint(coverage_hint)
    if hint is not None:
        payload['coverage_hint'] = hint
    result = messages(payload)
    audit.update(characters=sum(len(m['content']) for m in result), prompt_sha256=hashlib.sha256(encoded(result).encode()).hexdigest())
    if audit['characters'] > 16000:
        raise ModelError('budget', 'context_budget: serialized prompt exceeds cap')
    return result, audit


class ScopedGenerator:
    def __init__(self, client, target, tests, *, coverage_hint=None, generated_paths=()):
        self.client, self.target, self.tests = client, target, tests
        self.coverage_hint, self.generated_paths = coverage_hint, generated_paths
        self.last_completion = self.last_safe_candidate = self.context_audit = None

    def generate(self, request):
        try:
            messages, self.context_audit = build_messages(request, request.requirements, self.target, self.tests,
                GENERATION_SYSTEM + '\nThe target_definition is an excerpt from its original path/class; never treat it as a new standalone module. Related symbols are signatures, not implementations.',
                coverage_hint=self.coverage_hint, generated_paths=self.generated_paths)
            self.last_completion = self.client.complete(messages)
            candidate = strict_json(self.last_completion.content)
            safe = getattr(self.client, 'ensure_safe', None)
            if safe:
                safe(candidate)
            self.last_safe_candidate = candidate
            output = parse_generation(request, self.last_completion.content)
            if safe:
                safe(asdict(output))
            return output
        except ModelError as error:
            self.last_safe_candidate = self.last_completion = None
            kind = {'budget': FailureKind.BUDGET, 'environment': FailureKind.ENVIRONMENT}.get(error.kind, FailureKind.INVALID_OUTPUT)
            return GenerateOutput(request.task_id, request.repository.revision, failure=Failure(kind, str(error)))
        except (ValueError, TypeError, SyntaxError, UnicodeError, RecursionError):
            return GenerateOutput(request.task_id, request.repository.revision,
                                  failure=Failure(FailureKind.INVALID_OUTPUT, 'Invalid scoped generation context or output'))
