"""Conservative conversion of bounded pytest JUnit evidence to TestRun."""
import xml.etree.ElementTree as ET
from workflow.state import Failure, FailureKind, TestRun


def classify_result(run_id: str, digest: str, exit_code: int, junit: bytes,
                    *, timed_out: bool = False, artifacts: tuple[str, ...] = ()) -> TestRun:
    passed = failed = errors = skipped = 0
    kind = None
    if timed_out:
        kind = FailureKind.TIMEOUT
    elif exit_code in (125, 126, 127, 137):
        kind = FailureKind.ENVIRONMENT
    else:
        try:
            if len(junit) > 2_000_000 or b'<!DOCTYPE' in junit.upper() or b'<!ENTITY' in junit.upper():
                raise ValueError('unsafe XML')
            root = ET.fromstring(junit)
            if root.tag not in {'testsuite', 'testsuites'}:
                raise ValueError('not JUnit')
            assertions_only = True
            for case in root.iter('testcase'):
                if case.find('error') is not None:
                    errors += 1
                elif case.find('failure') is not None:
                    failed += 1
                    failure = case.find('failure')
                    detail = (failure.get('type', '') + ' ' + failure.get('message', '') + ' ' + (failure.text or ''))
                    assertions_only &= 'AssertionError' in detail or 'assert ' in detail
                elif case.find('skipped') is not None:
                    skipped += 1
                else:
                    passed += 1
            if exit_code == 2 or errors:
                kind = FailureKind.TEST_COLLECTION
            elif failed:
                kind = FailureKind.TEST_ASSERTION if assertions_only and exit_code == 1 else FailureKind.ENVIRONMENT
            elif exit_code or not passed:
                kind = FailureKind.ENVIRONMENT
        except (ET.ParseError, ValueError):
            kind = FailureKind.INVALID_OUTPUT if exit_code == 0 else FailureKind.ENVIRONMENT
    if kind:
        return TestRun(run_id, digest, exit_code or 1, passed, failed, errors, skipped,
                       Failure(kind, 'Run did not verify; inspect sanitized evidence', artifacts), artifacts)
    return TestRun(run_id, digest, 0, passed, failed, errors, skipped, artifacts=artifacts)
