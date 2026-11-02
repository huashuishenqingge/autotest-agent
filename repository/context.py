"""Build bounded context from the frozen parser snapshot, never re-read disk."""
from dataclasses import dataclass

from repository.parser import ParsedRepository
from workflow.state import GenerateInput, Policy, relative_path


@dataclass(frozen=True)
class ContextBundle:
    request: GenerateInput
    spans: tuple[tuple[str, int, int], ...]
    omitted_paths: tuple[str, ...]
    related_tests: tuple[tuple[str, str], ...]


def build_context(snapshot: ParsedRepository, *, task_id: str,
                  requirements: tuple[str, ...], targets: tuple[str, ...],
                  policy: Policy | None = None, max_context_bytes: int = 50000) -> ContextBundle:
    """Include whole target files first, direct local imports then related tests.

    Budget counts UTF-8 source/test contents, not the future model prompt. The
    model adapter must separately enforce its token and full-request budget.
    related_tests is read-only context outside GenerateInput.source_texts.
    """
    if not task_id or not requirements or any(not r.strip() for r in requirements):
        raise ValueError('task_id and independent requirements required')
    if not targets or max_context_bytes <= 0:
        raise ValueError('targets and positive budget required')
    by_path = {f.path: f for f in snapshot.files}
    targets = tuple(dict.fromkeys(targets))
    for path in targets:
        relative_path(path)
        if path not in snapshot.repository.source_files:
            raise ValueError('target is not inventoried source')
    target_modules = {by_path[p].module for p in targets}
    imports = {name for p in targets for name in by_path[p].imports}
    dependencies = [f.path for f in snapshot.files if f.kind == 'source' and f.module in imports and f.path not in targets]
    tests = [f.path for f in snapshot.files if f.kind == 'test' and
             (target_modules.intersection(f.imports) or any(_path_stem(p) in f.path for p in targets))]
    ordered = [*targets, *dependencies, *tests]
    used = 0
    accepted = []
    for path in ordered:
        size = len(by_path[path].text.encode('utf-8'))
        if used + size > max_context_bytes:
            if path in targets:
                raise ValueError('target source exceeds context budget')
            continue
        accepted.append(path)
        used += size
    policy = policy or Policy()
    request = GenerateInput(task_id, snapshot.repository, policy, tuple(requirements),
                            tuple((p, by_path[p].text) for p in accepted if by_path[p].kind == 'source'))
    return ContextBundle(request,
        tuple((p, 1, max(1, len(by_path[p].text.splitlines()))) for p in accepted),
        tuple(f.path for f in snapshot.files if f.path not in accepted),
        tuple((p, by_path[p].text) for p in accepted if by_path[p].kind == 'test'))


def _path_stem(path: str) -> str:
    return path.rsplit('/', 1)[-1].removesuffix('.py')
