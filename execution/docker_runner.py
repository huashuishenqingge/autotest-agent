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
from workflow.state import Policy, Repository, TestRun


@dataclass(frozen=True)
class ExecutionResult:
    run: TestRun
    artifact_directory: str
    cleanup_ok: bool


def stage_snapshot(root: Path, destination: Path, repository: Repository,
                   *, max_total_bytes: int = 10_000_000) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Copy the full approved inventory, no .git, config or arbitrary dependencies.

    Missing files fail closed instead of silently running a smaller test suite.
    Newly accepted tests must first be added to repository.test_files by the
    scheduler (dataclasses.replace); baseline revision stays unchanged.
    """
    root, destination = root.resolve(strict=True), destination.resolve(strict=True)
    # TemporaryDirectory defaults to 0700 on POSIX; the container uses nobody.
    destination.chmod(0o755)
    paths = repository.source_files + repository.test_files
    if len(paths) > 2000 or len({p.casefold() for p in paths}) != len(paths):
        raise ValueError('invalid or excessive inventory')
    entries = []
    total = 0
    for path in sorted(paths):
        target = checked_path(root, path)
        if not target.is_file() or not path.endswith('.py'):
            raise ValueError('approved Python file missing or unsupported')
        size = target.stat().st_size
        total += size
        if total > max_total_bytes or size > 1_000_000:
            raise ValueError('snapshot budget exceeded')
        with target.open('rb') as stream:
            data = stream.read(1_000_001)
        if len(data) != size:
            raise ValueError('file changed during snapshot')
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


def docker_command(snapshot: Path, name: str, image_id: str, policy: Policy) -> list[str]:
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise ValueError('reviewed immutable local image ID required')
    if not re.fullmatch(r'autotest-[0-9a-z]+', name):
        raise ValueError('invalid container name')
    if ',' in str(snapshot):
        raise ValueError('comma in mount path unsupported')
    return ['docker', 'run', '--detach', '--pull=never', '--name', name,
            '--network=none', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--user=65534:65534',
            '--pids-limit=64', '--memory=256m', '--memory-swap=256m', '--cpus=1',
            '--log-driver=none', '--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777',
            '--mount', f'type=bind,src={snapshot},dst=/workspace,readonly',
            '--workdir=/workspace', '--env=PYTHONDONTWRITEBYTECODE=1',
            '--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1', '--env=HOME=/tmp',
            image_id, 'python', '-I', '/opt/autotest/container_worker.py',
            str(policy.max_output_bytes)]


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return re.sub(r'(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', text)


def run_tests(root: Path, repository: Repository, *, image_id: str,
              artifact_root: Path, policy: Policy | None = None,
              secrets: tuple[str, ...] = (), docker_executable: str = 'docker') -> ExecutionResult:
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
    manifest = ()
    digest = 'unavailable'
    command = []
    container_may_exist = False
    with tempfile.TemporaryDirectory(prefix='autotest-snapshot-') as folder:
        snapshot = Path(folder)
        try:
            digest, manifest = stage_snapshot(Path(root), snapshot, repository)
            command = docker_command(snapshot, name, image_id, policy)
            command[0] = docker_executable
            container_may_exist = True
            launched = subprocess.run(command, capture_output=True, timeout=15, shell=False)
            if launched.returncode:
                log = 'Docker container launch failed; check engine and reviewed image'
            else:
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
                        code = report['exit_code']
                        if type(code) is not int:
                            raise ValueError('invalid exit code')
                        xml = report['junit'].encode('utf-8')
                        log = report['log']
                        if not isinstance(log, str) or len(xml) > 2_000_000:
                            raise ValueError('invalid report')
                        log = log.encode('utf-8')[:policy.max_output_bytes].decode('utf-8', errors='replace')
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
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
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
    log = _redact(log, secrets)
    sanitized_xml = _redact(xml.decode('utf-8', errors='replace'), secrets)
    (directory / 'log.txt').write_text(log, encoding='utf-8')
    (directory / 'junit.xml').write_text(sanitized_xml, encoding='utf-8')
    artifacts = tuple(f'{run_id}/{name}' for name in ('log.txt', 'junit.xml', 'manifest.json', 'run.json'))
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
