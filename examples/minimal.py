"""Run from package root: python -m examples.minimal"""
from workflow.state import (
    GenerateInput, GenerateOutput, FileProposal, Repository, Policy,
    WorkflowState, Phase, TestRun, advance, validate_proposal,
    RepairInput, RepairOutput, Failure, FailureKind,
)


class DemoGenerator:
    def generate(self, request: GenerateInput) -> GenerateOutput:
        return GenerateOutput(request.task_id, request.repository.revision,
            (FileProposal("tests/generated/test_add.py",
                "from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"),),
            rationale="Check the documented addition behavior")


class DemoRepairer:
    def repair(self, request: RepairInput) -> RepairOutput:
        import hashlib
        path, current_text = request.source_texts[0]
        return RepairOutput(request.task_id, request.repository.revision,
            request.failed_run.run_id,
            (FileProposal(path, "def add(a, b): return a + b\n",
                          hashlib.sha256(current_text.encode("utf-8")).hexdigest()),),
            diagnosis="The implementation subtracts one from the expected sum")


def main():
    repo = Repository("demo", "snapshot-demo-v1", ("app.py",))
    state = advance(WorkflowState("task-001"), Phase.PARSED, repository=repo)
    request = GenerateInput(state.task_id, repo, Policy(),
                            ("add returns the sum",), (("app.py", "def add(a, b): return a + b\n"),))
    output = DemoGenerator().generate(request)
    validate_proposal(request, output)
    state = advance(state, Phase.TESTS_PROPOSED)
    # Integration point: isolated executor writes proposal and runs the full suite.
    # This example uses synthetic evidence and does not claim actual test execution.
    state = advance(state, Phase.TESTED,
        run=TestRun("demo-run", "synthetic-applied-snapshot", 0, 1, 0, 0))
    state = advance(state, Phase.VERIFIED)
    print("Synthetic protocol demonstration:", state.phase.value)
    failed = TestRun("failed-demo", "synthetic-buggy-snapshot", 1, 0, 1, 0,
                    failure=Failure(FailureKind.TEST_ASSERTION, "expected 5, got 4"))
    repair_request = RepairInput("task-002", repo, Policy(), failed, 1,
        (("app.py", "def add(a, b): return a + b - 1\n"),),
        "test_add: assert add(2, 3) == 5; actual result: 4")
    repair_output = DemoRepairer().repair(repair_request)
    validate_proposal(repair_request, repair_output)
    print("Synthetic repair proposal validated:", repair_output.files[0].path)


if __name__ == "__main__":
    main()
