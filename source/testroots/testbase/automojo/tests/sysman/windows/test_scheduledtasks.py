"""
    Tests for :class:`WindowsScheduledTasksManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.scheduledtasks import (
    WindowsScheduledTaskActionResult,
    WindowsScheduledTaskActionStatus,
    WindowsScheduledTaskQueryError,
    WindowsScheduledTasksManager,
    WindowsScheduledTasksManagerConfig,
    WindowsTaskInfo,
    WindowsTaskOwner,
    WindowsTaskSpec,
    _parse_get_scheduledtask,
    _record_to_taskinfo,
    _render_action_fragment,
    _render_principal_fragment,
    _render_register_script,
    _render_trigger_fragment,
    _translate_schedule,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
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
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


# ----------------------------------------------------------------------
# Argv helpers
# ----------------------------------------------------------------------


def _build_powershell_argv(script: str) -> list:
    """
        Build the canonical PowerShell argv used by the manager.
    """
    rtnval = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]
    return rtnval


def _get_task_argv(name: str) -> list:
    """
        Argv for the per-task ``Get-ScheduledTask`` probe.
    """
    script = (
        "Get-ScheduledTask -TaskName '{}' "
        "| Select-Object TaskName, State, Actions, Triggers, "
        "Principal, Settings, LastRunTime, NextRunTime "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _list_tasks_argv() -> list:
    """
        Argv for the listing ``Get-ScheduledTask`` call.
    """
    script = (
        "Get-ScheduledTask "
        "| Select-Object TaskName, State, Actions, Triggers, "
        "Principal, Settings, LastRunTime, NextRunTime "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _seed_get_task(
    *,
    runner: _MockCommandRunner,
    name: str,
    record,
) -> None:
    """
        Seed a ``Get-ScheduledTask -TaskName <name>`` result.

        ``record`` may be a dict (becomes the JSON payload) or
        ``None`` (synthesises a "no task found" stderr).
    """
    argv = _get_task_argv(name=name)
    if record is None:
        runner.results[tuple(argv)] = RunResult(
            exit_code=1,
            stdout="",
            stderr="No MSFT_ScheduledTask objects found",
        )
        return
    payload = json.dumps(record)
    runner.results[tuple(argv)] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _system_record(*, name: str, executable: str, state: str = "Ready") -> dict:
    """
        Build a representative SYSTEM-owner ``Get-ScheduledTask``
        record for ``name``.
    """
    rtnval = {
        "TaskName": name,
        "State": state,
        "Actions": [{"Execute": executable, "Arguments": ""}],
        "Triggers": [{"CimClass": "MSFT_TaskDailyTrigger"}],
        "Principal": {"UserId": "NT AUTHORITY\\SYSTEM"},
        "Settings": {"Enabled": True},
    }
    return rtnval


def _user_record(
    *,
    name: str,
    executable: str,
    user: str,
    state: str = "Ready",
) -> dict:
    """
        Build a representative USER-owner record.
    """
    rtnval = {
        "TaskName": name,
        "State": state,
        "Actions": [{"Execute": executable, "Arguments": ""}],
        "Triggers": [{"CimClass": "MSFT_TaskDailyTrigger"}],
        "Principal": {"UserId": user},
        "Settings": {"Enabled": True},
    }
    return rtnval


# ----------------------------------------------------------------------
# Pure-function tests — schedule translation
# ----------------------------------------------------------------------


def test_translate_schedule_daily():
    """
        ``"daily 02:00"`` translates to a daily calendar dict.
    """
    rtnval = _translate_schedule(schedule="daily 02:00")
    testbase.assert_equal(rtnval["kind"], "daily")
    testbase.assert_equal(rtnval["time"], "02:00")
    return


def test_translate_schedule_every_minutes():
    """
        ``"every 5min"`` translates to a Minutes interval dict.
    """
    rtnval = _translate_schedule(schedule="every 5min")
    testbase.assert_equal(rtnval["kind"], "every")
    testbase.assert_equal(rtnval["unit"], "Minutes")
    testbase.assert_equal(rtnval["amount"], 5)
    return


def test_translate_schedule_every_hours():
    """
        ``"every 2hour"`` translates to an Hours interval dict.
    """
    rtnval = _translate_schedule(schedule="every 2hour")
    testbase.assert_equal(rtnval["kind"], "every")
    testbase.assert_equal(rtnval["unit"], "Hours")
    testbase.assert_equal(rtnval["amount"], 2)
    return


def test_translate_schedule_weekly():
    """
        ``"weekly Monday 03:30"`` translates to a weekly dict carrying
        the canonical day name.
    """
    rtnval = _translate_schedule(schedule="weekly Monday 03:30")
    testbase.assert_equal(rtnval["kind"], "weekly")
    testbase.assert_equal(rtnval["day"], "Monday")
    testbase.assert_equal(rtnval["time"], "03:30")
    return


def test_translate_schedule_rejects_invalid():
    """
        Unrecognised schedule strings raise ValueError.
    """
    raised = False
    try:
        _translate_schedule(schedule="never")
    except ValueError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_render_trigger_fragment_daily():
    """
        ``daily 02:00`` renders to a New-ScheduledTaskTrigger -Daily
        fragment.
    """
    rtnval = _render_trigger_fragment(
        schedule_dict={"kind": "daily", "time": "02:00"},
    )
    testbase.assert_equal(
        rtnval, "New-ScheduledTaskTrigger -Daily -At '02:00'",
    )
    return


def test_render_trigger_fragment_every_minutes():
    """
        ``every 5min`` renders to the ``-Once -RepetitionInterval``
        fragment.
    """
    rtnval = _render_trigger_fragment(
        schedule_dict={"kind": "every", "unit": "Minutes", "amount": 5},
    )
    expected = (
        "New-ScheduledTaskTrigger -Once -At (Get-Date) "
        "-RepetitionInterval (New-TimeSpan -Minutes 5) "
        "-RepetitionDuration ([TimeSpan]::MaxValue)"
    )
    testbase.assert_equal(rtnval, expected)
    return


def test_render_action_fragment_with_arguments():
    """
        ``_render_action_fragment`` joins argv tail into the
        ``-Argument`` payload.
    """
    spec = WindowsTaskSpec(
        name="t",
        command=["pwsh.exe", "-c", "Get-Date"],
        schedule="daily 02:00",
    )
    rtnval = _render_action_fragment(spec=spec)
    testbase.assert_equal(
        rtnval,
        "New-ScheduledTaskAction -Execute 'pwsh.exe' "
        "-Argument '-c Get-Date'",
    )
    return


def test_render_principal_fragment_system():
    """
        SYSTEM owner renders the SYSTEM principal fragment.
    """
    rtnval = _render_principal_fragment(
        owner=WindowsTaskOwner.SYSTEM,
        user=None,
        run_level="Highest",
    )
    expected = (
        "New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\\SYSTEM' "
        "-RunLevel Highest"
    )
    testbase.assert_equal(rtnval, expected)
    return


def test_render_principal_fragment_user_requires_user():
    """
        USER owner without a user raises ValueError.
    """
    raised = False
    try:
        _render_principal_fragment(
            owner=WindowsTaskOwner.USER,
            user=None,
            run_level="Highest",
        )
    except ValueError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_render_principal_fragment_user_with_user():
    """
        USER owner with a user renders an S4U principal fragment.
    """
    rtnval = _render_principal_fragment(
        owner=WindowsTaskOwner.USER,
        user="DOMAIN\\Alice",
        run_level="Limited",
    )
    expected = (
        "New-ScheduledTaskPrincipal -UserId 'DOMAIN\\Alice' "
        "-LogonType S4U -RunLevel Limited"
    )
    testbase.assert_equal(rtnval, expected)
    return


# ----------------------------------------------------------------------
# create_task
# ----------------------------------------------------------------------


def test_create_task_dispatches_register_script():
    """
        create_task composes the multi-step register script and
        dispatches it through the runner.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_get_task(runner=runner, name="my-task", record=None)
    spec = WindowsTaskSpec(
        name="my-task",
        command=["pwsh.exe", "-c", "Get-Date"],
        schedule="daily 02:00",
        owner=WindowsTaskOwner.SYSTEM,
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=spec)
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SUCCESS,
    )
    write_call = runner.calls[-1]
    write_script = write_call["args"][-1]
    testbase.assert_equal(
        "$action = New-ScheduledTaskAction -Execute 'pwsh.exe'" in write_script,
        True,
    )
    testbase.assert_equal(
        "$trigger = New-ScheduledTaskTrigger -Daily -At '02:00'" in write_script,
        True,
    )
    testbase.assert_equal(
        "Register-ScheduledTask -TaskName 'my-task'" in write_script,
        True,
    )
    return


def test_create_task_failed_when_schedule_invalid():
    """
        An unsupported schedule string returns FAILED with
        ``message="invalid_schedule"`` BEFORE the runner is invoked.
    """
    runner = _MockCommandRunner()
    spec = WindowsTaskSpec(
        name="my-task",
        command=["pwsh.exe"],
        schedule="never",
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=spec)
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "invalid_schedule")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_create_task_failed_when_user_owner_missing_user():
    """
        USER scope without a user (neither spec nor config) returns
        FAILED with ``message="user_required_for_user_scope"``.
    """
    runner = _MockCommandRunner()
    spec = WindowsTaskSpec(
        name="my-task",
        command=["pwsh.exe"],
        schedule="daily 02:00",
        owner=WindowsTaskOwner.USER,
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=spec)
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "user_required_for_user_scope")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_create_task_skipped_when_record_matches_spec():
    """
        create_task returns SKIPPED when an existing task's
        Actions[0].Execute and Principal.UserId already match the
        spec.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    record = _system_record(name="my-task", executable="pwsh.exe")
    record["Actions"][0]["Arguments"] = "-c Get-Date"
    _seed_get_task(runner=runner, name="my-task", record=record)
    spec = WindowsTaskSpec(
        name="my-task",
        command=["pwsh.exe", "-c", "Get-Date"],
        schedule="daily 02:00",
        owner=WindowsTaskOwner.SYSTEM,
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=spec)
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SKIPPED,
    )
    register_calls = [
        c for c in runner.calls
        if "Register-ScheduledTask" in c["args"][-1]
    ]
    testbase.assert_equal(register_calls, [])
    return


def test_create_task_user_scope_renders_s4u_principal():
    """
        USER scope renders an S4U principal fragment with the resolved
        user.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_get_task(runner=runner, name="alice-task", record=None)
    spec = WindowsTaskSpec(
        name="alice-task",
        command=["pwsh.exe"],
        schedule="every 5min",
        owner=WindowsTaskOwner.USER,
        user="DOMAIN\\Alice",
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=spec)
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SUCCESS,
    )
    write_script = runner.calls[-1]["args"][-1]
    testbase.assert_equal(
        "New-ScheduledTaskPrincipal -UserId 'DOMAIN\\Alice' "
        "-LogonType S4U" in write_script,
        True,
    )
    return


def test_create_task_dry_run_short_circuits():
    """
        Dry-run captures the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    spec = WindowsTaskSpec(
        name="my-task",
        command=["pwsh.exe"],
        schedule="daily 02:00",
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(dry_run=True),
    )
    result = manager.create_task(spec=spec)
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# delete_task / enable_task / disable_task / run_task_now
# ----------------------------------------------------------------------


def test_delete_task_dispatches_unregister():
    """
        delete_task on a registered task dispatches
        Unregister-ScheduledTask.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    record = _system_record(name="my-task", executable="pwsh.exe")
    _seed_get_task(runner=runner, name="my-task", record=record)
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.delete_task(
        name="my-task", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SUCCESS,
    )
    expected_script = (
        "Unregister-ScheduledTask -TaskName 'my-task' -Confirm:$false"
    )
    testbase.assert_equal(runner.calls[-1]["args"][-1], expected_script)
    return


def test_delete_task_skipped_when_absent():
    """
        delete_task on an unknown task returns SKIPPED.
    """
    runner = _MockCommandRunner()
    _seed_get_task(runner=runner, name="my-task", record=None)
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.delete_task(
        name="my-task", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SKIPPED,
    )
    return


def test_enable_task_failed_when_task_missing():
    """
        enable_task on an unknown task returns FAILED with
        ``message="task_not_found"``.
    """
    runner = _MockCommandRunner()
    _seed_get_task(runner=runner, name="my-task", record=None)
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.enable_task(
        name="my-task", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "task_not_found")
    return


def test_enable_task_skipped_when_already_enabled():
    """
        enable_task returns SKIPPED when the task is already in a
        non-disabled state.
    """
    runner = _MockCommandRunner()
    record = _system_record(
        name="my-task", executable="pwsh.exe", state="Ready",
    )
    _seed_get_task(runner=runner, name="my-task", record=record)
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.enable_task(
        name="my-task", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SKIPPED,
    )
    return


def test_enable_task_dispatches_enable_scheduledtask():
    """
        enable_task on a Disabled task dispatches Enable-ScheduledTask.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    record = _system_record(
        name="my-task", executable="pwsh.exe", state="Disabled",
    )
    record["Settings"]["Enabled"] = False
    _seed_get_task(runner=runner, name="my-task", record=record)
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.enable_task(
        name="my-task", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SUCCESS,
    )
    expected_script = "Enable-ScheduledTask -TaskName 'my-task'"
    testbase.assert_equal(runner.calls[-1]["args"][-1], expected_script)
    return


def test_disable_task_skipped_when_already_disabled():
    """
        disable_task returns SKIPPED when the task is already Disabled.
    """
    runner = _MockCommandRunner()
    record = _system_record(
        name="my-task", executable="pwsh.exe", state="Disabled",
    )
    record["Settings"]["Enabled"] = False
    _seed_get_task(runner=runner, name="my-task", record=record)
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.disable_task(
        name="my-task", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SKIPPED,
    )
    return


def test_run_task_now_dispatches_start_scheduledtask():
    """
        run_task_now dispatches Start-ScheduledTask unconditionally.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.run_task_now(
        name="my-task", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SUCCESS,
    )
    expected_script = "Start-ScheduledTask -TaskName 'my-task'"
    testbase.assert_equal(runner.calls[-1]["args"][-1], expected_script)
    return


# ----------------------------------------------------------------------
# get_task / list_tasks / validate_schedule
# ----------------------------------------------------------------------


def test_get_task_returns_taskinfo():
    """
        get_task returns a populated WindowsTaskInfo.
    """
    runner = _MockCommandRunner()
    record = _system_record(name="my-task", executable="pwsh.exe")
    _seed_get_task(runner=runner, name="my-task", record=record)
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    info = manager.get_task(
        name="my-task", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(info.name, "my-task")
    testbase.assert_equal(info.owner, WindowsTaskOwner.SYSTEM)
    testbase.assert_equal(info.state, "Ready")
    testbase.assert_equal(info.executable, "pwsh.exe")
    testbase.assert_equal(info.enabled, True)
    return


def test_get_task_returns_none_when_absent():
    """
        get_task returns None when Get-ScheduledTask reports the
        ``no MSFT_ScheduledTask objects found`` sentinel.
    """
    runner = _MockCommandRunner()
    _seed_get_task(runner=runner, name="missing", record=None)
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    info = manager.get_task(
        name="missing", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(info, None)
    return


def test_get_task_raises_on_unexpected_failure():
    """
        get_task raises WindowsScheduledTaskQueryError on an
        unrecognised non-zero exit.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_task_argv("my-task"))] = RunResult(
        exit_code=1, stdout="", stderr="some unexpected error",
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    raised = False
    try:
        manager.get_task(name="my-task", owner=WindowsTaskOwner.SYSTEM)
    except WindowsScheduledTaskQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_list_tasks_parses_array():
    """
        list_tasks parses the array shape into one info per record.
    """
    runner = _MockCommandRunner()
    payload = json.dumps([
        _system_record(name="t1", executable="pwsh.exe"),
        _user_record(
            name="t2", executable="cmd.exe", user="DOMAIN\\Bob",
        ),
    ])
    runner.results[tuple(_list_tasks_argv())] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    rtnval = manager.list_tasks()
    testbase.assert_equal(len(rtnval), 2)
    testbase.assert_equal(rtnval[0].name, "t1")
    testbase.assert_equal(rtnval[0].owner, WindowsTaskOwner.SYSTEM)
    testbase.assert_equal(rtnval[1].name, "t2")
    testbase.assert_equal(rtnval[1].owner, WindowsTaskOwner.USER)
    return


def test_list_tasks_normalises_bare_object():
    """
        A bare-object payload normalises into a single-element list.
    """
    runner = _MockCommandRunner()
    payload = json.dumps(
        _system_record(name="solo", executable="pwsh.exe"),
    )
    runner.results[tuple(_list_tasks_argv())] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    rtnval = manager.list_tasks()
    testbase.assert_equal(len(rtnval), 1)
    testbase.assert_equal(rtnval[0].name, "solo")
    return


def test_validate_schedule_accepts_supported_grammar():
    """
        validate_schedule accepts the supported grammar.
    """
    runner = _MockCommandRunner()
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    testbase.assert_equal(
        manager.validate_schedule(schedule="daily 02:00"), True,
    )
    testbase.assert_equal(
        manager.validate_schedule(schedule="every 5min"), True,
    )
    testbase.assert_equal(
        manager.validate_schedule(schedule="every 2hour"), True,
    )
    testbase.assert_equal(
        manager.validate_schedule(schedule="weekly Monday 03:30"), True,
    )
    return


def test_validate_schedule_rejects_unsupported_and_does_not_invoke_runner():
    """
        validate_schedule rejects unsupported strings and never invokes
        the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    testbase.assert_equal(
        manager.validate_schedule(schedule="never"), False,
    )
    testbase.assert_equal(
        manager.validate_schedule(schedule=""), False,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# Owner-scope and elevation policy
# ----------------------------------------------------------------------


def test_system_owner_writes_elevate_when_config_elevated():
    """
        SYSTEM-owner writes pass ``elevated=True`` to the runner when
        ``config.elevated`` is True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    record = _system_record(
        name="my-task", executable="pwsh.exe", state="Disabled",
    )
    record["Settings"]["Enabled"] = False
    _seed_get_task(runner=runner, name="my-task", record=record)
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(elevated=True),
    )
    result = manager.enable_task(
        name="my-task", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SUCCESS,
    )
    write_call = runner.calls[-1]
    testbase.assert_equal(write_call["elevated"], True)
    # The probe must still not elevate.
    probe_call = runner.calls[0]
    testbase.assert_equal(probe_call["elevated"], False)
    return


def test_user_owner_writes_never_elevate():
    """
        USER-owner writes never pass ``elevated=True`` to the runner,
        even when ``config.elevated`` is True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    record = _user_record(
        name="alice-task",
        executable="pwsh.exe",
        user="DOMAIN\\Alice",
        state="Disabled",
    )
    record["Settings"]["Enabled"] = False
    _seed_get_task(runner=runner, name="alice-task", record=record)
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(elevated=True),
    )
    result = manager.enable_task(
        name="alice-task", owner=WindowsTaskOwner.USER,
    )
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SUCCESS,
    )
    write_call = runner.calls[-1]
    testbase.assert_equal(write_call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Parser tests
# ----------------------------------------------------------------------


def test_parse_get_scheduledtask_handles_array():
    """
        ``Get-ScheduledTask`` JSON arrays parse into a list.
    """
    payload = json.dumps([
        _system_record(name="t1", executable="pwsh.exe"),
    ])
    rtnval = _parse_get_scheduledtask(text=payload)
    testbase.assert_equal(len(rtnval), 1)
    testbase.assert_equal(rtnval[0]["TaskName"], "t1")
    return


def test_record_to_taskinfo_infers_owner():
    """
        A record with NT AUTHORITY\\SYSTEM principal infers SYSTEM
        owner; a non-system principal infers USER owner.
    """
    sys_rec = _system_record(name="t1", executable="pwsh.exe")
    sys_info = _record_to_taskinfo(record=sys_rec, requested_name="t1")
    testbase.assert_equal(sys_info.owner, WindowsTaskOwner.SYSTEM)

    user_rec = _user_record(
        name="t2", executable="pwsh.exe", user="DOMAIN\\Bob",
    )
    user_info = _record_to_taskinfo(record=user_rec, requested_name="t2")
    testbase.assert_equal(user_info.owner, WindowsTaskOwner.USER)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_run_task_now_propagates_runner_failure():
    """
        run_task_now returns FAILED with the captured exit_code /
        stderr when the runner fails.
    """
    runner = _MockCommandRunner()
    write_argv = _build_powershell_argv(
        script="Start-ScheduledTask -TaskName 'my-task'",
    )
    runner.results[tuple(write_argv)] = RunResult(
        exit_code=2, stdout="", stderr="boom",
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.run_task_now(
        name="my-task", owner=WindowsTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "boom")
    return


def test_create_task_uses_render_register_script_text_for_command_capture():
    """
        The dispatched scriptblock matches what
        ``_render_register_script`` produces in isolation, locking the
        renderer-as-contract for downstream tooling.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_get_task(runner=runner, name="my-task", record=None)
    spec = WindowsTaskSpec(
        name="my-task",
        command=["pwsh.exe", "-Command", "Get-Date"],
        schedule="daily 02:00",
        owner=WindowsTaskOwner.SYSTEM,
    )
    manager = WindowsScheduledTasksManager(
        runner=runner,
        config=WindowsScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=spec)
    testbase.assert_equal(
        result.status, WindowsScheduledTaskActionStatus.SUCCESS,
    )
    expected_script = _render_register_script(
        spec=spec,
        schedule_dict={"kind": "daily", "time": "02:00"},
        principal_user=None,
    )
    testbase.assert_equal(runner.calls[-1]["args"][-1], expected_script)
    return
