"""Explicit opt-in real Docker acceptance cases for P03; writes local evidence."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from execution.docker_runner import run_tests
from workflow.state import Policy, Repository


CASES = {
    'original_and_generated': ({
        'app.py': 'def add(a, b): return a + b\n',
        'tests/test_original.py': 'from app import add\ndef test_original(): assert add(1, 2) == 3\n',
        'tests/generated/test_boundary.py': 'from app import add\ndef test_boundary(): assert add(0, 0) == 0\n',
    }, None, 2),
    'assertion': ({'tests/test_assertion.py': 'def test_failure(): assert 1 == 2\n'}, 'test_assertion', 0),
    'collection': ({'tests/test_import.py': 'import definitely_missing_autotest_dependency\n'}, 'test_collection', 0),
    'empty': ({'app.py': 'value = 1\n'}, 'environment', 0),
    'skipped': ({'tests/test_skip.py': 'import pytest\n@pytest.mark.skip(reason="fixture")\ndef test_skip(): pass\n'}, 'environment', 0),
    'timeout': ({'tests/test_loop.py': 'def test_loop():\n    while True: pass\n'}, 'timeout', 0),
    'long_log': ({'tests/test_log.py': 'def test_log():\n    print("x" * 20000)\n    assert False\n'}, 'test_assertion', 0),
    'continuous_log': ({'tests/test_continuous.py': 'import time\ndef test_continuous():\n    for _ in range(512):\n        print("x" * 1024)\n        time.sleep(0.003)\n    assert False\n'}, 'test_assertion', 0),
    'restrictions': ({'tests/test_restrictions.py': '''import os
from pathlib import Path
import socket
import pytest

def test_restrictions():
    assert os.getuid() == 65534
    assert 'CapEff:\\t0000000000000000' in Path('/proc/self/status').read_text()
    assert 'NoNewPrivs:\\t1' in Path('/proc/self/status').read_text()
    assert Path('/sys/fs/cgroup/memory.max').read_text().strip() == '268435456'
    assert Path('/sys/fs/cgroup/pids.max').read_text().strip() == '64'
    quota, period = map(int, Path('/sys/fs/cgroup/cpu.max').read_text().split())
    assert quota == period
    mounts = [line.split() for line in Path('/proc/mounts').read_text().splitlines()]
    for mountpoint in ('/', '/workspace'):
        mount = next(parts for parts in mounts if parts[1] == mountpoint)
        assert 'ro' in mount[3].split(',')
    assert sorted(p.name for p in Path('/sys/class/net').iterdir()) == ['lo']
    tmp_mount = next(line for line in Path('/proc/mounts').read_text().splitlines()
                     if line.split()[1] == '/tmp')
    assert 'size=65536k' in tmp_mount
    for target in ('/workspace/injected.py', '/opt/autotest/injected.py'):
        with pytest.raises(OSError):
            Path(target).write_text('forbidden')
    with socket.socket() as connection:
        connection.settimeout(1)
        with pytest.raises(OSError):
            connection.connect(('1.1.1.1', 443))
'''}, None, 1),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-id', required=True)
    parser.add_argument('--artifacts', type=Path, default=Path('artifacts/p03-acceptance'))
    args = parser.parse_args()
    results = []
    for name, (files, expected_kind, passed) in CASES.items():
        with tempfile.TemporaryDirectory(prefix='autotest-fixture-') as folder:
            root = Path(folder)
            for path, content in files.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding='utf-8')
            sources = tuple(p for p in files if not p.startswith('tests/'))
            tests = tuple(p for p in files if p.startswith('tests/'))
            baseline = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in files}
            repo = Repository(name, 'p03-fixed-fixture-v1', sources, tests)
            result = run_tests(root, repo, image_id=args.image_id, artifact_root=args.artifacts,
                               policy=Policy(timeout_seconds=3 if name == 'timeout' else 30,
                                             max_output_bytes=4096))
            run = result.run
            kind = run.failure.kind.value if run.failure else None
            assert kind == expected_kind, (name, asdict(result))
            assert run.passed == passed and result.cleanup_ok, (name, asdict(result))
            assert run.successful == (expected_kind is None), name
            assert baseline == {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in files}, name
            assert sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()) == sorted(files), name
            directory = Path(result.artifact_directory)
            manifest = json.loads((directory / 'manifest.json').read_text())
            assert dict(manifest['files']) == baseline, name
            digest = hashlib.sha256()
            for path, hashed in sorted(baseline.items()):
                digest.update(path.encode() + b'\0' + hashed.encode() + b'\0')
            assert run.snapshot_digest == digest.hexdigest(), name
            log = (directory / 'log.txt').read_text(encoding='utf-8')
            assert len(log.encode('utf-8')) <= 4096, name
            if name in {'long_log', 'continuous_log'}:
                assert '[log truncated]' in log, name
            probe = subprocess.run(['docker', 'ps', '-a', '--filter', 'name=autotest-' + run.run_id,
                                    '--format', '{{.Names}}'], capture_output=True, text=True, timeout=10, check=True)
            assert not probe.stdout.strip(), (name, 'container remains')
            results.append({'case': name, **asdict(result)})
            print(name + ': PASS', flush=True)
    args.artifacts.mkdir(parents=True, exist_ok=True)
    (args.artifacts / 'summary.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(f'{len(results)} real Docker cases passed; {args.artifacts / "summary.json"}')


if __name__ == '__main__':
    main()
