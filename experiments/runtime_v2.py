"""Fixed candidate identity; matching this profile is not formal qualification."""
import hashlib

from repair.fixtures import PROJECT_ROOT

IMAGE = 'sha256:1756fbdd32c39afbdc437e6595f5b69e71128c75de260e02aaae73f709506dcf'
WORKER_SHA = '73124927bcc8500a5ec1bb345f77c48316c2d401a53d80e87109bc2408b5f42e'
LOCK_SHA = '8f4d22acc8ff1aaa0b5bf56a283d6c7fe11790e08f416ceec709253bc907eee4'


def expected_runtime():
    return {'image_id': IMAGE, 'worker_sha256': WORKER_SHA,
            'dependency_lock_sha256': LOCK_SHA, 'context_version': 'p08-context-v1',
            'repair_version': 'p08-scoped-repair-v1', 'model': 'deepseek-v4-flash-ascend',
            'temperature': 0.2, 'thinking_mode': 'disabled', 'max_tokens': 2048,
            'input_characters': 16000, 'timeout_seconds': 60}


def validate_runtime(value):
    expected = expected_runtime()
    if (not isinstance(value, dict) or value != expected
            or any(type(value[k]) is not type(v) for k, v in expected.items())):
        raise ValueError('V2 candidate runtime differs from approved parameters')
    for name, digest, normalize in (('execution/container_worker.py', WORKER_SHA, True),
                                    ('execution/requirements-p08.lock', LOCK_SHA, False)):
        path = PROJECT_ROOT/name
        if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked V2 runtime file')
        raw = path.read_bytes()
        if hashlib.sha256(raw.replace(b'\r\n', b'\n') if normalize else raw).hexdigest() != digest:
            raise ValueError('V2 runtime implementation or dependency drift: '+name)
