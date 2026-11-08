"""Validated transition journal used by the P06 orchestrator."""
from dataclasses import asdict

from workflow.state import (WorkflowState, Phase, Failure, FailureKind, advance,
                            validate_proposal, RepairInput)


class Journal:
    def __init__(self, task_id):
        self.state = WorkflowState(task_id)
        self.events = [asdict(self.state)]

    def _advance(self, phase, **kwargs):
        next_state = advance(self.state, phase, **kwargs)
        self.state = next_state
        self.events.append(asdict(next_state))

    def parsed(self, repository):
        self._advance(Phase.PARSED, repository=repository)

    def accept(self, request, output):
        if request.task_id != self.state.task_id or request.repository != self.state.repository:
            raise ValueError('proposal belongs to another workflow')
        validate_proposal(request, output)
        if output.failure:
            raise ValueError('failed proposal cannot advance workflow')
        self._advance(Phase.FIX_PROPOSED if isinstance(request, RepairInput) else Phase.TESTS_PROPOSED)

    def tested(self, run):
        self._advance(Phase.TESTED, run=run)

    def verify(self, independent_run):
        if self.state.phase != Phase.TESTED or not self.state.runs or not self.state.runs[-1].successful:
            raise ValueError('full visible tests must pass before independent validation')
        if not independent_run.successful:
            raise ValueError('independent validation must pass')
        # The independent digest includes withheld tests and therefore normally
        # differs from the visible digest. The orchestrator must bind its input
        # source hashes to the accepted visible workspace before invoking this.
        self._advance(Phase.VERIFIED)
        self.events[-1]['independent_run'] = asdict(independent_run)

    def fail(self, reason, kind=FailureKind.INTERNAL):
        self._advance(Phase.FAILED, failure=Failure(kind, reason))
