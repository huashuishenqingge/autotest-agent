"""Bounded static inspection only: never import or execute downloaded Python."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

from execution.proposals import checked_path
from models.adapter import strict_json
from workflow.state import relative_path

FIELDS = {'id', 'url', 'tag', 'commit', 'package_root', 'license_files', 'config_files'}


def _validate(project):
    if not isinstance(project, dict) or set(project) != FIELDS:
        raise ValueError('invalid project fields')
    if not isinstance(project['id'], str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', project['id']):
        raise ValueError('invalid project id')
    if not isinstance(project['url'], str) or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git', project['url']):
        raise ValueError('expected official GitHub HTTPS repository URL')
    if not isinstance(project['commit'], str) or not re.fullmatch('[0-9a-f]{40}', project['commit']):
        raise ValueError('expected full commit SHA')
    if not isinstance(project['tag'], str) or not re.fullmatch('[A-Za-z0-9_.-]+', project['tag']):
        raise ValueError('invalid project tag')
    relative_path(project['package_root'])
    for field in ('license_files', 'config_files'):
        values = project[field]
        if not isinstance(values, list) or not values or any(not isinstance(p, str) for p in values) or len(set(values)) != len(values):
            raise ValueError('explicit unique metadata files required')
        for path in values:
            relative_path(path)


def load_projects(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 100_000:
        raise ValueError('invalid catalog file')
    data = strict_json(path.read_text(encoding='utf-8-sig'))
    if (not isinstance(data, dict) or set(data) != {'schema', 'projects'}
            or type(data['schema']) is not int or data['schema'] != 1
            or not isinstance(data['projects'], list) or not 1 <= len(data['projects']) <= 20):
        raise ValueError('invalid project catalog')
    for project in data['projects']:
        _validate(project)
    if len({p['id'] for p in data['projects']}) != len(data['projects']):
        raise ValueError('duplicate project id')
    return data['projects']


def _bytes(root, path):
    file = checked_path(root, path)
    if not file.is_file() or file.stat().st_size > 3_000_000:
        raise ValueError('missing or excessive inspection file')
    raw = file.read_bytes()
    if len(raw) > 3_000_000:
        raise ValueError('inspection file grew beyond budget')
    return raw


def _metadata(root, paths):
    return {p: {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
            for p in paths for raw in [_bytes(root, p)]}


def inspect_project(root, project):
    _validate(project)
    root = Path(root)
    if root.is_symlink() or getattr(root.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('linked checkout rejected')
    root = root.resolve(strict=True)
    head = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True,
                          text=True, timeout=15, check=True).stdout.strip()
    if head != project['commit']:
        raise ValueError('checkout commit differs from catalog')
    status = subprocess.run(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=all'],
                            capture_output=True, text=True, timeout=15, check=True).stdout
    if status.strip():
        raise ValueError('modified checkout rejected')
    origin = subprocess.run(['git', '-C', str(root), 'config', '--get', 'remote.origin.url'],
                            capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    if origin != project['url']:
        raise ValueError('checkout origin differs from catalog')
    tagged = subprocess.run(['git', '-C', str(root), 'rev-parse', 'refs/tags/' + project['tag'] + '^{commit}'],
                            capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    if tagged != project['commit']:
        raise ValueError('checkout tag differs from catalog')
    package = checked_path(root, project['package_root'])
    if not package.is_dir():
        raise ValueError('package root missing')
    licenses = _metadata(root, project['license_files'])
    configs = _metadata(root, project['config_files'])
    modules, fixtures, resources, issues, package_resources = [], [], [], [], []
    test_characters = 0
    file_count = total_bytes = 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != '.git')
        for name in dirs:
            checked_path(root, (Path(folder) / name).relative_to(root).as_posix())
        for name in sorted(names):
            path = (Path(folder) / name).relative_to(root).as_posix()
            file = checked_path(root, path)
            file_count += 1
            total_bytes += file.stat().st_size
            if file_count > 20000 or total_bytes > 100_000_000:
                raise ValueError('checkout inspection budget exceeded')
            parts = Path(path).parts
            if name == 'conftest.py':
                fixtures.append(path)
            if any(p in ('test', 'tests') for p in parts[:-1]) and not path.endswith('.py'):
                resources.append(path)
            if any(p in ('test', 'tests') for p in parts[:-1]) and path.endswith('.py'):
                test_characters += len(_bytes(root, path).decode('utf-8-sig'))
            if file.is_relative_to(package) and not path.endswith('.py'):
                package_resources.append(path)
            if not file.is_relative_to(package) or not path.endswith('.py'):
                continue
            raw = _bytes(root, path)
            try:
                text = raw.decode('utf-8-sig')
                tree = ast.parse(text)
                functions = [{'name': node.name, 'line': node.lineno, 'end_line': node.end_lineno}
                             for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
                modules.append({'path': path, 'bytes': len(raw), 'characters': len(text),
                                'sha256': hashlib.sha256(raw).hexdigest(), 'functions': functions})
            except (UnicodeError, SyntaxError, ValueError, RecursionError):
                issues.append({'path': path, 'issue': 'source_not_parseable'})
    characters = sum(m['characters'] for m in modules)
    config = tomllib.loads(_bytes(root, 'pyproject.toml').decode('utf-8-sig')) if 'pyproject.toml' in configs else {}
    return {'project': project, 'commit_verified': True, 'clean_checkout_verified': True,
            'origin_and_tag_verified': True, 'licenses': licenses, 'configs': configs,
            'runtime_dependencies_declared': config.get('project', {}).get('dependencies', []),
            'dependency_discovery_note': 'pyproject direct declarations only; not a resolved or exhaustive test dependency lock',
            'module_count': len(modules), 'functions': sum(len(m['functions']) for m in modules),
            'package_characters': characters, 'prompt_limit_characters': 16000,
            'package_exceeds_prompt_alone': characters > 16000,
            'test_python_characters': test_characters,
            'repair_inputs_lower_bound_characters': characters + test_characters,
            'modules_over_prompt_alone': [m['path'] for m in modules if m['characters'] > 16000],
            'modules': modules, 'fixture_modules': fixtures, 'non_python_test_files': resources,
            'non_python_package_files': package_resources, 'issues': issues, 'execution_status': 'not_run'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=Path(__file__).with_name('projects.json'))
    parser.add_argument('--reference', type=Path, default=Path('reference/p08'))
    parser.add_argument('--export', type=Path, required=True)
    args = parser.parse_args()
    if args.reference.is_symlink() or getattr(args.reference.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('linked reference directory rejected')
    reference = args.reference.resolve(strict=True)
    report = {'schema': 1, 'kind': 'static preflight, not execution evidence',
              'projects': [inspect_project(checked_path(reference, p['id']), p) for p in load_projects(args.catalog)]}
    args.export.parent.mkdir(parents=True, exist_ok=True)
    args.export.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps([{'id': p['project']['id'], 'package_characters': p['package_characters'],
                       'large_modules': len(p['modules_over_prompt_alone']), 'execution_status': p['execution_status']}
                      for p in report['projects']]))


if __name__ == '__main__':
    main()
