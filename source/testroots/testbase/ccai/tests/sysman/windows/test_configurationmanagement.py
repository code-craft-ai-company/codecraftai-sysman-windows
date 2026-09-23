"""
    Tests for :class:`WindowsConfigurationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    No live PowerShell process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.configurationmanagement import (
    WindowsConfigActionStatus,
    WindowsConfigManagerConfig,
    WindowsConfigQueryError,
    WindowsConfigurationManager,
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


def _registry_get_argv(*, path: str, key: str) -> list:
    """
        Build the get_setting argv for a registry path.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "(Get-ItemProperty -Path '{path}' -Name '{key}' "
        "-ErrorAction Stop).'{key}'"
    ).format(path=path, key=key)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _registry_set_argv(
    *, path: str, key: str, value_literal: str, reg_type: str,
) -> list:
    """
        Build the set_setting argv for a registry path.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "if (-not (Test-Path -Path '{path}')) "
        "{{ New-Item -Path '{path}' -Force | Out-Null }}; "
        "Set-ItemProperty -Path '{path}' -Name '{key}' "
        "-Value {value_literal} -Type {reg_type}"
    ).format(
        path=path,
        key=key,
        value_literal=value_literal,
        reg_type=reg_type,
    )
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _registry_remove_argv(*, path: str, key: str) -> list:
    """
        Build the remove_setting argv for a registry path.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "Remove-ItemProperty -Path '{}' -Name '{}'"
    ).format(path, key)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _file_read_argv(path: str) -> list:
    """
        Build the read argv for a plain-text file.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "Get-Content -Path '{}' -Raw -ErrorAction Stop"
    ).format(path)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _file_write_argv(path: str) -> list:
    """
        Build the write argv for a plain-text file via Set-Content.
    """
    script = (
        "$content = [Console]::In.ReadToEnd(); "
        "Set-Content -Path '{}' -Value $content -NoNewline -Encoding utf8"
    ).format(path)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


_TCPIP_PATH = r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
_FILE_PATH = "C:\\ProgramData\\app\\config.ini"


# ----------------------------------------------------------------------
# read_setting — registry
# ----------------------------------------------------------------------


def test_read_setting_registry_runs_get_item_property():
    """
        read_setting on a registry source dispatches Get-ItemProperty
        and returns a WindowsSettingValue.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="7200000\n", stderr="")
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    info = manager.read_setting(file=_TCPIP_PATH, key="KeepAliveTime")
    testbase.assert_equal(info.key, "KeepAliveTime")
    testbase.assert_equal(info.value, "7200000")
    testbase.assert_equal(info.routing, "registry")
    return


def test_read_setting_registry_missing_key_raises():
    """
        read_setting raises WindowsConfigQueryError when the value is
        absent.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="MissingKey",
    ))] = RunResult(
        exit_code=1,
        stdout="",
        stderr="PropertyNotFound: MissingKey does not exist",
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    raised = False
    try:
        manager.read_setting(file=_TCPIP_PATH, key="MissingKey")
    except WindowsConfigQueryError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# read_setting — plain text
# ----------------------------------------------------------------------


def test_read_setting_file_parses_value():
    """
        read_setting on a plain file parses shell-style KEY=VALUE.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_file_read_argv(_FILE_PATH))] = RunResult(
        exit_code=0, stdout='LOG_LEVEL="info"\nFOO=bar\n', stderr="",
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    info = manager.read_setting(file=_FILE_PATH, key="LOG_LEVEL")
    testbase.assert_equal(info.key, "LOG_LEVEL")
    testbase.assert_equal(info.value, "info")
    testbase.assert_equal(info.routing, "file")
    return


def test_read_setting_file_missing_raises():
    """
        read_setting raises when the file is unreadable.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=1, stdout="", stderr="Cannot find path",
        ),
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    raised = False
    try:
        manager.read_setting(file=_FILE_PATH, key="LOG_LEVEL")
    except WindowsConfigQueryError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# write_setting — registry
# ----------------------------------------------------------------------


def test_write_setting_registry_dword_inferred():
    """
        write_setting on a registry source with an int value infers
        DWord and dispatches the documented Set-ItemProperty argv.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="100\n", stderr="")
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.write_setting(
        file=_TCPIP_PATH, key="KeepAliveTime", value=7200000,
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SUCCESS)
    testbase.assert_equal(result.routing, "registry")
    expected = _registry_set_argv(
        path=_TCPIP_PATH,
        key="KeepAliveTime",
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


def test_write_setting_registry_string_inferred():
    """
        String values are written with -Type String.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="HostName",
    ))] = RunResult(exit_code=0, stdout="oldhost\n", stderr="")
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.write_setting(
        file=_TCPIP_PATH, key="HostName", value="newhost",
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SUCCESS)
    expected = _registry_set_argv(
        path=_TCPIP_PATH,
        key="HostName",
        value_literal="'newhost'",
        reg_type="String",
    )
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_write_setting_registry_multistring_inferred():
    """
        list / tuple values are written with -Type MultiString.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="DependOnService",
    ))] = RunResult(exit_code=0, stdout="OldService\n", stderr="")
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.write_setting(
        file=_TCPIP_PATH,
        key="DependOnService",
        value=["NetworkProvider", "RpcSs"],
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_true("MultiString" in write_calls[0]["args"][-1])
    testbase.assert_true("NetworkProvider" in write_calls[0]["args"][-1])
    return


def test_write_setting_registry_skipped_when_value_matches():
    """
        write_setting returns SKIPPED when current value matches.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="7200000\n", stderr="")
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.write_setting(
        file=_TCPIP_PATH, key="KeepAliveTime", value=7200000,
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_write_setting_registry_dry_run_no_runner_call():
    """
        Dry-run short-circuits write_setting without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(dry_run=True),
    )
    result = manager.write_setting(
        file=_TCPIP_PATH, key="KeepAliveTime", value=7200000,
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_write_setting_registry_hklm_elevates():
    """
        HKLM writes always elevate, regardless of config.elevated.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="100\n", stderr="")
    manager = WindowsConfigurationManager(
        runner=runner,
        # Note: config.elevated is False but HKLM still forces elevation.
        config=WindowsConfigManagerConfig(elevated=False),
    )
    manager.write_setting(
        file=_TCPIP_PATH, key="KeepAliveTime", value=7200000,
    )
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_write_setting_registry_hkcu_does_not_elevate():
    """
        HKCU writes never elevate, even when config.elevated is True.
    """
    hkcu_path = r"HKCU:\Software\Test"
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_registry_get_argv(
        path=hkcu_path, key="Value",
    ))] = RunResult(exit_code=0, stdout="old\n", stderr="")
    manager = WindowsConfigurationManager(
        runner=runner,
        config=WindowsConfigManagerConfig(elevated=True),
    )
    manager.write_setting(
        file=hkcu_path, key="Value", value="newvalue",
    )
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# write_setting — plain file
# ----------------------------------------------------------------------


def test_write_setting_file_rewrites_content():
    """
        write_setting on a plain file writes the new content via stdin.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_file_read_argv(_FILE_PATH))] = RunResult(
        exit_code=0, stdout='FOO="oldvalue"\n', stderr="",
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.write_setting(
        file=_FILE_PATH, key="FOO", value="newvalue",
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SUCCESS)
    testbase.assert_equal(result.routing, "file")
    write_calls = [
        c for c in runner.calls
        if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_true('FOO="newvalue"' in write_calls[0]["stdin"])
    return


def test_write_setting_file_skipped_when_already_set():
    """
        write_setting on a plain file returns SKIPPED when the desired
        line already matches.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_file_read_argv(_FILE_PATH))] = RunResult(
        exit_code=0, stdout='FOO="bar"\n', stderr="",
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.write_setting(
        file=_FILE_PATH, key="FOO", value="bar",
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# remove_setting
# ----------------------------------------------------------------------


def test_remove_setting_registry_runs_remove_item_property():
    """
        remove_setting on a registry source runs Remove-ItemProperty.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="7200000\n", stderr="")
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.remove_setting(
        file=_TCPIP_PATH, key="KeepAliveTime",
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SUCCESS)
    expected = _registry_remove_argv(
        path=_TCPIP_PATH, key="KeepAliveTime",
    )
    write_calls = [
        c for c in runner.calls
        if "Remove-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_remove_setting_registry_skipped_when_absent():
    """
        remove_setting returns SKIPPED when the value is absent.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="KeepAliveTime",
    ))] = RunResult(
        exit_code=1,
        stdout="",
        stderr="PropertyNotFound: KeepAliveTime does not exist",
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.remove_setting(
        file=_TCPIP_PATH, key="KeepAliveTime",
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "Remove-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_remove_setting_file_rewrites_without_key():
    """
        remove_setting on a plain file rewrites without the targeted key.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_file_read_argv(_FILE_PATH))] = RunResult(
        exit_code=0,
        stdout='FOO="bar"\nBAR="baz"\n',
        stderr="",
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.remove_setting(file=_FILE_PATH, key="FOO")
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    stdin = write_calls[0]["stdin"]
    testbase.assert_true('FOO=' not in stdin)
    testbase.assert_true('BAR="baz"' in stdin)
    return


# ----------------------------------------------------------------------
# list_settings
# ----------------------------------------------------------------------


def test_list_settings_registry_parses_json():
    """
        list_settings on a registry source parses ConvertTo-Json output.
    """
    runner = _MockCommandRunner()
    payload = json.dumps({"KeepAliveTime": 7200000, "HostName": "myhost"})
    # Use default_result for the dynamic list script.
    runner.default_result = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    listed = manager.list_settings(file=_TCPIP_PATH)
    testbase.assert_equal(listed.get("KeepAliveTime"), "7200000")
    testbase.assert_equal(listed.get("HostName"), "myhost")
    return


def test_list_settings_file_parses_kv():
    """
        list_settings on a plain file parses shell-style KEY=VALUE.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_file_read_argv(_FILE_PATH))] = RunResult(
        exit_code=0, stdout='FOO="bar"\nBAR=baz\n', stderr="",
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    listed = manager.list_settings(file=_FILE_PATH)
    testbase.assert_equal(listed.get("FOO"), "bar")
    testbase.assert_equal(listed.get("BAR"), "baz")
    return


# ----------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------


def test_routing_registry_vs_file():
    """
        HKLM:/HKCU: prefixes route to registry; other paths route to
        plain file.  This is exercised indirectly via the result
        ``routing`` field.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="100\n", stderr="")
    runner.results[tuple(_file_read_argv(_FILE_PATH))] = RunResult(
        exit_code=0, stdout='FOO="oldvalue"\n', stderr="",
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )

    reg_result = manager.write_setting(
        file=_TCPIP_PATH, key="KeepAliveTime", value=7200000,
    )
    testbase.assert_equal(reg_result.routing, "registry")

    file_result = manager.write_setting(
        file=_FILE_PATH, key="FOO", value="newvalue",
    )
    testbase.assert_equal(file_result.routing, "file")
    return


# ----------------------------------------------------------------------
# reload_configuration
# ----------------------------------------------------------------------


def test_reload_configuration_runs_restart_service():
    """
        reload_configuration dispatches Restart-Service for the named
        service.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.reload_configuration(service="spooler")
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if "Restart-Service" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    expected = _build_powershell_argv(
        script="Restart-Service -Name 'spooler'",
    )
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_reload_configuration_none_returns_skipped():
    """
        reload_configuration with the literal "none" sentinel returns
        SKIPPED without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.reload_configuration(service="none")
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SKIPPED)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# Backup / restore
# ----------------------------------------------------------------------


def test_backup_registry_runs_reg_export():
    """
        backup_configuration on a registry source dispatches reg.exe
        export with the colon-less hive form.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.backup_configuration(file=_TCPIP_PATH)
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SUCCESS)
    testbase.assert_equal(result.routing, "registry")
    # The HKLM: prefix must be converted to HKLM\ for reg.exe.
    reg_calls = [c for c in runner.calls if c["args"][0] == "reg.exe"]
    testbase.assert_equal(len(reg_calls), 1)
    argv = reg_calls[0]["args"]
    testbase.assert_equal(argv[0], "reg.exe")
    testbase.assert_equal(argv[1], "export")
    # Path should NOT contain the colon between hive and path.
    testbase.assert_true(argv[2].startswith("HKLM\\"))
    testbase.assert_true(":" not in argv[2].split("\\")[0])
    testbase.assert_equal(argv[-1], "/y")
    return


def test_backup_file_runs_copy_item():
    """
        backup_configuration on a plain file dispatches Copy-Item via
        PowerShell.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.backup_configuration(file=_FILE_PATH)
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SUCCESS)
    testbase.assert_equal(result.routing, "file")
    write_calls = [
        c for c in runner.calls
        if "Copy-Item" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_true(_FILE_PATH in write_calls[0]["args"][-1])
    return


def test_restore_registry_runs_reg_import():
    """
        restore_configuration with a registry target dispatches reg
        import.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.restore_configuration(
        backup_path="C:\\backups\\tcpip.reg",
        target_path=_TCPIP_PATH,
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SUCCESS)
    testbase.assert_equal(result.routing, "registry")
    reg_calls = [c for c in runner.calls if c["args"][0] == "reg.exe"]
    testbase.assert_equal(len(reg_calls), 1)
    testbase.assert_equal(reg_calls[0]["args"][1], "import")
    testbase.assert_equal(
        reg_calls[0]["args"][2], "C:\\backups\\tcpip.reg",
    )
    return


def test_restore_file_skipped_when_byte_match():
    """
        restore_configuration on a plain file returns SKIPPED when the
        target byte-matches the backup.
    """
    backup_path = "C:\\backups\\config.bak"
    target_path = _FILE_PATH
    runner = _MockCommandRunner()
    runner.results[tuple(_file_read_argv(backup_path))] = RunResult(
        exit_code=0, stdout='FOO="bar"\n', stderr="",
    )
    runner.results[tuple(_file_read_argv(target_path))] = RunResult(
        exit_code=0, stdout='FOO="bar"\n', stderr="",
    )
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    result = manager.restore_configuration(
        backup_path=backup_path, target_path=target_path,
    )
    testbase.assert_equal(result.status, WindowsConfigActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "Copy-Item" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# Type-inference
# ----------------------------------------------------------------------


def test_infer_reg_type_table():
    """
        Type inference covers bool, int, str, expand-var-string, list,
        and bytes.
    """
    from ccai.sysman.windows.configurationmanagement import (
        _infer_reg_type,
    )
    testbase.assert_equal(_infer_reg_type(value=True), "DWord")
    testbase.assert_equal(_infer_reg_type(value=42), "DWord")
    testbase.assert_equal(_infer_reg_type(value="hello"), "String")
    testbase.assert_equal(
        _infer_reg_type(value="%SystemRoot%\\bin"),
        "ExpandString",
    )
    testbase.assert_equal(
        _infer_reg_type(value=["a", "b"]),
        "MultiString",
    )
    testbase.assert_equal(
        _infer_reg_type(value=b"\x01\x02"),
        "Binary",
    )
    return


# ----------------------------------------------------------------------
# Reads never elevate
# ----------------------------------------------------------------------


def test_reads_never_elevate():
    """
        Reads pass elevated=False even when config.elevated is True.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="7200000\n", stderr="")
    manager = WindowsConfigurationManager(
        runner=runner,
        config=WindowsConfigManagerConfig(elevated=True),
    )
    manager.read_setting(file=_TCPIP_PATH, key="KeepAliveTime")
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
        Every PowerShell-dispatched command runs with -NoProfile and
        -NonInteractive.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_registry_get_argv(
        path=_TCPIP_PATH, key="KeepAliveTime",
    ))] = RunResult(exit_code=0, stdout="100\n", stderr="")
    manager = WindowsConfigurationManager(
        runner=runner, config=WindowsConfigManagerConfig(),
    )
    manager.read_setting(file=_TCPIP_PATH, key="KeepAliveTime")
    for call_record in runner.calls:
        argv = call_record["args"]
        # Skip reg.exe argvs — they are NOT PowerShell-wrapped.
        if argv[0] == "reg.exe":
            continue
        testbase.assert_equal(argv[0], "powershell.exe")
        testbase.assert_equal(argv[1], "-NoProfile")
        testbase.assert_equal(argv[2], "-NonInteractive")
        testbase.assert_equal(argv[3], "-Command")
        testbase.assert_equal(len(argv), 5)
    return
