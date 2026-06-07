"""
    Tests for :class:`WindowsPrecisionTimeManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``w32tm.exe`` / ``net.exe`` / ``sc.exe`` is
    invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.precisiontimemanagement import (
    WindowsPrecisionTimeActionStatus,
    WindowsPrecisionTimeManager,
    WindowsPrecisionTimeManagerConfig,
    WindowsPrecisionTimeQueryError,
)


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


def _w32tm_status_payload() -> str:
    """
        Build a representative ``w32tm /query /status /verbose``
        payload.
    """
    rtnval = (
        "Leap Indicator: 0(no warning)\n"
        "Stratum: 3 (secondary reference - syncd by (S)NTP)\n"
        "Precision: -23 (119.209ns per tick)\n"
        "Root Delay: 0.0123456s\n"
        "Root Dispersion: 0.0023456s\n"
        "ReferenceId: 0xC0A80001 (source IP:  192.168.0.1)\n"
        "Last Successful Sync Time: 4/24/2026 10:30:01 AM\n"
        "Source: ntp1.example.com,0x1\n"
        "Poll Interval: 10 (1024s)\n"
        "Phase Offset: 0.0001234s\n"
        "ClockRate: 0.0156250s\n"
        "State Machine: 2 (Sync)\n"
        "Time Source Flags: 0 (None)\n"
        "Server Role: 0 (None)\n"
        "Last Sync Error: 0 (The command completed successfully.)\n"
        "Time since Last Good Sync Time: 30.1234567s\n"
    )
    return rtnval


def _w32tm_peers_payload() -> str:
    """
        Build a representative ``w32tm /query /peers`` payload.
    """
    rtnval = (
        "#Peers: 2\n"
        "\n"
        "Peer: ntp1.example.com,0x1\n"
        "State: Active\n"
        "Time Remaining: 60.0000000s\n"
        "Mode: 1 (Symmetric Active)\n"
        "Stratum: 2 (secondary reference - syncd by (S)NTP)\n"
        "PeerPoll Interval: 10 (1024s)\n"
        "HostPoll Interval: 10 (1024s)\n"
        "Last Successful Sync Time: 4/24/2026 10:30:01 AM\n"
        "\n"
        "Peer: ntp2.example.com,0x1\n"
        "State: Pending\n"
        "Time Remaining: 60.0000000s\n"
        "Mode: 1 (Symmetric Active)\n"
        "Stratum: 3 (secondary reference - syncd by (S)NTP)\n"
        "Last Successful Sync Time: unspecified\n"
    )
    return rtnval


def _w32tm_stripchart_payload() -> str:
    """
        Build a representative
        ``w32tm /stripchart .. /samples:1 /dataonly`` payload.
    """
    rtnval = (
        "Tracking ntp1.example.com [192.168.0.1:123].\n"
        "Collecting 1 samples.\n"
        "The current time is 4/24/2026 10:30:01 AM.\n"
        "17:30:01, +00.0001234s\n"
    )
    return rtnval


def _sc_query_running_payload() -> str:
    """
        Build a representative ``sc query w32time`` running payload.
    """
    rtnval = (
        "SERVICE_NAME: w32time\n"
        "        TYPE               : 20  WIN32_SHARE_PROCESS\n"
        "        STATE              : 4  RUNNING\n"
        "                                (STOPPABLE, NOT_PAUSABLE,\n"
        "                                 ACCEPTS_SHUTDOWN)\n"
        "        WIN32_EXIT_CODE    : 0  (0x0)\n"
    )
    return rtnval


def _sc_query_stopped_payload() -> str:
    """
        Build a representative ``sc query w32time`` stopped payload.
    """
    rtnval = (
        "SERVICE_NAME: w32time\n"
        "        TYPE               : 20  WIN32_SHARE_PROCESS\n"
        "        STATE              : 1  STOPPED\n"
        "        WIN32_EXIT_CODE    : 1077  (0x435)\n"
    )
    return rtnval


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


def test_get_time_status_parses_w32tm_output():
    """
        get_time_status parses ``w32tm /query /status /verbose``.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("w32tm", "/query", "/status", "/verbose")
    ] = RunResult(
        exit_code=0, stdout=_w32tm_status_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(),
    )
    status = manager.get_time_status()

    testbase.assert_equal(status.enabled, True)
    testbase.assert_equal(status.synchronized, True)
    testbase.assert_equal(
        status.current_server, "ntp1.example.com,0x1",
    )
    testbase.assert_equal(status.stratum, 3)
    testbase.assert_equal(
        abs((status.offset_seconds or 0.0) - 0.0001234) < 1e-9, True,
    )
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_get_time_status_returns_inactive_when_w32time_down():
    """
        get_time_status returns enabled=False when w32tm reports the
        ``0x80070426`` "service has not been started" stderr.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("w32tm", "/query", "/status", "/verbose")
    ] = RunResult(
        exit_code=1, stdout="",
        stderr=(
            "The following error occurred: The service has not been "
            "started. (0x80070426)"
        ),
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(),
    )
    status = manager.get_time_status()

    testbase.assert_equal(status.enabled, False)
    testbase.assert_equal(status.synchronized, False)
    testbase.assert_equal(status.current_server, None)
    return


def test_get_time_sources_parses_w32tm_output():
    """
        get_time_sources parses ``w32tm /query /peers`` columnar
        output.
    """
    runner = _MockCommandRunner()
    runner.results[("w32tm", "/query", "/peers")] = RunResult(
        exit_code=0, stdout=_w32tm_peers_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(),
    )
    sources = manager.get_time_sources()

    testbase.assert_equal(len(sources), 2)
    by_server = {item.server: item for item in sources}
    testbase.assert_equal(by_server["ntp1.example.com"].state, "selected")
    testbase.assert_equal(by_server["ntp1.example.com"].stratum, 2)
    testbase.assert_equal(by_server["ntp2.example.com"].state, "candidate")
    testbase.assert_equal(by_server["ntp2.example.com"].stratum, 3)
    return


def test_get_drift_parses_w32tm_output():
    """
        get_drift parses ``w32tm /stripchart`` offset.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("w32tm", "/query", "/status", "/verbose")
    ] = RunResult(
        exit_code=0, stdout=_w32tm_status_payload(), stderr="",
    )
    runner.results[
        ("w32tm", "/stripchart",
         "/computer:ntp1.example.com,0x1",
         "/samples:1", "/dataonly")
    ] = RunResult(
        exit_code=0, stdout=_w32tm_stripchart_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(),
    )
    drift = manager.get_drift()

    testbase.assert_equal(
        abs(drift.offset_seconds - 0.0001234) < 1e-9, True,
    )
    testbase.assert_equal(drift.stratum, 3)
    return


# ----------------------------------------------------------------------
# add_time_source
# ----------------------------------------------------------------------


def test_add_time_source_invokes_w32tm():
    """
        add_time_source dispatches
        ``w32tm /config /manualpeerlist:"<list>"`` and bounces the
        service.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("w32tm", "/query", "/peers")] = RunResult(
        exit_code=0, stdout=_w32tm_peers_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.add_time_source(server="ntp3.example.com")

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.SUCCESS,
    )
    config_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["w32tm", "/config"]
    ]
    testbase.assert_equal(len(config_calls), 1)
    testbase.assert_equal(
        config_calls[0]["args"],
        [
            "w32tm", "/config",
            "/manualpeerlist:ntp1.example.com ntp2.example.com "
            "ntp3.example.com",
            "/syncfromflags:manual",
            "/update",
        ],
    )
    testbase.assert_equal(config_calls[0]["elevated"], True)
    start_calls = [
        c for c in runner.calls if c["args"][:2] == ["net", "start"]
    ]
    testbase.assert_equal(len(start_calls), 1)
    testbase.assert_equal(start_calls[0]["args"], ["net", "start", "w32time"])
    return


def test_add_time_source_skipped_when_present():
    """
        add_time_source returns SKIPPED when the server is already
        in the active peer list.
    """
    runner = _MockCommandRunner()
    runner.results[("w32tm", "/query", "/peers")] = RunResult(
        exit_code=0, stdout=_w32tm_peers_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(),
    )
    result = manager.add_time_source(server="ntp1.example.com")

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.SKIPPED,
    )
    config_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["w32tm", "/config"]
    ]
    testbase.assert_equal(config_calls, [])
    return


def test_add_time_source_dry_run():
    """
        add_time_source returns DRY_RUN and never invokes the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(dry_run=True),
    )
    result = manager.add_time_source(server="ntp3.example.com")

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        [
            "w32tm", "/config",
            "/manualpeerlist:ntp3.example.com",
            "/syncfromflags:manual",
            "/update",
        ],
    )
    return


# ----------------------------------------------------------------------
# remove_time_source
# ----------------------------------------------------------------------


def test_remove_time_source_invokes_w32tm():
    """
        remove_time_source rewrites the peer list with the target
        excluded.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("w32tm", "/query", "/peers")] = RunResult(
        exit_code=0, stdout=_w32tm_peers_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.remove_time_source(server="ntp1.example.com")

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.SUCCESS,
    )
    config_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["w32tm", "/config"]
    ]
    testbase.assert_equal(
        config_calls[0]["args"],
        [
            "w32tm", "/config",
            "/manualpeerlist:ntp2.example.com",
            "/syncfromflags:manual",
            "/update",
        ],
    )
    return


def test_remove_time_source_skipped_when_absent():
    """
        remove_time_source returns SKIPPED when the server is not
        present.
    """
    runner = _MockCommandRunner()
    runner.results[("w32tm", "/query", "/peers")] = RunResult(
        exit_code=0, stdout=_w32tm_peers_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(),
    )
    result = manager.remove_time_source(server="ntp99.example.com")

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.SKIPPED,
    )
    config_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["w32tm", "/config"]
    ]
    testbase.assert_equal(config_calls, [])
    return


# ----------------------------------------------------------------------
# set_time_servers
# ----------------------------------------------------------------------


def test_set_time_servers_invokes_w32tm():
    """
        set_time_servers rewrites the full peer list and bounces the
        service.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("w32tm", "/query", "/peers")] = RunResult(
        exit_code=0, stdout=_w32tm_peers_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.set_time_servers(
        servers=["a.example.com", "b.example.com"],
    )

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.SUCCESS,
    )
    config_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["w32tm", "/config"]
    ]
    testbase.assert_equal(
        config_calls[0]["args"],
        [
            "w32tm", "/config",
            "/manualpeerlist:a.example.com b.example.com",
            "/syncfromflags:manual",
            "/update",
        ],
    )
    testbase.assert_equal(config_calls[0]["elevated"], True)
    return


def test_set_time_servers_skipped_when_match():
    """
        set_time_servers returns SKIPPED on order-insensitive match.
    """
    runner = _MockCommandRunner()
    runner.results[("w32tm", "/query", "/peers")] = RunResult(
        exit_code=0, stdout=_w32tm_peers_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(),
    )
    result = manager.set_time_servers(
        servers=["ntp2.example.com", "ntp1.example.com"],
    )

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.SKIPPED,
    )
    return


# ----------------------------------------------------------------------
# force_sync_now
# ----------------------------------------------------------------------


def test_force_sync_now_invokes_w32tm():
    """
        force_sync_now dispatches ``w32tm /resync /force``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.force_sync_now()

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.SUCCESS,
    )
    testbase.assert_equal(
        result.command, ["w32tm", "/resync", "/force"],
    )
    testbase.assert_equal(runner.calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# enable_sync / disable_sync
# ----------------------------------------------------------------------


def test_enable_sync_skipped_when_already_enabled():
    """
        enable_sync returns SKIPPED when the service is already
        running.
    """
    runner = _MockCommandRunner()
    runner.results[("sc", "query", "w32time")] = RunResult(
        exit_code=0, stdout=_sc_query_running_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(),
    )
    result = manager.enable_sync()

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.SKIPPED,
    )
    start_calls = [
        c for c in runner.calls if c["args"][:2] == ["net", "start"]
    ]
    testbase.assert_equal(start_calls, [])
    return


def test_disable_sync_invokes_net_stop():
    """
        disable_sync dispatches ``net stop w32time`` and then
        ``sc config w32time start= disabled``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("sc", "query", "w32time")] = RunResult(
        exit_code=0, stdout=_sc_query_running_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.disable_sync()

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.SUCCESS,
    )
    stop_calls = [
        c for c in runner.calls if c["args"][:2] == ["net", "stop"]
    ]
    testbase.assert_equal(len(stop_calls), 1)
    testbase.assert_equal(stop_calls[0]["args"], ["net", "stop", "w32time"])
    config_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["sc", "config"]
        and len(c["args"]) >= 5
    ]
    testbase.assert_equal(
        config_calls[0]["args"],
        ["sc", "config", "w32time", "start=", "disabled"],
    )
    testbase.assert_equal(config_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Cross-cutting: elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes ``elevated=True`` when the config is
        configured to elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("w32tm", "/query", "/peers")] = RunResult(
        exit_code=0, stdout=_w32tm_peers_payload(), stderr="",
    )
    runner.results[("sc", "query", "w32time")] = RunResult(
        exit_code=0, stdout=_sc_query_stopped_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(elevated=True),
    )
    manager.add_time_source(server="ntp3.example.com")
    manager.force_sync_now()
    manager.enable_sync()

    for call in runner.calls:
        head = call["args"]
        if head[:2] == ["w32tm", "/query"]:
            testbase.assert_equal(call["elevated"], False)
            continue
        if head[:2] == ["sc", "query"]:
            testbase.assert_equal(call["elevated"], False)
            continue
        if head[:2] == ["w32tm", "/config"]:
            testbase.assert_equal(call["elevated"], True)
            continue
        if head[:2] == ["w32tm", "/resync"]:
            testbase.assert_equal(call["elevated"], True)
            continue
        if head[:2] == ["net", "start"]:
            testbase.assert_equal(call["elevated"], True)
            continue
        if head[:2] == ["net", "stop"]:
            testbase.assert_equal(call["elevated"], True)
            continue
        if head[:2] == ["sc", "config"]:
            testbase.assert_equal(call["elevated"], True)
            continue
    return


def test_reads_never_elevate():
    """
        Read-only verbs never pass ``elevated=True``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[
        ("w32tm", "/query", "/status", "/verbose")
    ] = RunResult(
        exit_code=0, stdout=_w32tm_status_payload(), stderr="",
    )
    runner.results[("w32tm", "/query", "/peers")] = RunResult(
        exit_code=0, stdout=_w32tm_peers_payload(), stderr="",
    )
    runner.results[
        ("w32tm", "/stripchart",
         "/computer:ntp1.example.com,0x1",
         "/samples:1", "/dataonly")
    ] = RunResult(
        exit_code=0, stdout=_w32tm_stripchart_payload(), stderr="",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(elevated=True),
    )
    manager.get_time_status()
    manager.get_time_sources()
    manager.get_drift()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("w32tm", "/resync", "/force")] = RunResult(
        exit_code=1, stdout="",
        stderr="The computer did not resync because no time data was "
               "available.",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.force_sync_now()

    testbase.assert_equal(
        result.status, WindowsPrecisionTimeActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        result.stderr,
        "The computer did not resync because no time data was "
        "available.",
    )
    testbase.assert_equal(
        result.command, ["w32tm", "/resync", "/force"],
    )
    return


def test_get_time_status_raises_on_unexpected_w32tm_failure():
    """
        get_time_status raises when w32tm fails for a reason other
        than the service being down.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("w32tm", "/query", "/status", "/verbose")
    ] = RunResult(
        exit_code=1, stdout="",
        stderr="The system cannot find the file specified.",
    )

    manager = WindowsPrecisionTimeManager(
        runner=runner,
        config=WindowsPrecisionTimeManagerConfig(),
    )
    raised = False
    try:
        manager.get_time_status()
    except WindowsPrecisionTimeQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return
