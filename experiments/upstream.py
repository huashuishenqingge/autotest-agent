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
from experiments.preflight import inspect_project, load_projects
from repair.fixtures import PROJECT_ROOT
from workflow.state import Repository


def run_upstream(project, *, image_id, artifact_root):
    root = PROJECT_ROOT / 'reference/p08' / project['id']
    metadata = inspect_project(root, project)
    source = {m['path']: checked_path(root, m['path']).read_bytes() for m in metadata['modules']}
    if project['id'] == 'jinja':
        dep = next(p for p in load_projects(PROJECT_ROOT / 'experiments/projects.json') if p['id'] == 'markupsafe')
        dep_root = PROJECT_ROOT / 'reference/p08/markupsafe'
        for module in inspect_project(dep_root, dep)['modules']:
            source[module['path']] = checked_path(dep_root, module['path']).read_bytes()
    tests, support, excluded = {}, {}, []
    resources = json.loads((PROJECT_ROOT / 'experiments/upstream-resources.json').read_text())['projects'][project['id']]
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
            tests[path] = file.read_bytes()
        else:
            if path not in resources:
                raise ValueError('non-Python test data not explicitly approved')
    for path, spec in resources.items():
        if spec['source'] == 'fixed checkout':
            data = checked_path(root, path).read_bytes()
        elif spec['source'] == 'approved wheel data':
            wheel = checked_path(PROJECT_ROOT / 'artifacts/p08a/image/wheels', spec['wheel_filename'])
            if hashlib.sha256(wheel.read_bytes()).hexdigest() != spec['wheel_sha256']:
                raise ValueError('wheel bytes changed')
            with zipfile.ZipFile(wheel) as archive:
                data = archive.read(spec['member'])
        else:
            raise ValueError('unknown resource origin')
        if hashlib.sha256(data).hexdigest() != spec['sha256']:
            raise ValueError('reviewed support bytes changed')
        if path.endswith('.py'):
            tests[path] = data  # Explicit Python fixture; never hide code in support.
        else:
            support[path] = data
    package = project['package_root'].split('/')[-1]
    tests['tests/test_p08_working_copy_origin.py'] = f'''from pathlib import Path
import {package}
def test_p08_working_copy_origin():
    assert Path({package}.__file__).resolve().is_relative_to(Path('/workspace/{project['package_root']}'))
'''.encode()
    if project['id'] in ('jinja', 'markupsafe'):
        tests['tests/test_p08_working_copy_origin.py'] += (b"    import markupsafe\n" if project['id'] == 'jinja' else b'')
        tests['tests/test_p08_working_copy_origin.py'] += b"    assert Path(markupsafe.__file__).resolve().is_relative_to(Path('/workspace/src/markupsafe'))\n    assert markupsafe._escape_inner.__module__ == 'markupsafe._native'\n"
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
