"""
    Tests for :class:`WindowsEnvironmentConfigurationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    No live PowerShell process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.environmentconfiguration import (
    WindowsEnvActionResult,
    WindowsEnvActionStatus,
    WindowsEnvironmentConfigurationManager,
    WindowsEnvironmentConfigurationManagerConfig,
    WindowsEnvQueryError,
    WindowsEnvScope,
    WindowsEnvValue,
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


def _machine_get_argv(name: str) -> list:
    """
        Build the read argv the manager produces for a Machine-scope read.
    """
    script = (
        "[Environment]::GetEnvironmentVariable('{}', 'Machine')"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _machine_set_argv(*, name: str, value: str) -> list:
    """
        Build the write argv the manager produces for a Machine-scope set.
    """
    script = (
        "[Environment]::SetEnvironmentVariable('{}', '{}', 'Machine')"
    ).format(name, value)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _machine_unset_argv(name: str) -> list:
    """
        Build the unset argv the manager produces for a Machine-scope unset.
    """
    script = (
        "[Environment]::SetEnvironmentVariable('{}', $null, 'Machine')"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _user_get_argv(name: str) -> list:
    """
        Build the read argv the manager produces for a User-scope read of
        the current user.
    """
    script = (
        "[Environment]::GetEnvironmentVariable('{}', 'User')"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _user_set_argv(*, name: str, value: str) -> list:
    """
        Build the write argv for a User-scope set of the current user.
    """
    script = (
        "[Environment]::SetEnvironmentVariable('{}', '{}', 'User')"
    ).format(name, value)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _user_unset_argv(name: str) -> list:
    """
        Build the unset argv for a User-scope unset of the current user.
    """
    script = (
        "[Environment]::SetEnvironmentVariable('{}', $null, 'User')"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _sid_resolve_argv(user: str) -> list:
    """
        Build the SID-resolution argv.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "(New-Object System.Security.Principal.NTAccount('{}')).Translate("
        "[System.Security.Principal.SecurityIdentifier]).Value"
    ).format(user)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _other_user_set_argv(*, sid: str, name: str, value: str) -> list:
    """
        Build the write argv for an other-user set via per-SID registry.
    """
    registry_path = "Registry::HKEY_USERS\\{}\\Environment".format(sid)
    script = (
        "Set-ItemProperty -Path '{}' -Name '{}' -Value '{}'"
    ).format(registry_path, name, value)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _other_user_remove_argv(*, sid: str, name: str) -> list:
    """
        Build the remove argv for an other-user unset via per-SID registry.
    """
    registry_path = "Registry::HKEY_USERS\\{}\\Environment".format(sid)
    script = (
        "Remove-ItemProperty -Path '{}' -Name '{}'"
    ).format(registry_path, name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _other_user_get_argv(*, sid: str, name: str) -> list:
    """
        Build the read argv for an other-user get via per-SID registry.
    """
    registry_path = "Registry::HKEY_USERS\\{}\\Environment".format(sid)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "(Get-ItemProperty -Path '{path}' -Name '{key}' "
        "-ErrorAction Stop).'{key}'"
    ).format(path=registry_path, key=name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


# ----------------------------------------------------------------------
# set_system_env
# ----------------------------------------------------------------------


def test_set_system_env_dispatches_setenvironmentvariable_machine():
    """
        set_system_env dispatches the documented Machine-scope script.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_machine_get_argv("FOO"))] = RunResult(
        exit_code=0, stdout="oldvalue\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(),
    )
    result = manager.set_system_env(name="FOO", value="bar")
    testbase.assert_equal(result.status, WindowsEnvActionStatus.SUCCESS)
    expected = _machine_set_argv(name="FOO", value="bar")
    write_calls = [
        c for c in runner.calls
        if "SetEnvironmentVariable('FOO', 'bar'" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    testbase.assert_equal(result.scope, WindowsEnvScope.SYSTEM)
    return


def test_set_system_env_skipped_when_value_matches():
    """
        set_system_env returns SKIPPED when the existing Machine-scope
        value already matches.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_machine_get_argv("FOO"))] = RunResult(
        exit_code=0, stdout="bar\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(),
    )
    result = manager.set_system_env(name="FOO", value="bar")
    testbase.assert_equal(result.status, WindowsEnvActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "SetEnvironmentVariable('FOO', 'bar'" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_system_env_dry_run_short_circuits():
    """
        Dry-run short-circuits set_system_env without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(dry_run=True),
    )
    result = manager.set_system_env(name="FOO", value="bar")
    testbase.assert_equal(result.status, WindowsEnvActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    expected = _machine_set_argv(name="FOO", value="bar")
    testbase.assert_equal(result.command, expected)
    return


def test_set_system_env_elevates_when_configured():
    """
        System writes pass ``elevated=True`` when config.elevated is True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_machine_get_argv("FOO"))] = RunResult(
        exit_code=0, stdout="oldvalue\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(elevated=True),
    )
    manager.set_system_env(name="FOO", value="bar")
    write_calls = [
        c for c in runner.calls
        if "SetEnvironmentVariable('FOO', 'bar'" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# unset_system_env
# ----------------------------------------------------------------------


def test_unset_system_env_dispatches_null_value():
    """
        unset_system_env dispatches a ``$null``-valued
        SetEnvironmentVariable call.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_machine_get_argv("FOO"))] = RunResult(
        exit_code=0, stdout="some value\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(),
    )
    result = manager.unset_system_env(name="FOO")
    testbase.assert_equal(result.status, WindowsEnvActionStatus.SUCCESS)
    expected = _machine_unset_argv("FOO")
    write_calls = [
        c for c in runner.calls
        if "$null, 'Machine'" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_unset_system_env_skipped_when_absent():
    """
        unset_system_env returns SKIPPED when the variable is not
        currently set.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_machine_get_argv("MISSING"))] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(),
    )
    result = manager.unset_system_env(name="MISSING")
    testbase.assert_equal(result.status, WindowsEnvActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "$null, 'Machine'" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# get_system_env / list_system_env
# ----------------------------------------------------------------------


def test_get_system_env_returns_value():
    """
        get_system_env returns a :class:`WindowsEnvValue` for a present
        variable.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_machine_get_argv("PATH"))] = RunResult(
        exit_code=0, stdout="C:\\Windows\\System32\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(),
    )
    info = manager.get_system_env(name="PATH")
    testbase.assert_equal(info.name, "PATH")
    testbase.assert_equal(info.value, "C:\\Windows\\System32")
    testbase.assert_equal(info.scope, WindowsEnvScope.SYSTEM)
    return


def test_get_system_env_returns_none_when_empty():
    """
        get_system_env returns ``None`` when the variable is absent.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_machine_get_argv("MISSING"))] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(),
    )
    info = manager.get_system_env(name="MISSING")
    testbase.assert_equal(info, None)
    return


def test_list_system_env_parses_json_dict():
    """
        list_system_env parses the JSON dict and returns key->value.
    """
    runner = _MockCommandRunner()
    payload = json.dumps({
        "PATH": "C:\\Windows\\System32",
        "TEMP": "C:\\Windows\\Temp",
    })
    list_argv = _build_powershell_argv(
        script=(
            "[Environment]::GetEnvironmentVariables('Machine') "
            "| ConvertTo-Json -Compress"
        ),
    )
    runner.results[tuple(list_argv)] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(),
    )
    listed = manager.list_system_env()
    testbase.assert_equal(listed.get("PATH"), "C:\\Windows\\System32")
    testbase.assert_equal(listed.get("TEMP"), "C:\\Windows\\Temp")
    return


def test_list_system_env_filters_pseudo_keys():
    """
        PowerShell-pseudo keys like ``IsReadOnly`` are filtered.
    """
    runner = _MockCommandRunner()
    payload = json.dumps({
        "PATH": "C:\\Windows\\System32",
        "IsReadOnly": False,
        "PSPath": "should be filtered",
        "Count": 5,
    })
    list_argv = _build_powershell_argv(
        script=(
            "[Environment]::GetEnvironmentVariables('Machine') "
            "| ConvertTo-Json -Compress"
        ),
    )
    runner.results[tuple(list_argv)] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(),
    )
    listed = manager.list_system_env()
    testbase.assert_equal(listed.get("PATH"), "C:\\Windows\\System32")
    testbase.assert_true("IsReadOnly" not in listed)
    testbase.assert_true("PSPath" not in listed)
    testbase.assert_true("Count" not in listed)
    return


def test_list_system_env_raises_on_runner_failure():
    """
        list_system_env raises WindowsEnvQueryError on non-zero exit.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=1, stdout="", stderr="Access denied",
        ),
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(),
    )
    raised = False
    try:
        manager.list_system_env()
    except WindowsEnvQueryError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# set_user_env — current user
# ----------------------------------------------------------------------


def test_set_user_env_current_user_uses_user_scope_api():
    """
        set_user_env for the current user uses the .NET 'User' scope API.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_get_argv("FOO"))] = RunResult(
        exit_code=0, stdout="oldvalue\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(
            current_user="alice",
        ),
    )
    result = manager.set_user_env(user="alice", name="FOO", value="bar")
    testbase.assert_equal(result.status, WindowsEnvActionStatus.SUCCESS)
    expected = _user_set_argv(name="FOO", value="bar")
    write_calls = [
        c for c in runner.calls
        if "SetEnvironmentVariable('FOO', 'bar', 'User')" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_set_user_env_current_user_does_not_elevate():
    """
        Current-user writes do not pass elevated=True even when config
        is elevated.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_get_argv("FOO"))] = RunResult(
        exit_code=0, stdout="oldvalue\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(
            current_user="alice", elevated=True,
        ),
    )
    manager.set_user_env(user="alice", name="FOO", value="bar")
    write_calls = [
        c for c in runner.calls
        if "SetEnvironmentVariable('FOO', 'bar', 'User')" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# set_user_env — other user (per-SID registry)
# ----------------------------------------------------------------------


def test_set_user_env_other_user_uses_per_sid_registry():
    """
        set_user_env for a different user resolves the SID and writes
        via the per-SID registry path.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    sid = "S-1-5-21-1234567890-987654321-1122334455-1001"
    runner.results[tuple(_sid_resolve_argv("bob"))] = RunResult(
        exit_code=0, stdout=sid + "\n", stderr="",
    )
    runner.results[tuple(_other_user_get_argv(sid=sid, name="FOO"))] = RunResult(
        exit_code=0, stdout="oldvalue\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(
            current_user="alice",
        ),
    )
    result = manager.set_user_env(user="bob", name="FOO", value="bar")
    testbase.assert_equal(result.status, WindowsEnvActionStatus.SUCCESS)
    expected = _other_user_set_argv(sid=sid, name="FOO", value="bar")
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    testbase.assert_true(sid in result.registry_path)
    return


def test_set_user_env_other_user_elevates_when_configured():
    """
        Other-user writes pass elevated=True when config is elevated.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    sid = "S-1-5-21-1234567890-987654321-1122334455-1001"
    runner.results[tuple(_sid_resolve_argv("bob"))] = RunResult(
        exit_code=0, stdout=sid + "\n", stderr="",
    )
    runner.results[tuple(_other_user_get_argv(sid=sid, name="FOO"))] = RunResult(
        exit_code=0, stdout="oldvalue\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(
            current_user="alice", elevated=True,
        ),
    )
    manager.set_user_env(user="bob", name="FOO", value="bar")
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_user_env_unknown_user_returns_failed():
    """
        set_user_env returns FAILED with a descriptive message when the
        SID translation fails.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_sid_resolve_argv("nobody"))] = RunResult(
        exit_code=1, stdout="", stderr="user not found",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(
            current_user="alice",
        ),
    )
    result = manager.set_user_env(
        user="nobody", name="FOO", value="bar",
    )
    testbase.assert_equal(result.status, WindowsEnvActionStatus.FAILED)
    testbase.assert_true("failed to resolve SID" in result.message)
    write_calls = [
        c for c in runner.calls
        if "Set-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# unset_user_env
# ----------------------------------------------------------------------


def test_unset_user_env_current_user_dispatches_null_value():
    """
        unset_user_env for the current user dispatches a ``$null``-
        valued SetEnvironmentVariable call.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_get_argv("FOO"))] = RunResult(
        exit_code=0, stdout="some value\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(
            current_user="alice",
        ),
    )
    result = manager.unset_user_env(user="alice", name="FOO")
    testbase.assert_equal(result.status, WindowsEnvActionStatus.SUCCESS)
    expected = _user_unset_argv("FOO")
    write_calls = [
        c for c in runner.calls
        if "$null, 'User'" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_unset_user_env_other_user_uses_remove_item_property():
    """
        unset_user_env for a different user routes through
        Remove-ItemProperty against the per-SID registry path.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    sid = "S-1-5-21-1234567890-987654321-1122334455-1001"
    runner.results[tuple(_sid_resolve_argv("bob"))] = RunResult(
        exit_code=0, stdout=sid + "\n", stderr="",
    )
    runner.results[tuple(_other_user_get_argv(sid=sid, name="FOO"))] = RunResult(
        exit_code=0, stdout="some value\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(
            current_user="alice",
        ),
    )
    result = manager.unset_user_env(user="bob", name="FOO")
    testbase.assert_equal(result.status, WindowsEnvActionStatus.SUCCESS)
    expected = _other_user_remove_argv(sid=sid, name="FOO")
    write_calls = [
        c for c in runner.calls
        if "Remove-ItemProperty" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


# ----------------------------------------------------------------------
# Reads never elevate
# ----------------------------------------------------------------------


def test_reads_never_elevate():
    """
        Reads (machine and user) pass elevated=False even with
        config.elevated=True.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_machine_get_argv("PATH"))] = RunResult(
        exit_code=0, stdout="some\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(elevated=True),
    )
    manager.get_system_env(name="PATH")
    elevated_reads = [
        c for c in runner.calls
        if "GetEnvironmentVariable" in c["args"][-1] and c["elevated"]
    ]
    testbase.assert_equal(elevated_reads, [])
    return


# ----------------------------------------------------------------------
# Argv shape
# ----------------------------------------------------------------------


def test_powershell_argv_includes_noprofile():
    """
        Every dispatched command runs PowerShell with -NoProfile and
        -NonInteractive.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_machine_get_argv("FOO"))] = RunResult(
        exit_code=0, stdout="bar\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(),
    )
    manager.get_system_env(name="FOO")
    for call_record in runner.calls:
        argv = call_record["args"]
        testbase.assert_equal(argv[0], "powershell.exe")
        testbase.assert_equal(argv[1], "-NoProfile")
        testbase.assert_equal(argv[2], "-NonInteractive")
        testbase.assert_equal(argv[3], "-Command")
        testbase.assert_equal(len(argv), 5)
    return


# ----------------------------------------------------------------------
# Domain-qualified usernames
# ----------------------------------------------------------------------


def test_current_user_matched_with_domain_prefix():
    """
        Domain-qualified usernames match the configured current_user
        on the bare-account portion.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_get_argv("FOO"))] = RunResult(
        exit_code=0, stdout="oldvalue\n", stderr="",
    )
    manager = WindowsEnvironmentConfigurationManager(
        runner=runner,
        config=WindowsEnvironmentConfigurationManagerConfig(
            current_user="alice",
        ),
    )
    # CONTOSO\alice should match configured "alice" via bare-name
    # comparison and route to the User-scope path.
    result = manager.set_user_env(
        user="CONTOSO\\alice", name="FOO", value="bar",
    )
    testbase.assert_equal(result.status, WindowsEnvActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if "SetEnvironmentVariable('FOO', 'bar', 'User')" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    return
