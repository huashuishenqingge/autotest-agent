"""Reviewed real-project admission manifests; evaluator inputs stay separate."""
import hashlib
from pathlib import Path
import re

from experiments.functions import locate_function, replace_function
from experiments.preflight import _bytes, inspect_project, load_projects
from experiments.tasks import _files, _read
from workflow.state import relative_path


def _fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise ValueError('invalid admission fields')


def load_project_admission(visible_path, private_path, catalog_path, reference_root):
    visible, private = _read(visible_path), _read(private_path)
    _fields(visible, 'schema scope id project_id dependency_project_ids target defect_type requirements original_tests baseline_origin support')
    _fields(private, 'schema id mutation description holdout_tests')
    if (type(visible['schema']) is not int or visible['schema'] != 1
            or type(private['schema']) is not int or private['schema'] != 1
            or visible['scope'] != 'P08A-admission-only'
            or not isinstance(visible['id'], str)
            or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', visible['id'])
            or private['id'] != visible['id']):
        raise ValueError('invalid admission identity or scope')
    if visible['defect_type'] not in ('boundary', 'null_type', 'exception_handling'):
        raise ValueError('invalid defect class')
    requirements = visible['requirements']
    if (not isinstance(requirements, list) or not requirements
            or any(not isinstance(r, str) or not r.strip() for r in requirements)):
        raise ValueError('independent requirements required')
    for text in (visible['baseline_origin'], private['description']):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('explicit provenance required')
    catalog = {p['id']: p for p in load_projects(catalog_path)}
    primary = visible['project_id']
    dependencies = visible['dependency_project_ids']
    if (not isinstance(primary, str) or primary not in catalog
            or not isinstance(dependencies, list)
            or any(not isinstance(p, str) or p not in catalog or p == primary for p in dependencies)
            or len(set(dependencies)) != len(dependencies)):
        raise ValueError('unknown or duplicate project')
    source, metadata, primary_paths = {}, {}, ()
    for project_id in [primary] + dependencies:
        project = catalog[project_id]
        root = Path(reference_root) / project_id
        inspected = inspect_project(root, project)
        if inspected['issues']:
            raise ValueError('project inspection issues')
        metadata[project_id] = inspected
        paths = []
        for module in inspected['modules']:
            path = relative_path(module['path'])
            if not path.startswith(project['package_root'] + '/') or not path.endswith('.py'):
                raise ValueError('module outside approved package')
            raw = _bytes(root, path)
            if hashlib.sha256(raw).hexdigest() != module['sha256']:
                raise ValueError('source changed after inspection')
            if path.casefold() in {p.casefold() for p in source}:
                raise ValueError('project source overlap')
            source[path] = raw.decode('utf-8')
            paths.append(path)
        if project_id == primary:
            primary_paths = tuple(paths)
    target = visible['target']
    _fields(target, 'path qualified_name')
    path = relative_path(target['path'])
    if path not in primary_paths or not isinstance(target['qualified_name'], str):
        raise ValueError('target must belong to primary project')
    code = locate_function(source[path], target['qualified_name'])['code']
    mutation = private['mutation']
    _fields(mutation, 'old new')
    if (any(not isinstance(mutation[k], str) or not mutation[k] for k in ('old', 'new'))
            or mutation['old'] == mutation['new'] or code.count(mutation['old']) != 1):
        raise ValueError('mutation must match exactly once inside target')
    mutant = dict(source)
    mutant[path] = replace_function(source[path], target['qualified_name'], code.replace(mutation['old'], mutation['new']))
    _files(visible['original_tests'])
    _files(private['holdout_tests'])
    if not isinstance(visible['support'], dict) or len(visible['support']) > 2000:
        raise ValueError('bounded support map required')
    support = {}
    for name, spec in visible['support'].items():
        relative_path(name)
        _fields(spec, 'encoding content sha256')
        if spec['encoding'] != 'utf8' or not isinstance(spec['content'], str):
            raise ValueError('explicit UTF-8 admission data required')
        raw = spec['content'].encode('utf-8')
        if (len(raw) > 1_000_000 or not isinstance(spec['sha256'], str)
                or hashlib.sha256(raw).hexdigest() != spec['sha256']):
            raise ValueError('support hash or budget mismatch')
        support[name] = raw
    all_paths = list(source) + list(visible['original_tests']) + list(private['holdout_tests']) + list(support)
    if len({p.casefold() for p in all_paths}) != len(all_paths):
        raise ValueError('source/test/support inventories overlap')
    if len(all_paths) > 2000 or sum(len(t.encode()) for t in source.values()) + sum(map(len, support.values())) > 10_000_000:
        raise ValueError('execution inventory budget exceeded')
    case = {'id': visible['id'], 'sources': mutant, 'original_tests': visible['original_tests'],
            'requirements': requirements, 'target': (path, target['qualified_name']),
            'support': support, 'coverage_sources': primary_paths}
    validation = {'reference_sources': source, 'holdout_tests': private['holdout_tests']}
    provenance = {'project': catalog[primary], 'scope': 'P08A admission; not a formal surviving-upstream mutant',
                  'baseline_origin': visible['baseline_origin'], 'excluded_from_P08B': True,
                  'mutation': private['description'], 'licenses': metadata[primary]['licenses'],
                  'source_hashes': {p: hashlib.sha256(t.encode()).hexdigest() for p, t in source.items()},
                  'support_hashes': {p: hashlib.sha256(b).hexdigest() for p, b in support.items()}}
    if dependencies:
        provenance['dependency_projects'] = [catalog[p] for p in dependencies]
    return case, validation, provenance
