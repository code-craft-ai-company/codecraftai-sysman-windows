"""
    Windows network-configuration feature.

    Implements the cross-OS network-configuration contract for Windows
    using the PowerShell ``NetTCPIP`` family (``Get-NetAdapter``,
    ``New-NetIPAddress`` / ``Remove-NetIPAddress``,
    ``Enable-NetAdapter`` / ``Disable-NetAdapter``,
    ``New-NetRoute`` / ``Remove-NetRoute``,
    ``Set-DnsClientServerAddress`` / ``Get-DnsClientServerAddress``)
    plus ``Rename-Computer`` for the static hostname.

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner` (``powershell.exe`` invocations).  This
        module does NOT consume an :class:`IFileOperator`.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_interfaces``, ``get_dns_servers``, ``get_hostname``, all
        idempotency probes) never elevate.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so that ``$PROFILE`` cannot mutate
        cmdlet behaviour and PowerShell never blocks waiting for
        interactive input on a runner that has no controlling terminal.

    Reads use ``| ConvertTo-Json -Compress -Depth 5`` for a single
    robust serialization that round-trips across PowerShell 5.1 and
    PowerShell 7+.

    Reboot semantics:
        :meth:`set_hostname` calls ``Rename-Computer -NewName <name>
        -Force``.  ``Rename-Computer`` requires a reboot to take full
        effect — Windows defers the rename to the next boot.  The
        :class:`WindowsNetworkActionResult` returned from
        :meth:`set_hostname` always has ``requires_reboot=True`` on
        SUCCESS so the cross-OS coordinator can surface the deferred
        nature of the change.

    Netmask conversion:
        ``configure_interface`` accepts either a dotted-quad netmask
        (``"255.255.255.0"``) or a numeric prefix length (``"24"``).
        Conversion happens via :func:`_dotted_quad_to_prefix`, which
        is a pure function so it can be unit-tested in isolation.

    Routes:
        ``add_route`` REQUIRES a CIDR-form destination (e.g.
        ``"10.0.0.0/8"``).  ``New-NetRoute`` requires a
        ``-DestinationPrefix`` value and an explicit ``-NextHop``;
        idempotency is checked by listing
        ``Get-NetRoute -DestinationPrefix <dest>``.
"""

import json
import os

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_DEFAULT_POWERSHELL = "powershell.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

# Status strings surfaced on :class:`WindowsInterfaceInfo`.
_INTERFACE_STATUS_UP = "Up"
_INTERFACE_STATUS_DOWN = "Down"
_INTERFACE_STATUS_DISABLED = "Disabled"


WindowsNetworkActionStatus = ActionStatus


@dataclass
class WindowsNetworkConfigurationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsNetworkConfigurationManager`.
        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell executable
                                      used to run cmdlets.  Defaults to
                                      ``"powershell.exe"`` (PATH-resolved
                                      Windows PowerShell 5.1).  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL


@dataclass
class WindowsNetworkActionResult(FeatureActionResult):
    """
        Result of a network-configuration write operation.

        :param interface: The interface alias targeted, or ``None`` for
                          operations that are not interface-scoped
                          (``set_hostname``, ``add_route``,
                          ``remove_route``).
        :param destination: The route destination, when applicable.
        :param requires_reboot: True when the action requires a reboot
                                to take full effect.  Currently set by
                                :meth:`WindowsNetworkConfigurationManager.set_hostname`.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    interface: Optional[str] = None
    destination: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None


@dataclass
class WindowsInterfaceInfo:
    """
        Summary of a single network interface as enumerated by
        :meth:`WindowsNetworkConfigurationManager.get_interfaces`.

        :param name: ``Get-NetAdapter`` ``Name`` field.
        :param alias: ``Get-NetAdapter`` ``InterfaceAlias`` field.  On
                      most modern Windows builds this matches ``name``;
                      teaming / bridging can introduce a separate alias.
        :param status: One of ``"Up"``, ``"Down"``, ``"Disabled"``, or
                       a passthrough of any other ``Get-NetAdapter``
                       Status string.
        :param mac_address: ``Get-NetAdapter`` ``MacAddress`` field, or
                            ``None`` when absent.
        :param link_speed: ``Get-NetAdapter`` ``LinkSpeed`` field, or
                           ``None`` when absent.
        :param ipv4_addresses: ``addr/prefix`` strings parsed from
                               ``Get-NetIPAddress`` rows where
                               ``AddressFamily`` indicates IPv4.
        :param ipv6_addresses: ``addr/prefix`` strings parsed from
                               ``Get-NetIPAddress`` rows where
                               ``AddressFamily`` indicates IPv6.
        :param interface_index: ``Get-NetAdapter`` ``InterfaceIndex``
                                field, or ``None`` when absent.
    """
    name: str
    alias: str
    status: str
    mac_address: Optional[str] = None
    link_speed: Optional[str] = None
    ipv4_addresses: list[str] = field(default_factory=list)
    ipv6_addresses: list[str] = field(default_factory=list)
    interface_index: Optional[int] = None


@dataclass
class WindowsRouteInfo:
    """
        Parsed routing-table entry as enumerated by ``Get-NetRoute``.

        :param destination: ``DestinationPrefix`` (e.g. ``"10.0.0.0/8"``).
        :param next_hop: ``NextHop`` field.  ``"0.0.0.0"`` and ``"::"``
                         (link-style next hops) are normalised to
                         ``None``.
        :param interface_alias: ``InterfaceAlias`` field.
        :param metric: ``RouteMetric`` field, or ``None`` when absent.
    """
    destination: str
    next_hop: Optional[str]
    interface_alias: Optional[str]
    metric: Optional[int] = None


class WindowsNetworkQueryError(Exception):
    """
        Raised when a network read fails for a reason other than the
        target simply being absent.
    """

    def __init__(
        self,
        *,
        command: Optional[list[str]] = None,
        exit_code: Optional[int] = None,
        stderr: str = "",
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed (when relevant).
            :param exit_code: The runner's exit code (when relevant).
            :param stderr: The runner's captured stderr (when relevant).
            :param message: Optional pre-built message used for cases
                            where there is no failed argv to report
                            (e.g. JSON parse errors).
        """
        if command is None:
            self.command: list[str] = []
        else:
            self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Network query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(
                " ".join(self.command),
            ))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _dotted_quad_to_prefix(*, netmask: str) -> int:
    """
        Convert a dotted-quad netmask or numeric prefix string into a
        numeric IPv4 prefix length.

        Pure function so it can be unit-tested in isolation.

        :param netmask: Either a dotted-quad netmask
                        (e.g. ``"255.255.255.0"``) or a numeric
                        prefix string (e.g. ``"24"``).

        :returns: Prefix length as an int.
        :raises ValueError: When the input is neither a valid prefix
                            number in [0, 32] nor a valid dotted-quad
                            netmask.
    """
    if netmask is None:
        err_msg = "netmask must not be None"
        raise ValueError(err_msg)

    stripped = netmask.strip()
    if len(stripped) == 0:
        err_msg = "netmask must not be empty"
        raise ValueError(err_msg)

    if "." not in stripped:
        try:
            prefix_value = int(stripped)
        except ValueError:
            err_msg = "Invalid prefix value: {!r}".format(stripped)
            raise ValueError(err_msg) from None
        if prefix_value < 0 or prefix_value > 32:
            err_msg = "Prefix out of range [0, 32]: {!r}".format(stripped)
            raise ValueError(err_msg)
        rtnval = prefix_value
        return rtnval

    octets = stripped.split(".")
    if len(octets) != 4:
        err_msg = "Invalid dotted-quad netmask: {!r}".format(stripped)
        raise ValueError(err_msg)

    bits = 0
    for octet in octets:
        try:
            value = int(octet)
        except ValueError:
            err_msg = "Invalid octet in netmask: {!r}".format(stripped)
            raise ValueError(err_msg) from None
        if value < 0 or value > 255:
            err_msg = "Octet out of range in netmask: {!r}".format(stripped)
            raise ValueError(err_msg)
        for bit_position in range(8):
            if (value >> (7 - bit_position)) & 1:
                bits += 1

    rtnval = bits
    return rtnval


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.

        :param value: The raw text.

        :returns: The escaped text suitable for embedding between
                  single quotes in a PowerShell scriptblock.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _normalize_status_field(*, raw_value) -> str:
    """
        Normalize a ``Get-NetAdapter`` ``Status`` field.

        ``Get-NetAdapter`` emits ``"Up"``, ``"Disabled"``,
        ``"Disconnected"``, ``"NotPresent"``, etc.  This helper folds
        ``"Disconnected"`` / ``"NotPresent"`` to ``"Down"`` so the
        idempotency guards on enable / disable can compare against the
        canonical UP / DOWN / DISABLED tri-state.

        :param raw_value: Raw field value from the parsed JSON object.

        :returns: One of ``"Up"``, ``"Down"``, ``"Disabled"``, or a
                  pass-through of any other reported string.
    """
    if raw_value is None:
        rtnval = ""
        return rtnval
    if not isinstance(raw_value, str):
        rtnval = str(raw_value)
        return rtnval

    text = raw_value.strip()
    if text == "Up":
        rtnval = _INTERFACE_STATUS_UP
        return rtnval
    if text == "Disabled":
        rtnval = _INTERFACE_STATUS_DISABLED
        return rtnval
    if text in ("Disconnected", "NotPresent", "Down"):
        rtnval = _INTERFACE_STATUS_DOWN
        return rtnval
    rtnval = text
    return rtnval


def _decode_address_family(*, raw_value) -> str:
    """
        Decode a ``Get-NetIPAddress`` ``AddressFamily`` field.

        ``ConvertTo-Json`` emits the underlying enum as an integer
        (``2`` for IPv4, ``23`` for IPv6) on Windows PowerShell 5.1 and
        as a string on PowerShell 7+.  We accept both shapes.

        :param raw_value: Raw field value.

        :returns: ``"IPv4"``, ``"IPv6"``, or ``"Unknown"``.
    """
    if isinstance(raw_value, int):
        if raw_value == 2:
            rtnval = "IPv4"
            return rtnval
        if raw_value == 23:
            rtnval = "IPv6"
            return rtnval
        rtnval = "Unknown"
        return rtnval

    if isinstance(raw_value, str):
        text = raw_value.strip()
        if text in ("IPv4", "IPv6"):
            rtnval = text
            return rtnval
        rtnval = "Unknown"
        return rtnval

    rtnval = "Unknown"
    return rtnval


def _normalize_to_list(*, parsed):
    """
        Normalise a ``ConvertTo-Json`` payload into a list.

        ``ConvertTo-Json`` emits a bare object instead of a single-
        element array when the source pipeline produced exactly one
        item.  This helper folds the bare-object shape into a single-
        element list so downstream parsers always see an iterable.

        :param parsed: Output of :func:`json.loads`.

        :returns: A list.  ``None`` and other non-list / non-dict types
                  collapse to an empty list.
    """
    if isinstance(parsed, list):
        rtnval = parsed
        return rtnval
    if isinstance(parsed, dict):
        rtnval = [parsed]
        return rtnval
    rtnval = []
    return rtnval


def _parse_get_netadapter(*, text: str) -> list[dict]:
    """
        Parse the JSON output of ``Get-NetAdapter | ConvertTo-Json``.

        :param text: Raw stdout text.

        :returns: A list of dicts (the bare-object shape is normalised
                  into a one-element list).  Returns an empty list when
                  the input fails to parse.
    """
    stripped = text.strip()
    if len(stripped) == 0:
        rtnval: list[dict] = []
        return rtnval

    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = []
        return rtnval

    rtnval = _normalize_to_list(parsed=parsed)
    return rtnval


def _parse_get_netipaddress(*, text: str) -> dict:
    """
        Parse the JSON output of ``Get-NetIPAddress | ConvertTo-Json``.

        :param text: Raw stdout text.

        :returns: A dict keyed on ``InterfaceAlias``.  Each value is a
                  ``{"ipv4": [...], "ipv6": [...]}`` dict of
                  ``"<addr>/<prefix>"`` strings.
    """
    rtnval: dict = {}
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval

    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval

    records = _normalize_to_list(parsed=parsed)
    for record in records:
        if not isinstance(record, dict):
            continue
        alias_raw = record.get("InterfaceAlias")
        if alias_raw is None:
            continue
        alias = str(alias_raw)
        if len(alias) == 0:
            continue

        address_raw = record.get("IPAddress")
        if address_raw is None:
            continue
        address = str(address_raw)
        if len(address) == 0:
            continue

        prefix_raw = record.get("PrefixLength")
        if isinstance(prefix_raw, int):
            cidr = "{}/{}".format(address, prefix_raw)
        else:
            cidr = address

        family = _decode_address_family(
            raw_value=record.get("AddressFamily"),
        )

        if alias not in rtnval:
            rtnval[alias] = {"ipv4": [], "ipv6": []}

        if family == "IPv4":
            rtnval[alias]["ipv4"].append(cidr)
        elif family == "IPv6":
            rtnval[alias]["ipv6"].append(cidr)

    return rtnval


def _merge_adapter_and_ip(
    *,
    adapter_records: list[dict],
    ip_by_alias: dict,
) -> list[WindowsInterfaceInfo]:
    """
        Merge ``Get-NetAdapter`` rows with ``Get-NetIPAddress`` rows
        keyed on ``InterfaceAlias``.

        :param adapter_records: Output of :func:`_parse_get_netadapter`.
        :param ip_by_alias: Output of :func:`_parse_get_netipaddress`.

        :returns: One :class:`WindowsInterfaceInfo` per adapter row.
    """
    rtnval: list[WindowsInterfaceInfo] = []
    for record in adapter_records:
        if not isinstance(record, dict):
            continue

        name_raw = record.get("Name")
        if name_raw is None:
            continue
        name = str(name_raw)
        if len(name) == 0:
            continue

        alias_raw = record.get("InterfaceAlias")
        if alias_raw is None or len(str(alias_raw)) == 0:
            alias = name
        else:
            alias = str(alias_raw)

        status = _normalize_status_field(raw_value=record.get("Status"))

        mac_raw = record.get("MacAddress")
        if mac_raw is None or len(str(mac_raw)) == 0:
            mac_address: Optional[str] = None
        else:
            mac_address = str(mac_raw)

        link_speed_raw = record.get("LinkSpeed")
        if link_speed_raw is None or len(str(link_speed_raw)) == 0:
            link_speed: Optional[str] = None
        else:
            link_speed = str(link_speed_raw)

        index_raw = record.get("InterfaceIndex")
        if isinstance(index_raw, int):
            interface_index: Optional[int] = index_raw
        else:
            interface_index = None

        ipv4_list: list[str] = []
        ipv6_list: list[str] = []
        if alias in ip_by_alias:
            ipv4_list = list(ip_by_alias[alias].get("ipv4", []))
            ipv6_list = list(ip_by_alias[alias].get("ipv6", []))
        elif name in ip_by_alias:
            # Some tooling reports IP rows keyed by the kernel name
            # rather than the alias; fall back to that shape.
            ipv4_list = list(ip_by_alias[name].get("ipv4", []))
            ipv6_list = list(ip_by_alias[name].get("ipv6", []))

        info = WindowsInterfaceInfo(
            name=name,
            alias=alias,
            status=status,
            mac_address=mac_address,
            link_speed=link_speed,
            ipv4_addresses=ipv4_list,
            ipv6_addresses=ipv6_list,
            interface_index=interface_index,
        )
        rtnval.append(info)

    return rtnval


def _parse_get_dns(*, text: str) -> list[str]:
    """
        Parse the JSON output of
        ``Get-DnsClientServerAddress -InterfaceAlias <iface> |
        ConvertTo-Json -Compress -Depth 5``.

        ``Get-DnsClientServerAddress`` returns rows with a
        ``ServerAddresses`` array (one row per address family).  This
        parser flattens every ``ServerAddresses`` entry into a single
        ordered list.

        :param text: Raw stdout text.

        :returns: List of DNS server addresses in declaration order.
                  Empty list when the payload is absent or unparseable.
    """
    rtnval: list[str] = []
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval

    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval

    records = _normalize_to_list(parsed=parsed)
    for record in records:
        if not isinstance(record, dict):
            continue
        servers = record.get("ServerAddresses")
        if servers is None:
            continue
        if isinstance(servers, list):
            for entry in servers:
                if entry is None:
                    continue
                text_entry = str(entry)
                if len(text_entry) == 0:
                    continue
                rtnval.append(text_entry)
        elif isinstance(servers, str):
            if len(servers) > 0:
                rtnval.append(servers)

    return rtnval


def _parse_get_netroute(*, text: str) -> list[WindowsRouteInfo]:
    """
        Parse the JSON output of ``Get-NetRoute | ConvertTo-Json``.

        ``NextHop == "0.0.0.0"`` (IPv4) and ``"::"`` (IPv6) are the
        ``Get-NetRoute`` placeholders for "directly connected" and
        collapse to ``None`` on the parsed :class:`WindowsRouteInfo`.

        :param text: Raw stdout text.

        :returns: One :class:`WindowsRouteInfo` per parsed row.
    """
    rtnval: list[WindowsRouteInfo] = []
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval

    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval

    records = _normalize_to_list(parsed=parsed)
    for record in records:
        if not isinstance(record, dict):
            continue

        dest_raw = record.get("DestinationPrefix")
        if dest_raw is None:
            continue
        destination = str(dest_raw)
        if len(destination) == 0:
            continue

        next_hop_raw = record.get("NextHop")
        if next_hop_raw is None:
            next_hop: Optional[str] = None
        else:
            next_hop_text = str(next_hop_raw).strip()
            if next_hop_text in ("0.0.0.0", "::", ""):
                next_hop = None
            else:
                next_hop = next_hop_text

        alias_raw = record.get("InterfaceAlias")
        if alias_raw is None or len(str(alias_raw)) == 0:
            interface_alias: Optional[str] = None
        else:
            interface_alias = str(alias_raw)

        metric_raw = record.get("RouteMetric")
        if isinstance(metric_raw, int):
            metric: Optional[int] = metric_raw
        else:
            metric = None

        info = WindowsRouteInfo(
            destination=destination,
            next_hop=next_hop,
            interface_alias=interface_alias,
            metric=metric,
        )
        rtnval.append(info)

    return rtnval


# ----------------------------------------------------------------------
# WindowsNetworkConfigurationManager
# ----------------------------------------------------------------------


class WindowsNetworkConfigurationManager(BaseFeatureManager):
    """
        Windows network-configuration manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`; persistent state
        is owned by the underlying NetTCPIP / DnsClient providers and
        the SCM-backed Computer Browser service.  Reads never elevate;
        writes elevate when ``config.elevated`` is True.
    """

    result_class = WindowsNetworkActionResult
    status_class = WindowsNetworkActionStatus
    feature_name = "network-configuration"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_interfaces(self) -> list[WindowsInterfaceInfo]:
        """
            Enumerate network interfaces.

            Runs ``Get-NetAdapter | Select Name,InterfaceAlias,Status,
            LinkSpeed,MacAddress,InterfaceIndex | ConvertTo-Json
            -Compress -Depth 5`` and ``Get-NetIPAddress | Select
            InterfaceAlias,IPAddress,AddressFamily,PrefixLength |
            ConvertTo-Json -Compress -Depth 5`` and merges the two
            payloads on ``InterfaceAlias``.  Reads never elevate.

            :returns: One :class:`WindowsInterfaceInfo` per discovered
                      adapter.
            :raises WindowsNetworkQueryError: When either cmdlet exits
                                              non-zero with stderr.
        """
        adapter_script = (
            "Get-NetAdapter "
            "| Select-Object Name, InterfaceAlias, Status, LinkSpeed, "
            "MacAddress, InterfaceIndex "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        adapter_command = self._build_powershell_argv(script=adapter_script)
        adapter_run = self._runner.run(
            list(adapter_command), elevated=False,
        )
        if adapter_run.exit_code != 0:
            raise WindowsNetworkQueryError(
                command=adapter_command,
                exit_code=adapter_run.exit_code,
                stderr=adapter_run.stderr,
            )

        ip_script = (
            "Get-NetIPAddress "
            "| Select-Object InterfaceAlias, IPAddress, AddressFamily, "
            "PrefixLength "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        ip_command = self._build_powershell_argv(script=ip_script)
        ip_run = self._runner.run(list(ip_command), elevated=False)
        if ip_run.exit_code != 0:
            raise WindowsNetworkQueryError(
                command=ip_command,
                exit_code=ip_run.exit_code,
                stderr=ip_run.stderr,
            )

        adapter_records = _parse_get_netadapter(text=adapter_run.stdout)
        ip_by_alias = _parse_get_netipaddress(text=ip_run.stdout)
        rtnval = _merge_adapter_and_ip(
            adapter_records=adapter_records,
            ip_by_alias=ip_by_alias,
        )
        return rtnval

    def get_dns_servers(self, interface: str) -> list[str]:
        """
            Read DNS servers configured on ``interface``.

            Runs ``Get-DnsClientServerAddress -InterfaceAlias <iface> |
            ConvertTo-Json -Compress -Depth 5`` and flattens the
            resulting ``ServerAddresses`` arrays into a single list.
            Reads never elevate.

            :param interface: Interface alias.

            :returns: List of DNS servers in declaration order.  Empty
                      when none are configured.
            :raises WindowsNetworkQueryError: When the cmdlet exits
                                              non-zero with stderr.
        """
        escaped = _ps_escape_single_quoted(value=interface)
        script = (
            "Get-DnsClientServerAddress -InterfaceAlias '{}' "
            "| Select-Object InterfaceAlias, AddressFamily, "
            "ServerAddresses "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise WindowsNetworkQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_get_dns(text=run_result.stdout)
        return rtnval

    def get_hostname(self) -> str:
        """
            Read the current Unix-style hostname via
            ``[System.Net.Dns]::GetHostName()``.

            :returns: The hostname with surrounding whitespace stripped.
                      Empty string when the call exits non-zero.
        """
        script = "[System.Net.Dns]::GetHostName()"
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval
        rtnval = run_result.stdout.strip()
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def configure_interface(
        self,
        name: str,
        address: str,
        netmask: str,
        gateway: Optional[str],
    ) -> WindowsNetworkActionResult:
        """
            Configure persistent IPv4 addressing for ``name``.

            Renders a single PowerShell scriptblock that runs
            ``Remove-NetIPAddress -InterfaceAlias <iface>
            -Confirm:$false`` (best-effort cleanup) followed by
            ``New-NetIPAddress -InterfaceAlias <iface> -IPAddress <ip>
            -PrefixLength <N> -DefaultGateway <gw>``.  ``-DefaultGateway``
            is omitted when ``gateway`` is ``None``.

            Idempotent: when ``Get-NetIPAddress`` reports the desired
            ``<address>/<prefix>`` already configured on the interface,
            returns ``SKIPPED`` without touching state.

            :param name: Interface alias.
            :param address: IPv4 address in dotted-quad form.
            :param netmask: Either a dotted-quad netmask
                            (``"255.255.255.0"``) or a numeric prefix
                            (``"24"``).
            :param gateway: Default gateway, or ``None`` to omit
                            ``-DefaultGateway``.

            :returns: The :class:`WindowsNetworkActionResult`.
        """
        try:
            prefix = _dotted_quad_to_prefix(netmask=netmask)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsNetworkActionStatus.FAILED,
                interface=name,
                detail=name,
                command=[],
                message="invalid_netmask",
                stderr=str(exc),
            )
            return failed

        escaped_iface = _ps_escape_single_quoted(value=name)
        escaped_address = _ps_escape_single_quoted(value=address)
        if gateway is None:
            new_fragment = (
                "New-NetIPAddress -InterfaceAlias '{iface}' "
                "-IPAddress '{addr}' -PrefixLength {prefix}"
            ).format(iface=escaped_iface, addr=escaped_address, prefix=prefix)
        else:
            escaped_gateway = _ps_escape_single_quoted(value=gateway)
            new_fragment = (
                "New-NetIPAddress -InterfaceAlias '{iface}' "
                "-IPAddress '{addr}' -PrefixLength {prefix} "
                "-DefaultGateway '{gw}'"
            ).format(
                iface=escaped_iface,
                addr=escaped_address,
                prefix=prefix,
                gw=escaped_gateway,
            )
        script = (
            "Remove-NetIPAddress -InterfaceAlias '{iface}' "
            "-Confirm:$false -ErrorAction SilentlyContinue; "
            "{new_frag}"
        ).format(iface=escaped_iface, new_frag=new_fragment)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsNetworkActionStatus.DRY_RUN,
                interface=name,
                detail=name,
                command=command,
            )
            return dry

        if self._interface_already_has_address(
            interface=name, address=address, prefix=prefix,
        ):
            skipped = self._build_result(
                status=WindowsNetworkActionStatus.SKIPPED,
                interface=name,
                detail=name,
                command=command,
                message="interface already has desired IP/prefix",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=name,
            detail=name,
        )
        return rtnval

    def enable_interface(self, name: str) -> WindowsNetworkActionResult:
        """
            Enable a network adapter via ``Enable-NetAdapter -Name
            <iface>``.

            Idempotent: when ``Get-NetAdapter`` reports the adapter
            already in the ``Up`` state, returns ``SKIPPED``.

            :param name: Interface alias.

            :returns: The :class:`WindowsNetworkActionResult`.
        """
        escaped_iface = _ps_escape_single_quoted(value=name)
        script = "Enable-NetAdapter -Name '{}' -Confirm:$false".format(
            escaped_iface,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsNetworkActionStatus.DRY_RUN,
                interface=name,
                detail=name,
                command=command,
            )
            return dry

        current_status = self._query_adapter_status(interface=name)
        if current_status == _INTERFACE_STATUS_UP:
            skipped = self._build_result(
                status=WindowsNetworkActionStatus.SKIPPED,
                interface=name,
                detail=name,
                command=command,
                message="interface is already enabled",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=name,
            detail=name,
        )
        return rtnval

    def disable_interface(self, name: str) -> WindowsNetworkActionResult:
        """
            Disable a network adapter via ``Disable-NetAdapter -Name
            <iface> -Confirm:$false``.

            Idempotent: when ``Get-NetAdapter`` reports the adapter
            already in the ``Disabled`` state, returns ``SKIPPED``.

            :param name: Interface alias.

            :returns: The :class:`WindowsNetworkActionResult`.
        """
        escaped_iface = _ps_escape_single_quoted(value=name)
        script = "Disable-NetAdapter -Name '{}' -Confirm:$false".format(
            escaped_iface,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsNetworkActionStatus.DRY_RUN,
                interface=name,
                detail=name,
                command=command,
            )
            return dry

        current_status = self._query_adapter_status(interface=name)
        if current_status == _INTERFACE_STATUS_DISABLED:
            skipped = self._build_result(
                status=WindowsNetworkActionStatus.SKIPPED,
                interface=name,
                detail=name,
                command=command,
                message="interface is already disabled",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=name,
            detail=name,
        )
        return rtnval

    def add_route(
        self,
        dest: str,
        gateway: str,
    ) -> WindowsNetworkActionResult:
        """
            Add a route via ``New-NetRoute -DestinationPrefix <dest>
            -NextHop <gateway> -InterfaceAlias <iface>``.

            ``New-NetRoute`` requires an ``-InterfaceAlias``.  This
            method derives one by probing
            ``Get-NetRoute -DestinationPrefix 0.0.0.0/0`` for the
            current default-route interface.  When that probe fails
            (no default route present), the verb returns ``FAILED`` with
            ``message="no_default_route_interface"``.

            ``dest`` MUST be in CIDR form (e.g. ``"10.0.0.0/8"``).

            Idempotent: when ``Get-NetRoute -DestinationPrefix <dest>``
            already reports a row with a matching ``NextHop``, returns
            ``SKIPPED``.

            :param dest: Destination prefix in CIDR form.
            :param gateway: Gateway IP.

            :returns: The :class:`WindowsNetworkActionResult`.
        """
        if self._config.dry_run:
            dry_command = self._build_powershell_argv(
                script=self._add_route_script(
                    dest=dest, gateway=gateway, interface_alias="",
                ),
            )
            dry = self._build_result(
                status=WindowsNetworkActionStatus.DRY_RUN,
                interface=None,
                detail=dest,
                destination=dest,
                command=dry_command,
            )
            return dry

        try:
            interface_alias = self._resolve_default_interface()
        except WindowsNetworkQueryError as exc:
            failed_command = self._build_powershell_argv(
                script=self._add_route_script(
                    dest=dest, gateway=gateway, interface_alias="",
                ),
            )
            failed = self._build_result(
                status=WindowsNetworkActionStatus.FAILED,
                interface=None,
                detail=dest,
                destination=dest,
                command=failed_command,
                message="no_default_route_interface",
                stderr=str(exc),
            )
            return failed

        script = self._add_route_script(
            dest=dest, gateway=gateway, interface_alias=interface_alias,
        )
        command = self._build_powershell_argv(script=script)

        if self._route_matches(dest=dest, gateway=gateway):
            skipped = self._build_result(
                status=WindowsNetworkActionStatus.SKIPPED,
                interface=interface_alias,
                detail=dest,
                destination=dest,
                command=command,
                message="route already present with matching gateway",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=interface_alias,
            detail=dest,
            destination=dest,
        )
        return rtnval

    def remove_route(self, dest: str) -> WindowsNetworkActionResult:
        """
            Remove a route via ``Remove-NetRoute -DestinationPrefix
            <dest> -Confirm:$false``.

            Idempotent: when ``Get-NetRoute -DestinationPrefix <dest>``
            reports no matching row, returns ``SKIPPED``.

            :param dest: Destination prefix in CIDR form.

            :returns: The :class:`WindowsNetworkActionResult`.
        """
        escaped_dest = _ps_escape_single_quoted(value=dest)
        script = (
            "Remove-NetRoute -DestinationPrefix '{}' -Confirm:$false"
        ).format(escaped_dest)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsNetworkActionStatus.DRY_RUN,
                interface=None,
                detail=dest,
                destination=dest,
                command=command,
            )
            return dry

        if not self._route_exists(dest=dest):
            skipped = self._build_result(
                status=WindowsNetworkActionStatus.SKIPPED,
                interface=None,
                detail=dest,
                destination=dest,
                command=command,
                message="route is not present",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=None,
            detail=dest,
            destination=dest,
        )
        return rtnval

    def set_dns_servers(
        self,
        interface: str,
        servers: list[str],
    ) -> WindowsNetworkActionResult:
        """
            Set per-interface DNS servers via
            ``Set-DnsClientServerAddress -InterfaceAlias <iface>
            -ServerAddresses <servers...>``.

            When ``servers`` is empty, dispatches
            ``Set-DnsClientServerAddress -InterfaceAlias <iface>
            -ResetServerAddresses`` to clear the configuration.

            Idempotent: when the sorted current list matches the sorted
            desired list, returns ``SKIPPED``.

            :param interface: Interface alias.
            :param servers: Ordered list of DNS server IPs.  An empty
                            list clears the configuration.

            :returns: The :class:`WindowsNetworkActionResult`.
        """
        escaped_iface = _ps_escape_single_quoted(value=interface)
        if len(servers) == 0:
            script = (
                "Set-DnsClientServerAddress -InterfaceAlias '{}' "
                "-ResetServerAddresses"
            ).format(escaped_iface)
        else:
            escaped_servers = [
                "'{}'".format(_ps_escape_single_quoted(value=s))
                for s in servers
            ]
            script = (
                "Set-DnsClientServerAddress -InterfaceAlias '{}' "
                "-ServerAddresses @({})"
            ).format(escaped_iface, ", ".join(escaped_servers))
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsNetworkActionStatus.DRY_RUN,
                interface=interface,
                detail=interface,
                command=command,
            )
            return dry

        try:
            current = self.get_dns_servers(interface=interface)
        except WindowsNetworkQueryError:
            current = []

        if sorted(current) == sorted(servers):
            skipped = self._build_result(
                status=WindowsNetworkActionStatus.SKIPPED,
                interface=interface,
                detail=interface,
                command=command,
                message="DNS servers already match desired list",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=interface,
            detail=interface,
        )
        return rtnval

    def set_hostname(self, name: str) -> WindowsNetworkActionResult:
        """
            Set the static hostname via ``Rename-Computer -NewName
            <name> -Force``.

            **Reboot required.**  ``Rename-Computer`` defers the rename
            to the next boot; the SUCCESS result therefore always carries
            ``requires_reboot=True`` so the cross-OS coordinator can
            surface the deferred change to the operator.  This module
            does NOT issue ``Restart-Computer`` — the immediate-reboot
            path is reserved for a companion ``set_hostname_and_reboot``
            extension.

            Idempotent: when ``[System.Net.Dns]::GetHostName()`` already
            reports a case-insensitive match for ``name``, returns
            ``SKIPPED``.  Windows hostnames are not case-sensitive.

            :param name: Desired hostname.

            :returns: The :class:`WindowsNetworkActionResult` with
                      ``requires_reboot=True`` on SUCCESS.
        """
        escaped_name = _ps_escape_single_quoted(value=name)
        script = "Rename-Computer -NewName '{}' -Force".format(escaped_name)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsNetworkActionStatus.DRY_RUN,
                interface=None,
                detail=name,
                command=command,
                requires_reboot=True,
            )
            return dry

        current = self.get_hostname()
        if current.lower() == name.lower() and len(current) > 0:
            skipped = self._build_result(
                status=WindowsNetworkActionStatus.SKIPPED,
                interface=None,
                detail=name,
                command=command,
                message="hostname is already set",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsNetworkActionStatus.SUCCESS
            requires_reboot = True
        else:
            mapped_status = WindowsNetworkActionStatus.FAILED
            requires_reboot = False

        rtnval = WindowsNetworkActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            interface=None,
            destination=None,
            requires_reboot=requires_reboot,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv / dispatch / result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsNetworkConfigurationManagerConfig:
        """
            Return the manager's config typed.

            :returns: The typed configuration.
        """
        if isinstance(
            self._config, WindowsNetworkConfigurationManagerConfig,
        ):
            rtnval = self._config
        else:
            rtnval = WindowsNetworkConfigurationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list[str]:
        """
            Build the canonical PowerShell argv for a scriptblock.

            :param script: PowerShell scriptblock text.

            :returns: The full argv list.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _add_route_script(
        self,
        *,
        dest: str,
        gateway: str,
        interface_alias: str,
    ) -> str:
        """
            Build the ``New-NetRoute`` scriptblock for
            :meth:`add_route`.

            :param dest: Destination prefix.
            :param gateway: Gateway IP.
            :param interface_alias: Interface alias to bind to.

            :returns: The scriptblock text.
        """
        escaped_dest = _ps_escape_single_quoted(value=dest)
        escaped_gateway = _ps_escape_single_quoted(value=gateway)
        escaped_iface = _ps_escape_single_quoted(value=interface_alias)
        rtnval = (
            "New-NetRoute -InterfaceAlias '{iface}' "
            "-DestinationPrefix '{dest}' -NextHop '{gw}'"
        ).format(
            iface=escaped_iface,
            dest=escaped_dest,
            gw=escaped_gateway,
        )
        return rtnval

    def _query_adapter_status(self, *, interface: str) -> str:
        """
            Probe ``Get-NetAdapter -Name <iface>`` and return the
            ``Status`` string.

            :param interface: Interface alias.

            :returns: One of ``"Up"``, ``"Down"``, ``"Disabled"``, or
                      ``""`` when the read fails.
        """
        escaped = _ps_escape_single_quoted(value=interface)
        script = (
            "Get-NetAdapter -Name '{}' "
            "| Select-Object Status "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = ""
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            rtnval = ""
            return rtnval

        if isinstance(parsed, list):
            if len(parsed) == 0:
                rtnval = ""
                return rtnval
            record = parsed[0]
        else:
            record = parsed

        if not isinstance(record, dict):
            rtnval = ""
            return rtnval

        rtnval = _normalize_status_field(raw_value=record.get("Status"))
        return rtnval

    def _interface_already_has_address(
        self,
        *,
        interface: str,
        address: str,
        prefix: int,
    ) -> bool:
        """
            Probe ``Get-NetIPAddress -InterfaceAlias <iface>`` and
            check whether the desired ``address/prefix`` is already
            configured.

            :param interface: Interface alias.
            :param address: Desired IPv4 address.
            :param prefix: Desired numeric prefix length.

            :returns: True when the desired entry is already present.
        """
        escaped = _ps_escape_single_quoted(value=interface)
        script = (
            "Get-NetIPAddress -InterfaceAlias '{}' "
            "| Select-Object InterfaceAlias, IPAddress, AddressFamily, "
            "PrefixLength "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        ip_by_alias = _parse_get_netipaddress(text=run_result.stdout)
        if interface not in ip_by_alias:
            rtnval = False
            return rtnval

        wanted = "{}/{}".format(address, prefix)
        if wanted in ip_by_alias[interface].get("ipv4", []):
            rtnval = True
            return rtnval
        if wanted in ip_by_alias[interface].get("ipv6", []):
            rtnval = True
            return rtnval

        rtnval = False
        return rtnval

    def _route_exists(self, *, dest: str) -> bool:
        """
            Probe ``Get-NetRoute -DestinationPrefix <dest>`` for any
            matching route.

            :param dest: Destination prefix.

            :returns: True when at least one row is reported.
        """
        escaped = _ps_escape_single_quoted(value=dest)
        script = (
            "Get-NetRoute -DestinationPrefix '{}' "
            "| Select-Object DestinationPrefix, NextHop, "
            "InterfaceAlias, RouteMetric "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        routes = _parse_get_netroute(text=run_result.stdout)
        rtnval = len(routes) > 0
        return rtnval

    def _route_matches(self, *, dest: str, gateway: str) -> bool:
        """
            Probe ``Get-NetRoute -DestinationPrefix <dest>`` and check
            for a row with a matching ``NextHop``.

            :param dest: Destination prefix.
            :param gateway: Gateway IP.

            :returns: True when a matching row exists.
        """
        escaped = _ps_escape_single_quoted(value=dest)
        script = (
            "Get-NetRoute -DestinationPrefix '{}' "
            "| Select-Object DestinationPrefix, NextHop, "
            "InterfaceAlias, RouteMetric "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        routes = _parse_get_netroute(text=run_result.stdout)
        for route in routes:
            if route.next_hop is not None and route.next_hop == gateway:
                rtnval = True
                return rtnval

        rtnval = False
        return rtnval

    def _resolve_default_interface(self) -> str:
        """
            Resolve the interface alias that owns the default IPv4 route
            via ``Get-NetRoute -DestinationPrefix 0.0.0.0/0``.

            :returns: The interface alias of the default route.
            :raises WindowsNetworkQueryError: When the cmdlet fails or
                                              no default route exists.
        """
        script = (
            "Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
            "| Select-Object DestinationPrefix, NextHop, "
            "InterfaceAlias, RouteMetric "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsNetworkQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        routes = _parse_get_netroute(text=run_result.stdout)
        for route in routes:
            if (
                route.interface_alias is not None
                and len(route.interface_alias) > 0
            ):
                rtnval = route.interface_alias
                return rtnval

        err_msg_lines = [
            "No default route reported by Get-NetRoute -DestinationPrefix 0.0.0.0/0.",
        ]
        err_msg = os.linesep.join(err_msg_lines)
        raise WindowsNetworkQueryError(
            command=command,
            exit_code=run_result.exit_code,
            stderr=run_result.stderr,
            message=err_msg,
        )

    def _dispatch_write(
        self,
        *,
        command: list[str],
        interface: Optional[str],
        detail: Optional[str],
        destination: Optional[str] = None,
    ) -> WindowsNetworkActionResult:
        """
            Dispatch a write command, honouring elevation, and map the
            runner result onto a :class:`WindowsNetworkActionResult`.

            :param command: Full argv.
            :param interface: Interface alias to echo on the result.
            :param detail: Result ``detail`` value.
            :param destination: Optional route destination.

            :returns: The :class:`WindowsNetworkActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsNetworkActionStatus.SUCCESS
        else:
            mapped_status = WindowsNetworkActionStatus.FAILED

        rtnval = WindowsNetworkActionResult(
            status=mapped_status,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            interface=interface,
            destination=destination,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsNetworkActionStatus,
        interface: Optional[str],
        detail: Optional[str],
        command: list[str],
        destination: Optional[str] = None,
        requires_reboot: bool = False,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsNetworkActionResult:
        """
            Construct a :class:`WindowsNetworkActionResult` without
            invoking the runner.

            :param status: Outcome status.
            :param interface: Interface alias.
            :param detail: Result ``detail`` value.
            :param command: Would-have-been argv (copied for capture).
            :param destination: Optional route destination.
            :param requires_reboot: Reboot-required marker (used by
                                    :meth:`set_hostname` dry-run).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`WindowsNetworkActionResult`.
        """
        rtnval = WindowsNetworkActionResult(
            status=status,
            detail=detail,
            command=list(command),
            interface=interface,
            destination=destination,
            requires_reboot=requires_reboot,
            message=message,
            stderr=stderr,
        )
        return rtnval
