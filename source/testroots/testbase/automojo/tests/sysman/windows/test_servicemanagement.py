"""
    Tests for :class:`WindowsServiceManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    No live PowerShell process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.servicemanagement import (
    WindowsServiceActionStatus,
    WindowsServiceInfo,
    WindowsServiceManager,
    WindowsServiceManagerConfig,
    WindowsServiceQueryError,
    WindowsServiceScope,
    WindowsServiceStatus,
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
# Helpers
# ----------------------------------------------------------------------


_GET_SERVICE_SCRIPT_TEMPLATE = (
    "Get-Service -Name '{name}' "
    "| Select-Object Name, DisplayName, Status, StartType, MachineName "
    "| ConvertTo-Json -Compress -Depth 5"
)

_LIST_SERVICES_SCRIPT = (
    "Get-Service "
    "| Select-Object Name, DisplayName, Status, StartType "
    "| ConvertTo-Json -Compress -Depth 5"
)


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


def _get_service_argv(name: str) -> list:
    """
        Build the argv the manager produces for a get_service_status
        probe of ``name``.
    """
    script = _GET_SERVICE_SCRIPT_TEMPLATE.format(name=name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _service_status_payload(
    *,
    name: str,
    status_int: int = 4,
    start_type_int: int = 2,
    display_name: str = "Example Service",
) -> str:
    """
        Build a representative ``Get-Service | ConvertTo-Json`` payload
        for a single service.
    """
    record = {
        "Name": name,
        "DisplayName": display_name,
        "Status": status_int,
        "StartType": start_type_int,
        "MachineName": ".",
    }
    rtnval = json.dumps(record)
    return rtnval


def _seed_service_state(
    *,
    runner: _MockCommandRunner,
    name: str,
    status_int: int = 4,
    start_type_int: int = 2,
    display_name: str = "Example Service",
) -> None:
    """
        Seed a ``Get-Service`` JSON result for the given service state.
    """
    argv = _get_service_argv(name=name)
    payload = _service_status_payload(
        name=name,
        status_int=status_int,
        start_type_int=start_type_int,
        display_name=display_name,
    )
    runner.results[tuple(argv)] = RunResult(
        exit_code=0,
        stdout=payload,
        stderr="",
    )
    return


def _success_runner_for(
    *,
    name: str,
    status_int: int = 4,
    start_type_int: int = 2,
) -> _MockCommandRunner:
    """
        Build a runner seeded with a service state and a default
        success :class:`RunResult` for any un-seeded write command.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_service_state(
        runner=runner,
        name=name,
        status_int=status_int,
        start_type_int=start_type_int,
    )
    return runner


# ----------------------------------------------------------------------
# start_service
# ----------------------------------------------------------------------


def test_start_service_invokes_start_service():
    """
        start_service on a stopped service dispatches
        ``Start-Service``.
    """
    runner = _success_runner_for(
        name="spooler", status_int=1, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.start_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SUCCESS)
    write_call = runner.calls[-1]
    expected = _build_powershell_argv(
        script="Start-Service -Name 'spooler'",
    )
    testbase.assert_equal(write_call["args"], expected)
    testbase.assert_equal(result.service_name, "spooler")
    testbase.assert_equal(result.scope, WindowsServiceScope.SYSTEM)
    return


def test_start_service_skipped_when_running():
    """
        start_service returns SKIPPED when the service is already
        running.
    """
    runner = _success_runner_for(
        name="spooler", status_int=4, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.start_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "Start-Service" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_start_service_dry_run_short_circuits():
    """
        Dry-run short-circuits start_service without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsServiceManager(
        runner=runner,
        config=WindowsServiceManagerConfig(dry_run=True),
    )
    result = manager.start_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    expected = _build_powershell_argv(
        script="Start-Service -Name 'spooler'",
    )
    testbase.assert_equal(result.command, expected)
    return


# ----------------------------------------------------------------------
# stop_service
# ----------------------------------------------------------------------


def test_stop_service_invokes_stop_service():
    """
        stop_service on a running service dispatches ``Stop-Service``.
    """
    runner = _success_runner_for(
        name="spooler", status_int=4, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.stop_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SUCCESS)
    write_call = runner.calls[-1]
    expected = _build_powershell_argv(
        script="Stop-Service -Name 'spooler'",
    )
    testbase.assert_equal(write_call["args"], expected)
    return


def test_stop_service_skipped_when_stopped():
    """
        stop_service returns SKIPPED when the service is already
        stopped.
    """
    runner = _success_runner_for(
        name="spooler", status_int=1, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.stop_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "Stop-Service" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# restart_service / reload_service
# ----------------------------------------------------------------------


def test_restart_service_unconditional():
    """
        restart_service always dispatches ``Restart-Service``, even
        when the service is already running.
    """
    runner = _success_runner_for(
        name="spooler", status_int=4, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.restart_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SUCCESS)
    restart_calls = [
        c for c in runner.calls
        if "Restart-Service" in c["args"][-1]
    ]
    testbase.assert_equal(len(restart_calls), 1)
    expected = _build_powershell_argv(
        script="Restart-Service -Name 'spooler'",
    )
    testbase.assert_equal(restart_calls[0]["args"], expected)
    return


def test_reload_service_returns_failed_with_message():
    """
        reload_service returns FAILED without invoking the runner
        because Windows services have no native reload signal.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.reload_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.FAILED)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.message,
        "Windows services have no native reload signal; "
        "use restart_service for a reconfigure-with-restart.",
    )
    return


# ----------------------------------------------------------------------
# enable_service / disable_service / mask_service / unmask_service
# ----------------------------------------------------------------------


def test_enable_service_invokes_set_service_automatic():
    """
        enable_service on a non-Automatic service dispatches
        ``Set-Service -StartupType Automatic``.
    """
    runner = _success_runner_for(
        name="spooler", status_int=1, start_type_int=3,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.enable_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SUCCESS)
    expected = _build_powershell_argv(
        script="Set-Service -Name 'spooler' -StartupType Automatic",
    )
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_enable_service_skipped_when_automatic():
    """
        enable_service returns SKIPPED when the service is already
        configured for ``Automatic`` start.
    """
    runner = _success_runner_for(
        name="spooler", status_int=4, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.enable_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SKIPPED)
    enable_calls = [
        c for c in runner.calls
        if "Set-Service" in c["args"][-1]
    ]
    testbase.assert_equal(enable_calls, [])
    return


def test_disable_service_invokes_set_service_disabled():
    """
        disable_service on a non-Disabled service dispatches
        ``Set-Service -StartupType Disabled``.
    """
    runner = _success_runner_for(
        name="spooler", status_int=4, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.disable_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SUCCESS)
    expected = _build_powershell_argv(
        script="Set-Service -Name 'spooler' -StartupType Disabled",
    )
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_mask_service_invokes_set_service_disabled():
    """
        mask_service emulates "mask" via
        ``Set-Service -StartupType Disabled`` because Windows has no
        native mask primitive.  When the service is currently
        Automatic, the call dispatches a disable.
    """
    runner = _success_runner_for(
        name="spooler", status_int=4, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.mask_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SUCCESS)
    expected = _build_powershell_argv(
        script="Set-Service -Name 'spooler' -StartupType Disabled",
    )
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_unmask_service_invokes_set_service_automatic():
    """
        unmask_service inverts the mask fallback: when the service is
        currently Disabled (== masked in this fallback), the call
        dispatches ``Set-Service -StartupType Automatic``.
    """
    runner = _success_runner_for(
        name="spooler", status_int=1, start_type_int=4,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.unmask_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SUCCESS)
    expected = _build_powershell_argv(
        script="Set-Service -Name 'spooler' -StartupType Automatic",
    )
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


# ----------------------------------------------------------------------
# get_service_status / list_services
# ----------------------------------------------------------------------


def test_get_service_status_parses_json():
    """
        get_service_status parses ``Get-Service | ConvertTo-Json`` into
        a :class:`WindowsServiceStatus`.
    """
    runner = _MockCommandRunner()
    payload = _service_status_payload(
        name="spooler",
        status_int=4,
        start_type_int=2,
        display_name="Print Spooler",
    )
    runner.results[tuple(_get_service_argv(name="spooler"))] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    status = manager.get_service_status(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(status.service_name, "spooler")
    testbase.assert_equal(status.scope, WindowsServiceScope.SYSTEM)
    testbase.assert_equal(status.state, "Running")
    testbase.assert_equal(status.start_type, "Automatic")
    testbase.assert_equal(status.display_name, "Print Spooler")
    return


def test_get_service_status_decodes_status_int():
    """
        Each ``ServiceControllerStatus`` integer decodes to the
        documented status string.
    """
    expected_pairs = [
        (1, "Stopped"),
        (2, "StartPending"),
        (3, "StopPending"),
        (4, "Running"),
        (5, "ContinuePending"),
        (6, "PausePending"),
        (7, "Paused"),
    ]
    for status_int, expected_str in expected_pairs:
        runner = _MockCommandRunner()
        payload = _service_status_payload(
            name="svc",
            status_int=status_int,
            start_type_int=2,
        )
        runner.results[tuple(_get_service_argv(name="svc"))] = RunResult(
            exit_code=0, stdout=payload, stderr="",
        )
        manager = WindowsServiceManager(
            runner=runner, config=WindowsServiceManagerConfig(),
        )
        status = manager.get_service_status(
            name="svc", scope=WindowsServiceScope.SYSTEM,
        )
        testbase.assert_equal(status.state, expected_str)
    return


def test_get_service_status_decodes_start_type_int():
    """
        Each ``ServiceStartMode`` integer decodes to the documented
        start-type string.
    """
    expected_pairs = [
        (0, "Boot"),
        (1, "System"),
        (2, "Automatic"),
        (3, "Manual"),
        (4, "Disabled"),
        (5, "AutomaticDelayedStart"),
    ]
    for start_type_int, expected_str in expected_pairs:
        runner = _MockCommandRunner()
        payload = _service_status_payload(
            name="svc",
            status_int=4,
            start_type_int=start_type_int,
        )
        runner.results[tuple(_get_service_argv(name="svc"))] = RunResult(
            exit_code=0, stdout=payload, stderr="",
        )
        manager = WindowsServiceManager(
            runner=runner, config=WindowsServiceManagerConfig(),
        )
        status = manager.get_service_status(
            name="svc", scope=WindowsServiceScope.SYSTEM,
        )
        testbase.assert_equal(status.start_type, expected_str)
    return


def test_list_services_parses_json_array():
    """
        list_services parses a JSON array from
        ``Get-Service | ConvertTo-Json`` into
        :class:`WindowsServiceInfo` rows.
    """
    records = [
        {
            "Name": "spooler",
            "DisplayName": "Print Spooler",
            "Status": 4,
            "StartType": 2,
        },
        {
            "Name": "wuauserv",
            "DisplayName": "Windows Update",
            "Status": 1,
            "StartType": 3,
        },
        {
            "Name": "DisabledExample",
            "DisplayName": "Disabled Example",
            "Status": 1,
            "StartType": 4,
        },
    ]
    runner = _MockCommandRunner()
    runner.results[tuple(_build_powershell_argv(
        script=_LIST_SERVICES_SCRIPT,
    ))] = RunResult(
        exit_code=0, stdout=json.dumps(records), stderr="",
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    listed = manager.list_services(scope=WindowsServiceScope.SYSTEM)

    testbase.assert_equal(len(listed), 3)
    first = listed[0]
    testbase.assert_equal(first.name, "spooler")
    testbase.assert_equal(first.status, "Running")
    testbase.assert_equal(first.start_type, "Automatic")
    testbase.assert_equal(first.display_name, "Print Spooler")
    testbase.assert_equal(first.scope, WindowsServiceScope.SYSTEM)

    second = listed[1]
    testbase.assert_equal(second.name, "wuauserv")
    testbase.assert_equal(second.status, "Stopped")
    testbase.assert_equal(second.start_type, "Manual")

    third = listed[2]
    testbase.assert_equal(third.name, "DisabledExample")
    testbase.assert_equal(third.status, "Stopped")
    testbase.assert_equal(third.start_type, "Disabled")
    return


# ----------------------------------------------------------------------
# reload_daemon
# ----------------------------------------------------------------------


def test_reload_daemon_returns_success_noop():
    """
        reload_daemon returns SUCCESS without invoking the runner
        because Windows has no daemon-reload equivalent.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsServiceManager(
        runner=runner,
        config=WindowsServiceManagerConfig(elevated=True),
    )
    result = manager.reload_daemon(scope=WindowsServiceScope.SYSTEM)
    testbase.assert_equal(result.status, WindowsServiceActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.message,
        "Windows Service Control Manager applies service definitions "
        "immediately; no daemon-reload concept.",
    )
    return


# ----------------------------------------------------------------------
# Scope + elevation
# ----------------------------------------------------------------------


def test_user_scope_returns_failed_with_message():
    """
        USER-scope writes return a typed FAILED result without invoking
        the runner.  Per-user services are out of scope for this pilot.
    """
    runner = _MockCommandRunner()
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.start_service(
        name="ContosoAgent_1234", scope=WindowsServiceScope.USER,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.FAILED)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.message,
        "USER-scope service operations (per-user services suffixed with "
        "_<sessionid>) are not supported in this pilot.",
    )
    testbase.assert_equal(result.scope, WindowsServiceScope.USER)
    return


def test_write_with_elevated_config_passes_elevated_true():
    """
        SYSTEM-scope writes pass ``elevated=True`` when the config is
        configured to elevate.
    """
    runner = _success_runner_for(
        name="spooler", status_int=1, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner,
        config=WindowsServiceManagerConfig(elevated=True),
    )
    manager.start_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    write_calls = [
        c for c in runner.calls
        if "Start-Service" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_read_never_elevates():
    """
        Reads and idempotency-probe commands never elevate, even when
        the config is configured to elevate writes.
    """
    runner = _success_runner_for(
        name="spooler", status_int=1, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner,
        config=WindowsServiceManagerConfig(elevated=True),
    )
    # Direct read.
    manager.get_service_status(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    # Write whose idempotency probe must also not elevate.
    manager.start_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )

    elevated_probe_calls = [
        c for c in runner.calls
        if "Get-Service" in c["args"][-1] and c["elevated"]
    ]
    testbase.assert_equal(elevated_probe_calls, [])

    write_calls = [
        c for c in runner.calls
        if "Start-Service" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Failure surfaces
# ----------------------------------------------------------------------


def test_failed_command_returns_failed_result_with_stderr():
    """
        A non-zero exit from the write maps to FAILED with the
        captured stderr / exit_code / command preserved on the result.
    """
    runner = _success_runner_for(
        name="spooler", status_int=1, start_type_int=2,
    )
    write_argv = _build_powershell_argv(
        script="Start-Service -Name 'spooler'",
    )
    runner.results[tuple(write_argv)] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Access is denied",
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    result = manager.start_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, WindowsServiceActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "Access is denied")
    testbase.assert_equal(result.command, write_argv)
    return


# ----------------------------------------------------------------------
# Argv shape
# ----------------------------------------------------------------------


def test_powershell_argv_construction_includes_noprofile():
    """
        Every dispatched command runs PowerShell with ``-NoProfile`` and
        ``-NonInteractive`` so $PROFILE and interactive prompts cannot
        mutate cmdlet behaviour.
    """
    runner = _success_runner_for(
        name="spooler", status_int=1, start_type_int=2,
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    manager.start_service(
        name="spooler", scope=WindowsServiceScope.SYSTEM,
    )
    # Every recorded call must follow the shape
    # ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "<script>"].
    for call_record in runner.calls:
        argv = call_record["args"]
        testbase.assert_equal(argv[0], "powershell.exe")
        testbase.assert_equal(argv[1], "-NoProfile")
        testbase.assert_equal(argv[2], "-NonInteractive")
        testbase.assert_equal(argv[3], "-Command")
        testbase.assert_equal(len(argv), 5)
    return


# ----------------------------------------------------------------------
# Sanity: typed query exception
# ----------------------------------------------------------------------


def test_user_scope_read_raises_query_error():
    """
        USER-scope reads raise :class:`WindowsServiceQueryError` before
        invoking the runner.  Belt-and-braces coverage of the typed
        exception path that complements the FAILED-result write path
        tested above.
    """
    runner = _MockCommandRunner()
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    raised = False
    try:
        manager.get_service_status(
            name="ContosoAgent_1234", scope=WindowsServiceScope.USER,
        )
    except WindowsServiceQueryError:
        raised = True
    testbase.assert_true(raised)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_get_service_status_returns_not_found_for_unknown_service():
    """
        ``Get-Service`` failing with ``NoServiceFoundForGivenName``
        produces a synthetic ``NotFound`` :class:`WindowsServiceStatus`
        instead of raising.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_service_argv(name="missing"))] = RunResult(
        exit_code=1,
        stdout="",
        stderr=(
            "Get-Service : Cannot find any service with service name "
            "'missing'.\n"
            "+ FullyQualifiedErrorId : "
            "NoServiceFoundForGivenName,Microsoft.PowerShell.Commands."
            "GetServiceCommand"
        ),
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    status = manager.get_service_status(
        name="missing", scope=WindowsServiceScope.SYSTEM,
    )
    testbase.assert_equal(status.state, "NotFound")
    testbase.assert_equal(status.start_type, "NotFound")
    return


def test_list_services_parses_single_object_payload():
    """
        ``Get-Service | ConvertTo-Json`` may emit a single JSON object
        instead of an array when a machine has only one service.  The
        manager normalises that into a one-element list.
    """
    record = {
        "Name": "OnlyService",
        "DisplayName": "Only One",
        "Status": 4,
        "StartType": 2,
    }
    runner = _MockCommandRunner()
    runner.results[tuple(_build_powershell_argv(
        script=_LIST_SERVICES_SCRIPT,
    ))] = RunResult(
        exit_code=0, stdout=json.dumps(record), stderr="",
    )
    manager = WindowsServiceManager(
        runner=runner, config=WindowsServiceManagerConfig(),
    )
    listed = manager.list_services(scope=WindowsServiceScope.SYSTEM)
    testbase.assert_equal(len(listed), 1)
    only = listed[0]
    testbase.assert_equal(only.name, "OnlyService")
    testbase.assert_equal(only.status, "Running")
    testbase.assert_equal(only.start_type, "Automatic")
    return
