import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from workflow.state import *


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository("p1", "sha-baseline", ("src/app.py",))
        self.request = GenerateInput("t1", self.repo, Policy(), ("addition",), ())
        self.good = GenerateOutput("t1", "sha-baseline",
            (FileProposal("tests/generated/test_app.py", "assert True\n"),))
        self.bad_run = TestRun("r1", "snapshot-1", 1, 1, 1, 0,
            failure=Failure(FailureKind.TEST_ASSERTION, "expected 5, got 4"))

    def test_paths(self):
        for path in ("../x", "/x", "C:/x", "a\\b", "a//b", "a/./b",
                     ".git/config", "a/.env", "CON.txt", "x\x00"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                relative_path(path)
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(resolve_inside(Path(folder), "src/a.py"), Path(folder).resolve() / "src/a.py")

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            link = Path(root) / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable on this host")
            with self.assertRaises(ValueError):
                resolve_inside(Path(root), "escape/a.py")

    def test_generator_scope_and_identity(self):
        validate_proposal(self.request, self.good)
        for output in (replace(self.good, task_id="other"),
                       replace(self.good, base_revision="stale"),
                       replace(self.good, files=()),
                       replace(self.good, files=(FileProposal("src/app.py", "x"),)),
                       replace(self.good, files=self.good.files * 2),
                       replace(self.good, failure=Failure(FailureKind.INTERNAL, "oops"))):
            with self.assertRaises(ValueError):
                validate_proposal(self.request, output)

    def test_repair_protocol(self):
        request = RepairInput("t1", self.repo, Policy(), self.bad_run, 1, (), "assertion")
        output = RepairOutput("t1", "sha-baseline", "r1",
                              (FileProposal("src/app.py", "fixed", "a" * 64),), "off by one")
        validate_proposal(request, output)
        for bad in (replace(output, based_on_run_id="stale"),
                    replace(output, files=(FileProposal("src/app.py", "fixed"),)),
                    replace(output, files=(FileProposal("tests/test_x.py", "fixed", "a" * 64),))):
            with self.assertRaises(ValueError):
                validate_proposal(request, bad)
        with self.assertRaises(ValueError):
            validate_proposal(replace(request, attempt=4), output)

    def test_state_machine_and_budget(self):
        state = advance(WorkflowState("t1", policy=Policy(max_fix_attempts=1)),
                        Phase.PARSED, repository=self.repo)
        state = advance(state, Phase.TESTS_PROPOSED)
        state = advance(state, Phase.TESTED, run=self.bad_run)
        with self.assertRaises(ValueError):
            advance(state, Phase.VERIFIED)
        state = advance(state, Phase.FIX_PROPOSED)
        self.assertEqual(state.attempts, 1)
        state = advance(state, Phase.TESTED, run=self.bad_run)
        with self.assertRaises(ValueError):
            advance(state, Phase.FIX_PROPOSED)
        state = advance(state, Phase.FAILED, failure=Failure(FailureKind.BUDGET, "limit"))
        with self.assertRaises(ValueError):
            advance(state, Phase.PARSED, repository=self.repo)

    def test_success_and_empty_suite(self):
        for count in (0, 2):
            state = advance(WorkflowState("t1"), Phase.PARSED, repository=self.repo)
            state = advance(state, Phase.TESTS_PROPOSED)
            state = advance(state, Phase.TESTED, run=TestRun("ok", "snapshot", 0, count, 0, 0))
            if count:
                self.assertEqual(advance(state, Phase.VERIFIED).phase, Phase.VERIFIED)
            else:
                with self.assertRaises(ValueError):
                    advance(state, Phase.VERIFIED)

    def test_five_projects(self):
        run = TestRun("ok", "snapshot", 0, 1, 0, 0)
        results = tuple(ProjectEvaluation(f"p{i}", "revision", 1, run, run, 0, 0, 1.0) for i in range(5))
        validate_five_projects(results)
        for bad in (results[:4], (results[0],) * 5):
            with self.assertRaises(ValueError):
                validate_five_projects(bad)


if __name__ == "__main__":
    unittest.main()
