"""
    Tests for :class:`WindowsLocalCommandRunner`.

    Tests use the real :mod:`subprocess` against harmless commands
    (``/bin/true``, ``/bin/false``, ``echo``, ``cat``) when those
    binaries are available on the host.  Where a deterministic outcome
    is hard to achieve in real subprocess (timeout, aspect ordering,
    elevation), :func:`unittest.mock.patch` is used instead.

    The test environment runs on macOS / Linux where the elevation
    probe naturally returns False; the elevation tests mock the
    detection helper to drive both the "elevated" and "not elevated"
    branches deterministically.
"""

import os
import subprocess

from unittest import mock

from ccai import testbase

from ccai.sysman.windows.runners.aspects.base import IWindowsExecutionAspect
from ccai.sysman.windows.runners.local import (
    WindowsLocalCommandRunner,
    WindowsLocalRunnerConfig,
)
from ccai.sysman.windows.runners.models import (
    WindowsExecutionContext,
    WindowsRunResult,
)


def _resolve_binary(*candidates: str) -> str:
    """
        Pick the first executable path from ``candidates`` that
        exists on the current host, or return an empty string when
        none are available.
    """
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


_ECHO = _resolve_binary("/bin/echo", "/usr/bin/echo")
_TRUE = _resolve_binary("/bin/true", "/usr/bin/true")
_FALSE = _resolve_binary("/bin/false", "/usr/bin/false")
_CAT = _resolve_binary("/bin/cat", "/usr/bin/cat")


def _has_binary(path: str) -> bool:
    """
        Return True when ``path`` is an executable file on disk.
    """
    if path is None or len(path) == 0:
        return False
    rtnval = os.path.isfile(path) and os.access(path, os.X_OK)
    return rtnval


def test_run_success_returns_zero_exit():
    """
        A command that exits zero must produce a result with
        ``exit_code == 0`` and ``success == True``.
    """
    if not _has_binary(_TRUE):
        return
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    result = runner.run([_TRUE])
    testbase.assert_equal(result.exit_code, 0)
    testbase.assert_true(result.success)
    testbase.assert_equal(result.transport, "local")
    testbase.assert_false(result.elevation_blocked)
    return


def test_run_failure_returns_nonzero_exit():
    """
        A command that exits non-zero must produce a result with a
        non-zero exit code and ``success == False``.
    """
    if not _has_binary(_FALSE):
        return
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    result = runner.run([_FALSE])
    testbase.assert_false(result.exit_code == 0)
    testbase.assert_false(result.success)
    return


def test_run_captures_stdout_and_stderr():
    """
        Stdout written by the child must appear in
        :attr:`WindowsRunResult.stdout`.
    """
    if not _has_binary(_ECHO):
        return
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    result = runner.run([_ECHO, "hello-windows"])
    testbase.assert_true("hello-windows" in result.stdout)
    testbase.assert_equal(result.stderr, "")
    return


def test_run_with_env_passes_env():
    """
        Caller-supplied env values must be visible to the child
        process when the runner merges with :data:`os.environ`.
    """
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    completed = subprocess.CompletedProcess(
        args=["env"], returncode=0, stdout="WINDOWS_TEST_VAR=42\n", stderr="",
    )
    with mock.patch("subprocess.run", return_value=completed) as run_mock:
        runner.run(["env"], env={"WINDOWS_TEST_VAR": "42"})
    env_passed = run_mock.call_args.kwargs["env"]
    testbase.assert_equal(env_passed["WINDOWS_TEST_VAR"], "42")
    return


def test_run_with_cwd_uses_cwd():
    """
        The cwd argument must be forwarded to :func:`subprocess.run`.
    """
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    completed = subprocess.CompletedProcess(
        args=["pwd"], returncode=0, stdout="/tmp\n", stderr="",
    )
    with mock.patch("subprocess.run", return_value=completed) as run_mock:
        runner.run(["pwd"], cwd="/tmp")
    testbase.assert_equal(run_mock.call_args.kwargs["cwd"], "/tmp")
    return


def test_run_with_stdin_passes_stdin():
    """
        Stdin payload must be piped to the child.
    """
    if not _has_binary(_CAT):
        return
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    result = runner.run([_CAT], stdin="windows-stdin-payload")
    testbase.assert_equal(result.exit_code, 0)
    testbase.assert_true("windows-stdin-payload" in result.stdout)
    return


def test_run_does_not_prepend_sudo_or_anything_when_elevated():
    """
        Unlike Ubuntu/macOS, Windows MUST NOT prepend any privilege
        command to the argv when ``elevated=True``.  The argv passes
        through unmodified.
    """
    completed = subprocess.CompletedProcess(
        args=["whoami"], returncode=0, stdout="ADMIN", stderr="",
    )
    with mock.patch.object(
        WindowsLocalCommandRunner, "_detect_elevation", return_value=True,
    ):
        with mock.patch("subprocess.run", return_value=completed) as run_mock:
            runner_admin = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
            runner_admin.run(["whoami"], elevated=True)
    actual_args = run_mock.call_args.args[0]
    testbase.assert_equal(actual_args, ["whoami"])
    return


def test_run_with_timeout_returns_timed_out_true():
    """
        A subprocess timeout must surface as ``timed_out=True`` and
        ``exit_code=-1``.
    """
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    expired = subprocess.TimeoutExpired(cmd=["sleep", "10"], timeout=1.0)
    with mock.patch("subprocess.run", side_effect=expired):
        result = runner.run(["sleep", "10"], timeout=1.0)
    testbase.assert_true(result.timed_out)
    testbase.assert_equal(result.exit_code, -1)
    testbase.assert_false(result.success)
    return


def test_run_with_on_output_callback_streams():
    """
        The ``on_output`` callback must receive the captured stdout.
    """
    chunks_out: list = []
    chunks_err: list = []
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    completed = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout="hi", stderr="warn",
    )
    with mock.patch("subprocess.run", return_value=completed):
        runner.run(
            ["echo"],
            on_output=chunks_out.append,
            on_error=chunks_err.append,
        )
    testbase.assert_equal(chunks_out, ["hi"])
    testbase.assert_equal(chunks_err, ["warn"])
    return


def test_run_without_capture_does_not_collect():
    """
        ``capture_output=False`` must not raise and must produce a
        result; ``stdout`` and ``stderr`` are empty under capture-off
        because the child writes to the parent's tty.
    """
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    completed = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout=None, stderr=None,
    )
    with mock.patch("subprocess.run", return_value=completed) as run_mock:
        result = runner.run(["echo", "off"], capture_output=False)
    testbase.assert_equal(result.exit_code, 0)
    testbase.assert_equal(result.stdout, "")
    testbase.assert_equal(result.stderr, "")
    testbase.assert_equal(run_mock.call_args.kwargs["capture_output"], False)
    return


def test_run_missing_executable_returns_nonzero():
    """
        A missing executable must produce a non-zero result rather
        than raising.
    """
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    result = runner.run(["/no/such/binary/zzz"])
    testbase.assert_false(result.exit_code == 0)
    testbase.assert_false(result.success)
    return


def test_run_dry_run_skips_subprocess():
    """
        Dry-run mode must not invoke :func:`subprocess.run`.
    """
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig(dry_run=True))
    with mock.patch("subprocess.run") as run_mock:
        result = runner.run(["rm", "-rf", "/tmp/junk"])
    testbase.assert_true(result.dry_run)
    testbase.assert_equal(result.exit_code, 0)
    run_mock.assert_not_called()
    return


def test_metrics_track_success_and_failure():
    """
        Successful and failed runs must each increment the
        appropriate metrics counter.
    """
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    ok = subprocess.CompletedProcess(
        args=["t"], returncode=0, stdout="", stderr="",
    )
    bad = subprocess.CompletedProcess(
        args=["f"], returncode=1, stdout="", stderr="bad",
    )
    with mock.patch("subprocess.run", side_effect=[ok, bad]):
        runner.run(["t"])
        runner.run(["f"])
    metrics = runner.get_metrics()
    testbase.assert_equal(metrics.success_count, 1)
    testbase.assert_equal(metrics.failure_count, 1)
    testbase.assert_equal(metrics.last_error, "bad")
    return


def test_aspect_invocation_order():
    """
        Aspects must run before-execute in registration order and
        after-execute in reverse.
    """
    events: list = []

    class _Recorder:
        enabled = True

        def __init__(self, name: str) -> None:
            self._name = name
            return

        def before_execute(self, context: WindowsExecutionContext) -> None:
            events.append(("before", self._name))
            return

        def after_execute(
            self,
            context: WindowsExecutionContext,
            result: WindowsRunResult,
        ) -> WindowsRunResult:
            events.append(("after", self._name))
            return result

    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    aspect_a = _Recorder(name="A")
    aspect_b = _Recorder(name="B")
    runner.add_aspect(aspect_a)
    runner.add_aspect(aspect_b)

    completed = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout="", stderr="",
    )
    with mock.patch("subprocess.run", return_value=completed):
        runner.run(["echo"])

    expected = [
        ("before", "A"),
        ("before", "B"),
        ("after", "B"),
        ("after", "A"),
    ]
    testbase.assert_equal(events, expected)
    testbase.assert_true(isinstance(aspect_a, IWindowsExecutionAspect))
    return


def test_elevation_check_at_construction():
    """
        Construction must call the elevation probe exactly once and
        cache the result on :attr:`is_elevated`.
    """
    with mock.patch.object(
        WindowsLocalCommandRunner,
        "_detect_elevation",
        return_value=False,
    ) as detect_mock:
        runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    testbase.assert_equal(detect_mock.call_count, 1)
    testbase.assert_false(runner.is_elevated)

    with mock.patch.object(
        WindowsLocalCommandRunner,
        "_detect_elevation",
        return_value=True,
    ) as detect_mock_admin:
        runner_admin = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
    testbase.assert_equal(detect_mock_admin.call_count, 1)
    testbase.assert_true(runner_admin.is_elevated)
    return


def test_run_with_elevated_returns_failed_when_not_elevated():
    """
        When the host is NOT elevated and ``elevated=True`` is
        requested, :meth:`run` must return a failure result with
        ``elevation_blocked=True`` and exit code -1, WITHOUT invoking
        :func:`subprocess.run`.
    """
    with mock.patch.object(
        WindowsLocalCommandRunner,
        "_detect_elevation",
        return_value=False,
    ):
        runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())

    with mock.patch("subprocess.run") as run_mock:
        result = runner.run(["whoami"], elevated=True)

    testbase.assert_true(result.elevation_blocked)
    testbase.assert_equal(result.exit_code, -1)
    testbase.assert_false(result.success)
    testbase.assert_true("not elevated" in result.stderr.lower())
    run_mock.assert_not_called()
    return


def test_run_with_elevated_succeeds_when_elevated():
    """
        When the host IS elevated, ``elevated=True`` must NOT block;
        the runner forwards the argv to :func:`subprocess.run`
        unchanged.
    """
    with mock.patch.object(
        WindowsLocalCommandRunner,
        "_detect_elevation",
        return_value=True,
    ):
        runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())

    completed = subprocess.CompletedProcess(
        args=["whoami"], returncode=0, stdout="ADMIN", stderr="",
    )
    with mock.patch("subprocess.run", return_value=completed) as run_mock:
        result = runner.run(["whoami"], elevated=True)

    testbase.assert_false(result.elevation_blocked)
    testbase.assert_equal(result.exit_code, 0)
    testbase.assert_true(result.success)
    forwarded_args = run_mock.call_args.args[0]
    testbase.assert_equal(forwarded_args, ["whoami"])
    return


def test_run_with_elevated_skips_check_when_skip_flag_set():
    """
        ``WindowsLocalRunnerConfig.skip_elevation_check=True`` must
        bypass the pre-check and forward the argv even when the host
        is not elevated.
    """
    with mock.patch.object(
        WindowsLocalCommandRunner,
        "_detect_elevation",
        return_value=False,
    ):
        runner = WindowsLocalCommandRunner(
            WindowsLocalRunnerConfig(skip_elevation_check=True),
        )

    completed = subprocess.CompletedProcess(
        args=["whoami"], returncode=0, stdout="USER", stderr="",
    )
    with mock.patch("subprocess.run", return_value=completed) as run_mock:
        result = runner.run(["whoami"], elevated=True)

    testbase.assert_false(result.elevation_blocked)
    testbase.assert_equal(result.exit_code, 0)
    run_mock.assert_called_once()
    return


def test_creation_flags_include_no_window_when_configured():
    """
        On Windows hosts, ``suppress_console_window=True`` must
        translate to ``CREATE_NO_WINDOW`` in the
        :func:`subprocess.run` call.

        On non-Windows hosts the runner forwards ``creationflags=0``;
        the test asserts that the flag value has the right shape and
        that it is forwarded as a kwarg.
    """
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig(
        suppress_console_window=True,
    ))
    completed = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout="", stderr="",
    )
    with mock.patch("platform.system", return_value="Windows"):
        no_window_value = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        with mock.patch.object(
            subprocess, "CREATE_NO_WINDOW", no_window_value, create=True,
        ):
            with mock.patch("subprocess.run", return_value=completed) as run_mock:
                runner.run(["echo"])
    forwarded_flags = run_mock.call_args.kwargs.get("creationflags")
    testbase.assert_equal(forwarded_flags, no_window_value)
    return


def test_creation_flags_zero_when_suppress_disabled():
    """
        ``suppress_console_window=False`` must force
        ``creationflags=0`` even on Windows.
    """
    runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig(
        suppress_console_window=False,
    ))
    completed = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout="", stderr="",
    )
    with mock.patch("platform.system", return_value="Windows"):
        with mock.patch("subprocess.run", return_value=completed) as run_mock:
            runner.run(["echo"])
    forwarded_flags = run_mock.call_args.kwargs.get("creationflags")
    testbase.assert_equal(forwarded_flags, 0)
    return


def test_elevation_block_count_increments_on_block():
    """
        Each blocked-elevation refusal must increment the metrics
        counter ``elevation_block_count``.
    """
    with mock.patch.object(
        WindowsLocalCommandRunner,
        "_detect_elevation",
        return_value=False,
    ):
        runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())

    runner.run(["whoami"], elevated=True)
    runner.run(["whoami"], elevated=True)

    metrics = runner.get_metrics()
    testbase.assert_equal(metrics.elevation_block_count, 2)
    testbase.assert_equal(metrics.failure_count, 2)
    return
