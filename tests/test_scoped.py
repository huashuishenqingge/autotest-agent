import ast
import hashlib
import json
import unittest

from agents.repairer import RepairContext, context_digest
from experiments.scoped import locate_function, replace_function, ScopedRepairer
from models.adapter import Completion
from workflow.state import Repository, Policy, RepairInput, TestRun, Failure, FailureKind, GenerateInput


class ScopedTests(unittest.TestCase):
    def test_utf8_method_crlf_decorators_tail_and_other_bytes_preserved(self):
        source = '# 中文\r\nclass C:\r\n    @staticmethod\r\n    def f(值: int = 1) -> int:\r\n        return 值 - 1  # keep\r\n\r\nOUTSIDE = "不变"\r\n'
        scope = locate_function(source, 'C.f')
        self.assertEqual(scope['line'], 4)
        fixed = replace_function(source, 'C.f', 'def f(值: int = 1) -> int:\n    return 值 + 1')
        self.assertEqual(fixed, source.replace('return 值 - 1', 'return 值 + 1'))
        ast.parse(fixed)

    def test_ambiguous_conditional_shadowed_and_nested_targets_rejected(self):
        for source, name in [('def f(): pass\ndef f(): pass\n', 'f'),
                             ('if True:\n def f(): pass\n', 'f'),
                             ('def f(): pass\nf = 1\n', 'f'),
                             ('def f(): pass\nwith manager() as f:\n pass\n', 'f'),
                             ('def f(): pass\ndel f\n', 'f'),
                             ('def f(): pass\nmatch 1:\n case f: pass\n', 'f'),
                             ('def f(): pass\nglobals()["f"] = 1\n', 'f'),
                             ('def outer():\n def inner(): pass\n', 'outer.inner')]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                locate_function(source, name)

    def test_signature_decorator_async_change_or_module_injection_rejected(self):
        source = 'async def f(x: int = 1) -> int:\n    return x - 1\n'
        for candidate in ['def f(x: int = 1) -> int:\n    return x + 1',
                          'async def f(y: int = 1) -> int:\n    return y',
                          '@decorator\nasync def f(x: int = 1) -> int:\n    return x',
                          'async def f(x: int = 1) -> int:\n    return x\nOTHER = 1']:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                replace_function(source, 'f', candidate)

    def setup_agent(self, client, *, source='def f(x):\n    return x > 1\n', digest=None):
        tests = (('tests/test_app.py', 'from app import f\ndef test_f(): assert f(1)\n'),)
        repo = Repository('p', 'fixed', ('app.py',), (tests[0][0],))
        hashes = (('app.py', hashlib.sha256(source.encode()).hexdigest()),)
        run = TestRun('r', digest or context_digest((('app.py', source),), tests), 1, 0, 1, 0,
                      failure=Failure(FailureKind.TEST_ASSERTION, 'assertion'))
        request = RepairInput('t', repo, Policy(), run, 1, (('app.py', source),), 'assert f(1)')
        return ScopedRepairer(client, RepairContext(('f(x) is x >= 1',), hashes, tests),
                              ('app.py', 'f'), failure_evidence=(('test_app', 'test_f', 'assert f(1)', 'AssertionError'),)), request

    def test_scoped_wire_rebinds_full_file_hash_and_does_not_send_full_module(self):
        class Client:
            def complete(self, messages):
                self.messages = messages
                return Completion(json.dumps({'task_id':'t','base_revision':'fixed','based_on_run_id':'r',
                    'diagnosis':'app.py:2 excludes endpoint','replacements':[{'path':'app.py','qualified_name':'f',
                    'content':'def f(x):\n    return x >= 1'}]}), 'fake', None)
        client = Client()
        source = 'def f(x):\n    return x > 1\n\n' + '# omitted marker\n' * 1200
        agent, request = self.setup_agent(client, source=source)
        output = agent.repair(request)
        self.assertIsNone(output.failure)
        self.assertEqual(output.files[0].expected_sha256, hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(output.files[0].content, source.replace('return x > 1', 'return x >= 1'))
        self.assertNotIn('omitted marker', json.dumps(client.messages))
        self.assertLessEqual(sum(len(m['content']) for m in client.messages), 16000)

    def test_stale_snapshot_and_overlong_target_fail_before_model(self):
        class Client:
            def complete(self, messages):
                raise AssertionError('must not call model')
        for source, digest in [('def f(x): return x > 1\n', 'wrong'),
                               ('def f(x):\n    """'+'x'*7000+'"""\n    return x > 1\n', None),
                               ('def f(x): return x > 1\n'+'# long module\n'*10000,None)]:
            agent, request = self.setup_agent(Client(), source=source, digest=digest)
            self.assertIsNotNone(agent.repair(request).failure)

    def test_b2_and_a0_contexts_differ_only_by_bounded_hint(self):
        from experiments.context import build_messages
        repo = Repository('p','r',('app.py',),('tests/test_old.py','tests/generated/test_new.py'))
        request = GenerateInput('t',repo,Policy(),('f returns x+1',),(('app.py','def f(x):\n    return x+1\n'),))
        tests = tuple((p,'from app import f\ndef test_f(): assert f(1)==2\n') for p in repo.test_files)
        args = (request, request.requirements, ('app.py','f'), tests, 'system')
        a, audit_a = build_messages(*args,generated_paths=(repo.test_files[1],))
        b, audit_b = build_messages(*args,generated_paths=(repo.test_files[1],),coverage_hint={'files':{'app.py':{'missing_lines':list(range(1,10000))}}})
        data_a, data_b = json.loads(a[1]['content']),json.loads(b[1]['content'])
        hint = data_b.pop('coverage_hint')
        self.assertEqual(data_a,data_b)
        self.assertEqual(audit_a['selected'],audit_b['selected'])
        self.assertTrue(hint['truncated'])
        self.assertLessEqual(len(json.dumps(hint,ensure_ascii=False)),1500)
        self.assertEqual(data_a['related_tests'][0]['path'],repo.test_files[1])

    def test_wrong_output_run_target_and_signature_never_propose_files(self):
        import copy
        original={'task_id':'t','base_revision':'fixed','based_on_run_id':'r','diagnosis':'app.py:2 endpoint',
                  'replacements':[{'path':'app.py','qualified_name':'f','content':'def f(x):\n    return x >= 1'}]}
        for mutate in (lambda p:p.update(based_on_run_id='old'),
                       lambda p:p['replacements'][0].update(path='other.py'),
                       lambda p:p['replacements'][0].update(qualified_name='other'),
                       lambda p:p['replacements'][0].update(content='def f(y):\n    return y >= 1')):
            data=copy.deepcopy(original)
            mutate(data)
            class Client:
                def complete(self,messages):
                    return Completion(json.dumps(data),'fake',None)
            agent, request=self.setup_agent(Client())
            output=agent.repair(request)
            self.assertIsNotNone(output.failure)
            self.assertEqual(output.files,())
