"""Deterministic, bounded Python source inventory and approximate AST index."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from workflow.state import Repository, relative_path


@dataclass(frozen=True)
class ParseIssue:
    path: str
    code: str


@dataclass(frozen=True)
class Symbol:
    name: str
    path: str
    kind: str
    start_line: int
    end_line: int
    signature: str


@dataclass(frozen=True)
class SourceFile:
    path: str
    module: str
    kind: str
    text: str
    sha256: str
    imports: tuple[str, ...]
    calls: tuple[str, ...]  # Syntax only: dynamic dispatch is NOT resolved.


@dataclass(frozen=True)
class ParsedRepository:
    repository: Repository
    files: tuple[SourceFile, ...]
    symbols: tuple[Symbol, ...]
    issues: tuple[ParseIssue, ...]


EXCLUDED_DIRS = frozenset({
    'tests_disabled', 'node_modules', 'site-packages', 'vendor', 'third_party',
    'build', 'dist', '__pycache__', 'htmlcov', 'venv', 'env', 'artifacts',
    'runs', 'reference', 'generated',
})
CONFIG_FILES = frozenset({'setup.py', 'conftest.py', 'noxfile.py', 'fabfile.py', 'manage.py'})


def _linked(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _module(path: str) -> str:
    parts = list(Path(path).with_suffix('').parts)
    if parts[0] == 'src':
        parts.pop(0)
    if parts and parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


class _Index(ast.NodeVisitor):
    def __init__(self, path: str, module: str):
        self.path, self.module = path, module
        self.scope: list[str] = []
        self.symbols: list[Symbol] = []

    def _definition(self, node, kind):
        name = '.'.join(filter(None, (self.module, *self.scope, node.name)))
        signature = ast.unparse(node.args) if hasattr(node, 'args') else ''
        self.symbols.append(Symbol(name, self.path, kind, node.lineno, node.end_lineno, signature))
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node):
        self._definition(node, 'class')

    def visit_FunctionDef(self, node):
        self._definition(node, 'function')

    def visit_AsyncFunctionDef(self, node):
        self._definition(node, 'async_function')


def parse_repository(root: Path, *, project_id: str, max_file_bytes: int = 100000,
                     max_total_bytes: int = 10000000, max_files: int = 2000) -> ParsedRepository:
    """Snapshot eligible Python files; rejected inputs are explicit issues.

    revision hashes all eligible file paths and bytes, including files rejected
    by encoding/syntax/size checks. It is an analysis snapshot, NOT an execution
    workspace digest or a Git commit. Call only on a stable, controlled copy;
    this read-side analysis does not replace executor write-time checks.
    """
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or min(max_file_bytes, max_total_bytes, max_files) <= 0:
        raise ValueError('directory and positive budgets required')
    issues: list[ParseIssue] = []
    files: list[SourceFile] = []
    symbols: list[Symbol] = []
    digest = hashlib.sha256()
    seen: set[str] = set()
    total = count = 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        safe_dirs = []
        for name in sorted(dirs):
            child = Path(folder) / name
            path = child.relative_to(root).as_posix()
            if _linked(child):
                issues.append(ParseIssue(path, 'link_rejected'))
            elif not name.startswith('.') and name.casefold() not in EXCLUDED_DIRS:
                safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in sorted(names):
            if not name.endswith('.py') or name.startswith('.') or name.casefold() in CONFIG_FILES:
                continue
            target = Path(folder) / name
            path = target.relative_to(root).as_posix()
            try:
                relative_path(path)
            except ValueError:
                issues.append(ParseIssue(path, 'invalid_path'))
                continue
            if path.casefold() in seen:
                raise ValueError(f'case-insensitive path collision: {path}')
            seen.add(path.casefold())
            if _linked(target) or not stat.S_ISREG(target.lstat().st_mode):
                issues.append(ParseIssue(path, 'link_rejected'))
                continue
            if not target.resolve(strict=True).is_relative_to(root):
                raise ValueError('path escapes repository')
            count += 1
            size = target.stat().st_size
            total += size
            if count > max_files or total > max_total_bytes:
                raise ValueError('repository budget exceeded')
            # Streaming hash bounds memory even for an oversized file.
            file_hash = hashlib.sha256()
            chunks = []
            with target.open('rb') as stream:
                read_size = 0
                while chunk := stream.read(65536):
                    read_size += len(chunk)
                    if read_size > max_total_bytes:
                        raise ValueError('repository budget exceeded')
                    file_hash.update(chunk)
                    if read_size <= max_file_bytes:
                        chunks.append(chunk)
            raw_hash = file_hash.hexdigest()
            digest.update(path.encode('utf-8') + b'\0' + raw_hash.encode('ascii') + b'\0')
            if read_size > max_file_bytes:
                issues.append(ParseIssue(path, 'file_too_large'))
                continue
            try:
                text = b''.join(chunks).decode('utf-8-sig')
            except UnicodeDecodeError:
                issues.append(ParseIssue(path, 'encoding_error'))
                continue
            try:
                tree = ast.parse(text, filename=path)
            except (SyntaxError, ValueError, RecursionError):
                issues.append(ParseIssue(path, 'syntax_error'))
                continue
            kind = 'test' if any(p.casefold() in {'test', 'tests'} for p in Path(path).parts[:-1]) or name.startswith('test_') or name.endswith('_test.py') else 'source'
            module = _module(path)
            imports, calls = set(), set()
            package = module.split('.') if name == '__init__.py' else module.split('.')[:-1]
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(item.name for item in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        prefix = package[:len(package) - node.level + 1]
                        base = '.'.join(prefix + ([node.module] if node.module else []))
                    else:
                        base = node.module or ''
                    imports.add(base)
                    imports.update('.'.join(filter(None, (base, item.name))) for item in node.names if item.name != '*')
                elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
                    calls.add(ast.unparse(node.func))
            index = _Index(path, module)
            index.visit(tree)
            symbols.extend(index.symbols)
            files.append(SourceFile(path, module, kind, text, raw_hash, tuple(sorted(imports)), tuple(sorted(calls))))
    files.sort(key=lambda f: f.path)
    symbols.sort(key=lambda s: (s.path, s.start_line))
    repo = Repository(project_id, 'analysis-sha256:' + digest.hexdigest(),
                      tuple(f.path for f in files if f.kind == 'source'),
                      tuple(f.path for f in files if f.kind == 'test'),
                      tuple(s.name for s in symbols))
    return ParsedRepository(repo, tuple(files), tuple(symbols), tuple(issues))
