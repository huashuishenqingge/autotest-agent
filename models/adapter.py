"""Bounded OpenAI-compatible HTTPS client; no SDK or automatic retries."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bearer credential to a redirected host.


urlopen = build_opener(_NoRedirect()).open


class ModelError(ValueError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('nonfinite JSON number')
    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)


@dataclass(frozen=True)
class Config:
    base_url: str
    model: str
    api_key: str = field(repr=False)

    def __post_init__(self):
        address = urlsplit(self.base_url)
        if (address.scheme != 'https' or not address.hostname or address.username or address.password
                or address.query or address.fragment or self.base_url.endswith('/')):
            raise ValueError('base URL must be a credential-free HTTPS address without trailing slash')
        if (not self.model.strip() or len(self.model) > 200 or not self.api_key.strip()
                or any(ord(c) < 32 for c in self.model + self.api_key)):
            raise ValueError('model and API key required; control characters forbidden')

    @classmethod
    def from_env(cls, env_file: Path | None = None):
        names = {'USTC_LLM_BASE_URL', 'USTC_LLM_MODEL', 'USTC_LLM_API_KEY'}
        values = {}
        if env_file is not None and env_file.exists():
            if (env_file.is_symlink() or env_file.stat().st_size > 8192
                    or getattr(env_file.lstat(), 'st_file_attributes', 0) & 0x400):
                raise ValueError('Local config must be a small regular file')
            for line in env_file.read_text(encoding='utf-8-sig').splitlines():
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                name, separator, value = line.partition('=')
                name, value = name.strip(), value.strip()
                if not separator or name not in names or name in values:
                    raise ValueError('Invalid local config field; values are not displayed')
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\"', "'"}:
                    value = value[1:-1]
                values[name] = value
        values.update({k: os.environ[k] for k in names if k in os.environ})
        return cls(values.get('USTC_LLM_BASE_URL', 'https://api.llm.ustc.edu.cn/v1'),
                   values.get('USTC_LLM_MODEL', ''), values.get('USTC_LLM_API_KEY', ''))


class Budget:
    """Durable approved attempts; extending the limit never resets prior usage."""
    def __init__(self, path: Path, *, max_attempts: int | None = 3):
        if max_attempts is not None and (type(max_attempts) is not int or max_attempts not in (3, 4)):
            raise ValueError('Expected approved three/four limit or explicit unlimited approval')
        self.path = Path(path)
        self.max_attempts = max_attempts

    def reserve(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix('.lock')
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            raise ModelError('budget', 'Call budget is locked or unavailable') from None
        temporary = None
        try:
            os.close(fd)
            attempts = 0
            if self.path.exists():
                if self.path.is_symlink() or self.path.stat().st_size > 1024:
                    raise ValueError('invalid ledger')
                data = strict_json(self.path.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    raise ValueError('invalid ledger')
                if data.get('version') == 1 and set(data) == {'version', 'attempts'}:
                    previous_limit = 3
                elif data.get('version') == 2 and set(data) == {'version', 'attempts', 'approved_limit'}:
                    previous_limit = data['approved_limit']
                    if type(previous_limit) is not int or previous_limit not in (3, 4):
                        raise ValueError('invalid prior approval')
                elif (data.get('version') == 3 and set(data) == {'version', 'attempts', 'approved_limit'}
                      and data['approved_limit'] is None):
                    previous_limit = None
                else:
                    raise ValueError('invalid ledger schema')
                attempts = data['attempts']
                if type(attempts) is not int or attempts < 0 or (previous_limit is not None and attempts > previous_limit):
                    raise ValueError('invalid count')
            if self.max_attempts is not None and attempts >= self.max_attempts:
                raise ModelError('budget', 'Approved generation request budget exhausted')
            fd, temporary = tempfile.mkstemp(prefix='budget-', dir=self.path.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'version': 3 if self.max_attempts is None else 2,
                           'attempts': attempts + 1, 'approved_limit': self.max_attempts}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            if isinstance(error, ModelError):
                raise
            raise ModelError('budget', 'Call budget is invalid or cannot be saved') from None
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()
            lock.unlink()


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    usage: dict | None


class ModelClient:
    def __init__(self, config: Config, budget: Budget, *, thinking_mode: str | None = None):
        if thinking_mode not in (None, 'disabled', 'enabled'):
            raise ValueError('invalid thinking mode')
        self.config = config
        self.budget = budget
        self.thinking_mode = thinking_mode
        self.last_metadata = None

    def ensure_safe(self, payload):
        """Check decoded fields, including dictionary keys, without logging them."""
        pending = [payload]
        while pending:
            value = pending.pop()
            if isinstance(value, str) and self.config.api_key in value:
                raise ModelError('invalid_output', 'Model response contains credential material')
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, (list, tuple)):
                pending.extend(value)

    def _request(self, suffix: str, body=None):
        raw = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        request = Request(self.config.base_url + suffix, data=raw, headers={
            'Authorization': 'Bearer ' + self.config.api_key, 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=300) as response:
                data = response.read(1_000_001)
            if len(data) > 1_000_000:
                raise ModelError('invalid_output', 'Model response exceeds byte budget')
            text = data.decode('utf-8')
            if self.config.api_key in text:
                raise ModelError('invalid_output', 'Model response contains credential material')
            payload = strict_json(text)
            self.ensure_safe(payload)
            return payload
        except HTTPError as error:
            self.last_metadata = {'http_status': error.code}
            raise ModelError('environment', f'Model service returned HTTP {error.code}; no retry was made') from None
        except (URLError, OSError, TimeoutError) as error:
            self.last_metadata = {'network_error_type': type(error).__name__,
                                  'cause_type': type(getattr(error, 'reason', None)).__name__}
            raise ModelError('environment', 'Model service connection failed or timed out; no retry was made') from None
        except (UnicodeError, ValueError, RecursionError) as error:
            if isinstance(error, ModelError):
                raise
            raise ModelError('invalid_output', 'Model response is not valid bounded JSON') from None

    def list_models(self):
        payload = self._request('/models')
        try:
            models = tuple(item['id'] for item in payload['data'])
            if not models or any(not isinstance(m, str) or not m or len(m) > 200
                                 or any(ord(c) < 32 for c in m) for m in models):
                raise ValueError('invalid model IDs')
            return models
        except (KeyError, TypeError, ValueError):
            raise ModelError('invalid_output', 'Model listing is invalid') from None

    def complete(self, messages: list[dict]) -> Completion:
        self.last_metadata = None
        if (not messages or any(set(m) != {'role', 'content'} or m['role'] not in {'system', 'user', 'assistant'}
                                or not isinstance(m['content'], str) for m in messages)):
            raise ModelError('invalid_output', 'Invalid prompt messages')
        if sum(len(m['content']) for m in messages) > 16000:
            raise ModelError('budget', 'Full prompt exceeds 16000-character budget')
        if any(self.config.api_key in m['content'] for m in messages):
            raise ModelError('invalid_output', 'Prompt contains credential material')
        self.budget.reserve()
        body = {'model': self.config.model, 'messages': messages,
                'max_tokens': 2048, 'temperature': 0.2, 'stream': False}
        if self.thinking_mode:
            body['thinking'] = {'type': self.thinking_mode}
        payload = self._request('/chat/completions', body)
        try:
            choice = payload['choices'][0]
            message = choice['message']
            usage = payload.get('usage')
            safe_usage = None
            if isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                               for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
                safe_usage = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
            reason = choice.get('finish_reason')
            self.last_metadata = {'finish_reason': reason if reason in ('stop', 'length', 'content_filter', 'tool_calls') else 'unknown',
                                  'usage': safe_usage, 'thinking_mode': self.thinking_mode,
                                  'content_characters': len(message.get('content') or ''),
                                  'reasoning_characters': len(message.get('reasoning_content') or '')}
            if (choice['finish_reason'] != 'stop' or message.get('refusal') or message.get('tool_calls')
                    or not isinstance(message['content'], str) or not message['content'].strip()):
                raise ValueError('incomplete completion')
            model = payload['model']
            if not isinstance(model, str) or not model or len(model) > 200 or any(ord(c) < 32 for c in model):
                raise ValueError('invalid model')
            usage = payload.get('usage')
            if usage is not None:
                fields = ('prompt_tokens', 'completion_tokens', 'total_tokens')
                if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in fields):
                    raise ValueError('invalid usage')
                usage = {k: usage[k] for k in fields}
            return Completion(message['content'], model, usage)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ModelError('invalid_output', 'Model completion is incomplete or has invalid metadata') from None
