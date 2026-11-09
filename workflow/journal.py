"""Validated transition journal used by the P06 orchestrator."""
from dataclasses import asdict, replace
import re

from workflow.state import (WorkflowState, Phase, Failure, FailureKind, advance,
                            validate_proposal, RepairInput)


class Journal:
    def __init__(self, task_id):
        self.state = WorkflowState(task_id)
        self.events = [dict(asdict(self.state), event_type='created')]
        self._accepted_generation = None

    def _advance(self, phase, **kwargs):
        next_state = advance(self.state, phase, **kwargs)
        self.state = next_state
        self.events.append(dict(asdict(next_state), event_type='transition'))

    def parsed(self, repository):
        self._advance(Phase.PARSED, repository=repository)

    def accept(self, request, output):
        if request.task_id != self.state.task_id or request.repository != self.state.repository:
            raise ValueError('proposal belongs to another workflow')
        validate_proposal(request, output)
        if output.failure:
            raise ValueError('failed proposal cannot advance workflow')
        if isinstance(request, RepairInput) and (not self.state.runs or request.failed_run != self.state.runs[-1]
                                                or request.attempt != self.state.attempts + 1):
            raise ValueError('repair must use latest recorded failure and next attempt')
        self._advance(Phase.FIX_PROPOSED if isinstance(request, RepairInput) else Phase.TESTS_PROPOSED)
        if not isinstance(request, RepairInput):
            self._accepted_generation = output

    def tested(self, run):
        self._advance(Phase.TESTED, run=run)

    def generated_inventory(self, output):
        if self.state.phase != Phase.TESTS_PROPOSED or output != self._accepted_generation:
            raise ValueError('generation must be accepted before inventory update')
        repo = replace(self.state.repository, test_files=self.state.repository.test_files + tuple(f.path for f in output.files))
        self.state = replace(self.state, repository=repo)
        self.events.append(dict(asdict(self.state), event_type='inventory_update'))
        self._accepted_generation = None

    def observed_failure(self, run):
        if self.state.phase != Phase.TESTED or run.failure is None or run.failure.kind != FailureKind.TEST_ASSERTION:
            raise ValueError('only refreshed assertion evidence may update tested state')
        self.state = replace(self.state, runs=self.state.runs + (run,))
        self.events.append(dict(asdict(self.state), event_type='failure_refresh'))

    def verify(self, independent_run, *, visible_sources=None, independent_sources=None):
        if self.state.phase != Phase.TESTED or not self.state.runs or not self.state.runs[-1].successful:
            raise ValueError('full visible tests must pass before independent validation')
        if not independent_run.successful:
            raise ValueError('independent validation must pass')
        if (not visible_sources or visible_sources != independent_sources
                or set(visible_sources) != set(self.state.repository.source_files)
                or any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in visible_sources.values())):
            raise ValueError('independent validation must execute the same approved source bytes')
        # The independent digest includes withheld tests and therefore normally
        # differs from the visible digest. The orchestrator must bind its input
        # source hashes to the accepted visible workspace before invoking this.
        self._advance(Phase.VERIFIED)
        self.events[-1]['independent_run'] = asdict(independent_run)
        self.events[-1]['source_binding'] = {'visible': visible_sources, 'independent': independent_sources}

    def fail(self, reason, kind=FailureKind.INTERNAL):
        self._advance(Phase.FAILED, failure=Failure(kind, reason))
