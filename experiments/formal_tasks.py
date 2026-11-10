"""Strict frozen-project inputs. Loading proves consistency, not eligibility."""
import ast
import difflib
import hashlib
import json
from pathlib import Path
import re

from experiments.functions import locate_function, replace_function
from experiments.preflight import _bytes, load_projects
from experiments.project_tasks import _fields
from experiments.tasks import _read
from experiments.upstream import build_reviewed_inventory, inventory_manifest
from repair.fixtures import PROJECT_ROOT
from workflow.state import relative_path

SCHEMA = 'p08-formal-v1'
IMAGE = 'sha256:7227650ec06570ea9a532bfb669d0888a4a92f7f179d2d442878f353b27ecd66'
STATUSES = {'pending', 'eligible', 'detected_by_original', 'invalid_mutation', 'reference_failed',
            'holdout_not_detected', 'unstable', 'context_budget', 'environment_failed', 'excluded_pilot'}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('nonempty text required')


def _protocol(value):
    expected = {'image_id': IMAGE,
                'dependency_lock_sha256': _sha((PROJECT_ROOT/'execution/requirements-p08.lock').read_bytes()),
                'worker_sha256': _sha((PROJECT_ROOT/'execution/container_worker.py').read_bytes().replace(b'\r\n', b'\n')),
                'context_version': 'p08-context-v1', 'repair_version': 'p08-scoped-repair-v1',
                'model': 'deepseek-v4-flash-ascend', 'temperature': 0.2, 'thinking_mode': 'disabled',
                'max_tokens': 2048, 'input_characters': 16000, 'timeout_seconds': 60}
    if (not isinstance(value, dict) or value != expected
            or any(type(value[k]) is not type(v) for k, v in expected.items())):
        raise ValueError('formal protocol differs from approved parameters')


def _requirement_sources(root, values):
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        raise ValueError('documented requirement sources required')
    for source in values:
        _fields(source, 'path start_line end_line sha256 excerpt')
        path = relative_path(source['path'])
        if Path(path).suffix.lower() not in ('.md', '.rst', '.txt'):
            raise ValueError('requirement source must be reviewed documentation')
        raw = _bytes(root, path)
        lines = raw.decode('utf-8-sig').splitlines()
        start, end = source['start_line'], source['end_line']
        if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines)
                or _sha(raw) != source['sha256'] or '\n'.join(lines[start-1:end]) != source['excerpt']):
            raise ValueError('documentation location, excerpt or hash changed')
        _text(source['excerpt'])


def _load(public_path, evaluator_path, *, catalog_path, reference_root, resource_root):
    reference_root = Path(reference_root)
    if reference_root.is_symlink() or getattr(reference_root.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('linked reference root rejected')
    reference_root = reference_root.resolve(strict=True)
    public, private = _read(public_path), _read(evaluator_path)
    for value in (public, private):
        _fields(value, 'schema tasks')
        if value['schema'] != SCHEMA:
            raise ValueError('unknown formal schema')
    if (not isinstance(public['tasks'], list) or not 1 <= len(public['tasks']) <= 500
            or not isinstance(private['tasks'], dict)):
        raise ValueError('bounded formal task list required')
    catalog = {p['id']: p for p in load_projects(catalog_path)}
    rows, ids, inventories = [], set(), {}
    for task in public['tasks']:
        _fields(task, 'id project_id origin target requirements requirement_sources coverage_sources inventory protocol owner reviewer')
        identity = task['id']
        if not isinstance(identity, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', identity) or identity in ids:
            raise ValueError('invalid or duplicate task identity')
        ids.add(identity)
        if task['origin'] != 'injected' or task['project_id'] not in catalog:
            raise ValueError('unknown project or unsupported defect origin')
        for name in ('owner', 'reviewer'):
            _text(task[name])
        if task['owner'].strip().casefold() == task['reviewer'].strip().casefold():
            raise ValueError('independent reviewer required')
        _protocol(task['protocol'])
        project = catalog[task['project_id']]
        if project['id'] not in inventories:
            inventories[project['id']] = build_reviewed_inventory(project, reference_root=reference_root,
                                                                 resource_root=resource_root)
        inventory = inventories[project['id']]
        manifest = inventory_manifest(inventory)
        if task['inventory'] != manifest:
            raise ValueError('complete reviewed inventory differs')
        if task['coverage_sources'] != list(inventory['coverage_sources']):
            raise ValueError('coverage must include exactly the primary source inventory')
        for path in list(inventory['tests']) + list(inventory['sources']) + list(inventory['support']):
            if path.casefold().startswith(('tests/generated/', 'tests/p08_holdout/')):
                raise ValueError('reserved experiment namespace already in upstream inventory')
        target = task['target']
        _fields(target, 'path qualified_name line end_line')
        path = relative_path(target['path'])
        if path not in inventory['coverage_sources']:
            raise ValueError('target must be in primary project')
        if project['id'] == 'markupsafe' and target['qualified_name'] == 'escape_silent':
            raise ValueError('P08A pilot target cannot enter formal tasks')
        sources = {p: b.decode('utf-8') for p, b in inventory['sources'].items()}
        scope = locate_function(sources[path], target['qualified_name'])
        if (type(target['line']) is not int or type(target['end_line']) is not int
                or (target['line'], target['end_line']) != (scope['line'], scope['end_line'])):
            raise ValueError('original target location mismatch')
        requirements = task['requirements']
        if not isinstance(requirements, list) or not requirements:
            raise ValueError('independent requirements required')
        for text in requirements:
            _text(text)
        mapping = {'path': path, 'qualified_name': target['qualified_name'], 'line': scope['line'],
                   'end_line': scope['end_line'], 'classes': scope['classes'], 'decorators': scope['decorators']}
        if (len(json.dumps(requirements, ensure_ascii=False)) > 2000
                or len(json.dumps(scope['code'], ensure_ascii=False)) > 6000
                or len(json.dumps(mapping, ensure_ascii=False)) > 1500
                or len(inventory['sources'][path]) > 100000):
            raise ValueError('context_budget: target or requirements exceed approved limits')
        _requirement_sources(Path(reference_root)/project['id'], task['requirement_sources'])
        evaluator = private['tasks'].get(identity)
        _fields(evaluator, 'defect_type reference_source_hashes mutant_source_hashes mutation diff holdout_tests expected_failure screening')
        if evaluator['defect_type'] not in ('boundary', 'null_type', 'exception_handling'):
            raise ValueError('unknown defect category')
        if evaluator['reference_source_hashes'] != manifest['sources']:
            raise ValueError('correct-source inventory changed')
        mutation = evaluator['mutation']
        _fields(mutation, 'old new line')
        for key in ('old', 'new'):
            _text(mutation[key])
        if mutation['old'] == mutation['new'] or scope['code'].count(mutation['old']) != 1:
            raise ValueError('mutation must change exactly one target occurrence')
        offset = scope['code'].index(mutation['old'])
        if type(mutation['line']) is not int or mutation['line'] != scope['line'] + scope['code'][:offset].count('\n'):
            raise ValueError('mutation location mismatch')
        mutant = dict(sources)
        mutant[path] = replace_function(sources[path], target['qualified_name'],
                                       scope['code'].replace(mutation['old'], mutation['new']))
        if evaluator['mutant_source_hashes'] != {p: _sha(t.encode()) for p, t in mutant.items()}:
            raise ValueError('mutant-source inventory changed')
        diff = ''.join(difflib.unified_diff(sources[path].splitlines(keepends=True), mutant[path].splitlines(keepends=True),
                                          fromfile='a/'+path, tofile='b/'+path))
        if evaluator['diff'] != diff or not diff:
            raise ValueError('mutation diff mismatch')
        # Replacement may enlarge the target; the actual mutant must also fit.
        if (len(mutant[path].encode()) > 100000
                or len(json.dumps(locate_function(mutant[path], target['qualified_name'])['code'], ensure_ascii=False)) > 6000):
            raise ValueError('context_budget: mutant exceeds approved limits')
        _text(evaluator['expected_failure'])
        screening = evaluator['screening']
        _fields(screening, 'status evidence')
        if screening['status'] not in STATUSES or not isinstance(screening['evidence'], list):
            raise ValueError('invalid screening declaration')
        for evidence in screening['evidence']:
            relative_path(evidence)
        holdout = evaluator['holdout_tests']
        expected_path = 'tests/p08_holdout/test_' + identity + '.py'
        if not isinstance(holdout, dict) or set(holdout) != {expected_path}:
            raise ValueError('dedicated evaluator-only test required')
        spec = holdout[expected_path]
        _fields(spec, 'content sha256')
        _text(spec['content'])
        data = spec['content'].encode('utf-8')
        if len(data) > 100000 or _sha(data) != spec['sha256']:
            raise ValueError('holdout hash or size mismatch')
        ast.parse(spec['content'])
        combined = dict(inventory, sources={p: t.encode() for p, t in mutant.items()},
                        tests=dict(inventory['tests'], **{expected_path: data}))
        inventory_manifest(combined)
        case = {'id': identity, 'sources': mutant,
                'original_tests': {p: b.decode('utf-8') for p, b in inventory['tests'].items()},
                'requirements': list(requirements), 'target': (path, target['qualified_name']),
                'support': dict(inventory['support']), 'coverage_sources': tuple(inventory['coverage_sources'])}
        rows.append({'case': case, 'validation': {'reference_sources': sources, 'holdout_tests': {expected_path: spec['content']}},
                     'provenance': {'task': task, 'source_manifest': manifest}, 'evaluation': evaluator})
    if ids != set(private['tasks']):
        raise ValueError('public and evaluator task identities differ')
    return tuple(rows)


def load_formal_tasks(public_path, evaluator_path, *, catalog_path, reference_root, resource_root):
    try:
        return _load(public_path, evaluator_path, catalog_path=catalog_path,
                     reference_root=reference_root, resource_root=resource_root)
    except (KeyError, TypeError, AttributeError, UnicodeError, SyntaxError, RecursionError):
        raise ValueError('invalid formal task data') from None
