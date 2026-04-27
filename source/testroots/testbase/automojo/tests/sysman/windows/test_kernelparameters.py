"""
    Tests for :class:`WindowsKernelParameterManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    No live PowerShell process is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.kernelparameters import (
    WindowsKernelParameterActionStatus,
    WindowsKernelParameterManager,
    WindowsKernelParameterManagerConfig,
    WindowsKernelParameterMappingEntry,
    WindowsKernelParameterQueryError,
    WindowsParameterValue,
)


# ----------------------------------------------------------------------
# Test scaffolding
# ----------------------------------------------------------------------


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


def _get_argv(*, registry_path: str, value_name: str) -> list:
    """
        Build the argv the manager produces for a get_parameter call.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "(Get-ItemProperty -Path '{}' -Name '{}' -ErrorAction Stop)"
        ".'{}'"
    ).format(registry_path, value_name, value_name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _set_argv(
    *,
    registry_path: str,
    value_name: str,
    value_literal: str,
    reg_type: str,
) -> list:
    """
        Build the argv the manager produces for a set_parameter call.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "if (-not (Test-Path -Path '{path}')) "
        "{{ New-Item -Path '{path}' -Force | Out-Null }}; "
        "Set-ItemProperty -Path '{path}' -Name '{key}' "
        "-Value {value_literal} -Type {reg_type}"
    ).format(
        path=registry_path,
        key=value_name,
        value_literal=value_literal,
        reg_type=reg_type,
    )
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _remove_argv(*, registry_path: str, value_name: str) -> list:
    """
        Build the argv the manager produces for a remove_persisted_parameter.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "Remove-ItemProperty -Path '{}' -Name '{}'"
    ).format(registry_path, value_name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


_TCPIP_PARAMETERS_PATH = (
    r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
)
_AFD_PARAMETERS_PATH = (
    r"HKLM:\SYSTEM\CurrentControlSet\Services\AFD\Parameters"
)


# ----------------------------------------------------------------------
# get_parameter
# ----------------------------------------------------------------------


def test_get_parameter_dispatches_get_item_property():
    """
        get_parameter for a mapped name dispatches the documented
        ``Get-ItemProperty`` argv and parses the stdout payload.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="7200000\n", stderr="")
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    info = manager.get_parameter(name="net.ipv4.tcp_keepalive_time")
    testbase.assert_equal(info.name, "net.ipv4.tcp_keepalive_time")
    testbase.assert_equal(info.value, "7200000")
    testbase.assert_equal(info.registry_path, _TCPIP_PARAMETERS_PATH)
    testbase.assert_equal(info.registry_key, "KeepAliveTime")
    return


def test_get_parameter_unmapped_returns_none():
    """
        get_parameter for an unmapped name returns ``None`` without
        invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    info = manager.get_parameter(name="kernel.totally.unknown.param")
    testbase.assert_equal(info, None)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_get_parameter_no_windows_equivalent_returns_none():
    """
        get_parameter for a name mapped to a sentinel-None entry (no
        Windows equivalent) returns ``None`` without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    info = manager.get_parameter(name="vm.swappiness")
    testbase.assert_equal(info, None)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_get_parameter_property_not_found_returns_none():
    """
        ``Get-ItemProperty`` reporting "PropertyNotFound" produces
        ``None`` rather than raising.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(
        exit_code=1,
        stdout="",
        stderr=(
            "Get-ItemProperty : Property KeepAliveTime does not exist "
            "at path ...\n"
            "+ FullyQualifiedErrorId : PropertyNotFound"
        ),
    )
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    info = manager.get_parameter(name="net.ipv4.tcp_keepalive_time")
    testbase.assert_equal(info, None)
    return


def test_get_parameter_unexpected_failure_raises():
    """
        get_parameter raises :class:`WindowsKernelParameterQueryError`
        when the runner reports an unrecognised stderr.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(
        exit_code=5,
        stdout="",
        stderr="Access is denied.",
    )
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    raised = False
    try:
        manager.get_parameter(name="net.ipv4.tcp_keepalive_time")
    except WindowsKernelParameterQueryError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# set_parameter
# ----------------------------------------------------------------------


def test_set_parameter_unmapped_rejected():
    """
        set_parameter on an unmapped name returns FAILED with the
        documented message and does not invoke the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(
        name="kernel.unknown.thing", value="1",
    )
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.FAILED,
    )
    testbase.assert_true(
        "is not in the supported Windows kernel-parameter mapping"
        in result.message,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_set_parameter_no_windows_equivalent_rejected():
    """
        set_parameter on a name mapped to a sentinel-None entry returns
        FAILED with a "no Windows equivalent" message.
    """
    runner = _MockCommandRunner()
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(name="vm.swappiness", value="10")
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.FAILED,
    )
    testbase.assert_true("no Windows equivalent" in result.message)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_set_parameter_dispatches_set_item_property_with_dword():
    """
        set_parameter on a mapped DWord parameter dispatches the
        ``Set-ItemProperty`` argv with bare-integer literal.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    # Seed a different current value so idempotency does not fire.
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="100\n", stderr="")
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(
        name="net.ipv4.tcp_keepalive_time", value="7200000",
    )
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.SUCCESS,
    )
    expected = _set_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
        value_literal="7200000",
        reg_type="DWord",
    )
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_set_parameter_propagates_requires_reboot():
    """
        The ``requires_reboot`` flag from the mapping table is copied
        verbatim onto every result.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="100\n", stderr="")
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(
        name="net.ipv4.tcp_keepalive_time", value="7200000",
    )
    testbase.assert_equal(result.requires_reboot, True)
    testbase.assert_equal(result.registry_path, _TCPIP_PARAMETERS_PATH)
    testbase.assert_equal(result.registry_key, "KeepAliveTime")
    return


def test_set_parameter_skipped_when_value_matches():
    """
        set_parameter returns SKIPPED when the current registry value
        already matches.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="7200000\n", stderr="")
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(
        name="net.ipv4.tcp_keepalive_time", value="7200000",
    )
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_parameter_dry_run_short_circuits():
    """
        Dry-run short-circuits set_parameter without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(dry_run=True),
    )
    result = manager.set_parameter(
        name="net.ipv4.tcp_keepalive_time", value="7200000",
    )
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    expected = _set_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
        value_literal="7200000",
        reg_type="DWord",
    )
    testbase.assert_equal(result.command, expected)
    return


def test_set_parameter_persistent_false_records_advisory_message():
    """
        ``persistent=False`` is advisory: the write still happens, but
        the result message documents that registry writes are
        inherently persistent.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="100\n", stderr="")
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(
        name="net.ipv4.tcp_keepalive_time",
        value="7200000",
        persistent=False,
    )
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.SUCCESS,
    )
    testbase.assert_true(
        "registry writes are inherently persistent" in result.message,
    )
    testbase.assert_equal(result.persistent, False)
    return


# ----------------------------------------------------------------------
# list_parameters
# ----------------------------------------------------------------------


def test_list_parameters_walks_mapping_and_omits_missing():
    """
        list_parameters queries every mapped entry and returns only
        those with a value present.  Entries with no Windows
        equivalent are silently skipped.
    """
    runner = _MockCommandRunner()
    # KeepAliveTime present with value 7200000
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="7200000\n", stderr="")
    # ListenBackLog property absent
    runner.results[tuple(_get_argv(
        registry_path=_AFD_PARAMETERS_PATH,
        value_name="ListenBackLog",
    ))] = RunResult(
        exit_code=1,
        stdout="",
        stderr="PropertyNotFound: ListenBackLog does not exist",
    )
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    listed = manager.list_parameters()

    # KeepAliveTime should be present with the expected value.
    testbase.assert_equal(
        listed.get("net.ipv4.tcp_keepalive_time"), "7200000",
    )
    # ListenBackLog reported PropertyNotFound -> not in the dict.
    testbase.assert_true("net.core.somaxconn" not in listed)
    # vm.swappiness has no Windows equivalent -> not in the dict.
    testbase.assert_true("vm.swappiness" not in listed)
    return


# ----------------------------------------------------------------------
# reload_parameters
# ----------------------------------------------------------------------


def test_reload_parameters_returns_success_noop():
    """
        reload_parameters returns SUCCESS without invoking the runner.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(elevated=True),
    )
    result = manager.reload_parameters()
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.SUCCESS,
    )
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_true(
        "No reload primitive is available" in result.message,
    )
    return


# ----------------------------------------------------------------------
# remove_persisted_parameter
# ----------------------------------------------------------------------


def test_remove_persisted_unmapped_rejected():
    """
        remove_persisted_parameter for an unmapped name returns FAILED
        without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    result = manager.remove_persisted_parameter(
        name="kernel.unknown.thing",
    )
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.FAILED,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_remove_persisted_dispatches_remove_item_property():
    """
        remove_persisted_parameter dispatches ``Remove-ItemProperty``
        when the value is currently present.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="7200000\n", stderr="")
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    result = manager.remove_persisted_parameter(
        name="net.ipv4.tcp_keepalive_time",
    )
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.SUCCESS,
    )
    expected = _remove_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    )
    write_calls = [
        c for c in runner.calls
        if "Remove-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_remove_persisted_skipped_when_value_absent():
    """
        remove_persisted_parameter returns SKIPPED when the registry
        value is not present.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(
        exit_code=1,
        stdout="",
        stderr="PropertyNotFound: KeepAliveTime does not exist",
    )
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    result = manager.remove_persisted_parameter(
        name="net.ipv4.tcp_keepalive_time",
    )
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if "Remove-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate_when_configured():
    """
        Writes pass ``elevated=True`` to the runner when the config is
        elevated.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="100\n", stderr="")
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(elevated=True),
    )
    manager.set_parameter(
        name="net.ipv4.tcp_keepalive_time", value="7200000",
    )
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Reads pass ``elevated=False`` even when config.elevated is True.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="7200000\n", stderr="")
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(elevated=True),
    )
    manager.get_parameter(name="net.ipv4.tcp_keepalive_time")
    elevated_reads = [
        c for c in runner.calls
        if "Get-ItemProperty" in c["args"][-1] and c["elevated"]
    ]
    testbase.assert_equal(elevated_reads, [])
    return


# ----------------------------------------------------------------------
# Argv shape
# ----------------------------------------------------------------------


def test_powershell_argv_includes_noprofile():
    """
        Every dispatched command runs PowerShell with ``-NoProfile`` and
        ``-NonInteractive``.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="7200000\n", stderr="")
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    manager.get_parameter(name="net.ipv4.tcp_keepalive_time")
    for call_record in runner.calls:
        argv = call_record["args"]
        testbase.assert_equal(argv[0], "powershell.exe")
        testbase.assert_equal(argv[1], "-NoProfile")
        testbase.assert_equal(argv[2], "-NonInteractive")
        testbase.assert_equal(argv[3], "-Command")
        testbase.assert_equal(len(argv), 5)
    return


# ----------------------------------------------------------------------
# Mapping-table accessor
# ----------------------------------------------------------------------


def test_get_mapping_table_returns_copy():
    """
        get_mapping_table returns a copy: mutating the returned dict
        does not affect the manager's internal table.
    """
    runner = _MockCommandRunner()
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    table = manager.get_mapping_table()
    table.pop("net.ipv4.tcp_keepalive_time", None)
    table["totally.new.entry"] = WindowsKernelParameterMappingEntry(
        registry_path=r"HKLM:\SOFTWARE\Test",
        value_name="TestValue",
        registry_type="DWord",
        requires_reboot=False,
    )
    fresh = manager.get_mapping_table()
    testbase.assert_true("net.ipv4.tcp_keepalive_time" in fresh)
    testbase.assert_true("totally.new.entry" not in fresh)
    return


def test_custom_parameter_map_overrides_bundled():
    """
        A custom ``parameter_map`` on the config replaces the bundled
        table.
    """
    runner = _MockCommandRunner()
    custom_map = {
        "test.only.entry": WindowsKernelParameterMappingEntry(
            registry_path=r"HKLM:\SOFTWARE\Test",
            value_name="TestValue",
            registry_type="DWord",
            requires_reboot=False,
        ),
    }
    config = WindowsKernelParameterManagerConfig(parameter_map=custom_map)
    manager = WindowsKernelParameterManager(runner=runner, config=config)
    table = manager.get_mapping_table()
    testbase.assert_equal(len(table), 1)
    testbase.assert_true("test.only.entry" in table)
    testbase.assert_true("net.ipv4.tcp_keepalive_time" not in table)
    return


# ----------------------------------------------------------------------
# Failure surface
# ----------------------------------------------------------------------


def test_set_parameter_runner_failure_becomes_failed_result():
    """
        A non-zero runner exit on the write maps to FAILED with stderr
        / exit_code preserved.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="100\n", stderr="")
    write_argv = _set_argv(
        registry_path=_TCPIP_PARAMETERS_PATH,
        value_name="KeepAliveTime",
        value_literal="7200000",
        reg_type="DWord",
    )
    runner.results[tuple(write_argv)] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Access is denied",
    )
    manager = WindowsKernelParameterManager(
        runner=runner,
        config=WindowsKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(
        name="net.ipv4.tcp_keepalive_time", value="7200000",
    )
    testbase.assert_equal(
        result.status, WindowsKernelParameterActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "Access is denied")
    return
