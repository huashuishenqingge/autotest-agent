"""Fixed-command Linux container runner; no local execution fallback.

Input is an explicitly approved source/test manifest. Tests can forge their own
reports, so these artifacts are execution feedback, not proof of semantic patch
correctness. Independent validation remains mandatory.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from execution.proposals import checked_path
from execution.results import classify_result
from workflow.state import Policy, Repository, TestRun, relative_path


@dataclass(frozen=True)
class ExecutionResult:
    run: TestRun
    artifact_directory: str
    cleanup_ok: bool


def stage_snapshot(root: Path, destination: Path, repository: Repository,
                   *, max_total_bytes: int = 10_000_000, support_files=()) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Copy the full approved inventory, no .git, config or arbitrary dependencies.

    Missing files fail closed instead of silently running a smaller test suite.
    Newly accepted tests must first be added to repository.test_files by the
    scheduler (dataclasses.replace); baseline revision stays unchanged.
    """
    root, destination = root.resolve(strict=True), destination.resolve(strict=True)
    # TemporaryDirectory defaults to 0700 on POSIX; the container uses nobody.
    destination.chmod(0o755)
    support_files = tuple(support_files)
    paths = repository.source_files + repository.test_files + support_files
    if len(paths) > 2000 or len({p.casefold() for p in paths}) != len(paths):
        raise ValueError('invalid or excessive inventory')
    entries = []
    total = 0
    for path in sorted(paths):
        target = checked_path(root, path)
        is_support = path in support_files
        denied = {'.py', '.pyw', '.pyc', '.pyo', '.exe', '.dll', '.so', '.pyd', '.dylib',
                  '.sh', '.bash', '.ps1', '.bat', '.cmd', '.com', '.js', '.mjs', '.cjs',
                  '.jar', '.class', '.wasm', '.pth', '.ipynb'}
        if (not target.is_file() or (not is_support and not path.endswith('.py'))
                or (is_support and Path(path).suffix.lower() in denied)):
            raise ValueError('approved Python file missing or unsupported')
        size = target.stat().st_size
        total += size
        if total > max_total_bytes or size > 1_000_000:
            raise ValueError('snapshot budget exceeded')
        with target.open('rb') as stream:
            data = stream.read(1_000_001)
        if len(data) != size:
            raise ValueError('file changed during snapshot')
        if is_support and data.startswith((b'MZ', b'\x7fELF', b'#!', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf')):
            raise ValueError('executable cannot be declared as support data')
        entries.append((path, data))
    digest = hashlib.sha256()
    manifest = []
    for path, data in entries:
        target = checked_path(destination, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        current = target.parent
        while current != destination:
            current.chmod(0o755)
            current = current.parent
        target.write_bytes(data)
        target.chmod(0o444)
        hashed = hashlib.sha256(data).hexdigest()
        digest.update(path.encode() + b'\0' + hashed.encode() + b'\0')
        manifest.append((path, hashed))
    return digest.hexdigest(), tuple(manifest)


def docker_command(snapshot: Path, name: str, image_id: str, policy: Policy, *, test_targets=()) -> list[str]:
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise ValueError('reviewed immutable local image ID required')
    if not re.fullmatch(r'autotest-[0-9a-z]+', name):
        raise ValueError('invalid container name')
    if ',' in str(snapshot):
        raise ValueError('comma in mount path unsupported')
    test_targets = tuple(test_targets)
    if len(set(p.casefold() for p in test_targets)) != len(test_targets):
        raise ValueError('duplicate collection targets')
    for path in test_targets:
        relative_path(path)
        if not path.endswith('.py'):
            raise ValueError('collection targets must be explicit Python files')
    command = ['docker', 'run', '--detach', '--pull=never', '--name', name,
            '--network=none', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--user=65534:65534',
            '--pids-limit=64', '--memory=256m', '--memory-swap=256m', '--cpus=1',
            '--log-driver=none', '--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777',
            '--mount', f'type=bind,src={snapshot},dst=/workspace,readonly',
            '--workdir=/workspace', '--env=PYTHONDONTWRITEBYTECODE=1',
            '--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1', '--env=HOME=/tmp',
            image_id, 'python', '-I', '/opt/autotest/container_worker.py',
            str(policy.max_output_bytes)]
    if test_targets:
        command.append(json.dumps(test_targets))
    return command


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return re.sub(r'(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', text)


def sanitized_junit(xml: bytes, secrets: tuple[str, ...]) -> bytes:
    """Redact decoded XML values without treating markup as log text."""
    import xml.etree.ElementTree as ET
    if len(xml) > 2_000_000 or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
        return b''
    try:
        root = ET.fromstring(xml)
        for node in root.iter():
            for key, value in node.attrib.items():
                node.set(key, _redact(value, secrets))
            if node.text:
                node.text = _redact(node.text, secrets)
            if node.tail:
                node.tail = _redact(node.tail, secrets)
        result = ET.tostring(root, encoding='utf-8')
        # Credentials in element/attribute names cannot be replaced safely.
        if len(result) > 2_000_000 or any(secret and secret in result.decode('utf-8') for secret in secrets):
            return b''
        return result
    except (ET.ParseError, ValueError, RecursionError):
        return b''


def bounded_log(text: str, limit: int) -> str:
    """Reserve space for a marker; never split a UTF-8 character."""
    if limit <= 0:
        raise ValueError('positive log budget required')
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text
    marker = b'\n[log truncated]'
    if limit < len(marker):
        return marker[-limit:].decode('ascii')
    return raw[:limit - len(marker)].decode('utf-8', errors='ignore') + marker.decode('ascii')


def run_tests(root: Path, repository: Repository, *, image_id: str,
              artifact_root: Path, policy: Policy | None = None,
              secrets: tuple[str, ...] = (), docker_executable: str = 'docker', support_files=(), test_targets=()) -> ExecutionResult:
    policy = policy or Policy()
    run_id = uuid.uuid4().hex
    directory = artifact_root.resolve() / run_id
    directory.mkdir(parents=True, exist_ok=False)
    name = 'autotest-' + run_id
    xml = b''
    code = 125
    timed_out = False
    log = ''
    cleanup_ok = True
    coverage_report = None
    manifest = ()
    digest = 'unavailable'
    command = []
    container_may_exist = False
    with tempfile.TemporaryDirectory(prefix='autotest-snapshot-') as folder:
        snapshot = Path(folder)
        try:
            test_targets = tuple(test_targets)
            if not set(test_targets).issubset(repository.test_files):
                raise ValueError('collection targets not in approved test inventory')
            digest, manifest = stage_snapshot(Path(root), snapshot, repository, support_files=support_files)
            command = docker_command(snapshot, name, image_id, policy, test_targets=test_targets)
            command[0] = docker_executable
            container_may_exist = True
            launched = subprocess.run(command, capture_output=True, timeout=15, shell=False)
            if launched.returncode:
                log = 'Docker container launch failed; check engine and reviewed image'
            else:
                if test_targets:
                    # Older reviewed images ignore extra arguments. Do not infer
                    # targeted collection from a successful legacy report.
                    fingerprint_code = ("import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('/opt/autotest/container_worker.py')"
                                        ".read_bytes().replace(b'\\r\\n',b'\\n')).hexdigest())")
                    fingerprint = subprocess.run([docker_executable, 'exec', name, 'python', '-I', '-c', fingerprint_code],
                                                 capture_output=True, timeout=5, shell=False)
                    expected = hashlib.sha256(Path(__file__).with_name('container_worker.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest().encode()
                    if fingerprint.returncode or fingerprint.stdout.strip() != expected:
                        raise ValueError('target collection requires current reviewed worker')
                deadline = time.monotonic() + policy.timeout_seconds
                while time.monotonic() < deadline:
                    probe = subprocess.run([docker_executable, 'exec', name, 'python', '-I',
                        '/opt/autotest/container_worker.py', '--read'], capture_output=True,
                        timeout=min(5, max(0.1, deadline - time.monotonic())), shell=False)
                    if probe.returncode == 0:
                        # Container worker enforces these bounds too. Reject invalid
                        # envelopes; never infer success from a log substring.
                        if len(probe.stdout) > 12_000_000:
                            raise ValueError('result envelope exceeds budget')
                        report = json.loads(probe.stdout)
                        coverage_report = report.get('coverage')
                        code = report['exit_code']
                        if type(code) is not int:
                            raise ValueError('invalid exit code')
                        xml = report['junit'].encode('utf-8')
                        log = report['log']
                        if not isinstance(log, str) or len(xml) > 2_000_000:
                            raise ValueError('invalid report')
                        break
                    if probe.returncode != 3:
                        log = 'Container stopped or result could not be read'
                        break
                    time.sleep(0.2)
                else:
                    timed_out = True
                    code = 124
                    log = 'Container time budget exhausted'
        except subprocess.TimeoutExpired:
            timed_out = True
            code = 124
            log = 'Docker operation exceeded time budget'
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
            code = 125
            log = 'Execution unavailable or invalid evidence; no host fallback was used'
        finally:
            if container_may_exist:
                try:
                    removal = subprocess.run([docker_executable, 'rm', '-f', name],
                        capture_output=True, timeout=10, shell=False)
                    # Missing container is expected after a launch failure.
                    cleanup_ok = removal.returncode == 0 or b'No such container' in removal.stderr
                except (OSError, subprocess.TimeoutExpired):
                    cleanup_ok = False
    # Raw logs and XML may contain repository secrets; persist sanitized text only.
    log = bounded_log(_redact(log, secrets), policy.max_output_bytes)
    xml = sanitized_junit(xml, secrets)
    if coverage_report is not None:
        try:
            coverage_text = json.dumps(coverage_report)
            if len(coverage_text.encode('utf-8')) <= 2_000_000:
                (directory / 'coverage.json').write_text(_redact(coverage_text, secrets), encoding='utf-8')
        except (TypeError, ValueError, RecursionError):
            pass  # No measurement evidence; coverage-required workflows fail closed.
    (directory / 'log.txt').write_text(log, encoding='utf-8')
    (directory / 'junit.xml').write_bytes(xml)
    artifacts = tuple(f'{run_id}/{name}' for name in ('log.txt', 'junit.xml', 'manifest.json', 'run.json'))
    if (directory / 'coverage.json').is_file():
        artifacts += (f'{run_id}/coverage.json',)
    run = classify_result(run_id, digest, code, xml, timed_out=timed_out, artifacts=artifacts)
    if not cleanup_ok and run.successful:
        run = classify_result(run_id, digest, 125, b'', artifacts=artifacts)
    (directory / 'manifest.json').write_text(json.dumps({
        'project_id': repository.project_id, 'base_revision': repository.revision,
        'image_id': image_id, 'files': manifest, 'command': command,
        'policy': asdict(policy), 'cleanup_ok': cleanup_ok,
    }, ensure_ascii=True, indent=2), encoding='utf-8')
    (directory / 'run.json').write_text(json.dumps(asdict(run), indent=2), encoding='utf-8')
    return ExecutionResult(run, str(directory), cleanup_ok)
