import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from agents.generator import Generator, make_messages, parse_generation
from models.adapter import Budget, Completion, Config, ModelClient, ModelError
from workflow.state import GenerateInput, Policy, Repository, FailureKind


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.request = GenerateInput('task', Repository('p', 'baseline', ('app.py',), ('tests/test_app.py',)),
                                     Policy(), ('age >= 18 is adult; negative ages raise ValueError',),
                                     (('app.py', 'def adult(age): return age > 18\n'),))
        self.valid = {'task_id': 'task', 'base_revision': 'baseline', 'rationale': 'Check the independent age threshold.',
                      'files': [{'path': 'tests/generated/test_adult.py',
                                 'content': 'from app import adult\ndef test_boundary(): assert adult(18) is True\n'}]}

    def parse(self, payload):
        return parse_generation(self.request, json.dumps(payload))

    def test_valid_proposal_and_readonly_context(self):
        result = self.parse(self.valid)
        self.assertEqual(result.task_id, 'task')
        messages = make_messages(self.request, (('tests/test_app.py', '# read only reference\n'),))
        data = json.loads(messages[1]['content'])
        self.assertEqual(data['requirements'], list(self.request.requirements))
        self.assertEqual(data['related_tests'][0][0], 'tests/test_app.py')
        self.assertNotIn('holdout', messages[1]['content'])
        self.assertIn('untrusted', messages[0]['content'])

    def test_reject_identity_unknown_keys_and_bad_json(self):
        for key in ('task_id', 'base_revision'):
            payload = dict(self.valid, **{key: 'stale'})
            with self.assertRaises(ValueError):
                self.parse(payload)
        for raw in ('```json\n{}\n```', '{"task_id":"a","task_id":"b"}', '[]', 'null'):
            with self.assertRaises(ValueError):
                parse_generation(self.request, raw)
        with self.assertRaises(ValueError):
            self.parse(dict(self.valid, command='run this'))

    def test_reject_paths_empty_tests_syntax_and_duplicate_files(self):
        for path, text in (('app.py', 'def test_x(): assert True'),
                           ('tests/generated/../test_x.py', 'def test_x(): assert True'),
                           ('tests/generated/test_x.py', 'def test_x('),
                           ('tests/generated/test_x.py', 'pass'),
                           ('tests/generated/test_x.py', 'def test_x(): pass')):
            with self.subTest(path=path, text=text):
                with self.assertRaises(ValueError):
                    self.parse(dict(self.valid, files=[{'path': path, 'content': text}]))
        for files in ([], self.valid['files'] * 2):
            with self.assertRaises(ValueError):
                self.parse(dict(self.valid, files=files))

    def test_related_tests_must_be_explicitly_inventoried(self):
        with self.assertRaises(ValueError):
            make_messages(self.request, (('private/test_holdout.py', 'secret'),))
        with self.assertRaises(ValueError):
            make_messages(self.request, (('tests/test_app.py', 'x'), ('tests/test_app.py', 'y')))

    def test_failures_are_typed_without_raw_response(self):
        class Client:
            def complete(self, messages):
                raise ModelError('environment', 'sanitized model transport failure')
        result = Generator(Client()).generate(self.request)
        self.assertEqual(result.failure.kind, FailureKind.ENVIRONMENT)
        self.assertEqual(result.files, ())

    def test_completion_metadata_and_invalid_output(self):
        payload = json.dumps(self.valid)
        class Client:
            def complete(self, messages):
                return Completion(payload, 'served-model', {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3})
        generator = Generator(Client())
        self.assertIsNone(generator.generate(self.request).failure)
        self.assertEqual(generator.last_completion.model, 'served-model')
        payload = '{invalid}'
        self.assertEqual(generator.generate(self.request).failure.kind, FailureKind.INVALID_OUTPUT)

    def test_credential_hidden_in_nested_proposal_json_is_not_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            client = ModelClient(Config('https://api.llm.ustc.edu.cn/v1', 'model', 'private-key'),
                                 Budget(Path(folder) / 'budget.json'))
            payload = dict(self.valid, rationale='private-key')
            content = json.dumps(payload).replace('private-key', '\\u0070rivate-key')
            generator = Generator(client)
            with patch.object(client, 'complete', return_value=Completion(content, 'model', None)):
                result = generator.generate(self.request)
            self.assertIsNotNone(result.failure)
            self.assertEqual(result.files, ())
            self.assertIsNone(generator.last_completion)
            self.assertNotIn('private-key', repr(result))


if __name__ == '__main__':
    unittest.main()
