"""Trusted image entrypoint. Reports stay on bounded tmpfs until host cleanup."""
import json
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
    process = subprocess.Popen([
        sys.executable, '-m', 'pytest', '-c', '/opt/autotest/pytest.ini',
        '-p', 'no:cacheprovider', '--tb=short', '-q', '--junitxml=/tmp/junit.xml',
        '/workspace'], cwd='/workspace', env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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
    staging = RESULT.with_suffix('.tmp')
    staging.write_text(json.dumps({'exit_code': code, 'junit': xml, 'log': log}), encoding='utf-8')
    staging.replace(RESULT)
    # Keep tmpfs alive so host can read results without mounting writable host data.
    while True:
        time.sleep(1)


if __name__ == '__main__':
    raise SystemExit(main())
