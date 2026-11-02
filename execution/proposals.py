"""Transactional file changes on a scheduler-owned, quiescent working copy.

Never apply to a live user repository or a concurrently writable untrusted tree.
The executor runs only a separate read-only copy; no other writer may share this
workspace. Path rechecks cannot replace that ownership requirement on Windows.
"""
from dataclasses import dataclass
import difflib
import hashlib
import os
from pathlib import Path
import stat
import tempfile

from workflow.state import FailureKind, relative_path, validate_proposal


class ApplyError(ValueError):
    def __init__(self, kind: FailureKind, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Change:
    path: str
    before: bytes | None
    after: bytes


@dataclass(frozen=True)
class AppliedPatch:
    root: str
    changes: tuple[Change, ...]
    diff: str


def checked_path(root: Path, value: str) -> Path:
    relative_path(value)
    current = root
    for part in value.split('/'):
        current = current / part
        if current.is_symlink():
            raise ApplyError(FailureKind.SECURITY, 'symbolic link rejected')
        if current.exists():
            info = current.lstat()
            if getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ApplyError(FailureKind.SECURITY, 'reparse point rejected')
    if not current.resolve().is_relative_to(root):
        raise ApplyError(FailureKind.SECURITY, 'path escapes working copy')
    return current


def _read(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ApplyError(FailureKind.SECURITY, 'regular file required')
    return path.read_bytes()


def _replace_file(root: Path, path: str, data: bytes | None) -> None:
    target = checked_path(root, path)
    if data is None:
        target.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    checked_path(root, path)
    fd, staging = tempfile.mkstemp(prefix='.autotest-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        checked_path(root, path)
        os.replace(staging, target)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


def _transaction(root: Path, operations: tuple[tuple[str, bytes | None, bytes | None], ...]) -> None:
    for path, expected, _ in operations:
        if _read(checked_path(root, path)) != expected:
            raise ApplyError(FailureKind.PATCH_CONFLICT, 'working copy changed')
    applied = []
    try:
        for path, expected, replacement in operations:
            if _read(checked_path(root, path)) != expected:
                raise ApplyError(FailureKind.PATCH_CONFLICT, 'working copy changed')
            _replace_file(root, path, replacement)
            applied.append((path, expected))
    except (OSError, ValueError) as exc:
        try:
            for path, before in reversed(applied):
                _replace_file(root, path, before)
        except (OSError, ValueError) as rollback_error:
            raise ApplyError(FailureKind.INTERNAL, 'rollback failed; quarantine working copy') from rollback_error
        if isinstance(exc, ApplyError):
            raise
        raise ApplyError(FailureKind.INTERNAL, 'write failed; changes rolled back') from exc


def apply_proposal(root: Path, request, output) -> AppliedPatch:
    validate_proposal(request, output)
    if output.failure:
        raise ApplyError(output.failure.kind, 'failed agent output cannot be applied')
    root = Path(root).resolve(strict=True)
    changes = []
    diffs = []
    for item in output.files:
        before = _read(checked_path(root, item.path))
        actual_hash = hashlib.sha256(before).hexdigest() if before is not None else None
        if (item.expected_sha256 is None and before is not None) or actual_hash != item.expected_sha256:
            raise ApplyError(FailureKind.PATCH_CONFLICT, 'file existence or hash precondition failed')
        after = item.content.encode('utf-8')
        changes.append(Change(item.path, before, after))
        diffs.extend(difflib.unified_diff(
            (before or b'').decode('utf-8-sig').splitlines(keepends=True),
            item.content.splitlines(keepends=True),
            fromfile='a/' + item.path if before is not None else '/dev/null', tofile='b/' + item.path))
    _transaction(root, tuple((c.path, c.before, c.after) for c in changes))
    return AppliedPatch(str(root), tuple(changes), ''.join(diffs))


def rollback(root: Path, receipt: AppliedPatch) -> None:
    root = Path(root).resolve(strict=True)
    if str(root) != receipt.root:
        raise ApplyError(FailureKind.PATCH_CONFLICT, 'receipt belongs to another working copy')
    _transaction(root, tuple((c.path, c.after, c.before) for c in reversed(receipt.changes)))
