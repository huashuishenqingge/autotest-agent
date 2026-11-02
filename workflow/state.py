"""Python 3.11+, standard library only. Agents propose; orchestrator executes.

Objects are immutable snapshots. Never deserialize untrusted data with pickle.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Protocol
import re

CONTRACT_VERSION = "1.0"


def relative_path(value: str) -> str:
    """Canonical portable repository path, deliberately stricter than POSIX."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("expected a portable relative path")
    if any(ord(c) < 32 for c in value) or value.startswith("/"):
        raise ValueError("absolute/control-character path")
    parts = value.split("/")
    if any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in parts):
        raise ValueError("noncanonical or traversal path")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if any(p.split(".")[0].upper() in reserved or any(c in p for c in '*?<>|"') for p in parts):
        raise ValueError("nonportable path")
    if any(p.lower() in {".git", ".ssh", ".aws", ".env"} for p in parts):
        raise ValueError("protected path")
    return value


def resolve_inside(root: Path, value: str) -> Path:
    """Check existing symlinks too. Recheck immediately before IO in executor."""
    base = root.resolve(strict=True)
    if not base.is_dir():
        raise ValueError("repository root is not a directory")
    result = (base / relative_path(value)).resolve()
    if not result.is_relative_to(base):
        raise ValueError("path escapes repository")
    return result


class Phase(str, Enum):
    CREATED = "created"
    PARSED = "parsed"
    TESTS_PROPOSED = "tests_proposed"
    TESTED = "tested"
    FIX_PROPOSED = "fix_proposed"
    VERIFIED = "verified"
    FAILED = "failed"


class FailureKind(str, Enum):
    PARSE_ERROR = "parse_error"
    INVALID_OUTPUT = "invalid_output"
    TEST_GENERATION = "test_generation"
    TEST_ASSERTION = "test_assertion"
    TEST_COLLECTION = "test_collection"
    ENVIRONMENT = "environment"
    TIMEOUT = "timeout"
    SECURITY = "security"
    PATCH_CONFLICT = "patch_conflict"
    REGRESSION = "regression"
    BUDGET = "budget"
    INTERNAL = "internal"


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    message: str  # sanitized; no credentials or complete environment dumps
    evidence: tuple[str, ...] = ()  # artifact IDs, not executable commands


@dataclass(frozen=True)
class Repository:
    project_id: str
    revision: str  # immutable baseline commit SHA or snapshot digest
    source_files: tuple[str, ...]
    test_files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()  # qualified symbols from repository parser

    def __post_init__(self):
        if not self.project_id or not self.revision:
            raise ValueError("project_id and immutable revision required")
        for path in self.source_files + self.test_files:
            relative_path(path)
        if set(self.source_files) & set(self.test_files):
            raise ValueError("source and test inventory overlap")


@dataclass(frozen=True)
class Policy:
    test_prefix: str = "tests/generated/"
    max_fix_attempts: int = 3
    timeout_seconds: int = 60
    max_output_bytes: int = 65536
    max_file_bytes: int = 100000

    def __post_init__(self):
        if not self.test_prefix.endswith("/"):
            raise ValueError("test_prefix must end with slash")
        relative_path(self.test_prefix[:-1])
        if min(self.max_fix_attempts, self.timeout_seconds,
               self.max_output_bytes, self.max_file_bytes) <= 0:
            raise ValueError("budgets must be positive")


@dataclass(frozen=True)
class FileProposal:
    path: str
    content: str  # complete UTF-8 text; no implicit deletes, renames or binary edits
    expected_sha256: str | None = None  # None means create ONLY if absent

    def __post_init__(self):
        relative_path(self.path)
        if self.expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", self.expected_sha256):
            raise ValueError("expected_sha256 must be a lowercase SHA-256")


@dataclass(frozen=True)
class TestRun:
    run_id: str
    snapshot_digest: str  # exact applied workspace, includes generated tests
    exit_code: int
    passed: int
    failed: int
    errors: int
    skipped: int = 0
    failure: Failure | None = None
    artifacts: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.run_id or not self.snapshot_digest or min(self.passed, self.failed, self.errors, self.skipped) < 0:
            raise ValueError("invalid run evidence")
        if self.exit_code == 0 and (self.failed or self.errors or self.failure):
            raise ValueError("inconsistent successful run")
        if self.exit_code != 0 and self.failure is None:
            raise ValueError("unsuccessful run requires classified failure")

    @property
    def successful(self) -> bool:
        return self.exit_code == 0 and self.passed > 0 and not self.failure


@dataclass(frozen=True)
class GenerateInput:
    task_id: str
    repository: Repository
    policy: Policy
    requirements: tuple[str, ...]
    # Content provider belongs to integration layer; inventory is not source text.
    source_texts: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class GenerateOutput:
    task_id: str
    base_revision: str
    files: tuple[FileProposal, ...] = ()
    rationale: str = ""
    failure: Failure | None = None


@dataclass(frozen=True)
class RepairInput:
    task_id: str
    repository: Repository
    policy: Policy
    failed_run: TestRun
    attempt: int  # 1-based
    source_texts: tuple[tuple[str, str], ...]
    sanitized_log: str


@dataclass(frozen=True)
class RepairOutput:
    task_id: str
    base_revision: str
    based_on_run_id: str
    files: tuple[FileProposal, ...] = ()
    diagnosis: str = ""
    failure: Failure | None = None


class TestGenerationAgent(Protocol):
    def generate(self, request: GenerateInput) -> GenerateOutput: ...


class DefectRepairAgent(Protocol):
    def repair(self, request: RepairInput) -> RepairOutput: ...


def validate_proposal(request: GenerateInput | RepairInput,
                      output: GenerateOutput | RepairOutput) -> None:
    """Call at trust boundary, before applying proposals. Raises ValueError."""
    generation = isinstance(request, GenerateInput)
    if generation != isinstance(output, GenerateOutput):
        raise ValueError("wrong agent output type")
    if output.task_id != request.task_id or output.base_revision != request.repository.revision:
        raise ValueError("stale or cross-task output")
    if isinstance(request, RepairInput):
        if not 1 <= request.attempt <= request.policy.max_fix_attempts:
            raise ValueError("repair budget exhausted")
        if request.failed_run.failure is None or request.failed_run.failure.kind != FailureKind.TEST_ASSERTION:
            raise ValueError("repair requires a reproducible assertion failure")
        if not isinstance(output, RepairOutput) or output.based_on_run_id != request.failed_run.run_id:
            raise ValueError("stale repair evidence")
    if output.failure is not None:
        if output.files:
            raise ValueError("failed output must not propose files")
        return
    if not output.files or len({f.path.casefold() for f in output.files}) != len(output.files):
        raise ValueError("empty or duplicate file proposals")
    for item in output.files:
        relative_path(item.path)
        if len(item.content.encode("utf-8")) > request.policy.max_file_bytes:
            raise ValueError("file exceeds budget")
        if generation:
            if not item.path.startswith(request.policy.test_prefix) or item.expected_sha256 is not None:
                raise ValueError("generator may only create new generated tests")
            if not PurePosixPath(item.path).name.startswith("test_") or not item.path.endswith(".py"):
                raise ValueError("expected a Python test module")
        elif item.path not in request.repository.source_files or item.expected_sha256 is None:
            raise ValueError("repair may only replace inventoried source with hash precondition")


@dataclass(frozen=True)
class WorkflowState:
    task_id: str
    phase: Phase = Phase.CREATED
    repository: Repository | None = None
    policy: Policy = field(default_factory=Policy)
    attempts: int = 0
    runs: tuple[TestRun, ...] = ()
    failure: Failure | None = None


def advance(state: WorkflowState, target: Phase, *, repository: Repository | None = None,
            run: TestRun | None = None, failure: Failure | None = None) -> WorkflowState:
    """Orchestrator-only reducer. Agent proposals must be validated separately."""
    edges = {
        Phase.CREATED: {Phase.PARSED}, Phase.PARSED: {Phase.TESTS_PROPOSED},
        Phase.TESTS_PROPOSED: {Phase.TESTED}, Phase.TESTED: {Phase.FIX_PROPOSED, Phase.VERIFIED},
        Phase.FIX_PROPOSED: {Phase.TESTED}, Phase.VERIFIED: set(), Phase.FAILED: set(),
    }
    if state.phase in (Phase.VERIFIED, Phase.FAILED):
        raise ValueError("terminal state")
    if target == Phase.FAILED:
        if failure is None:
            raise ValueError("failure evidence required")
        return replace(state, phase=target, failure=failure)
    if target not in edges[state.phase] or failure is not None:
        raise ValueError("illegal transition")
    if (repository is not None) != (target == Phase.PARSED):
        raise ValueError("repository supplied only when parsing completes")
    if (run is not None) != (target == Phase.TESTED):
        raise ValueError("run evidence supplied only for tested")
    if target == Phase.VERIFIED and (not state.runs or not state.runs[-1].successful):
        raise ValueError("verification requires successful nonempty full suite")
    if target == Phase.FIX_PROPOSED:
        if state.attempts >= state.policy.max_fix_attempts:
            raise ValueError("repair budget exhausted")
        if not state.runs or state.runs[-1].failure is None or state.runs[-1].failure.kind != FailureKind.TEST_ASSERTION:
            raise ValueError("only assertion failures enter repair")
    return replace(state, phase=target, repository=repository or state.repository,
                   attempts=state.attempts + int(target == Phase.FIX_PROPOSED),
                   runs=state.runs + ((run,) if run is not None else ()))


@dataclass(frozen=True)
class ProjectEvaluation:
    project_id: str
    revision: str
    generated_tests: int
    baseline: TestRun
    final: TestRun
    confirmed_defects: int
    fixed_defects: int
    elapsed_seconds: float

    def __post_init__(self):
        if not self.project_id or not self.revision or min(self.generated_tests, self.confirmed_defects,
                self.fixed_defects, self.elapsed_seconds) < 0 or self.fixed_defects > self.confirmed_defects:
            raise ValueError("invalid evaluation metrics")


def validate_five_projects(results: tuple[ProjectEvaluation, ...]) -> None:
    if len(results) != 5 or len({r.project_id for r in results}) != 5:
        raise ValueError("evaluation requires five distinct projects")
