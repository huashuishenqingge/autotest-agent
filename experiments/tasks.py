"""Strict controlled-pilot inputs, not the P08B formal experiment schema."""
import ast
import hashlib
from pathlib import Path
import re
import json

from models.adapter import strict_json
from workflow.state import relative_path


def _read(path):
    path = Path(path)
    if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400 or path.stat().st_size > 2_000_000:
        raise ValueError('invalid pilot manifest file')
    return strict_json(path.read_text(encoding='utf-8-sig'))


def _files(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 20:
        raise ValueError('nonempty bounded file map required')
    if len({p.casefold() for p in value}) != len(value):
        raise ValueError('case-insensitive pilot path conflict')
    for path, text in value.items():
        relative_path(path)
        if not path.endswith('.py') or not isinstance(text, str) or len(text.encode('utf-8')) > 100000:
            raise ValueError('bounded Python source required')
        try:
            ast.parse(text)
        except (ValueError, SyntaxError, RecursionError):
            raise ValueError('pilot must contain parseable Python') from None


def load_pilots(visible_path, validation_path):
    visible, private = _read(visible_path), _read(validation_path)
    if (not isinstance(visible, dict) or set(visible) != {'schema', 'scope', 'cases'}
            or type(visible['schema']) is not int or visible['schema'] != 1
            or visible['scope'] != 'controlled-pilot-only'
            or not isinstance(visible['cases'], list) or not 1 <= len(visible['cases']) <= 20):
        raise ValueError('invalid visible pilot schema')
    if (not isinstance(private, dict) or set(private) != {'schema', 'cases'}
            or type(private['schema']) is not int or private['schema'] != 1 or not isinstance(private['cases'], dict)):
        raise ValueError('invalid validation pilot schema')
    ids = set()
    for case in visible['cases']:
        if not isinstance(case, dict) or set(case) != {'id', 'defect_type', 'requirements', 'sources', 'original_tests'}:
            raise ValueError('invalid pilot fields')
        if not isinstance(case['id'], str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', case['id']) or case['id'] in ids:
            raise ValueError('invalid or duplicate pilot id')
        ids.add(case['id'])
        if case['defect_type'] not in ('boundary', 'null_type', 'exception_handling'):
            raise ValueError('invalid defect class')
        requirements = case['requirements']
        if not isinstance(requirements, list) or not requirements or any(not isinstance(r, str) or not r.strip() for r in requirements):
            raise ValueError('independent requirements required')
        _files(case['sources'])
        _files(case['original_tests'])
        validation = private['cases'].get(case['id'])
        if not isinstance(validation, dict) or set(validation) != {'reference_sources', 'holdout_tests'}:
            raise ValueError('matching evaluator-only validation required')
        _files(validation['reference_sources'])
        _files(validation['holdout_tests'])
        if set(validation['reference_sources']) != set(case['sources']):
            raise ValueError('reference source inventory mismatch')
        all_paths = list(case['sources']) + list(case['original_tests']) + list(validation['holdout_tests'])
        if len({p.casefold() for p in all_paths}) != len(all_paths):
            raise ValueError('source/visible/private inventories overlap')
    if ids != set(private['cases']):
        raise ValueError('visible and private case identities differ')
    manifest = {label: hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True,
                separators=(',', ':')).encode('utf-8')).hexdigest()
                for label, data in (('visible', visible), ('validation', private))}
    return visible['cases'], private['cases'], manifest
