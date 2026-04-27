"""
    Tests for :class:`WindowsResourceControlManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult`
    values keyed by argv tuple.  No live ``powershell.exe`` is
    invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.resourcecontrolmanagement import (
    WindowsIoLimitSpec,
    WindowsResourceControlActionResult,
    WindowsResourceControlActionStatus,
    WindowsResourceControlManager,
    WindowsResourceControlManagerConfig,
    WindowsResourceControlQueryError,
    WindowsResourceGroupInfo,
    WindowsResourceGroupSpec,
    WindowsResourceGroupUsage,
    _parse_get_process_output,
    _parse_spec_json,
    _percent_to_priority_class,
    _render_spec_json,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        call and returns pre-seeded :class:`RunResult` values
        keyed by argv tuple.
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


def _ps_argv(scriptblock: str) -> list:
    """
        :returns: The PowerShell argv shape used by the manager.
    """
    rtnval = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        scriptblock,
    ]
    return rtnval


def _spec_path(name: str) -> str:
    """
        :returns: Default spec path for ``name``.
    """
    rtnval = "C:\\ProgramData\\Automojo\\rcgroups\\{}.json".format(name)
    return rtnval


def _seed_test_path(
    runner: _MockCommandRunner,
    *,
    name: str,
    present: bool,
) -> None:
    """
        Seed the PowerShell ``Test-Path`` probe used by
        ``_spec_exists``.
    """
    spec_path = _spec_path(name)
    scriptblock = (
        "if (Test-Path -LiteralPath '{path}') {{ "
        "'TRUE' }} else {{ 'FALSE' }}"
    ).format(path=spec_path)
    key = tuple(_ps_argv(scriptblock))
    payload = "TRUE\n" if present else "FALSE\n"
    runner.results[key] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _seed_read_spec(
    runner: _MockCommandRunner,
    *,
    name: str,
    spec: Optional[WindowsResourceGroupSpec],
) -> None:
    """
        Seed the PowerShell ``Get-Content -Raw`` probe used by
        ``_read_spec_text``.
    """
    spec_path = _spec_path(name)
    scriptblock = (
        "if (Test-Path -LiteralPath '{path}') {{ "
        "Get-Content -LiteralPath '{path}' -Raw }}"
    ).format(path=spec_path)
    key = tuple(_ps_argv(scriptblock))

    if spec is None:
        runner.results[key] = RunResult(
            exit_code=0, stdout="", stderr="",
        )
    else:
        body = _render_spec_json(spec=spec)
        runner.results[key] = RunResult(
            exit_code=0, stdout=body, stderr="",
        )
    return


# ----------------------------------------------------------------------
# create_group
# ----------------------------------------------------------------------


def test_create_group_invokes_set_content():
    """
        ``create_group`` must invoke PowerShell ``Set-Content``
        to write the spec file.
    """
    runner = _MockCommandRunner()
    _seed_read_spec(runner, name="batch", spec=None)

    config = WindowsResourceControlManagerConfig(elevated=True)
    manager = WindowsResourceControlManager(runner=runner, config=config)

    spec = WindowsResourceGroupSpec(
        name="batch", cpu_percent=50, memory_bytes=1073741824,
    )
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.SUCCESS)

    set_content_calls = [
        call for call in runner.calls
        if "Set-Content" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(set_content_calls), 1)
    body_arg = set_content_calls[0]["args"][-1]
    testbase.assert_equal("Set-Content -LiteralPath" in body_arg, True)
    testbase.assert_equal("\"batch\"" in body_arg, True)
    testbase.assert_equal("\"cpu_percent\": 50" in body_arg, True)
    return


def test_create_group_skipped_when_present():
    """
        ``create_group`` must return SKIPPED when the on-disk
        spec matches byte-for-byte.
    """
    runner = _MockCommandRunner()
    spec = WindowsResourceGroupSpec(
        name="batch", cpu_percent=50, memory_bytes=1073741824,
    )
    _seed_read_spec(runner, name="batch", spec=spec)

    manager = WindowsResourceControlManager(runner=runner)
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.SKIPPED)

    set_content_calls = [
        call for call in runner.calls
        if "Set-Content" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(set_content_calls), 0)
    return


def test_create_group_dry_run():
    """
        ``create_group`` must return DRY_RUN with the planned
        ``Set-Content`` argv and never invoke the runner.
    """
    runner = _MockCommandRunner()
    config = WindowsResourceControlManagerConfig(dry_run=True)
    manager = WindowsResourceControlManager(runner=runner, config=config)

    spec = WindowsResourceGroupSpec(name="batch", cpu_percent=50)
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal("Set-Content" in result.command[-1], True)
    return


# ----------------------------------------------------------------------
# delete_group
# ----------------------------------------------------------------------


def test_delete_group_invokes_remove_item():
    """
        ``delete_group`` must invoke PowerShell ``Remove-Item``.
    """
    runner = _MockCommandRunner()
    _seed_test_path(runner, name="batch", present=True)

    manager = WindowsResourceControlManager(runner=runner)
    result = manager.delete_group(name="batch")

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.SUCCESS)

    remove_calls = [
        call for call in runner.calls
        if "Remove-Item" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(remove_calls), 1)
    return


def test_delete_group_skipped_when_absent():
    """
        ``delete_group`` must return SKIPPED when the spec
        file does not exist.
    """
    runner = _MockCommandRunner()
    _seed_test_path(runner, name="batch", present=False)

    manager = WindowsResourceControlManager(runner=runner)
    result = manager.delete_group(name="batch")

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.SKIPPED)
    remove_calls = [
        call for call in runner.calls
        if "Remove-Item" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(remove_calls), 0)
    return


# ----------------------------------------------------------------------
# set_cpu_limit
# ----------------------------------------------------------------------


def test_set_cpu_limit_invokes_priority_class():
    """
        ``set_cpu_limit`` must invoke PowerShell to set
        ``PriorityClass`` for every attached PID.
    """
    runner = _MockCommandRunner()
    existing = WindowsResourceGroupSpec(
        name="batch", cpu_percent=10, attached_pids=[111, 222],
    )
    _seed_read_spec(runner, name="batch", spec=existing)

    manager = WindowsResourceControlManager(runner=runner)
    result = manager.set_cpu_limit(name="batch", percent=80)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.SUCCESS)

    pc_calls = [
        call for call in runner.calls
        if "PriorityClass" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(pc_calls), 2)
    # 80% maps to AboveNormal.
    for call in pc_calls:
        testbase.assert_equal("AboveNormal" in call["args"][-1], True)
    return


def test_set_cpu_limit_skipped_when_match():
    """
        ``set_cpu_limit`` must return SKIPPED when the existing
        ``cpu_percent`` already maps to the same priority
        class.
    """
    runner = _MockCommandRunner()
    existing = WindowsResourceGroupSpec(
        name="batch", cpu_percent=80, attached_pids=[111],
    )
    _seed_read_spec(runner, name="batch", spec=existing)

    manager = WindowsResourceControlManager(runner=runner)
    # 80 and 90 both map to AboveNormal.
    result = manager.set_cpu_limit(name="batch", percent=90)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.SKIPPED)
    pc_calls = [
        call for call in runner.calls
        if "PriorityClass" in (call["args"][-1] if len(call["args"]) > 0 else "")
        and "Set-Content" not in call["args"][-1]
    ]
    testbase.assert_equal(len(pc_calls), 0)
    return


# ----------------------------------------------------------------------
# set_memory_limit (FAILED)
# ----------------------------------------------------------------------


def test_set_memory_limit_returns_failed_with_windows_detail():
    """
        ``set_memory_limit`` must return FAILED with
        ``detail="windows_no_powershell_job_object_memory_limit"``
        and never invoke the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsResourceControlManager(runner=runner)

    result = manager.set_memory_limit(name="batch", bytes_=1073741824)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.FAILED)
    testbase.assert_equal(
        result.detail, "windows_no_powershell_job_object_memory_limit",
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# set_io_limits (FAILED)
# ----------------------------------------------------------------------


def test_set_io_limits_returns_failed_with_windows_detail():
    """
        ``set_io_limits`` must return FAILED with
        ``detail="windows_no_powershell_io_limit"`` and never
        invoke the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsResourceControlManager(runner=runner)

    spec = WindowsIoLimitSpec(weight=200)
    result = manager.set_io_limits(name="batch", spec=spec)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.FAILED)
    testbase.assert_equal(result.detail, "windows_no_powershell_io_limit")
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# attach_process / detach_process
# ----------------------------------------------------------------------


def test_attach_process_invokes_priority_class():
    """
        ``attach_process`` must add the PID to the spec, write
        the spec, then apply the recorded ``PriorityClass``.
    """
    runner = _MockCommandRunner()
    existing = WindowsResourceGroupSpec(
        name="batch", cpu_percent=80, attached_pids=[111],
    )
    _seed_read_spec(runner, name="batch", spec=existing)

    config = WindowsResourceControlManagerConfig(elevated=True)
    manager = WindowsResourceControlManager(runner=runner, config=config)

    result = manager.attach_process(name="batch", pid=222)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.SUCCESS)
    testbase.assert_equal(result.pid, 222)

    apply_calls = [
        call for call in runner.calls
        if "(Get-Process -Id 222).PriorityClass" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(apply_calls), 1)
    testbase.assert_equal("AboveNormal" in apply_calls[0]["args"][-1], True)
    testbase.assert_equal(apply_calls[0]["elevated"], True)

    # Set-Content should have been called to update the spec.
    set_content_calls = [
        call for call in runner.calls
        if "Set-Content" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(set_content_calls), 1)
    testbase.assert_equal("\"attached_pids\":" in set_content_calls[0]["args"][-1], True)
    testbase.assert_equal("222" in set_content_calls[0]["args"][-1], True)
    return


def test_attach_process_skipped_when_present():
    """
        ``attach_process`` must return SKIPPED when the PID is
        already in the spec.
    """
    runner = _MockCommandRunner()
    existing = WindowsResourceGroupSpec(
        name="batch", cpu_percent=80, attached_pids=[111, 222],
    )
    _seed_read_spec(runner, name="batch", spec=existing)

    manager = WindowsResourceControlManager(runner=runner)
    result = manager.attach_process(name="batch", pid=222)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.SKIPPED)

    apply_calls = [
        call for call in runner.calls
        if "PriorityClass = '" in (call["args"][-1] if len(call["args"]) > 0 else "")
        and "Set-Content" not in call["args"][-1]
    ]
    testbase.assert_equal(len(apply_calls), 0)
    return


def test_detach_process_invokes_priority_class():
    """
        ``detach_process`` must remove the PID, write the
        spec, then reset ``PriorityClass`` to ``Normal``.
    """
    runner = _MockCommandRunner()
    existing = WindowsResourceGroupSpec(
        name="batch", cpu_percent=80, attached_pids=[111, 222],
    )
    _seed_read_spec(runner, name="batch", spec=existing)

    manager = WindowsResourceControlManager(runner=runner)
    result = manager.detach_process(name="batch", pid=222)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.SUCCESS)

    reset_calls = [
        call for call in runner.calls
        if "(Get-Process -Id 222).PriorityClass = 'Normal'" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(reset_calls), 1)

    set_content_calls = [
        call for call in runner.calls
        if "Set-Content" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(set_content_calls), 1)
    testbase.assert_equal("222" in set_content_calls[0]["args"][-1], False)
    return


# ----------------------------------------------------------------------
# list_groups
# ----------------------------------------------------------------------


def test_list_groups_returns_list():
    """
        ``list_groups`` must enumerate spec files via
        ``Get-ChildItem`` and parse each.
    """
    runner = _MockCommandRunner()
    list_scriptblock = (
        "if (Test-Path -LiteralPath 'C:\\ProgramData\\Automojo\\rcgroups') { "
        "Get-ChildItem -LiteralPath 'C:\\ProgramData\\Automojo\\rcgroups' -Filter '*.json' | "
        "Select-Object -ExpandProperty Name | "
        "ConvertTo-Json -Compress } else { '[]' }"
    )
    runner.results[tuple(_ps_argv(list_scriptblock))] = RunResult(
        exit_code=0,
        stdout="[\"batch.json\",\"worker.json\"]\n",
        stderr="",
    )

    batch_spec = WindowsResourceGroupSpec(
        name="batch", cpu_percent=50, memory_bytes=1073741824,
        attached_pids=[111, 222],
    )
    worker_spec = WindowsResourceGroupSpec(name="worker")
    _seed_read_spec(runner, name="batch", spec=batch_spec)
    _seed_read_spec(runner, name="worker", spec=worker_spec)

    manager = WindowsResourceControlManager(runner=runner)
    groups = manager.list_groups()

    testbase.assert_equal(len(groups), 2)
    by_name = {info.name: info for info in groups}
    testbase.assert_equal("batch" in by_name, True)
    testbase.assert_equal(by_name["batch"].cpu_percent, 50)
    testbase.assert_equal(by_name["batch"].memory_bytes, 1073741824)
    testbase.assert_equal(by_name["batch"].attached_pids, [111, 222])
    testbase.assert_equal(by_name["batch"].is_active, True)
    testbase.assert_equal(by_name["worker"].cpu_percent, None)

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_group_usage
# ----------------------------------------------------------------------


def test_get_group_usage_parses_get_process_output():
    """
        ``get_group_usage`` must aggregate ``Get-Process``
        snapshots across attached PIDs.
    """
    runner = _MockCommandRunner()
    spec = WindowsResourceGroupSpec(
        name="batch", attached_pids=[111, 222],
    )
    _seed_read_spec(runner, name="batch", spec=spec)

    sb_111 = (
        "Get-Process -Id 111 | Select-Object "
        "WorkingSet64,CPU | ConvertTo-Json -Compress"
    )
    sb_222 = (
        "Get-Process -Id 222 | Select-Object "
        "WorkingSet64,CPU | ConvertTo-Json -Compress"
    )
    runner.results[tuple(_ps_argv(sb_111))] = RunResult(
        exit_code=0,
        stdout="{\"WorkingSet64\":104857600,\"CPU\":3.5}\n",
        stderr="",
    )
    runner.results[tuple(_ps_argv(sb_222))] = RunResult(
        exit_code=0,
        stdout="{\"WorkingSet64\":52428800,\"CPU\":1.25}\n",
        stderr="",
    )

    manager = WindowsResourceControlManager(runner=runner)
    usage = manager.get_group_usage(name="batch")

    testbase.assert_equal(usage.name, "batch")
    testbase.assert_equal(usage.process_count, 2)
    testbase.assert_equal(usage.memory_used_bytes, 104857600 + 52428800)
    testbase.assert_equal(usage.cpu_usage_seconds, 3.5 + 1.25)
    testbase.assert_equal(usage.io_read_bytes, None)
    testbase.assert_equal(usage.io_write_bytes, None)
    return


def test_get_group_usage_unknown_group_raises():
    """
        ``get_group_usage`` must raise
        :class:`WindowsResourceControlQueryError` with
        ``detail="unknown_group"`` when the spec file is
        absent.
    """
    runner = _MockCommandRunner()
    _seed_read_spec(runner, name="batch", spec=None)

    manager = WindowsResourceControlManager(runner=runner)
    raised: Optional[WindowsResourceControlQueryError] = None
    try:
        manager.get_group_usage(name="batch")
    except WindowsResourceControlQueryError as exc:
        raised = exc
    testbase.assert_not_equal(raised, None)
    testbase.assert_equal(raised.detail, "unknown_group")
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs must pass ``elevated=True`` to the runner
        when ``config.elevated`` is True.
    """
    runner = _MockCommandRunner()
    _seed_read_spec(runner, name="batch", spec=None)

    config = WindowsResourceControlManagerConfig(elevated=True)
    manager = WindowsResourceControlManager(runner=runner, config=config)

    spec = WindowsResourceGroupSpec(name="batch", cpu_percent=50)
    manager.create_group(spec=spec)

    set_content_calls = [
        call for call in runner.calls
        if "Set-Content" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    for call in set_content_calls:
        testbase.assert_equal(call["elevated"], True)

    # Probes (Get-Content / Test-Path) must remain elevated=False.
    probe_calls = [
        call for call in runner.calls
        if "Get-Content" in (call["args"][-1] if len(call["args"]) > 0 else "")
        or "Test-Path" in (call["args"][-1] if len(call["args"]) > 0 else "")
    ]
    for call in probe_calls:
        if "Get-Content" in call["args"][-1] and "Set-Content" not in call["args"][-1]:
            testbase.assert_equal(call["elevated"], False)
    return


def test_reads_never_elevate():
    """
        Read verbs must always pass ``elevated=False`` to the
        runner regardless of ``config.elevated``.
    """
    runner = _MockCommandRunner()
    list_scriptblock = (
        "if (Test-Path -LiteralPath 'C:\\ProgramData\\Automojo\\rcgroups') { "
        "Get-ChildItem -LiteralPath 'C:\\ProgramData\\Automojo\\rcgroups' -Filter '*.json' | "
        "Select-Object -ExpandProperty Name | "
        "ConvertTo-Json -Compress } else { '[]' }"
    )
    runner.results[tuple(_ps_argv(list_scriptblock))] = RunResult(
        exit_code=0, stdout="[]\n", stderr="",
    )

    config = WindowsResourceControlManagerConfig(elevated=True)
    manager = WindowsResourceControlManager(runner=runner, config=config)

    groups = manager.list_groups()
    testbase.assert_equal(groups, [])

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing dispatched command must surface its
        ``exit_code``, ``stderr``, and ``command`` on the
        FAILED result.
    """
    runner = _MockCommandRunner()
    _seed_read_spec(runner, name="batch", spec=None)

    # Use default for ensure_group_dir (ok), but force the
    # Set-Content to fail.
    runner.default_result = RunResult(
        exit_code=1, stdout="", stderr="Set-Content : Access denied\n",
    )

    manager = WindowsResourceControlManager(runner=runner)
    spec = WindowsResourceGroupSpec(name="batch", cpu_percent=50)
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, WindowsResourceControlActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal("Access denied" in result.stderr, True)
    return


# ----------------------------------------------------------------------
# Pure helper tests
# ----------------------------------------------------------------------


def test_percent_to_priority_class_mapping():
    """
        Verify the percent -> PriorityClass mapping table.
    """
    testbase.assert_equal(_percent_to_priority_class(percent=10), "Idle")
    testbase.assert_equal(_percent_to_priority_class(percent=25), "Idle")
    testbase.assert_equal(_percent_to_priority_class(percent=50), "BelowNormal")
    testbase.assert_equal(_percent_to_priority_class(percent=75), "Normal")
    testbase.assert_equal(_percent_to_priority_class(percent=80), "AboveNormal")
    testbase.assert_equal(_percent_to_priority_class(percent=100), "High")
    testbase.assert_equal(_percent_to_priority_class(percent=None), "Normal")
    return


def test_render_and_parse_spec_round_trip():
    """
        ``_render_spec_json`` and ``_parse_spec_json`` must
        round-trip a populated spec.
    """
    spec = WindowsResourceGroupSpec(
        name="batch",
        cpu_percent=50, memory_bytes=1073741824,
        io_weight=200, io_read_bps=1048576, io_write_bps=2097152,
        attached_pids=[111, 222],
    )
    body = _render_spec_json(spec=spec)
    parsed = _parse_spec_json(text=body)
    testbase.assert_not_equal(parsed, None)
    testbase.assert_equal(parsed.name, "batch")
    testbase.assert_equal(parsed.cpu_percent, 50)
    testbase.assert_equal(parsed.memory_bytes, 1073741824)
    testbase.assert_equal(parsed.io_weight, 200)
    testbase.assert_equal(parsed.io_read_bps, 1048576)
    testbase.assert_equal(parsed.io_write_bps, 2097152)
    testbase.assert_equal(parsed.attached_pids, [111, 222])
    return


def test_parse_get_process_output_handles_compact_json():
    """
        ``_parse_get_process_output`` must parse the compact
        JSON shape returned by ``ConvertTo-Json -Compress``.
    """
    body = "{\"WorkingSet64\":104857600,\"CPU\":3.5}"
    mem, cpu = _parse_get_process_output(text=body)
    testbase.assert_equal(mem, 104857600)
    testbase.assert_equal(cpu, 3.5)
    return
