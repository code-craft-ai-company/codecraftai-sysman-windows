"""
    Tests for :class:`WindowsProcessManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell is invoked.

    The PowerShell argv shape is fixed at::

        [<ps>, "-NoProfile", "-NonInteractive", "-Command", <script>]

    so every keyed result entry includes the full argv tuple.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.processmanagement import (
    WindowsProcessActionStatus,
    WindowsProcessManager,
    WindowsProcessManagerConfig,
    WindowsProcessQueryError,
    WindowsProcessSpec,
)


_PS = "powershell.exe"
_NO_PROFILE = "-NoProfile"
_NON_INTERACTIVE = "-NonInteractive"
_COMMAND = "-Command"

_LIST_SELECT_FIELDS = (
    "Id,Name,Path,UserName,CPU,WorkingSet64,VirtualMemorySize64,"
    "StartTime,PriorityClass"
)
_J = "| ConvertTo-Json -Compress -Depth 5"


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "cwd": cwd,
            "env": env,
            "stdin": stdin,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=1,
            stdout="",
            stderr="not implemented",
        )
        return rtnval


def _ok(stdout: str = "") -> RunResult:
    """
        Build a canonical success :class:`RunResult` for the mock.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr="")
    return rtnval


def _fail(*, exit_code: int = 1, stderr: str = "boom") -> RunResult:
    """
        Build a canonical failure :class:`RunResult` for the mock.
    """
    rtnval = RunResult(exit_code=exit_code, stdout="", stderr=stderr)
    return rtnval


def _ps_argv(*, script: str) -> tuple:
    """
        Build the canonical PowerShell argv tuple for a scriptblock.
    """
    rtnval = (_PS, _NO_PROFILE, _NON_INTERACTIVE, _COMMAND, script)
    return rtnval


def _list_script() -> str:
    """
        Build the canonical Get-Process script for ``list_processes``.
    """
    rtnval = (
        "Get-Process | Select-Object {} {}"
        .format(_LIST_SELECT_FIELDS, _J)
    )
    return rtnval


def _get_script(*, pid: int) -> str:
    """
        Build the canonical Get-Process -Id script.
    """
    rtnval = (
        "Get-Process -Id {} | Select-Object {} {}"
        .format(pid, _LIST_SELECT_FIELDS, _J)
    )
    return rtnval


def _find_script(*, name: str) -> str:
    """
        Build the canonical Get-Process -Name script.
    """
    rtnval = (
        "Get-Process -Name '{}' -ErrorAction SilentlyContinue | "
        "Select-Object {} {}"
        .format(name, _LIST_SELECT_FIELDS, _J)
    )
    return rtnval


def _process_record(
    *,
    pid: int,
    name: str = "demo",
    user: str = "DOMAIN\\user",
    rss: int = 4096,
    vsz: int = 8192,
    cpu: float = 0.5,
    priority_class: str = "Normal",
) -> dict:
    """
        Build one Get-Process JSON record matching ``_LIST_SELECT_FIELDS``.
    """
    rtnval = {
        "Id": pid,
        "Name": name,
        "Path": "C:\\Windows\\{}.exe".format(name),
        "UserName": user,
        "CPU": cpu,
        "WorkingSet64": rss,
        "VirtualMemorySize64": vsz,
        "StartTime": "/Date(1700000000000)/",
        "PriorityClass": priority_class,
    }
    return rtnval


# ----------------------------------------------------------------------
# list_processes
# ----------------------------------------------------------------------


def test_list_processes_invokes_get_process_and_parses():
    """
        list_processes() shells out to ``Get-Process | Select-Object
        ... | ConvertTo-Json`` and parses the JSON array.
    """
    payload = json.dumps([
        _process_record(pid=1, name="System"),
        _process_record(pid=4242, name="explorer"),
    ])
    runner = _MockCommandRunner()
    runner.results[_ps_argv(script=_list_script())] = _ok(stdout=payload)

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )
    records = manager.list_processes()

    testbase.assert_equal(len(records), 2)
    testbase.assert_equal(records[0].pid, 1)
    testbase.assert_equal(records[0].command, "System")
    testbase.assert_equal(records[1].pid, 4242)
    testbase.assert_equal(records[1].command, "explorer")
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_process
# ----------------------------------------------------------------------


def test_get_process_parses_single_record():
    """
        get_process() runs Get-Process -Id and parses the single JSON
        object response.
    """
    payload = json.dumps(_process_record(pid=4242, name="python", user="alice"))
    runner = _MockCommandRunner()
    runner.results[_ps_argv(script=_get_script(pid=4242))] = _ok(
        stdout=payload,
    )

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )
    info = manager.get_process(pid=4242)

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.pid, 4242)
    testbase.assert_equal(info.command, "python")
    testbase.assert_equal(info.user, "alice")
    return


def test_get_process_returns_none_for_unknown_pid():
    """
        ``Get-Process -Id <unknown>`` exits non-zero with stderr
        containing 'Cannot find a process'.  get_process() maps that
        to ``None``.
    """
    runner = _MockCommandRunner()
    runner.results[_ps_argv(script=_get_script(pid=9999))] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Cannot find a process with the process identifier 9999.",
    )

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )
    info = manager.get_process(pid=9999)

    testbase.assert_true(info is None)
    return


# ----------------------------------------------------------------------
# kill_process
# ----------------------------------------------------------------------


def test_kill_process_invokes_stop_process():
    """
        kill_process() dispatches ``Stop-Process -Id <pid> -Force``.
    """
    runner = _MockCommandRunner()
    runner.results[_ps_argv(script=_get_script(pid=1234))] = _ok(
        stdout=json.dumps(_process_record(pid=1234)),
    )
    stop_script = "Stop-Process -Id 1234 -Force"
    runner.results[_ps_argv(script=stop_script)] = _ok()

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )
    result = manager.kill_process(pid=1234)

    testbase.assert_equal(result.status, WindowsProcessActionStatus.SUCCESS)
    testbase.assert_equal(result.pid, 1234)
    testbase.assert_equal(result.signal, "TERM")
    testbase.assert_equal(result.command[-1], stop_script)
    return


def test_kill_process_skipped_when_pid_not_found():
    """
        Idempotency: missing PID -> SKIPPED without invoking
        ``Stop-Process``.
    """
    runner = _MockCommandRunner()
    runner.results[_ps_argv(script=_get_script(pid=5555))] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Cannot find a process",
    )

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )
    result = manager.kill_process(pid=5555)

    testbase.assert_equal(result.status, WindowsProcessActionStatus.SKIPPED)
    stop_calls: list = []
    for record in runner.calls:
        if len(record["args"]) >= 5:
            script = record["args"][4]
            if script.startswith("Stop-Process"):
                stop_calls.append(record)
    testbase.assert_equal(len(stop_calls), 0)
    return


def test_kill_process_dry_run_short_circuits():
    """
        Dry-run config returns DRY_RUN without invoking the runner.
    """
    runner = _MockCommandRunner()
    config = WindowsProcessManagerConfig(dry_run=True)
    manager = WindowsProcessManager(runner, config)

    result = manager.kill_process(pid=1234, signal="KILL")

    testbase.assert_equal(result.status, WindowsProcessActionStatus.DRY_RUN)
    testbase.assert_equal(result.command[-1], "Stop-Process -Id 1234 -Force")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_kill_process_unmapped_signal_returns_failed():
    """
        Cross-OS signals other than TERM / KILL have no Windows
        mapping and MUST surface as FAILED without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )

    result = manager.kill_process(pid=4242, signal="HUP")

    testbase.assert_equal(result.status, WindowsProcessActionStatus.FAILED)
    testbase.assert_true("HUP" in result.detail)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# kill_process_tree
# ----------------------------------------------------------------------


def test_kill_process_tree_uses_taskkill_with_t_flag():
    """
        ``taskkill /F /T /PID`` performs the recursive tree kill in a
        single command on Windows.
    """
    runner = _MockCommandRunner()
    runner.results[_ps_argv(script=_get_script(pid=1000))] = _ok(
        stdout=json.dumps(_process_record(pid=1000)),
    )
    tree_script = "taskkill /F /T /PID 1000"
    runner.results[_ps_argv(script=tree_script)] = _ok()

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )
    result = manager.kill_process_tree(pid=1000)

    testbase.assert_equal(result.status, WindowsProcessActionStatus.SUCCESS)
    testbase.assert_equal(result.command[-1], tree_script)
    return


# ----------------------------------------------------------------------
# start_process
# ----------------------------------------------------------------------


def test_start_process_invokes_start_process_with_passthru():
    """
        start_process() shells out to ``Start-Process -FilePath ...
        -PassThru | Select-Object Id | ConvertTo-Json`` and recovers
        the spawned PID.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(stdout=json.dumps({"Id": 7777}))

    spec = WindowsProcessSpec(
        executable="C:\\Windows\\notepad.exe",
        args=[],
    )

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )
    result = manager.start_process(spec=spec)

    testbase.assert_equal(result.status, WindowsProcessActionStatus.SUCCESS)
    testbase.assert_equal(result.pid, 7777)
    testbase.assert_true("Start-Process" in result.command[-1])
    testbase.assert_true("-PassThru" in result.command[-1])
    return


def test_start_process_dry_run_short_circuits():
    """
        Dry-run start_process() returns DRY_RUN without calling the
        runner.
    """
    runner = _MockCommandRunner()
    config = WindowsProcessManagerConfig(dry_run=True)
    manager = WindowsProcessManager(runner, config)

    spec = WindowsProcessSpec(executable="C:\\Windows\\notepad.exe")
    result = manager.start_process(spec=spec)

    testbase.assert_equal(result.status, WindowsProcessActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# set_process_priority
# ----------------------------------------------------------------------


def test_set_process_priority_invokes_priorityclass_setter():
    """
        set_process_priority() dispatches
        ``(Get-Process -Id <pid>).PriorityClass = '<class>'`` after
        confirming the existing class differs.
    """
    runner = _MockCommandRunner()
    runner.results[_ps_argv(script=_get_script(pid=1234))] = _ok(
        stdout=json.dumps(_process_record(pid=1234, priority_class="Normal")),
    )
    set_script = "(Get-Process -Id 1234).PriorityClass = 'BelowNormal'"
    runner.results[_ps_argv(script=set_script)] = _ok()

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )
    # priority=10 in the default map points to BelowNormal.
    result = manager.set_process_priority(pid=1234, priority=10)

    testbase.assert_equal(result.status, WindowsProcessActionStatus.SUCCESS)
    testbase.assert_equal(result.command[-1], set_script)
    return


def test_set_process_priority_skipped_when_already_matches():
    """
        Idempotency: skip when the current PriorityClass already
        equals the mapped class.
    """
    runner = _MockCommandRunner()
    runner.results[_ps_argv(script=_get_script(pid=1234))] = _ok(
        stdout=json.dumps(
            _process_record(pid=1234, priority_class="BelowNormal"),
        ),
    )

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )
    result = manager.set_process_priority(pid=1234, priority=10)

    testbase.assert_equal(result.status, WindowsProcessActionStatus.SKIPPED)
    setter_calls: list = []
    for record in runner.calls:
        if len(record["args"]) >= 5:
            script = record["args"][4]
            if script.startswith("(Get-Process"):
                setter_calls.append(record)
    testbase.assert_equal(len(setter_calls), 0)
    return


# ----------------------------------------------------------------------
# find_processes_by_name
# ----------------------------------------------------------------------


def test_find_processes_by_name_parses_get_process_output():
    """
        find_processes_by_name() runs ``Get-Process -Name`` and parses
        the JSON array.
    """
    payload = json.dumps([
        _process_record(pid=100, name="nginx"),
        _process_record(pid=101, name="nginx"),
    ])
    runner = _MockCommandRunner()
    runner.results[_ps_argv(script=_find_script(name="nginx"))] = _ok(
        stdout=payload,
    )

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )
    records = manager.find_processes_by_name(name="nginx")

    testbase.assert_equal(len(records), 2)
    testbase.assert_equal(records[0].pid, 100)
    testbase.assert_equal(records[0].command, "nginx")
    testbase.assert_equal(records[1].pid, 101)
    return


# ----------------------------------------------------------------------
# Failure paths
# ----------------------------------------------------------------------


def test_failed_command_returns_failed_or_raises_on_read():
    """
        Reads raise :class:`WindowsProcessQueryError` on unexpected
        non-zero exit; writes return FAILED.
    """
    runner = _MockCommandRunner()
    runner.results[_ps_argv(script=_list_script())] = _fail(
        exit_code=2, stderr="ps explosion",
    )

    manager = WindowsProcessManager(
        runner, WindowsProcessManagerConfig(),
    )

    raised = False
    try:
        manager.list_processes()
    except WindowsProcessQueryError as ex:
        raised = True
        testbase.assert_equal(ex.exit_code, 2)

    testbase.assert_true(raised)

    # Write side: ``Stop-Process`` itself fails after a successful
    # probe.
    runner_w = _MockCommandRunner()
    runner_w.results[_ps_argv(script=_get_script(pid=1234))] = _ok(
        stdout=json.dumps(_process_record(pid=1234)),
    )
    runner_w.results[_ps_argv(
        script="Stop-Process -Id 1234 -Force",
    )] = _fail(exit_code=1, stderr="access denied")

    manager_w = WindowsProcessManager(
        runner_w, WindowsProcessManagerConfig(),
    )
    result = manager_w.kill_process(pid=1234)
    testbase.assert_equal(result.status, WindowsProcessActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_true("access denied" in result.stderr)
    return


# ----------------------------------------------------------------------
# Read elevation discipline
# ----------------------------------------------------------------------


def test_reads_never_elevate():
    """
        Even when ``config.elevated=True``, every read passes
        ``elevated=False`` to the runner.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(stdout="[]")
    runner.results[_ps_argv(script=_list_script())] = _ok(stdout="[]")
    runner.results[_ps_argv(script=_get_script(pid=1))] = _ok(
        stdout=json.dumps(_process_record(pid=1, name="System")),
    )
    runner.results[_ps_argv(script=_find_script(name="System"))] = _ok(
        stdout=json.dumps([_process_record(pid=1, name="System")]),
    )

    config = WindowsProcessManagerConfig(elevated=True)
    manager = WindowsProcessManager(runner, config)

    manager.list_processes()
    manager.get_process(pid=1)
    manager.find_processes_by_name(name="System")

    elevated_calls: list = []
    for record in runner.calls:
        if record["elevated"] is True:
            elevated_calls.append(record)

    testbase.assert_equal(len(elevated_calls), 0)
    return
