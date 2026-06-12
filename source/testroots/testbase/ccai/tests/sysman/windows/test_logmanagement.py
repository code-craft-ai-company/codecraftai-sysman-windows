"""
    Tests for :class:`WindowsLogManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell or wevtutil process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.logmanagement import (
    WindowsLogActionResult,
    WindowsLogActionStatus,
    WindowsLogEntries,
    WindowsLogEntry,
    WindowsLogInfo,
    WindowsLogManager,
    WindowsLogManagerConfig,
    WindowsLogQueryError,
    WindowsRotationPolicy,
    _build_get_log_script,
    _is_classic_channel,
    _normalize_to_list,
    _record_to_log_entry,
    _record_to_log_info,
    _stderr_looks_no_events,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in.
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

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


# ----------------------------------------------------------------------
# Helpers
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


def _seed(*, runner: _MockCommandRunner, argv: list, result: RunResult) -> None:
    """
        Seed a result for the given argv.
    """
    runner.results[tuple(argv)] = result
    return


# ----------------------------------------------------------------------
# Pure-function tests
# ----------------------------------------------------------------------


def test_is_classic_channel_application():
    """
        ``Application`` is a classic channel.
    """
    rtnval = _is_classic_channel(name="Application")
    testbase.assert_equal(rtnval, True)
    return


def test_is_classic_channel_modern():
    """
        Modern channels (containing ``/``) are NOT classic.
    """
    rtnval = _is_classic_channel(
        name="Microsoft-Windows-Security-Auditing/Operational",
    )
    testbase.assert_equal(rtnval, False)
    return


def test_normalize_to_list_dict_becomes_one_element_list():
    """
        A bare dict is wrapped in a one-element list.
    """
    rtnval = _normalize_to_list(parsed={"a": 1})
    testbase.assert_equal(rtnval, [{"a": 1}])
    return


def test_normalize_to_list_list_passthrough():
    """
        A list is returned unchanged.
    """
    rtnval = _normalize_to_list(parsed=[{"a": 1}, {"b": 2}])
    testbase.assert_equal(rtnval, [{"a": 1}, {"b": 2}])
    return


def test_record_to_log_entry_minimal():
    """
        A record with only a TimeCreated yields an entry with the
        timestamp surfaced and other fields defaulted.
    """
    record = {"TimeCreated": "2026-04-24T01:00:00Z"}
    rtnval = _record_to_log_entry(record=record)
    testbase.assert_equal(rtnval.time_created, "2026-04-24T01:00:00Z")
    testbase.assert_equal(rtnval.level, None)
    testbase.assert_equal(rtnval.provider_name, None)
    testbase.assert_equal(rtnval.id, None)
    testbase.assert_equal(rtnval.message, "")
    return


def test_record_to_log_entry_full():
    """
        A record with every field populated round-trips into the
        entry.
    """
    record = {
        "TimeCreated": "2026-04-24T01:00:00Z",
        "Level": 4,
        "ProviderName": "Microsoft-Windows-Kernel-General",
        "Id": 12,
        "Message": "Booted",
    }
    rtnval = _record_to_log_entry(record=record)
    testbase.assert_equal(rtnval.level, 4)
    testbase.assert_equal(
        rtnval.provider_name, "Microsoft-Windows-Kernel-General",
    )
    testbase.assert_equal(rtnval.id, 12)
    testbase.assert_equal(rtnval.message, "Booted")
    return


def test_record_to_log_info_full():
    """
        A list-log record is decoded into a :class:`WindowsLogInfo`.
    """
    record = {
        "LogName": "Application",
        "LogMode": "Circular",
        "MaximumSizeInBytes": 20971520,
        "RecordCount": 123,
    }
    rtnval = _record_to_log_info(record=record)
    testbase.assert_equal(rtnval.name, "Application")
    testbase.assert_equal(rtnval.log_mode, "Circular")
    testbase.assert_equal(rtnval.max_size_bytes, 20971520)
    testbase.assert_equal(rtnval.record_count, 123)
    return


def test_record_to_log_info_missing_name_returns_none():
    """
        A record without a ``LogName`` collapses to None.
    """
    rtnval = _record_to_log_info(record={"LogMode": "Circular"})
    testbase.assert_equal(rtnval, None)
    return


def test_stderr_no_events_detection():
    """
        The ``No events were found`` stderr is detected.
    """
    rtnval = _stderr_looks_no_events(
        stderr="No events were found that match the specified selection.",
    )
    testbase.assert_equal(rtnval, True)
    return


def test_build_get_log_script_no_since():
    """
        Without ``since``, the script uses the simple ``-LogName / -MaxEvents``
        form.
    """
    rtnval = _build_get_log_script(
        name="Application", lines=10, since=None,
    )
    expected = (
        "Get-WinEvent -LogName 'Application' -MaxEvents 10 "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    testbase.assert_equal(rtnval, expected)
    return


def test_build_get_log_script_with_since():
    """
        With ``since``, the script uses the ``-FilterHashtable`` form.
    """
    rtnval = _build_get_log_script(
        name="Application",
        lines=10,
        since="2026-04-23T00:00:00Z",
    )
    testbase.assert_equal(
        "FilterHashtable" in rtnval, True,
    )
    testbase.assert_equal(
        "[datetime]'2026-04-23T00:00:00Z'" in rtnval, True,
    )
    return


# ----------------------------------------------------------------------
# get_log
# ----------------------------------------------------------------------


def test_get_log_builds_get_winevent_command():
    """
        ``get_log`` constructs a ``Get-WinEvent | ConvertTo-Json`` argv
        and dispatches with ``elevated=False``.
    """
    runner = _MockCommandRunner()
    argv = _build_powershell_argv(
        _build_get_log_script(name="Application", lines=10, since=None),
    )
    _seed(
        runner=runner,
        argv=argv,
        result=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {"TimeCreated": "2026-04-24T01:00:00Z", "Message": "OK"},
            ]),
            stderr="",
        ),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    result = manager.get_log(name="Application", lines=10)
    testbase.assert_equal(len(result.entries), 1)
    testbase.assert_equal(
        result.entries[0].time_created, "2026-04-24T01:00:00Z",
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


def test_get_log_returns_empty_when_no_events_stderr():
    """
        ``get_log`` returns an empty :class:`WindowsLogEntries` when
        ``Get-WinEvent`` exits non-zero with the "no events" stderr.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=1,
            stdout="",
            stderr="No events were found that match the selection.",
        ),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    result = manager.get_log(name="Application", lines=10)
    testbase.assert_equal(result.entries, [])
    testbase.assert_equal(result.truncated, False)
    return


def test_get_log_truncated_flag_set_when_full():
    """
        ``truncated`` is True when the parsed entry count equals
        ``lines``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=0,
            stdout=json.dumps([
                {"TimeCreated": "t1"},
                {"TimeCreated": "t2"},
            ]),
            stderr="",
        ),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    result = manager.get_log(name="Application", lines=2)
    testbase.assert_equal(result.truncated, True)
    return


def test_get_log_raises_on_unexpected_failure():
    """
        Non-zero ``Get-WinEvent`` exits that aren't "no events" raise
        :class:`WindowsLogQueryError`.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=1,
            stdout="",
            stderr="Cmdlet not found",
        ),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    raised = False
    try:
        manager.get_log(name="Application", lines=10)
    except WindowsLogQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_get_log_handles_single_object_payload():
    """
        ``Get-WinEvent`` may emit a bare object when there is only one
        match.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=0,
            stdout=json.dumps({
                "TimeCreated": "2026-04-24T01:00:00Z",
                "Message": "single",
            }),
            stderr="",
        ),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    result = manager.get_log(name="Application", lines=10)
    testbase.assert_equal(len(result.entries), 1)
    testbase.assert_equal(result.entries[0].message, "single")
    return


# ----------------------------------------------------------------------
# list_logs
# ----------------------------------------------------------------------


def test_list_logs_parses_listlog_json():
    """
        ``list_logs`` parses a ``Get-WinEvent -ListLog *`` JSON array.
    """
    payload = json.dumps([
        {
            "LogName": "Application",
            "LogMode": "Circular",
            "MaximumSizeInBytes": 20971520,
            "RecordCount": 100,
        },
        {
            "LogName": "System",
            "LogMode": "AutoBackup",
            "MaximumSizeInBytes": 41943040,
            "RecordCount": 50,
        },
    ])
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout=payload, stderr=""),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    result = manager.list_logs()
    testbase.assert_equal(len(result), 2)
    testbase.assert_equal(result[0].name, "Application")
    testbase.assert_equal(result[1].log_mode, "AutoBackup")
    return


def test_list_logs_raises_on_failure():
    """
        Non-zero exit raises :class:`WindowsLogQueryError`.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=1, stdout="", stderr="boom",
        ),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    raised = False
    try:
        manager.list_logs()
    except WindowsLogQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# rotate_log
# ----------------------------------------------------------------------


def test_rotate_log_dispatches_epl_then_cl():
    """
        ``rotate_log`` invokes ``wevtutil epl`` followed by
        ``wevtutil cl``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    result = manager.rotate_log(name="Application", timestamp="20260424T010000Z")
    testbase.assert_equal(
        result.status, WindowsLogActionStatus.SUCCESS,
    )
    epl_call = runner.calls[0]
    cl_call = runner.calls[1]
    testbase.assert_equal(epl_call["args"][0], "wevtutil.exe")
    testbase.assert_equal(epl_call["args"][1], "epl")
    testbase.assert_equal(epl_call["args"][2], "Application")
    testbase.assert_equal(cl_call["args"][1], "cl")
    testbase.assert_equal(cl_call["args"][2], "Application")
    testbase.assert_equal(
        result.archive_path.endswith("Application-20260424T010000Z.evtx"),
        True,
    )
    return


def test_rotate_log_dry_run_does_not_invoke_runner():
    """
        Dry-run rotate_log returns DRY_RUN with no runner calls.
    """
    runner = _MockCommandRunner()
    manager = WindowsLogManager(
        runner=runner,
        config=WindowsLogManagerConfig(dry_run=True),
    )
    result = manager.rotate_log(name="Application")
    testbase.assert_equal(
        result.status, WindowsLogActionStatus.DRY_RUN,
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_rotate_log_failed_when_epl_fails():
    """
        When the ``wevtutil epl`` step fails, ``rotate_log`` returns
        FAILED with ``message="wevtutil_epl_failed"`` and does NOT
        attempt the clear.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=2, stdout="", stderr="export failed",
        ),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    result = manager.rotate_log(name="Application")
    testbase.assert_equal(
        result.status, WindowsLogActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "wevtutil_epl_failed")
    testbase.assert_equal(len(runner.calls), 1)
    return


# ----------------------------------------------------------------------
# truncate_log
# ----------------------------------------------------------------------


def test_truncate_log_classic_uses_clear_eventlog():
    """
        Classic channels dispatch ``Clear-EventLog``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    result = manager.truncate_log(name="Application")
    testbase.assert_equal(
        result.status, WindowsLogActionStatus.SUCCESS,
    )
    script = runner.calls[-1]["args"][-1]
    testbase.assert_equal(
        "Clear-EventLog -LogName 'Application'" in script, True,
    )
    return


def test_truncate_log_modern_uses_wevtutil():
    """
        Modern channels dispatch ``wevtutil cl``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    result = manager.truncate_log(
        name="Microsoft-Windows-Security-Auditing/Operational",
    )
    testbase.assert_equal(
        result.status, WindowsLogActionStatus.SUCCESS,
    )
    args = runner.calls[-1]["args"]
    testbase.assert_equal(args[0], "wevtutil.exe")
    testbase.assert_equal(args[1], "cl")
    testbase.assert_equal(
        args[2], "Microsoft-Windows-Security-Auditing/Operational",
    )
    return


def test_truncate_log_dry_run():
    """
        Dry-run truncate_log returns DRY_RUN without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsLogManager(
        runner=runner,
        config=WindowsLogManagerConfig(dry_run=True),
    )
    result = manager.truncate_log(name="Application")
    testbase.assert_equal(
        result.status, WindowsLogActionStatus.DRY_RUN,
    )
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# configure_rotation
# ----------------------------------------------------------------------


def test_configure_rotation_dispatches_wevtutil_sl():
    """
        configure_rotation runs ``wevtutil sl <name> /ms:<bytes>
        /rt:<bool>`` when the existing policy does not match.
    """
    payload = json.dumps([{
        "LogName": "Application",
        "LogMode": "AutoBackup",
        "MaximumSizeInBytes": 1000,
    }])
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout=payload, stderr=""),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    policy = WindowsRotationPolicy(size_kb=2048, retain=True)
    result = manager.configure_rotation(name="Application", policy=policy)
    testbase.assert_equal(
        result.status, WindowsLogActionStatus.SUCCESS,
    )
    write_args = runner.calls[-1]["args"]
    testbase.assert_equal(write_args[0], "wevtutil.exe")
    testbase.assert_equal(write_args[1], "sl")
    testbase.assert_equal(write_args[2], "Application")
    testbase.assert_equal(write_args[3], "/ms:2097152")
    testbase.assert_equal(write_args[4], "/rt:true")
    return


def test_configure_rotation_skipped_when_policy_matches():
    """
        configure_rotation returns SKIPPED when the existing channel
        already matches the requested policy.
    """
    payload = json.dumps([{
        "LogName": "Application",
        "LogMode": "Circular",
        "MaximumSizeInBytes": 2097152,
    }])
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout=payload, stderr=""),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    policy = WindowsRotationPolicy(size_kb=2048, retain=True)
    result = manager.configure_rotation(name="Application", policy=policy)
    testbase.assert_equal(
        result.status, WindowsLogActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "wevtutil.exe" and c["args"][1] == "sl"
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_configure_rotation_failed_when_size_kb_missing():
    """
        configure_rotation returns FAILED with
        ``message="size_kb_required"`` when the policy lacks a size.
    """
    runner = _MockCommandRunner()
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    policy = WindowsRotationPolicy(size_kb=None, retain=True)
    result = manager.configure_rotation(name="Application", policy=policy)
    testbase.assert_equal(
        result.status, WindowsLogActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "size_kb_required")
    testbase.assert_equal(runner.calls, [])
    return


def test_configure_rotation_dry_run():
    """
        Dry-run configure_rotation returns DRY_RUN with no runner
        calls.
    """
    runner = _MockCommandRunner()
    manager = WindowsLogManager(
        runner=runner,
        config=WindowsLogManagerConfig(dry_run=True),
    )
    policy = WindowsRotationPolicy(size_kb=1024, retain=False)
    result = manager.configure_rotation(name="Application", policy=policy)
    testbase.assert_equal(
        result.status, WindowsLogActionStatus.DRY_RUN,
    )
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# tail_log
# ----------------------------------------------------------------------


def test_tail_log_non_follow_yields_one_event_then_terminates():
    """
        ``tail_log`` with ``follow=False`` yields the single most-recent
        event (if any) and terminates.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=0,
            stdout=json.dumps({
                "TimeCreated": "t1",
                "Message": "tail",
            }),
            stderr="",
        ),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    iterator = manager.tail_log(name="Application", follow=False)
    yielded = list(iterator)
    testbase.assert_equal(len(yielded), 1)
    testbase.assert_equal(yielded[0].message, "tail")
    return


def test_tail_log_no_events_terminates_when_not_follow():
    """
        With no events and ``follow=False``, the iterator terminates
        without yielding.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=1, stdout="",
            stderr="No events were found.",
        ),
    )
    manager = WindowsLogManager(
        runner=runner, config=WindowsLogManagerConfig(),
    )
    iterator = manager.tail_log(name="Application", follow=False)
    yielded = list(iterator)
    testbase.assert_equal(yielded, [])
    return
