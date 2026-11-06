from dataclasses import replace
import hashlib
import json
import unittest

from agents.repairer import RepairContext, Repairer, make_repair_messages, parse_repair, context_digest
from models.adapter import Completion
from workflow.state import Repository, Policy, RepairInput, Failure, FailureKind, TestRun


class RepairerTests(unittest.TestCase):
    def setUp(self):
        self.source = 'def adult(age): return age > 18\n'
        self.hashed = hashlib.sha256(self.source.encode()).hexdigest()
        self.repo = Repository('p', 'baseline', ('app.py',), ('test_app.py',))
        self.context = RepairContext(('Age 18 and above is adult.',), (('app.py', self.hashed),),
                                     (('test_app.py', 'from app import adult\ndef test_age(): assert adult(18)\n'),))
        self.request = RepairInput('t', self.repo, Policy(),
            TestRun('failed', context_digest((('app.py', self.source),), self.context.visible_tests), 1, 0, 1, 0,
                    failure=Failure(FailureKind.TEST_ASSERTION, 'assertion')), 1, (('app.py', self.source),), 'assert False')
        self.valid = {'task_id': 't', 'base_revision': 'baseline', 'based_on_run_id': 'failed',
                      'diagnosis': 'app.py:1 excludes age 18; requirement includes it. Use >=.',
                      'files': [{'path': 'app.py', 'content': 'def adult(age): return age >= 18\n', 'expected_sha256': self.hashed}]}

    def parse(self, payload):
        return parse_repair(self.request, self.context, json.dumps(payload))

    def test_valid_current_evidence_and_readonly_context(self):
        self.assertIsNone(self.parse(self.valid).failure)
        messages = make_repair_messages(self.request, self.context)
        prompt = json.loads(messages[1]['content'])
        self.assertEqual(prompt['requirements'], list(self.context.requirements))
        self.assertEqual(prompt['based_on_run_id'], 'failed')
        self.assertIn('1:', prompt['sources'][0]['numbered'])
        self.assertNotIn('holdout', messages[1]['content'])

    def test_stale_run_hash_paths_and_syntax_are_rejected(self):
        for key in ('task_id', 'base_revision', 'based_on_run_id'):
            with self.assertRaises(ValueError):
                self.parse(dict(self.valid, **{key: 'old'}))
        for changes in ({'expected_sha256': '0'*64}, {'path': 'test_app.py'}, {'path': '../app.py'},
                        {'content': 'def invalid('}, {'content': self.source}):
            with self.assertRaises(ValueError):
                self.parse(dict(self.valid, files=[dict(self.valid['files'][0], **changes)]))

    def test_missing_diagnosis_and_extra_command_are_rejected(self):
        for payload in (dict(self.valid, diagnosis=''), dict(self.valid, command='run')):
            with self.assertRaises(ValueError):
                self.parse(payload)

    def test_nonassertion_overbudget_and_changed_snapshot_do_not_call(self):
        class Client:
            calls = 0
            def complete(self, messages):
                self.calls += 1
                return Completion('{}', 'fake', None)
        for request in (replace(self.request, attempt=4),
                        replace(self.request, source_texts=(('app.py', self.source + '# changed'),)),
                        replace(self.request, failed_run=replace(self.request.failed_run,
                            failure=Failure(FailureKind.TEST_COLLECTION, 'collection')))):
            client = Client()
            output = Repairer(client, self.context).repair(request)
            self.assertIsNotNone(output.failure)
            self.assertEqual(client.calls, 0)

    def test_hidden_or_missing_visible_test_context_rejected(self):
        for context in (replace(self.context, visible_tests=()),
                        replace(self.context, visible_tests=(('test_holdout.py', 'secret'),))):
            with self.assertRaises(ValueError):
                make_repair_messages(self.request, context)


if __name__ == '__main__':
    unittest.main()
