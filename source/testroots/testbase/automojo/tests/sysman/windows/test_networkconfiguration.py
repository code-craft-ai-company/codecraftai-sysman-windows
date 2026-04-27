"""
    Tests for :class:`WindowsNetworkConfigurationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.networkconfiguration import (
    WindowsInterfaceInfo,
    WindowsNetworkActionResult,
    WindowsNetworkActionStatus,
    WindowsNetworkConfigurationManager,
    WindowsNetworkConfigurationManagerConfig,
    WindowsNetworkQueryError,
    WindowsRouteInfo,
    _dotted_quad_to_prefix,
    _merge_adapter_and_ip,
    _parse_get_dns,
    _parse_get_netadapter,
    _parse_get_netipaddress,
    _parse_get_netroute,
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


def _adapter_argv() -> list:
    """
        Argv for the ``Get-NetAdapter`` listing call.
    """
    script = (
        "Get-NetAdapter "
        "| Select-Object Name, InterfaceAlias, Status, LinkSpeed, "
        "MacAddress, InterfaceIndex "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _ip_argv() -> list:
    """
        Argv for the ``Get-NetIPAddress`` listing call.
    """
    script = (
        "Get-NetIPAddress "
        "| Select-Object InterfaceAlias, IPAddress, AddressFamily, "
        "PrefixLength "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _adapter_status_argv(name: str) -> list:
    """
        Argv for the per-interface ``Get-NetAdapter -Name`` probe.
    """
    script = (
        "Get-NetAdapter -Name '{}' "
        "| Select-Object Status "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _ip_for_iface_argv(name: str) -> list:
    """
        Argv for the per-interface ``Get-NetIPAddress`` probe.
    """
    script = (
        "Get-NetIPAddress -InterfaceAlias '{}' "
        "| Select-Object InterfaceAlias, IPAddress, AddressFamily, "
        "PrefixLength "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _route_for_dest_argv(dest: str) -> list:
    """
        Argv for the per-destination ``Get-NetRoute`` probe.
    """
    script = (
        "Get-NetRoute -DestinationPrefix '{}' "
        "| Select-Object DestinationPrefix, NextHop, "
        "InterfaceAlias, RouteMetric "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(dest)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _default_route_argv() -> list:
    """
        Argv for the default-route discovery probe.
    """
    rtnval = _route_for_dest_argv(dest="0.0.0.0/0")
    return rtnval


def _dns_argv(name: str) -> list:
    """
        Argv for the per-interface ``Get-DnsClientServerAddress`` probe.
    """
    script = (
        "Get-DnsClientServerAddress -InterfaceAlias '{}' "
        "| Select-Object InterfaceAlias, AddressFamily, "
        "ServerAddresses "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _hostname_argv() -> list:
    """
        Argv for the ``[System.Net.Dns]::GetHostName()`` probe.
    """
    rtnval = _build_powershell_argv(
        script="[System.Net.Dns]::GetHostName()",
    )
    return rtnval


# ----------------------------------------------------------------------
# Pure helpers (netmask, parsers)
# ----------------------------------------------------------------------


def test_dotted_quad_to_prefix_class_c():
    """
        ``255.255.255.0`` converts to /24.
    """
    rtnval = _dotted_quad_to_prefix(netmask="255.255.255.0")
    testbase.assert_equal(rtnval, 24)
    return


def test_dotted_quad_to_prefix_class_b():
    """
        ``255.255.0.0`` converts to /16.
    """
    rtnval = _dotted_quad_to_prefix(netmask="255.255.0.0")
    testbase.assert_equal(rtnval, 16)
    return


def test_dotted_quad_to_prefix_passes_through_numeric():
    """
        Numeric prefixes pass through unchanged.
    """
    rtnval = _dotted_quad_to_prefix(netmask="20")
    testbase.assert_equal(rtnval, 20)
    return


def test_dotted_quad_to_prefix_rejects_invalid():
    """
        Invalid input raises ValueError.
    """
    raised = False
    try:
        _dotted_quad_to_prefix(netmask="not-a-mask")
    except ValueError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_parse_get_netadapter_handles_array():
    """
        ``Get-NetAdapter`` JSON arrays parse into a list of dicts.
    """
    payload = json.dumps([
        {"Name": "Ethernet0", "InterfaceAlias": "Ethernet0",
         "Status": "Up", "LinkSpeed": "1 Gbps", "MacAddress": "00-11-22-33-44-55",
         "InterfaceIndex": 12},
    ])
    records = _parse_get_netadapter(text=payload)
    testbase.assert_equal(len(records), 1)
    testbase.assert_equal(records[0]["Name"], "Ethernet0")
    return


def test_parse_get_netadapter_handles_bare_object():
    """
        A bare-object payload normalises into a single-element list.
    """
    payload = json.dumps({
        "Name": "Wi-Fi", "InterfaceAlias": "Wi-Fi",
        "Status": "Up", "MacAddress": None, "InterfaceIndex": 5,
    })
    records = _parse_get_netadapter(text=payload)
    testbase.assert_equal(len(records), 1)
    testbase.assert_equal(records[0]["Name"], "Wi-Fi")
    return


def test_parse_get_netipaddress_groups_ipv4_and_ipv6():
    """
        IPv4 and IPv6 entries are bucketed correctly per InterfaceAlias.
    """
    payload = json.dumps([
        {"InterfaceAlias": "Ethernet0", "IPAddress": "10.0.0.5",
         "AddressFamily": 2, "PrefixLength": 24},
        {"InterfaceAlias": "Ethernet0", "IPAddress": "fe80::1",
         "AddressFamily": 23, "PrefixLength": 64},
        {"InterfaceAlias": "Wi-Fi", "IPAddress": "192.168.1.5",
         "AddressFamily": "IPv4", "PrefixLength": 24},
    ])
    grouped = _parse_get_netipaddress(text=payload)
    testbase.assert_equal(grouped["Ethernet0"]["ipv4"], ["10.0.0.5/24"])
    testbase.assert_equal(grouped["Ethernet0"]["ipv6"], ["fe80::1/64"])
    testbase.assert_equal(grouped["Wi-Fi"]["ipv4"], ["192.168.1.5/24"])
    return


def test_merge_adapter_and_ip_joins_by_alias():
    """
        Adapter rows are merged with IP rows by InterfaceAlias.
    """
    adapter_records = [
        {"Name": "Ethernet0", "InterfaceAlias": "Ethernet0",
         "Status": "Up", "LinkSpeed": "1 Gbps", "MacAddress": "AA",
         "InterfaceIndex": 7},
    ]
    ip_by_alias = {
        "Ethernet0": {"ipv4": ["10.0.0.5/24"], "ipv6": ["fe80::1/64"]},
    }
    merged = _merge_adapter_and_ip(
        adapter_records=adapter_records, ip_by_alias=ip_by_alias,
    )
    testbase.assert_equal(len(merged), 1)
    info = merged[0]
    testbase.assert_equal(info.name, "Ethernet0")
    testbase.assert_equal(info.alias, "Ethernet0")
    testbase.assert_equal(info.status, "Up")
    testbase.assert_equal(info.ipv4_addresses, ["10.0.0.5/24"])
    testbase.assert_equal(info.ipv6_addresses, ["fe80::1/64"])
    return


def test_parse_get_dns_handles_array_of_arrays():
    """
        ``Get-DnsClientServerAddress`` payload with ServerAddresses
        arrays flattens into a single ordered list.
    """
    payload = json.dumps([
        {"InterfaceAlias": "Ethernet0", "AddressFamily": 2,
         "ServerAddresses": ["1.1.1.1", "8.8.8.8"]},
        {"InterfaceAlias": "Ethernet0", "AddressFamily": 23,
         "ServerAddresses": []},
    ])
    rtnval = _parse_get_dns(text=payload)
    testbase.assert_equal(rtnval, ["1.1.1.1", "8.8.8.8"])
    return


def test_parse_get_netroute_normalises_zero_next_hop():
    """
        ``NextHop == "0.0.0.0"`` and ``"::"`` collapse to ``None``.
    """
    payload = json.dumps([
        {"DestinationPrefix": "10.0.0.0/24", "NextHop": "0.0.0.0",
         "InterfaceAlias": "Ethernet0", "RouteMetric": 256},
        {"DestinationPrefix": "::/0", "NextHop": "::",
         "InterfaceAlias": "Ethernet0", "RouteMetric": 256},
        {"DestinationPrefix": "0.0.0.0/0", "NextHop": "10.0.0.1",
         "InterfaceAlias": "Ethernet0", "RouteMetric": 0},
    ])
    routes = _parse_get_netroute(text=payload)
    testbase.assert_equal(len(routes), 3)
    testbase.assert_equal(routes[0].next_hop, None)
    testbase.assert_equal(routes[1].next_hop, None)
    testbase.assert_equal(routes[2].next_hop, "10.0.0.1")
    return


# ----------------------------------------------------------------------
# get_interfaces
# ----------------------------------------------------------------------


def test_get_interfaces_merges_adapter_and_ip_payloads():
    """
        get_interfaces merges Get-NetAdapter and Get-NetIPAddress
        payloads on InterfaceAlias.
    """
    runner = _MockCommandRunner()
    adapter_payload = json.dumps([
        {"Name": "Ethernet0", "InterfaceAlias": "Ethernet0",
         "Status": "Up", "LinkSpeed": "1 Gbps",
         "MacAddress": "00-11-22-33-44-55", "InterfaceIndex": 12},
    ])
    ip_payload = json.dumps([
        {"InterfaceAlias": "Ethernet0", "IPAddress": "10.0.0.5",
         "AddressFamily": 2, "PrefixLength": 24},
    ])
    runner.results[tuple(_adapter_argv())] = RunResult(
        exit_code=0, stdout=adapter_payload, stderr="",
    )
    runner.results[tuple(_ip_argv())] = RunResult(
        exit_code=0, stdout=ip_payload, stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    rtnval = manager.get_interfaces()
    testbase.assert_equal(len(rtnval), 1)
    testbase.assert_equal(rtnval[0].alias, "Ethernet0")
    testbase.assert_equal(rtnval[0].ipv4_addresses, ["10.0.0.5/24"])
    # Reads must not elevate.
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_get_interfaces_raises_on_get_netadapter_failure():
    """
        get_interfaces raises when Get-NetAdapter exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_adapter_argv())] = RunResult(
        exit_code=1, stdout="", stderr="boom",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    raised = False
    try:
        manager.get_interfaces()
    except WindowsNetworkQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# configure_interface
# ----------------------------------------------------------------------


def test_configure_interface_dispatches_remove_and_new():
    """
        configure_interface dispatches the chained Remove-NetIPAddress
        + New-NetIPAddress scriptblock.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_ip_for_iface_argv("Ethernet0"))] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.configure_interface(
        name="Ethernet0",
        address="10.0.0.5",
        netmask="255.255.255.0",
        gateway="10.0.0.1",
    )
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SUCCESS,
    )
    write_call = runner.calls[-1]
    expected_script = (
        "Remove-NetIPAddress -InterfaceAlias 'Ethernet0' "
        "-Confirm:$false -ErrorAction SilentlyContinue; "
        "New-NetIPAddress -InterfaceAlias 'Ethernet0' "
        "-IPAddress '10.0.0.5' -PrefixLength 24 "
        "-DefaultGateway '10.0.0.1'"
    )
    testbase.assert_equal(
        write_call["args"], _build_powershell_argv(script=expected_script),
    )
    return


def test_configure_interface_omits_default_gateway_when_none():
    """
        configure_interface omits ``-DefaultGateway`` when ``gateway``
        is ``None``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_ip_for_iface_argv("Ethernet0"))] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.configure_interface(
        name="Ethernet0",
        address="10.0.0.5",
        netmask="24",
        gateway=None,
    )
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SUCCESS,
    )
    write_script = runner.calls[-1]["args"][-1]
    testbase.assert_equal("DefaultGateway" in write_script, False)
    testbase.assert_equal(
        "New-NetIPAddress -InterfaceAlias 'Ethernet0' "
        "-IPAddress '10.0.0.5' -PrefixLength 24" in write_script,
        True,
    )
    return


def test_configure_interface_skipped_when_already_set():
    """
        configure_interface returns SKIPPED when the desired
        ``address/prefix`` is already present.
    """
    runner = _MockCommandRunner()
    ip_payload = json.dumps([
        {"InterfaceAlias": "Ethernet0", "IPAddress": "10.0.0.5",
         "AddressFamily": 2, "PrefixLength": 24},
    ])
    runner.results[tuple(_ip_for_iface_argv("Ethernet0"))] = RunResult(
        exit_code=0, stdout=ip_payload, stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.configure_interface(
        name="Ethernet0",
        address="10.0.0.5",
        netmask="255.255.255.0",
        gateway="10.0.0.1",
    )
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if "New-NetIPAddress" in c["args"][-1]
        and "Get-NetIPAddress" not in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_configure_interface_dry_run_short_circuits():
    """
        Dry-run captures the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(dry_run=True),
    )
    result = manager.configure_interface(
        name="Ethernet0",
        address="10.0.0.5",
        netmask="24",
        gateway="10.0.0.1",
    )
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_configure_interface_failed_on_invalid_netmask():
    """
        configure_interface returns FAILED with
        ``message="invalid_netmask"`` when the netmask cannot be parsed.
    """
    runner = _MockCommandRunner()
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.configure_interface(
        name="Ethernet0",
        address="10.0.0.5",
        netmask="not-a-mask",
        gateway=None,
    )
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "invalid_netmask")
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# enable_interface / disable_interface
# ----------------------------------------------------------------------


def _seed_status(
    *,
    runner: _MockCommandRunner,
    name: str,
    status: str,
) -> None:
    """
        Seed an adapter-status JSON payload for ``name``.
    """
    payload = json.dumps({"Status": status})
    runner.results[tuple(_adapter_status_argv(name))] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def test_enable_interface_dispatches_enable_netadapter():
    """
        enable_interface on a non-Up adapter dispatches
        Enable-NetAdapter.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_status(runner=runner, name="Ethernet0", status="Disabled")
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.enable_interface(name="Ethernet0")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SUCCESS,
    )
    write_script = runner.calls[-1]["args"][-1]
    testbase.assert_equal(
        write_script, "Enable-NetAdapter -Name 'Ethernet0' -Confirm:$false",
    )
    return


def test_enable_interface_skipped_when_already_up():
    """
        enable_interface returns SKIPPED when status is already Up.
    """
    runner = _MockCommandRunner()
    _seed_status(runner=runner, name="Ethernet0", status="Up")
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.enable_interface(name="Ethernet0")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if "Enable-NetAdapter" in c["args"][-1]
        and "Get-NetAdapter" not in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_disable_interface_skipped_when_already_disabled():
    """
        disable_interface returns SKIPPED when status is already
        Disabled.
    """
    runner = _MockCommandRunner()
    _seed_status(runner=runner, name="Ethernet0", status="Disabled")
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.disable_interface(name="Ethernet0")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SKIPPED,
    )
    return


def test_disable_interface_dispatches_disable_netadapter():
    """
        disable_interface on a non-Disabled adapter dispatches
        Disable-NetAdapter.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_status(runner=runner, name="Ethernet0", status="Up")
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.disable_interface(name="Ethernet0")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SUCCESS,
    )
    expected_script = "Disable-NetAdapter -Name 'Ethernet0' -Confirm:$false"
    testbase.assert_equal(runner.calls[-1]["args"][-1], expected_script)
    return


# ----------------------------------------------------------------------
# add_route / remove_route
# ----------------------------------------------------------------------


def _seed_default_route(
    *,
    runner: _MockCommandRunner,
    interface: str,
) -> None:
    """
        Seed the default-route discovery probe to point at ``interface``.
    """
    payload = json.dumps([
        {"DestinationPrefix": "0.0.0.0/0", "NextHop": "10.0.0.1",
         "InterfaceAlias": interface, "RouteMetric": 0},
    ])
    runner.results[tuple(_default_route_argv())] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def test_add_route_dispatches_new_netroute():
    """
        add_route dispatches New-NetRoute with the resolved default
        interface alias when no matching route exists.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_default_route(runner=runner, interface="Ethernet0")
    runner.results[tuple(_route_for_dest_argv("10.0.0.0/8"))] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.add_route(dest="10.0.0.0/8", gateway="10.0.0.1")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SUCCESS,
    )
    write_script = runner.calls[-1]["args"][-1]
    expected_script = (
        "New-NetRoute -InterfaceAlias 'Ethernet0' "
        "-DestinationPrefix '10.0.0.0/8' -NextHop '10.0.0.1'"
    )
    testbase.assert_equal(write_script, expected_script)
    return


def test_add_route_skipped_when_matching_gateway_exists():
    """
        add_route returns SKIPPED when Get-NetRoute already reports a
        matching gateway.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_default_route(runner=runner, interface="Ethernet0")
    payload = json.dumps([
        {"DestinationPrefix": "10.0.0.0/8", "NextHop": "10.0.0.1",
         "InterfaceAlias": "Ethernet0", "RouteMetric": 256},
    ])
    runner.results[tuple(_route_for_dest_argv("10.0.0.0/8"))] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.add_route(dest="10.0.0.0/8", gateway="10.0.0.1")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SKIPPED,
    )
    return


def test_add_route_failed_when_no_default_route():
    """
        add_route returns FAILED with the
        ``no_default_route_interface`` marker when there is no default
        route to derive the interface alias from.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_default_route_argv())] = RunResult(
        exit_code=1, stdout="", stderr="no route",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.add_route(dest="10.0.0.0/8", gateway="10.0.0.1")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "no_default_route_interface")
    return


def test_remove_route_dispatches_remove_netroute():
    """
        remove_route dispatches Remove-NetRoute with the destination.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    payload = json.dumps([
        {"DestinationPrefix": "10.0.0.0/8", "NextHop": "10.0.0.1",
         "InterfaceAlias": "Ethernet0", "RouteMetric": 256},
    ])
    runner.results[tuple(_route_for_dest_argv("10.0.0.0/8"))] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.remove_route(dest="10.0.0.0/8")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SUCCESS,
    )
    expected_script = (
        "Remove-NetRoute -DestinationPrefix '10.0.0.0/8' -Confirm:$false"
    )
    testbase.assert_equal(runner.calls[-1]["args"][-1], expected_script)
    return


def test_remove_route_skipped_when_absent():
    """
        remove_route returns SKIPPED when no matching row is reported.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_route_for_dest_argv("10.0.0.0/8"))] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.remove_route(dest="10.0.0.0/8")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SKIPPED,
    )
    return


# ----------------------------------------------------------------------
# set_dns_servers / get_dns_servers
# ----------------------------------------------------------------------


def test_set_dns_servers_dispatches_serveraddresses_array():
    """
        set_dns_servers dispatches Set-DnsClientServerAddress with a
        ``-ServerAddresses @(...)`` literal.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_dns_argv("Ethernet0"))] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.set_dns_servers(
        interface="Ethernet0",
        servers=["1.1.1.1", "8.8.8.8"],
    )
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SUCCESS,
    )
    expected_script = (
        "Set-DnsClientServerAddress -InterfaceAlias 'Ethernet0' "
        "-ServerAddresses @('1.1.1.1', '8.8.8.8')"
    )
    testbase.assert_equal(runner.calls[-1]["args"][-1], expected_script)
    return


def test_set_dns_servers_uses_reset_for_empty_list():
    """
        set_dns_servers with an empty list dispatches
        ``-ResetServerAddresses``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    payload = json.dumps([
        {"InterfaceAlias": "Ethernet0", "AddressFamily": 2,
         "ServerAddresses": ["1.1.1.1"]},
    ])
    runner.results[tuple(_dns_argv("Ethernet0"))] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.set_dns_servers(interface="Ethernet0", servers=[])
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SUCCESS,
    )
    expected_script = (
        "Set-DnsClientServerAddress -InterfaceAlias 'Ethernet0' "
        "-ResetServerAddresses"
    )
    testbase.assert_equal(runner.calls[-1]["args"][-1], expected_script)
    return


def test_set_dns_servers_skipped_when_matching():
    """
        set_dns_servers returns SKIPPED when the sorted current list
        matches the sorted desired list.
    """
    runner = _MockCommandRunner()
    payload = json.dumps([
        {"InterfaceAlias": "Ethernet0", "AddressFamily": 2,
         "ServerAddresses": ["1.1.1.1", "8.8.8.8"]},
    ])
    runner.results[tuple(_dns_argv("Ethernet0"))] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.set_dns_servers(
        interface="Ethernet0",
        servers=["8.8.8.8", "1.1.1.1"],
    )
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SKIPPED,
    )
    return


def test_get_dns_servers_returns_flattened_list():
    """
        get_dns_servers flattens ServerAddresses arrays into a single
        list.
    """
    runner = _MockCommandRunner()
    payload = json.dumps([
        {"InterfaceAlias": "Ethernet0", "AddressFamily": 2,
         "ServerAddresses": ["1.1.1.1", "8.8.8.8"]},
    ])
    runner.results[tuple(_dns_argv("Ethernet0"))] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    rtnval = manager.get_dns_servers(interface="Ethernet0")
    testbase.assert_equal(rtnval, ["1.1.1.1", "8.8.8.8"])
    # Reads must not elevate.
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_hostname / set_hostname
# ----------------------------------------------------------------------


def test_get_hostname_returns_stripped_stdout():
    """
        get_hostname returns the trimmed stdout from
        ``[System.Net.Dns]::GetHostName()``.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_hostname_argv())] = RunResult(
        exit_code=0, stdout="WIN-HOST\r\n", stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    rtnval = manager.get_hostname()
    testbase.assert_equal(rtnval, "WIN-HOST")
    return


def test_set_hostname_dispatches_rename_computer_and_marks_reboot():
    """
        set_hostname dispatches Rename-Computer and the SUCCESS result
        carries ``requires_reboot=True``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_hostname_argv())] = RunResult(
        exit_code=0, stdout="OLD-HOST\r\n", stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.set_hostname(name="NEW-HOST")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SUCCESS,
    )
    testbase.assert_equal(result.requires_reboot, True)
    expected_script = "Rename-Computer -NewName 'NEW-HOST' -Force"
    testbase.assert_equal(runner.calls[-1]["args"][-1], expected_script)
    return


def test_set_hostname_skipped_when_matching_case_insensitive():
    """
        set_hostname returns SKIPPED when GetHostName already reports
        the desired name (case-insensitive).
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_hostname_argv())] = RunResult(
        exit_code=0, stdout="WIN-host\r\n", stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.set_hostname(name="WIN-HOST")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if "Rename-Computer" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_hostname_dry_run_marks_reboot():
    """
        set_hostname dry-run records ``requires_reboot=True`` so the
        coordinator can still surface the deferred change.
    """
    runner = _MockCommandRunner()
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(dry_run=True),
    )
    result = manager.set_hostname(name="NEW-HOST")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.DRY_RUN,
    )
    testbase.assert_equal(result.requires_reboot, True)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate_when_config_elevated():
    """
        Writes pass ``elevated=True`` to the runner when
        ``config.elevated`` is True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_status(runner=runner, name="Ethernet0", status="Disabled")
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(elevated=True),
    )
    result = manager.enable_interface(name="Ethernet0")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.SUCCESS,
    )
    write_call = runner.calls[-1]
    testbase.assert_equal(write_call["elevated"], True)
    # The status probe must still not elevate.
    status_call = runner.calls[0]
    testbase.assert_equal(status_call["elevated"], False)
    return


def test_reads_never_elevate_when_config_elevated():
    """
        Read verbs (``get_dns_servers``) never pass ``elevated=True``
        even when ``config.elevated`` is True.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_dns_argv("Ethernet0"))] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(elevated=True),
    )
    manager.get_dns_servers(interface="Ethernet0")
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_enable_interface_failed_propagates_runner_failure():
    """
        A non-zero exit from the runner maps to FAILED with the
        captured stdout / stderr / exit_code on the result.
    """
    runner = _MockCommandRunner()
    _seed_status(runner=runner, name="Ethernet0", status="Disabled")
    write_argv = _build_powershell_argv(
        script="Enable-NetAdapter -Name 'Ethernet0' -Confirm:$false",
    )
    runner.results[tuple(write_argv)] = RunResult(
        exit_code=2, stdout="", stderr="boom",
    )
    manager = WindowsNetworkConfigurationManager(
        runner=runner,
        config=WindowsNetworkConfigurationManagerConfig(),
    )
    result = manager.enable_interface(name="Ethernet0")
    testbase.assert_equal(
        result.status, WindowsNetworkActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "boom")
    return
