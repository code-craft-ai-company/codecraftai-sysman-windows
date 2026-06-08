"""
    Skeleton tests for the Windows execution aspects.

    Each aspect gets construction and one or two basic behaviour
    checks.  The intent is to anchor the public surface; deeper
    coverage will land alongside the SSH runner backend.
"""

from ccai import testbase

from ccai.sysman.windows.runners.aspects import (
    WindowsPersistenceAspect,
    WindowsPersistenceAspectConfig,
    WindowsRetryAspect,
    WindowsRetryAspectConfig,
    WindowsTimeoutAspect,
    WindowsTimeoutAspectConfig,
)
from ccai.sysman.windows.runners.models import (
    WindowsExecutionContext,
    WindowsRunResult,
)


def _empty_context() -> WindowsExecutionContext:
    """
        Build a fresh, neutral execution context for use in tests.
    """
    rtnval = WindowsExecutionContext(
        args=["echo"],
        elevated=False,
        cwd=None,
        env=None,
        timeout=None,
        transport="local",
        dry_run=False,
    )
    return rtnval


class _ListSink:
    """
        In-memory replacement for a JSON-line file sink.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        return

    def write(self, line: str) -> None:
        self.lines.append(line)
        return


def test_persistence_aspect_writes_record():
    """
        The persistence aspect writes one JSON line per execution.
    """
    sink = _ListSink()
    aspect = WindowsPersistenceAspect(
        WindowsPersistenceAspectConfig(sink=sink),
    )
    context = _empty_context()
    aspect.before_execute(context)
    result = WindowsRunResult(
        exit_code=0,
        stdout="hi",
        stderr="",
        command=["echo"],
    )
    aspect.after_execute(context, result)
    testbase.assert_equal(len(sink.lines), 1)
    testbase.assert_true("\"exit_code\": 0" in sink.lines[0])
    return


def test_retry_aspect_recommends_retry_on_known_exit_code():
    """
        The retry aspect recommends a retry when the exit code is in
        the configured retryable set and the attempt is below the
        ceiling.
    """
    aspect = WindowsRetryAspect(
        WindowsRetryAspectConfig(
            max_retries=3,
            retryable_exit_codes=[42],
        ),
    )
    result = WindowsRunResult(
        exit_code=42,
        stdout="",
        stderr="",
        command=["thing"],
    )
    decision = aspect.should_retry(result=result, attempt=0)
    testbase.assert_true(decision.should_retry)
    testbase.assert_true("42" in decision.reason)
    return


def test_retry_aspect_stops_after_max_retries():
    """
        The retry aspect stops recommending retries once
        ``max_retries`` is reached.
    """
    aspect = WindowsRetryAspect(
        WindowsRetryAspectConfig(max_retries=2, retryable_exit_codes=[1]),
    )
    result = WindowsRunResult(
        exit_code=1,
        stdout="",
        stderr="",
        command=["thing"],
    )
    decision = aspect.should_retry(result=result, attempt=2)
    testbase.assert_false(decision.should_retry)
    testbase.assert_true("max retries" in decision.reason)
    return


def test_retry_aspect_does_not_retry_on_elevation_block():
    """
        Elevation-blocked failures are not retryable: re-running the
        same command without first relaunching elevated will block
        again, so the retry aspect short-circuits.
    """
    aspect = WindowsRetryAspect(
        WindowsRetryAspectConfig(max_retries=3, retryable_exit_codes=[-1]),
    )
    result = WindowsRunResult(
        exit_code=-1,
        stdout="",
        stderr="Process is not elevated",
        command=["whoami"],
        elevation_blocked=True,
    )
    decision = aspect.should_retry(result=result, attempt=0)
    testbase.assert_false(decision.should_retry)
    testbase.assert_true("elevation" in decision.reason.lower())
    return


def test_timeout_aspect_applies_default_timeout():
    """
        The timeout aspect injects the configured default when no
        timeout is set on the context.
    """
    aspect = WindowsTimeoutAspect(
        WindowsTimeoutAspectConfig(default_timeout=30.0),
    )
    context = _empty_context()
    aspect.before_execute(context)
    testbase.assert_equal(context.timeout, 30.0)
    return


def test_timeout_aspect_respects_caller_value_when_override_allowed():
    """
        With ``allow_override=True`` the caller's timeout wins.
    """
    aspect = WindowsTimeoutAspect(
        WindowsTimeoutAspectConfig(default_timeout=30.0, allow_override=True),
    )
    context = _empty_context()
    context.timeout = 5.0
    aspect.before_execute(context)
    testbase.assert_equal(context.timeout, 5.0)
    return


def test_timeout_aspect_forces_default_when_override_disallowed():
    """
        With ``allow_override=False`` the default always wins.
    """
    aspect = WindowsTimeoutAspect(
        WindowsTimeoutAspectConfig(default_timeout=10.0, allow_override=False),
    )
    context = _empty_context()
    context.timeout = 5.0
    aspect.before_execute(context)
    testbase.assert_equal(context.timeout, 10.0)
    return
