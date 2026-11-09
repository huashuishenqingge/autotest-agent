"""Trusted image entrypoint. Reports stay on bounded tmpfs until host cleanup."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

RESULT = Path('/tmp/autotest-result.json')


def main():
    if sys.argv[1:] == ['--read']:
        if not RESULT.is_file():
            return 3
        if RESULT.is_symlink() or RESULT.stat().st_size > 12_000_000:
            return 4
        sys.stdout.buffer.write(RESULT.read_bytes())
        return 0
    limit = min(max(int(sys.argv[1]), 1), 1_000_000)
    env = dict(os.environ, PYTHONPATH='/workspace:/workspace/src',
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    targets = json.loads(sys.argv[2]) if len(sys.argv) == 3 else []
    if (not isinstance(targets, list) or any(not isinstance(p, str) or not p.endswith('.py')
            or any(part in ('', '.', '..') for part in p.split('/')) or p.startswith('/')
            or '\\' in p or ':' in p or any(ord(c) < 32 for c in p) for p in targets)):
        return 5
    pytest_args = ['-c', '/opt/autotest/pytest.ini',
        '-p', 'no:cacheprovider', '--tb=short', '-q', '--junitxml=/tmp/junit.xml',
        *(['/workspace/' + p for p in targets] if targets else ['/workspace'])]
    measured = importlib.util.find_spec('coverage') is not None
    if measured:
        # Import the trusted coverage package in isolated mode before adding
        # repository import paths. The target cannot shadow the measurement tool.
        command = [sys.executable, '-I', '-c',
                   "import coverage,sys; from coverage.cmdline import main; sys.path[:0]=['/workspace','/workspace/src']; sys.exit(main())",
                   'run', '--branch', '--source=/workspace', '--data-file=/tmp/.coverage', '-m', 'pytest', *pytest_args]
    else:
        command = [sys.executable, '-m', 'pytest', *pytest_args]
    process = subprocess.Popen(command, cwd='/workspace', env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = bytearray()
    truncated = False
    def drain():
        nonlocal truncated
        while chunk := process.stdout.read(4096):
            remaining = max(0, limit - len(output))
            output.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining
    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    code = process.wait()
    thread.join(timeout=1)
    junit = Path('/tmp/junit.xml')
    xml = ''
    if junit.is_file() and not junit.is_symlink() and junit.stat().st_size <= 2_000_000:
        xml = junit.read_text(encoding='utf-8', errors='replace')
    log = output.decode('utf-8', errors='replace')
    if truncated:
        log += '\n[log truncated]'
    coverage_report = None
    if measured:
        exported = subprocess.run([sys.executable, '-I', '-m', 'coverage', 'json',
            '--data-file=/tmp/.coverage', '-o', '/tmp/coverage.json'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        coverage_path = Path('/tmp/coverage.json')
        if exported.returncode == 0 and coverage_path.is_file() and not coverage_path.is_symlink() and coverage_path.stat().st_size <= 2_000_000:
            coverage_report = json.loads(coverage_path.read_text(encoding='utf-8'))
    staging = RESULT.with_suffix('.tmp')
    staging.write_text(json.dumps({'exit_code': code, 'junit': xml, 'log': log, 'coverage': coverage_report}), encoding='utf-8')
    staging.replace(RESULT)
    # Keep tmpfs alive so host can read results without mounting writable host data.
    while True:
        time.sleep(1)


if __name__ == '__main__':
    raise SystemExit(main())
