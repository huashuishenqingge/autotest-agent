"""Summarize coverage.py JSON for exactly the approved source files."""
from workflow.state import relative_path


def summarize(report, source_files):
    if not source_files or len(set(source_files)) != len(source_files):
        raise ValueError('nonempty unique source inventory required')
    try:
        files = {}
        for name, value in report['files'].items():
            normalized = name.removeprefix('/workspace/')
            if normalized in source_files:
                if normalized in files:
                    raise ValueError('ambiguous source coverage')
                files[normalized] = value
        result = {'lines': {'covered': 0, 'total': 0}, 'branches': {'covered': 0, 'total': 0}, 'files': {}}
        for path in source_files:
            relative_path(path)
            item = files[path]
            executed, missing = item['executed_lines'], item['missing_lines']
            if any(type(n) is not int or n <= 0 for n in executed + missing):
                raise ValueError('invalid source line')
            executed, missing = set(executed), set(missing)
            if executed & missing:
                raise ValueError('conflicting line evidence')
            branch_sets = []
            for field in ('executed_branches', 'missing_branches'):
                edges = item.get(field, [])
                if any(len(edge) != 2 or any(type(n) is not int for n in edge) for edge in edges):
                    raise ValueError('invalid branch edge')
                branch_sets.append({tuple(edge) for edge in edges})
            covered_branches, missing_branches = branch_sets
            if covered_branches & missing_branches:
                raise ValueError('conflicting branch evidence')
            result['lines']['covered'] += len(executed)
            result['lines']['total'] += len(executed | missing)
            result['branches']['covered'] += len(covered_branches)
            result['branches']['total'] += len(covered_branches | missing_branches)
            result['files'][path] = {'missing_lines': sorted(missing),
                                      'missing_branches': sorted(missing_branches)}
        return result
    except (KeyError, TypeError, AttributeError):
        raise ValueError('coverage evidence does not match approved source inventory') from None
