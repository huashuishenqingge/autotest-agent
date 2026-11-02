import tempfile
import unittest
from pathlib import Path

from repository.parser import parse_repository
from repository.context import build_context
from workflow.state import Policy


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def test_src_layout_symbols_and_imports_without_execution(self):
        self.write('src/pkg/app.py', 'from .helper import inc\nraise RuntimeError("do not execute")\nclass Box:\n    async def add(self, x: int = 1):\n        return inc(x)\n')
        self.write('src/pkg/helper.py', 'def inc(x):\n    return x + 1\n')
        self.write('tests/test_app.py', 'def test_app(): pass\n')
        self.write('.venv/lib/vendor.py', 'bad syntax!')
        self.write('setup.py', 'bad syntax!')
        result = parse_repository(self.root, project_id='demo')
        self.assertEqual(result.repository.source_files, ('src/pkg/app.py', 'src/pkg/helper.py'))
        self.assertEqual(result.repository.test_files, ('tests/test_app.py',))
        symbol = next(s for s in result.symbols if s.name == 'pkg.app.Box.add')
        self.assertEqual((symbol.path, symbol.start_line, symbol.end_line), ('src/pkg/app.py', 4, 5))
        self.assertIn('x: int=1', symbol.signature.replace(' = ', '='))
        app = next(f for f in result.files if f.path.endswith('app.py') and f.kind == 'source')
        self.assertIn('pkg.helper', app.imports)
        self.assertIn('inc', app.calls)

    def test_revision_is_deterministic_and_changes_with_bytes(self):
        self.write('app.py', 'x = 1\n')
        first = parse_repository(self.root, project_id='p')
        self.assertEqual(first.repository.revision, parse_repository(self.root, project_id='p').repository.revision)
        self.write('app.py', 'x = 2\n')
        self.assertNotEqual(first.repository.revision, parse_repository(self.root, project_id='p').repository.revision)

    def test_invalid_large_and_non_utf8_are_reported_not_writable(self):
        self.write('broken.py', 'def (:')
        self.write('large.py', '#' * 101)
        (self.root / 'binary.py').write_bytes(b'\xff')
        result = parse_repository(self.root, project_id='p', max_file_bytes=100)
        self.assertEqual(result.repository.source_files, ())
        self.assertEqual({i.code for i in result.issues}, {'syntax_error', 'file_too_large', 'encoding_error'})

    def test_sensitive_and_config_files_are_excluded(self):
        for path in ('.env/secret.py', '.git/hook.py', 'build/generated.py', 'conftest.py', 'noxfile.py'):
            self.write(path, 'x = 1')
        self.write('app.py', 'x = 1')
        self.assertEqual(parse_repository(self.root, project_id='p').repository.source_files, ('app.py',))

    def test_links_not_followed(self):
        self.write('app.py', 'x = 1')
        try:
            (self.root / 'linked.py').symlink_to(self.root / 'app.py')
        except OSError:
            self.skipTest('symlink creation unavailable')
        result = parse_repository(self.root, project_id='p')
        self.assertNotIn('linked.py', result.repository.source_files)
        self.assertIn('link_rejected', {i.code for i in result.issues})

    def test_case_collision_rejected_when_supported(self):
        self.write('App.py', 'x=1')
        self.write('app.py', 'x=2')
        if len(list(self.root.glob('*.py'))) < 2:
            self.skipTest('case insensitive filesystem')
        with self.assertRaisesRegex(ValueError, 'collision'):
            parse_repository(self.root, project_id='p')

    def test_context_target_dependencies_and_snapshot(self):
        self.write('src/pkg/app.py', 'from .helper import inc\ndef add(x):\n    return inc(x)\n')
        self.write('src/pkg/helper.py', 'def inc(x): return x+1\n')
        self.write('unrelated.py', 'x=1')
        snapshot = parse_repository(self.root, project_id='p')
        self.write('src/pkg/app.py', 'changed on disk')
        result = build_context(snapshot, task_id='t', requirements=('increment',), targets=('src/pkg/app.py',), max_context_bytes=1000)
        self.assertEqual(tuple(p for p, _ in result.request.source_texts), ('src/pkg/app.py', 'src/pkg/helper.py'))
        self.assertIn('def add', result.request.source_texts[0][1])
        self.assertEqual(result.request.repository.revision, snapshot.repository.revision)
        self.assertEqual(result.spans[0], ('src/pkg/app.py', 1, 3))
        self.assertIn('unrelated.py', result.omitted_paths)

    def test_context_budget_does_not_silently_truncate_target(self):
        self.write('app.py', 'def f(): return 1\n')
        snapshot = parse_repository(self.root, project_id='p')
        with self.assertRaisesRegex(ValueError, 'budget'):
            build_context(snapshot, task_id='t', requirements=('one',), targets=('app.py',), max_context_bytes=1)
        for path in ('../app.py', 'missing.py'):
            with self.assertRaises(ValueError):
                build_context(snapshot, task_id='t', requirements=('one',), targets=(path,))
        with self.assertRaises(ValueError):
            build_context(snapshot, task_id='t', requirements=(), targets=('app.py',))

    def test_total_and_file_count_budgets(self):
        self.write('app.py', 'x=1')
        self.write('other.py', 'x=2')
        for budgets in ({'max_files': 1}, {'max_total_bytes': 2}):
            with self.assertRaisesRegex(ValueError, 'budget'):
                parse_repository(self.root, project_id='p', **budgets)

    def test_related_tests_are_separate_and_optional_files_are_omitted(self):
        self.write('app.py', 'import helper\nx=1\n')
        self.write('helper.py', 'y=2\n')
        self.write('tests/test_app.py', 'import app\ndef test_x(): assert app.x==1\n')
        snapshot = parse_repository(self.root, project_id='p')
        target_size = len(next(f.text for f in snapshot.files if f.path == 'app.py').encode('utf-8'))
        small = build_context(snapshot, task_id='t', requirements=('one',), targets=('app.py',), max_context_bytes=target_size)
        self.assertEqual(len(small.request.source_texts), 1)
        self.assertIn('helper.py', small.omitted_paths)
        full = build_context(snapshot, task_id='t', requirements=('one',), targets=('app.py',))
        self.assertEqual(full.related_tests[0][0], 'tests/test_app.py')
        self.assertNotIn('tests/test_app.py', dict(full.request.source_texts))


if __name__ == '__main__':
    unittest.main()
