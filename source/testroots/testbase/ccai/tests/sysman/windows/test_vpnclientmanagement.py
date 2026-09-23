"""
    Tests for :class:`WindowsVPNClientManager`.

    All tests use a ``_MockCommandRunner`` keyed on the argv prefix.
    No live PowerShell process or MSI download is invoked.  Mocks
    return a :class:`_StubRunResult` superset of the cross-OS
    :class:`RunResult` so the tests can inject the Windows-only
    ``elevation_blocked`` flag.
"""

import json
import unittest

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ccai import testbase

from ccai.aspects.aspects import Aspects
from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.vpnclientmanagement import (
    VPNProvider,
    WindowsVPNActionStatus,
    WindowsVPNClientManager,
    WindowsVPNClientManagerConfig,
    WindowsVPNClientQueryError,
    _parse_get_service_json,
    _parse_tailscale_status_json,
    _ps_escape_single_quoted,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _StubRunResult:
    """
        :class:`RunResult` superset that carries the Windows-only
        ``elevation_blocked`` flag.
    """
    exit_code: Optional[int]
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    elevation_blocked: bool = False


@dataclass
class _Call:
    """
        Recorded runner invocation for assertion-style tests.
    """
    args: List[str]
    elevated: bool


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in.  Each ``run`` invocation
        is matched against ``programmed_results`` by argv-prefix (the
        first programmed prefix that the actual argv starts with wins).
        Unmatched argvs return a successful empty :class:`RunResult`.
    """

    programmed_results: List[Tuple[List[str], _StubRunResult]] = field(
        default_factory=list,
    )
    default_result: Optional[_StubRunResult] = None
    calls: List[_Call] = field(default_factory=list)

    def run(
        self,
        args: List[str],
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ):
        recorded = _Call(args=list(args), elevated=elevated)
        self.calls.append(recorded)

        for prefix, result in self.programmed_results:
            if self._argv_starts_with(args=args, prefix=prefix):
                return result

        for prefix, result in self.programmed_results:
            if self._argv_contains(args=args, needle=prefix):
                return result

        if self.default_result is not None:
            return self.default_result

        default = RunResult(exit_code=0, stdout="", stderr="")
        return default

    def _argv_starts_with(
        self,
        *,
        args: List[str],
        prefix: List[str],
    ) -> bool:
        if len(prefix) > len(args):
            return False
        for index in range(len(prefix)):
            if args[index] != prefix[index]:
                return False
        return True

    def _argv_contains(
        self,
        *,
        args: List[str],
        needle: List[str],
    ) -> bool:
        # Used for matching by PowerShell script substring inside the
        # ``-Command`` argv slot.
        if len(needle) != 1:
            return False
        token = needle[0]
        for arg in args:
            if isinstance(arg, str) and token in arg:
                return True
        return False


def _aspects() -> Aspects:
    """
        Build a default :class:`Aspects` instance for tests.
    """
    rtnval = Aspects()
    return rtnval


def _make_manager(
    *,
    runner: _MockCommandRunner,
    config: Optional[WindowsVPNClientManagerConfig] = None,
) -> WindowsVPNClientManager:
    """
        Build a :class:`WindowsVPNClientManager` for tests.
    """
    if config is None:
        cfg = WindowsVPNClientManagerConfig()
    else:
        cfg = config
    rtnval = WindowsVPNClientManager(runner=runner, config=cfg)
    return rtnval


# ----------------------------------------------------------------------
# Pure-function tests
# ----------------------------------------------------------------------


def test_ps_escape_single_quoted_doubles_single_quotes():
    rtnval = _ps_escape_single_quoted(value="it's")
    testbase.assert_equal(rtnval, "it''s")
    return


def test_parse_tailscale_status_json_running():
    payload = json.dumps({
        "BackendState": "Running",
        "Version": "1.62.0",
        "Self": {
            "TailscaleIPs": ["100.64.0.1"],
            "HostName": "winbox",
        },
        "CurrentTailnet": {"Name": "example.tailnet"},
        "Peer": {
            "abc": {"HostName": "peer1", "TailscaleIPs": ["100.64.0.2"]},
        },
    })
    status = _parse_tailscale_status_json(
        payload=payload, provider=VPNProvider.TAILSCALE,
    )
    testbase.assert_equal(status.running, True)
    testbase.assert_equal(status.installed, True)
    testbase.assert_equal(status.self_address, "100.64.0.1")
    testbase.assert_equal(status.hostname, "winbox")
    testbase.assert_equal(status.tailnet, "example.tailnet")
    testbase.assert_equal(status.version, "1.62.0")
    testbase.assert_equal(status.peer_addresses, {"peer1": "100.64.0.2"})
    return


def test_parse_tailscale_status_json_stopped():
    payload = json.dumps({"BackendState": "Stopped", "Version": "1.62.0"})
    status = _parse_tailscale_status_json(
        payload=payload, provider=VPNProvider.TAILSCALE,
    )
    testbase.assert_equal(status.running, False)
    testbase.assert_equal(status.installed, True)
    return


def test_parse_tailscale_status_json_empty_returns_uninstalled():
    status = _parse_tailscale_status_json(
        payload="", provider=VPNProvider.TAILSCALE,
    )
    testbase.assert_equal(status.installed, False)
    testbase.assert_equal(status.running, False)
    return


def test_parse_get_service_json_running():
    payload = json.dumps({
        "Name": "twingate.connector",
        "Status": "Running",
        "MachineName": ".",
    })
    status = _parse_get_service_json(
        payload=payload, provider=VPNProvider.TWINGATE,
    )
    testbase.assert_equal(status.installed, True)
    testbase.assert_equal(status.running, True)
    testbase.assert_equal(status.self_address, None)
    testbase.assert_equal(status.peer_addresses, {})
    return


def test_parse_get_service_json_stopped():
    payload = json.dumps({
        "Name": "twingate.connector",
        "Status": "Stopped",
    })
    status = _parse_get_service_json(
        payload=payload, provider=VPNProvider.TWINGATE,
    )
    testbase.assert_equal(status.installed, True)
    testbase.assert_equal(status.running, False)
    return


def test_parse_get_service_json_empty_returns_uninstalled():
    status = _parse_get_service_json(
        payload="", provider=VPNProvider.TWINGATE,
    )
    testbase.assert_equal(status.installed, False)
    return


# ----------------------------------------------------------------------
# install_tailscale
# ----------------------------------------------------------------------


def test_install_tailscale_downloads_then_runs_msiexec():
    runner = _MockCommandRunner(
        programmed_results=[
            (["Get-Package"], _StubRunResult(exit_code=0, stdout="")),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.install_tailscale(
        version="1.62.0", channel="stable", aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SUCCESS)
    testbase.assert_equal(result.provider, VPNProvider.TAILSCALE)

    invoking_calls = [
        c for c in runner.calls
        if any("msiexec" in arg for arg in c.args if isinstance(arg, str))
    ]
    testbase.assert_true(len(invoking_calls) > 0)
    return


def test_install_tailscale_skipped_when_present():
    installed_payload = json.dumps(
        {"Name": "Tailscale", "Version": "1.62.0"},
    )
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["Get-Package"],
                _StubRunResult(exit_code=0, stdout=installed_payload),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.install_tailscale(
        version="1.62.0", channel="stable", aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SKIPPED)
    invoking_calls = [
        c for c in runner.calls
        if any("msiexec" in arg for arg in c.args if isinstance(arg, str))
    ]
    testbase.assert_equal(len(invoking_calls), 0)
    return


def test_install_tailscale_failed_on_msiexec_error():
    runner = _MockCommandRunner(
        programmed_results=[
            (["Get-Package"], _StubRunResult(exit_code=0, stdout="")),
        ],
        default_result=_StubRunResult(
            exit_code=1603, stdout="", stderr="msiexec_failure",
        ),
    )
    manager = _make_manager(runner=runner)
    result = manager.install_tailscale(
        version="1.62.0", channel="stable", aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1603)
    return


def test_install_tailscale_msi_url_is_arch_qualified_stable_form():
    # Regression: the previous ``tailscale-setup-<version>.msi`` /
    # ``-latest.msi`` form 404s on pkgs.tailscale.com; the published
    # artifacts are arch-qualified.
    runner = _MockCommandRunner()
    cfg = WindowsVPNClientManagerConfig(dry_run=True)
    manager = _make_manager(runner=runner, config=cfg)

    result = manager.install_tailscale(
        version="1.62.0", channel="stable", aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.DRY_RUN)

    command_text = " ".join(result.command)
    expected_url = (
        "https://pkgs.tailscale.com/stable/"
        "tailscale-setup-1.62.0-amd64.msi"
    )
    testbase.assert_true(expected_url in command_text)
    testbase.assert_true("tailscale-setup-1.62.0.msi" not in command_text)
    return


def test_install_tailscale_msi_url_honours_arch_override():
    runner = _MockCommandRunner()
    cfg = WindowsVPNClientManagerConfig(
        dry_run=True, tailscale_msi_arch="arm64",
    )
    manager = _make_manager(runner=runner, config=cfg)

    result = manager.install_tailscale(
        version="1.62.0", channel="stable", aspects=_aspects(),
    )
    command_text = " ".join(result.command)
    expected_url = (
        "https://pkgs.tailscale.com/stable/"
        "tailscale-setup-1.62.0-arm64.msi"
    )
    testbase.assert_true(expected_url in command_text)
    return


# ----------------------------------------------------------------------
# install_twingate_connector
# ----------------------------------------------------------------------


def test_install_twingate_connector_downloads_then_runs_msiexec():
    runner = _MockCommandRunner(
        programmed_results=[
            (["Get-Package"], _StubRunResult(exit_code=0, stdout="")),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.install_twingate_connector(
        version="1.0.0", aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SUCCESS)
    testbase.assert_equal(result.provider, VPNProvider.TWINGATE)
    return


def test_install_twingate_connector_skipped_when_present():
    installed_payload = json.dumps(
        {"Name": "Twingate Connector", "Version": "1.0.0"},
    )
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["Get-Package"],
                _StubRunResult(exit_code=0, stdout=installed_payload),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.install_twingate_connector(
        version="1.0.0", aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# configure
# ----------------------------------------------------------------------


def test_configure_tailscale_returns_skipped():
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner)
    result = manager.configure(
        provider=VPNProvider.TAILSCALE,
        settings={"foo": "bar"},
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SKIPPED)
    testbase.assert_equal(result.provider, VPNProvider.TAILSCALE)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_configure_twingate_writes_via_set_content():
    runner = _MockCommandRunner(
        programmed_results=[
            (["Get-Content"], _StubRunResult(exit_code=1, stdout="")),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.configure(
        provider=VPNProvider.TWINGATE,
        settings={"NETWORK": "example", "TOKEN": "abc"},
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SUCCESS)
    # The connector config is written via a base64 round-trip
    # (WriteAllText) so multi-line content round-trips byte-exact.
    write_calls = [
        c for c in runner.calls
        if any(
            "WriteAllText" in arg
            for arg in c.args
            if isinstance(arg, str)
        )
    ]
    testbase.assert_true(len(write_calls) > 0)
    return


def test_configure_twingate_skipped_when_content_matches():
    rendered = "NETWORK=example\nTOKEN=abc\n"
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["Get-Content"],
                _StubRunResult(exit_code=0, stdout=rendered),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.configure(
        provider=VPNProvider.TWINGATE,
        settings={"NETWORK": "example", "TOKEN": "abc"},
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SKIPPED)
    set_content_calls = [
        c for c in runner.calls
        if any("Set-Content" in arg for arg in c.args if isinstance(arg, str))
    ]
    testbase.assert_equal(len(set_content_calls), 0)
    return


# ----------------------------------------------------------------------
# bring_up
# ----------------------------------------------------------------------


def test_bring_up_tailscale_sets_service_then_runs_up():
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["tailscale.exe", "status", "--json"],
                _StubRunResult(
                    exit_code=0,
                    stdout=json.dumps({"BackendState": "Stopped"}),
                ),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="tskey-abc",
        hostname="winbox",
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SUCCESS)

    set_service = [
        c for c in runner.calls
        if any("Set-Service" in a for a in c.args if isinstance(a, str))
    ]
    start_service = [
        c for c in runner.calls
        if any("Start-Service" in a for a in c.args if isinstance(a, str))
    ]
    tailscale_up = [
        c for c in runner.calls
        if c.args[:2] == ["tailscale.exe", "up"]
    ]
    testbase.assert_true(len(set_service) > 0)
    testbase.assert_true(len(start_service) > 0)
    testbase.assert_equal(len(tailscale_up), 1)
    return


def test_bring_up_tailscale_skipped_when_running_with_matching_state():
    payload = json.dumps({
        "BackendState": "Running",
        "Self": {"HostName": "winbox", "TailscaleIPs": ["100.64.0.1"]},
    })
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["tailscale.exe", "status", "--json"],
                _StubRunResult(exit_code=0, stdout=payload),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="tskey",
        hostname="winbox",
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SKIPPED)
    return


def test_bring_up_twingate_starts_service():
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["powershell.exe"],
                _StubRunResult(exit_code=0, stdout=""),
            ),
        ],
    )

    # Pre-seed an explicit status response.
    runner.programmed_results.insert(0, (
        ["Get-Service"],
        _StubRunResult(
            exit_code=0,
            stdout=json.dumps({
                "Name": "twingate.connector", "Status": "Stopped",
            }),
        ),
    ))

    manager = _make_manager(runner=runner)
    result = manager.bring_up(
        provider=VPNProvider.TWINGATE,
        authkey="",
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SUCCESS)
    start_service = [
        c for c in runner.calls
        if any("Start-Service" in a for a in c.args if isinstance(a, str))
    ]
    testbase.assert_true(len(start_service) > 0)
    return


def test_bring_up_returns_elevation_blocked_when_uac_denies():
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["tailscale.exe", "status", "--json"],
                _StubRunResult(
                    exit_code=0,
                    stdout=json.dumps({"BackendState": "Stopped"}),
                ),
            ),
        ],
        default_result=_StubRunResult(
            exit_code=-1,
            stdout="",
            stderr="elevation_required_but_process_is_not_elevated",
            elevation_blocked=True,
        ),
    )
    config = WindowsVPNClientManagerConfig(elevated=True)
    manager = _make_manager(runner=runner, config=config)
    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="tskey",
        hostname="winbox",
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.FAILED)
    testbase.assert_true("elevation_blocked" in result.detail)
    return


# ----------------------------------------------------------------------
# bring_down
# ----------------------------------------------------------------------


def test_bring_down_tailscale_stops_service():
    payload = json.dumps({"BackendState": "Running"})
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["tailscale.exe", "status", "--json"],
                _StubRunResult(exit_code=0, stdout=payload),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.bring_down(
        provider=VPNProvider.TAILSCALE, aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SUCCESS)
    stop_calls = [
        c for c in runner.calls
        if any("Stop-Service" in a for a in c.args if isinstance(a, str))
    ]
    down_calls = [
        c for c in runner.calls if c.args[:2] == ["tailscale.exe", "down"]
    ]
    testbase.assert_equal(len(down_calls), 1)
    testbase.assert_true(len(stop_calls) > 0)
    return


def test_bring_down_twingate_stops_service():
    payload = json.dumps({"Status": "Running"})
    runner = _MockCommandRunner(
        programmed_results=[
            (["Get-Service"], _StubRunResult(exit_code=0, stdout=payload)),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.bring_down(
        provider=VPNProvider.TWINGATE, aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SUCCESS)
    stop_calls = [
        c for c in runner.calls
        if any("Stop-Service" in a for a in c.args if isinstance(a, str))
    ]
    testbase.assert_true(len(stop_calls) > 0)
    return


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------


def test_status_tailscale_parses_running_json():
    payload = json.dumps({
        "BackendState": "Running",
        "Self": {"TailscaleIPs": ["100.64.0.5"], "HostName": "host"},
        "Version": "1.62.0",
    })
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["tailscale.exe", "status", "--json"],
                _StubRunResult(exit_code=0, stdout=payload),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    status = manager.status(
        provider=VPNProvider.TAILSCALE, aspects=_aspects(),
    )
    testbase.assert_equal(status.running, True)
    testbase.assert_equal(status.self_address, "100.64.0.5")
    testbase.assert_equal(status.version, "1.62.0")
    return


def test_status_tailscale_returns_installed_false_when_missing():
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["tailscale.exe", "status", "--json"],
                _StubRunResult(
                    exit_code=1,
                    stderr="'tailscale' is not recognized as an "
                           "internal or external command",
                ),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    status = manager.status(
        provider=VPNProvider.TAILSCALE, aspects=_aspects(),
    )
    testbase.assert_equal(status.installed, False)
    testbase.assert_equal(status.running, False)
    return


def test_status_tailscale_raises_on_unknown_failure():
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["tailscale.exe", "status", "--json"],
                _StubRunResult(exit_code=1, stderr="some unknown error"),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    raised = False
    try:
        manager.status(provider=VPNProvider.TAILSCALE, aspects=_aspects())
    except WindowsVPNClientQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_status_twingate_parses_get_service_json():
    payload = json.dumps({"Name": "twingate.connector", "Status": "Running"})
    runner = _MockCommandRunner(
        programmed_results=[
            (["Get-Service"], _StubRunResult(exit_code=0, stdout=payload)),
        ],
    )
    manager = _make_manager(runner=runner)
    status = manager.status(
        provider=VPNProvider.TWINGATE, aspects=_aspects(),
    )
    testbase.assert_equal(status.running, True)
    testbase.assert_equal(status.installed, True)
    testbase.assert_equal(status.self_address, None)
    return


# ----------------------------------------------------------------------
# uninstall
# ----------------------------------------------------------------------


def test_uninstall_tailscale_runs_msiexec_x_with_product_code():
    code_payload = json.dumps({
        "Name": "Tailscale",
        "ProductCode": "{ABC-123}",
    })
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["Get-Package"],
                _StubRunResult(exit_code=0, stdout=code_payload),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.uninstall(
        provider=VPNProvider.TAILSCALE,
        purge=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SUCCESS)

    # The uninstall script resolves the ProductCode itself via an
    # inline Get-Package lookup (S-H8 fix: no un-runnable placeholder),
    # then runs msiexec /x against the resolved variable.
    msiexec_calls = [
        c for c in runner.calls
        if any(
            "msiexec" in arg and "/x" in arg and "Get-Package" in arg
            for arg in c.args
            if isinstance(arg, str)
        )
    ]
    testbase.assert_true(len(msiexec_calls) > 0)
    # The literal placeholder must never appear in any dispatched argv.
    for call in runner.calls:
        for arg in call.args:
            if isinstance(arg, str):
                testbase.assert_true("_product_code>" not in arg)
    return


def test_uninstall_skipped_when_absent():
    runner = _MockCommandRunner(
        programmed_results=[
            (["Get-Package"], _StubRunResult(exit_code=0, stdout="")),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.uninstall(
        provider=VPNProvider.TAILSCALE,
        purge=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SKIPPED)
    return


def test_uninstall_purge_removes_state_directory():
    code_payload = json.dumps({
        "Name": "Tailscale", "ProductCode": "{GUID}",
    })
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["Get-Package"],
                _StubRunResult(exit_code=0, stdout=code_payload),
            ),
        ],
    )
    manager = _make_manager(runner=runner)
    result = manager.uninstall(
        provider=VPNProvider.TAILSCALE,
        purge=True,
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, WindowsVPNActionStatus.SUCCESS)
    purge_calls = [
        c for c in runner.calls
        if any("Remove-Item" in a for a in c.args if isinstance(a, str))
    ]
    testbase.assert_true(len(purge_calls) > 0)
    return


# ----------------------------------------------------------------------
# Dry-run / elevation
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_every_write_verb():
    runner = _MockCommandRunner()
    cfg = WindowsVPNClientManagerConfig(dry_run=True)
    manager = _make_manager(runner=runner, config=cfg)

    r1 = manager.install_tailscale(
        version="1.0.0", channel="stable", aspects=_aspects(),
    )
    testbase.assert_equal(r1.status, WindowsVPNActionStatus.DRY_RUN)

    r2 = manager.install_twingate_connector(
        version="1.0.0", aspects=_aspects(),
    )
    testbase.assert_equal(r2.status, WindowsVPNActionStatus.DRY_RUN)

    r3 = manager.configure(
        provider=VPNProvider.TWINGATE,
        settings={"k": "v"},
        aspects=_aspects(),
    )
    testbase.assert_equal(r3.status, WindowsVPNActionStatus.DRY_RUN)

    r4 = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="tskey",
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(r4.status, WindowsVPNActionStatus.DRY_RUN)

    r5 = manager.bring_down(
        provider=VPNProvider.TAILSCALE, aspects=_aspects(),
    )
    testbase.assert_equal(r5.status, WindowsVPNActionStatus.DRY_RUN)

    r6 = manager.uninstall(
        provider=VPNProvider.TWINGATE,
        purge=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(r6.status, WindowsVPNActionStatus.DRY_RUN)

    testbase.assert_equal(len(runner.calls), 0)
    return


def test_writes_pass_elevated_when_configured():
    runner = _MockCommandRunner(
        programmed_results=[
            (["Get-Package"], _StubRunResult(exit_code=0, stdout="")),
        ],
    )
    cfg = WindowsVPNClientManagerConfig(elevated=True)
    manager = _make_manager(runner=runner, config=cfg)
    manager.install_tailscale(
        version="1.62.0", channel="stable", aspects=_aspects(),
    )

    elevated_calls = [c for c in runner.calls if c.elevated is True]
    non_elevated_calls = [c for c in runner.calls if c.elevated is False]
    testbase.assert_true(len(elevated_calls) > 0)
    # Get-Package probe (read) is one of the non-elevated calls.
    testbase.assert_true(len(non_elevated_calls) > 0)
    return


def test_reads_never_elevate():
    payload = json.dumps({"BackendState": "Stopped"})
    runner = _MockCommandRunner(
        programmed_results=[
            (
                ["tailscale.exe", "status", "--json"],
                _StubRunResult(exit_code=0, stdout=payload),
            ),
        ],
    )
    cfg = WindowsVPNClientManagerConfig(elevated=True)
    manager = _make_manager(runner=runner, config=cfg)
    manager.status(provider=VPNProvider.TAILSCALE, aspects=_aspects())

    for call in runner.calls:
        testbase.assert_equal(call.elevated, False)
    return


# ----------------------------------------------------------------------
# Hypothesis property tests
# ----------------------------------------------------------------------


try:
    from hypothesis import given
    from hypothesis import strategies as st

    _HYPOTHESIS_AVAILABLE = True
except ImportError:
    _HYPOTHESIS_AVAILABLE = False


if _HYPOTHESIS_AVAILABLE is True:

    _VERSION_STRATEGY = st.from_regex(
        r"\A[1-9][0-9]{0,2}\.[0-9]{1,3}\.[0-9]{1,3}\Z", fullmatch=True,
    )
    _CHANNEL_STRATEGY = st.sampled_from(["stable", "unstable"])

    class _PropertyTests(unittest.TestCase):
        """
            Hypothesis-driven idempotency and dry-run safety checks.
        """

        @given(version=_VERSION_STRATEGY, channel=_CHANNEL_STRATEGY)
        def test_property_install_tailscale_idempotent(
            self, version: str, channel: str,
        ) -> None:
            installed_payload = json.dumps(
                {"Name": "Tailscale", "Version": version},
            )
            runner = _MockCommandRunner(
                programmed_results=[
                    (
                        ["Get-Package"],
                        _StubRunResult(
                            exit_code=0, stdout=installed_payload,
                        ),
                    ),
                ],
            )
            manager = _make_manager(runner=runner)
            first = manager.install_tailscale(
                version=version, channel=channel, aspects=_aspects(),
            )
            second = manager.install_tailscale(
                version=version, channel=channel, aspects=_aspects(),
            )
            self.assertEqual(first.status, WindowsVPNActionStatus.SKIPPED)
            self.assertEqual(second.status, WindowsVPNActionStatus.SKIPPED)
            return

        @given(purge=st.booleans(), constraints={"max_examples": 25})
        def test_property_uninstall_idempotent(self, purge: bool) -> None:
            for provider in (VPNProvider.TAILSCALE, VPNProvider.TWINGATE):
                runner = _MockCommandRunner(
                    programmed_results=[
                        (
                            ["Get-Package"],
                            _StubRunResult(exit_code=0, stdout=""),
                        ),
                    ],
                )
                manager = _make_manager(runner=runner)
                first = manager.uninstall(
                    provider=provider, purge=purge, aspects=_aspects(),
                )
                second = manager.uninstall(
                    provider=provider, purge=purge, aspects=_aspects(),
                )
                self.assertEqual(first.status, WindowsVPNActionStatus.SKIPPED)
                self.assertEqual(second.status, WindowsVPNActionStatus.SKIPPED)
            return

        @given(
    constraints={"max_examples": 10},hostname=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
            min_size=1, max_size=20,
        ))
        def test_property_bring_up_idempotent_on_matching_state(
            self, hostname: str,
        ) -> None:
            payload = json.dumps({
                "BackendState": "Running",
                "Self": {
                    "HostName": hostname,
                    "TailscaleIPs": ["100.64.0.1"],
                },
            })
            runner = _MockCommandRunner(
                programmed_results=[
                    (
                        ["tailscale.exe", "status", "--json"],
                        _StubRunResult(exit_code=0, stdout=payload),
                    ),
                ],
            )
            manager = _make_manager(runner=runner)
            result = manager.bring_up(
                provider=VPNProvider.TAILSCALE,
                authkey="tskey",
                hostname=hostname,
                advertise_tags=None,
                advertise_routes=None,
                exit_node=False,
                aspects=_aspects(),
            )
            self.assertEqual(result.status, WindowsVPNActionStatus.SKIPPED)
            return

        @given(
    constraints={"max_examples": 20},
            version=_VERSION_STRATEGY,
            purge=st.booleans(),
        )
        def test_property_dry_run_never_invokes_runner(
            self, version: str, purge: bool,
        ) -> None:
            runner = _MockCommandRunner()
            cfg = WindowsVPNClientManagerConfig(dry_run=True)
            manager = _make_manager(runner=runner, config=cfg)

            manager.install_tailscale(
                version=version, channel="stable", aspects=_aspects(),
            )
            manager.install_twingate_connector(
                version=version, aspects=_aspects(),
            )
            manager.bring_up(
                provider=VPNProvider.TAILSCALE,
                authkey="tskey",
                hostname=None,
                advertise_tags=None,
                advertise_routes=None,
                exit_node=False,
                aspects=_aspects(),
            )
            manager.bring_down(
                provider=VPNProvider.TAILSCALE, aspects=_aspects(),
            )
            manager.uninstall(
                provider=VPNProvider.TWINGATE,
                purge=purge,
                aspects=_aspects(),
            )
            self.assertEqual(len(runner.calls), 0)
            return


# ----------------------------------------------------------------------
# Audit regression tests — C3 (authkey redaction), C4 (authkey off
# cmdline), S-H8 (no placeholder), S-H10 (absent vs empty),
# S-H11 (msiexec exit-code check)
# ----------------------------------------------------------------------


def test_bring_up_redacts_authkey_in_command():
    """
        C3: any auth key embedded in a returned command argv is
        redacted.
    """
    runner = _MockCommandRunner(
        programmed_results=[
            (["tailscale.exe", "status"], _StubRunResult(
                exit_code=0,
                stdout=json.dumps({"BackendState": "Stopped"}),
            )),
        ],
    )
    config = WindowsVPNClientManagerConfig(elevated=True)
    manager = _make_manager(runner=runner, config=config)

    secret = "tskey-auth-SUPERSECRET"
    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey=secret,
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )

    # No returned command, and no recorded argv, may carry the secret.
    if result.command is not None:
        joined = " ".join(result.command)
        testbase.assert_true(secret not in joined)
    for call in runner.calls:
        for arg in call.args:
            if isinstance(arg, str):
                testbase.assert_true(secret not in arg)
    return


def test_bring_up_keeps_authkey_off_tailscale_up_cmdline():
    """
        C4: ``tailscale up`` reads the key from a file, not from the
        process command line.
    """
    runner = _MockCommandRunner(
        programmed_results=[
            (["tailscale.exe", "status"], _StubRunResult(
                exit_code=0,
                stdout=json.dumps({"BackendState": "Stopped"}),
            )),
        ],
    )
    config = WindowsVPNClientManagerConfig(elevated=True)
    manager = _make_manager(runner=runner, config=config)

    manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="tskey-auth-ABC",
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )

    up_calls = [
        c for c in runner.calls
        if c.args[:2] == ["tailscale.exe", "up"]
    ]
    testbase.assert_equal(len(up_calls), 1)
    joined = " ".join(up_calls[0].args)
    testbase.assert_true("--auth-key=file:" in joined)
    testbase.assert_true("--authkey=tskey" not in joined)
    return


def test_uninstall_dry_run_emits_no_placeholder():
    """
        S-H8: a dry-run uninstall must not emit an un-runnable
        ``<..._product_code>`` placeholder.
    """
    runner = _MockCommandRunner()
    config = WindowsVPNClientManagerConfig(dry_run=True)
    manager = _make_manager(runner=runner, config=config)

    result = manager.uninstall(
        provider=VPNProvider.TAILSCALE,
        purge=False,
        aspects=_aspects(),
    )

    testbase.assert_equal(result.status, WindowsVPNActionStatus.DRY_RUN)
    testbase.assert_true(result.command is not None)
    joined = " ".join(result.command)
    testbase.assert_true("_product_code>" not in joined)
    testbase.assert_true("Get-Package" in joined)
    return


def test_read_file_content_distinguishes_absent_from_empty():
    """
        S-H10: an absent file yields ``None``; a present-but-empty
        file yields an empty string.
    """
    runner_absent = _MockCommandRunner(
        programmed_results=[
            (["powershell.exe"], _StubRunResult(
                exit_code=0, stdout="__CCAI_FILE_ABSENT__\r\n",
            )),
        ],
    )
    manager_absent = _make_manager(runner=runner_absent)
    absent = manager_absent._read_file_content(
        path="C:\\nope.conf", aspects=_aspects(),
    )
    testbase.assert_true(absent is None)

    runner_empty = _MockCommandRunner(
        programmed_results=[
            (["powershell.exe"], _StubRunResult(
                exit_code=0, stdout="__CCAI_FILE_PRESENT__\r\n",
            )),
        ],
    )
    manager_empty = _make_manager(runner=runner_empty)
    empty = manager_empty._read_file_content(
        path="C:\\empty.conf", aspects=_aspects(),
    )
    testbase.assert_equal(empty, "")
    return


def test_install_msi_script_checks_exit_code():
    """
        S-H11: the install MSI script propagates a non-zero msiexec
        exit code so a failed install is not reported as SUCCESS.
    """
    manager = _make_manager(runner=_MockCommandRunner())
    script = manager._render_install_msi_script(
        msi_url="https://example/x.msi",
        msi_path="C:\\tmp\\x.msi",
        cache_dir="C:\\tmp",
    )
    testbase.assert_true("ExitCode" in script)
    testbase.assert_true("exit $proc.ExitCode" in script)
    return
