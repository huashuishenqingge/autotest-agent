"""Frozen upstream pytest admission under the controlled harness, no model calls."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

from execution.docker_runner import run_tests
from execution.proposals import checked_path
from experiments.preflight import _bytes, inspect_project, load_projects
from experiments.tasks import _read
from workflow.state import relative_path
from repair.fixtures import PROJECT_ROOT
from workflow.state import Repository


def inventory_manifest(inventory):
    """Exact approved bytes, shared by the formal loader and upstream runner."""
    paths, total = set(), 0
    manifest = {}
    denied = {'.py', '.pyw', '.pyc', '.pyo', '.exe', '.dll', '.so', '.pyd', '.dylib',
              '.sh', '.bash', '.ps1', '.bat', '.cmd', '.com', '.js', '.mjs', '.cjs',
              '.jar', '.class', '.wasm', '.pth', '.ipynb'}
    for kind in ('sources', 'tests', 'support'):
        manifest[kind] = {}
        for path, data in inventory[kind].items():
            relative_path(path)
            if path.casefold() in paths or not isinstance(data, bytes) or len(data) > 1_000_000:
                raise ValueError('inventory conflict or file budget exceeded')
            paths.add(path.casefold())
            total += len(data)
            if kind != 'support' and not path.endswith('.py'):
                raise ValueError('Python inventory requires Python paths')
            if kind == 'support' and (Path(path).suffix.lower() in denied or data.startswith(
                    (b'MZ', b'\x7fELF', b'#!', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf'))):
                raise ValueError('executable cannot be support data')
            manifest[kind][path] = hashlib.sha256(data).hexdigest()
    if len(paths) > 2000 or total > 10_000_000:
        raise ValueError('execution inventory budget exceeded')
    return dict(manifest, excluded=inventory['excluded'], provenance=inventory['provenance'])


def build_reviewed_inventory(project, *, reference_root, resource_root):
    reference_root = Path(reference_root)
    if reference_root.is_symlink() or getattr(reference_root.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('linked reference root rejected')
    reference_root = reference_root.resolve(strict=True)
    root = checked_path(reference_root, project['id'])
    metadata = inspect_project(root, project)
    source, dependencies, dependency_licenses = {}, [], {}
    def add_modules(checkout, specification, inspection):
        if inspection['issues']:
            raise ValueError('source inspection issues')
        for module in inspection['modules']:
            path = module['path']
            if not path.startswith(specification['package_root'] + '/'):
                raise ValueError('source outside package')
            data = _bytes(checkout, path)
            if hashlib.sha256(data).hexdigest() != module['sha256'] or path in source:
                raise ValueError('source changed after inspection or overlaps dependency')
            source[path] = data
    add_modules(root, project, metadata)
    coverage_sources = tuple(source)
    if project['id'] == 'jinja':
        dep = next(p for p in load_projects(PROJECT_ROOT / 'experiments/projects.json') if p['id'] == 'markupsafe')
        dep_root = checked_path(reference_root, dep['id'])
        inspected = inspect_project(dep_root, dep)
        add_modules(dep_root, dep, inspected)
        dependencies.append(dep)
        dependency_licenses[dep['id']] = inspected['licenses']
    tests, support, excluded = {}, {}, []
    resources = _read(PROJECT_ROOT / 'experiments/upstream-resources.json')['projects'][project['id']]
    for file in sorted((root / 'tests').rglob('*')):
        if not file.is_file():
            continue
        path = file.relative_to(root).as_posix()
        checked_path(root, path)
        if project['id'] == 'jinja' and path in ('tests/test_loader.py', 'tests/res/package.zip'):
            excluded.append({'path': path, 'reason': 'ZIP fixture contains executable Python; not an approved ordinary support resource',
                             'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
            continue
        if path.endswith('.py'):
            tests[path] = _bytes(root, path)
        else:
            if path not in resources:
                raise ValueError('non-Python test data not explicitly approved')
    for path, spec in resources.items():
        if spec['source'] == 'fixed checkout':
            data = _bytes(root, path)
        elif spec['source'] == 'approved wheel data':
            resource_root = Path(resource_root)
            if resource_root.is_symlink() or getattr(resource_root.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('linked resource root rejected')
            wheel = checked_path(resource_root.resolve(strict=True), spec['wheel_filename'])
            if hashlib.sha256(wheel.read_bytes()).hexdigest() != spec['wheel_sha256']:
                raise ValueError('wheel bytes changed')
            with zipfile.ZipFile(wheel) as archive:
                members = [m for m in archive.infolist() if m.filename == spec['member']]
                if len(members) != 1 or members[0].file_size > 1_000_000:
                    raise ValueError('invalid or excessive wheel resource')
                data = archive.read(members[0])
        else:
            raise ValueError('unknown resource origin')
        if hashlib.sha256(data).hexdigest() != spec['sha256']:
            raise ValueError('reviewed support bytes changed')
        if path.endswith('.py'):
            tests[path] = data  # Explicit Python fixture; never hide code in support.
        else:
            support[path] = data
    package = project['package_root'].split('/')[-1]
    if 'tests/test_p08_working_copy_origin.py' in tests:
        raise ValueError('origin test collides with upstream file')
    tests['tests/test_p08_working_copy_origin.py'] = f'''from pathlib import Path
import {package}
def test_p08_working_copy_origin():
    assert Path({package}.__file__).resolve().is_relative_to(Path('/workspace/{project['package_root']}'))
'''.encode()
    if project['id'] in ('jinja', 'markupsafe'):
        tests['tests/test_p08_working_copy_origin.py'] += (b"    import markupsafe\n" if project['id'] == 'jinja' else b'')
        tests['tests/test_p08_working_copy_origin.py'] += b"    assert Path(markupsafe.__file__).resolve().is_relative_to(Path('/workspace/src/markupsafe'))\n    assert markupsafe._escape_inner.__module__ == 'markupsafe._native'\n"
    inventory = {'sources': source, 'tests': tests, 'support': support,
                 'coverage_sources': coverage_sources, 'excluded': excluded,
                 'provenance': {'project': project, 'dependencies': dependencies,
                                'licenses': metadata['licenses'], 'dependency_licenses': dependency_licenses}}
    inventory_manifest(inventory)
    return inventory


def run_upstream(project, *, image_id, artifact_root):
    inventory = build_reviewed_inventory(project, reference_root=PROJECT_ROOT / 'reference/p08',
                                        resource_root=PROJECT_ROOT / 'artifacts/p08a/image/wheels')
    source, tests, support = (inventory[k] for k in ('sources', 'tests', 'support'))
    excluded = inventory['excluded']
    with tempfile.TemporaryDirectory(prefix='autotest-p08-upstream-') as folder:
        working = Path(folder).resolve()
        for path, data in {**source, **tests, **support}.items():
            file = checked_path(working, path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
        repo = Repository(project['id'], project['commit'], tuple(source), tuple(tests))
        result = run_tests(working, repo, image_id=image_id, artifact_root=artifact_root, support_files=tuple(support))
    return {'project': project, 'scope': 'upstream pytest under fixed controlled harness; not tox/type-check/doc build',
            'excluded': excluded, 'source_files': list(source), 'test_files': list(tests),
            'support_hashes': {p: hashlib.sha256(b).hexdigest() for p,b in support.items()},
            'execution': asdict(result)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image-id', required=True)
    p.add_argument('--project', action='append')
    p.add_argument('--artifacts', type=Path, default=PROJECT_ROOT / 'artifacts/p08a/upstream')
    p.add_argument('--export', type=Path, required=True)
    args = p.parse_args()
    if args.export.exists():
        raise FileExistsError('preserve previous admission results; choose a new export path')
    projects = load_projects(PROJECT_ROOT / 'experiments/projects.json')
    if args.project and not set(args.project).issubset({p['id'] for p in projects}):
        raise ValueError('unknown project')
    report = {'schema': 1, 'kind': 'new real upstream admission, no model calls', 'image_id': args.image_id, 'projects': []}
    for project in projects:
        if args.project and project['id'] not in args.project:
            continue
        row = run_upstream(project, image_id=args.image_id, artifact_root=args.artifacts)
        report['projects'].append(row)
        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(json.dumps(report, indent=2), encoding='utf-8')
        run = row['execution']['run']
        print(project['id'] + ': ' + json.dumps({k: run[k] for k in ('exit_code','passed','failed','errors','skipped')}), flush=True)
    return 0 if all(p['execution']['run']['exit_code'] == 0 and p['execution']['cleanup_ok'] for p in report['projects']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
