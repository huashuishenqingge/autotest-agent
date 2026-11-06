import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from models.adapter import Config, ModelClient, ModelError, Budget


class Response(io.BytesIO):
    pass


def envelope(content='hello', finish='stop', usage=None):
    return json.dumps({'model': 'deepseek-test', 'choices': [{'finish_reason': finish,
        'message': {'content': content}}], 'usage': usage}).encode()


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'budget.json'
        self.config = Config('https://api.llm.ustc.edu.cn/v1', 'deepseek-test', 'private-key')

    def client(self):
        return ModelClient(self.config, Budget(self.path))

    def test_valid_request_and_missing_usage(self):
        with patch('models.adapter.urlopen', return_value=Response(envelope())) as send:
            result = self.client().complete([{'role': 'user', 'content': 'hello'}])
        self.assertEqual(result.content, 'hello')
        self.assertIsNone(result.usage)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, self.config.base_url + '/chat/completions')
        self.assertEqual(json.loads(request.data)['max_tokens'], 2048)
        self.assertEqual(json.loads(self.path.read_text())['attempts'], 1)
        self.assertNotIn('private-key', repr(self.config))

    def test_usage_and_persistent_three_attempt_limit(self):
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
        with patch('models.adapter.urlopen', side_effect=lambda *a, **k: Response(envelope(usage=usage))) as send:
            for _ in range(3):
                self.assertEqual(self.client().complete([{'role': 'user', 'content': 'x'}]).usage, usage)
            with self.assertRaises(ModelError) as caught:
                self.client().complete([{'role': 'user', 'content': 'x'}])
        self.assertEqual(caught.exception.kind, 'budget')
        self.assertEqual(send.call_count, 3)

    def test_http_errors_are_sanitized_and_never_retried(self):
        for code in (401, 403, 429, 500):
            with self.subTest(code=code):
                budget = Budget(self.path.with_name(str(code) + '.json'))
                with patch('models.adapter.urlopen', side_effect=HTTPError('secret-url', code, 'private-key', {}, None)) as send:
                    with self.assertRaises(ModelError) as caught:
                        ModelClient(self.config, budget).complete([{'role': 'user', 'content': 'x'}])
                self.assertNotIn('private-key', str(caught.exception))
                self.assertEqual(send.call_count, 1)
                self.assertEqual(json.loads(budget.path.read_text())['attempts'], 1)

    def test_network_bad_json_truncation_and_size(self):
        for response in (URLError('private-key'), TimeoutError('private-key'), Response(b'not json'),
                         Response(envelope(finish='length')), Response(b'x' * 1_000_001)):
            with self.subTest(response=type(response).__name__):
                with tempfile.TemporaryDirectory() as folder:
                    with patch('models.adapter.urlopen', side_effect=response if isinstance(response, Exception) else None,
                               return_value=response):
                        with self.assertRaises(ModelError) as caught:
                            ModelClient(self.config, Budget(Path(folder) / 'b.json')).complete([{'role': 'user', 'content': 'x'}])
                    self.assertNotIn('private-key', str(caught.exception))

    def test_incomplete_response_retains_safe_finish_and_usage_metadata(self):
        usage = {'prompt_tokens': 10, 'completion_tokens': 2048, 'total_tokens': 2058}
        client = self.client()
        with patch('models.adapter.urlopen', return_value=Response(envelope(content='', finish='length', usage=usage))):
            with self.assertRaises(ModelError):
                client.complete([{'role': 'user', 'content': 'x'}])
        self.assertEqual(client.last_metadata['finish_reason'], 'length')
        self.assertEqual(client.last_metadata['usage'], usage)
        self.assertNotIn('private-key', json.dumps(client.last_metadata))

    def test_bad_config_and_oversized_prompt_do_not_send(self):
        for url in ('http://api.llm.ustc.edu.cn/v1', 'https://user:pw@example.com/v1',
                    'https://example.com/v1?token=secret', 'https://example.com/v1#x'):
            with self.assertRaises(ValueError):
                Config(url, 'model', 'private-key')
        with self.assertRaises(ValueError):
            Config(self.config.base_url, '', '')
        with patch('models.adapter.urlopen') as send:
            with self.assertRaises(ModelError):
                self.client().complete([{'role': 'user', 'content': 'x' * 16001}])
            send.assert_not_called()

    def test_corrupt_or_locked_budget_is_rejected(self):
        self.path.write_text('not json')
        with self.assertRaises(ModelError):
            Budget(self.path).reserve()
        self.path.unlink()
        self.path.with_suffix('.lock').write_text('busy')
        with self.assertRaises(ModelError):
            Budget(self.path).reserve()

    def test_local_env_config_and_environment_precedence(self):
        env_file = self.path.with_name('.env')
        env_file.write_text('USTC_LLM_MODEL=local-model\nUSTC_LLM_API_KEY=local-secret\n', encoding='utf-8')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Config.from_env(env_file).model, 'local-model')
        with patch.dict(os.environ, {'USTC_LLM_MODEL': 'environment-model'}, clear=True):
            self.assertEqual(Config.from_env(env_file).model, 'environment-model')
        env_file.write_text('USTC_LLM_API_KEY=do-not-print\nnot valid', encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            Config.from_env(env_file)
        self.assertNotIn('do-not-print', str(caught.exception))

    def test_model_listing_does_not_consume_generation_budget(self):
        with patch('models.adapter.urlopen', return_value=Response(b'{"data":[{"id":"deepseek-test"}]}')):
            self.assertEqual(self.client().list_models(), ('deepseek-test',))
        self.assertFalse(self.path.exists())

    def test_json_escaped_credential_echo_is_rejected(self):
        raw = envelope(content='private-key').replace(b'private-key', b'\\u0070rivate-key')
        with patch('models.adapter.urlopen', return_value=Response(raw)):
            with self.assertRaises(ModelError) as caught:
                self.client().complete([{'role': 'user', 'content': 'x'}])
        self.assertNotIn('private-key', str(caught.exception))

    def test_approved_fourth_attempt_migrates_without_reset(self):
        self.path.write_text('{"version":1,"attempts":3}')
        with patch('models.adapter.urlopen', return_value=Response(envelope())) as send:
            ModelClient(self.config, Budget(self.path, max_attempts=4)).complete([{'role': 'user', 'content': 'x'}])
            self.assertEqual(send.call_args.kwargs['timeout'], 300)
        ledger = json.loads(self.path.read_text())
        self.assertEqual(ledger, {'version': 2, 'attempts': 4, 'approved_limit': 4})
        with patch('models.adapter.urlopen') as send:
            with self.assertRaises(ModelError):
                ModelClient(self.config, Budget(self.path, max_attempts=4)).complete([{'role': 'user', 'content': 'x'}])
            send.assert_not_called()

    def test_unlimited_approval_preserves_usage_and_still_counts(self):
        self.path.write_text('{"version":2,"attempts":4,"approved_limit":4}')
        with patch('models.adapter.urlopen', side_effect=lambda *a, **k: Response(envelope())) as send:
            for _ in range(2):
                ModelClient(self.config, Budget(self.path, max_attempts=None)).complete([{'role': 'user', 'content': 'x'}])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(json.loads(self.path.read_text()), {'version': 3, 'attempts': 6, 'approved_limit': None})


if __name__ == '__main__':
    unittest.main()
