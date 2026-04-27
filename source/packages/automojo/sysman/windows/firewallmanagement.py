"""
    Windows firewall-management feature.

    Implements the cross-OS firewall-management contract for Windows
    using the PowerShell ``NetSecurity`` module
    (``New-NetFirewallRule``, ``Remove-NetFirewallRule``,
    ``Get-NetFirewallRule``, ``Set-NetFirewallProfile``,
    ``Get-NetFirewallProfile``, ``Get-NetConnectionProfile``).

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner` (``powershell.exe`` invocations).  This
        module does NOT consume an :class:`IFileOperator`.

    Zone mapping:
        The contract's ``zone`` argument maps directly onto a Windows
        Firewall profile name: one of ``"Domain"``, ``"Private"``,
        ``"Public"``, or ``"Any"``.  Any other value yields a
        ``FAILED`` result with ``message="invalid_zone"`` BEFORE the
        runner is invoked.

    Sentinel display-name prefixes:
        Every rule created through this manager uses a deterministic
        display-name prefix so that the manager can round-trip queries
        and idempotency probes:

            * ``automojo-svc-<service>`` for service-shape rules.
            * ``automojo-port-<port>-<protocol>`` for port-shape rules.
            * Rich rules carry the caller's ``-DisplayName``
              verbatim; ``add_rich_rule`` rejects rules without an
              explicit ``-DisplayName`` clause to preserve
              round-trippability.

    Verb behaviour highlights:
        * ``set_default_zone`` is a SKIPPED no-op when the zone is
          valid.  Windows has no "default profile" concept -- profiles
          activate based on the network category that the OS assigns
          to the current connection.  Invalid zones return FAILED so
          callers still receive type-safe rejection.
        * ``reload`` is a SUCCESS no-op.  Windows Firewall applies
          rules immediately when the underlying rule object commits.
        * ``query_zone`` filters
          ``Get-NetFirewallRule | Where-Object Profile`` and
          classifies rules by their display-name prefix.
        * ``query_state`` reports ``running=True`` iff every profile's
          ``Enabled`` field is True; ``default_zone`` is taken from
          ``Get-NetConnectionProfile``'s ``NetworkCategory`` for the
          active connection, which maps directly onto a profile name.

    Elevation policy:
        Reads never elevate.  Mutating verbs honour ``config.elevated``.
"""

import json
import os
import re

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

_VALID_ZONES = ("Domain", "Private", "Public", "Any")
_QUERIED_ZONES = ("Domain", "Private", "Public")

_VALID_PROTOCOLS = ("tcp", "udp")

_SVC_PREFIX = "automojo-svc-"
_PORT_PREFIX = "automojo-port-"

# Bundled service-to-(port, protocol) table.  Callers can override this
# by passing a non-None ``services_table`` on the config; the override
# completely replaces the bundled table.
_BUNDLED_SERVICES_TABLE: dict = {
    "ssh":   (22, "tcp"),
    "http":  (80, "tcp"),
    "https": (443, "tcp"),
    "smtp":  (25, "tcp"),
    "dns":   (53, "udp"),
    "smb":   (445, "tcp"),
    "rdp":   (3389, "tcp"),
}


WindowsFirewallActionStatus = ActionStatus


@dataclass
class WindowsFirewallManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsFirewallManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.
        :param services_table: Optional override for the bundled
                               ``service -> (port, protocol)`` table.
                               When ``None``, the bundled table is
                               used.  When set, the override
                               REPLACES the bundled table -- callers
                               must include every service they wish
                               to recognise.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    services_table: Optional[dict] = None


@dataclass
class WindowsFirewallActionResult(FeatureActionResult):
    """
        Result of a firewall-management write operation.

        :param zone: The zone (profile name) targeted, when
                     applicable.
        :param detail_name: For service / port / rich-rule writes,
                            the rule display name actually used.
                            ``None`` for verbs that are not rule-
                            scoped.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    zone: Optional[str] = None
    detail_name: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsZoneInfo:
    """
        Snapshot of a single Windows Firewall profile (zone).

        :param zone: The profile name (e.g. ``"Public"``).
        :param services: Service tokens (tail of ``automojo-svc-<...>``
                         display names).
        :param ports: Port tokens (tail of ``automojo-port-<port>-<proto>``
                      display names).
        :param rich_rules: Display names of rules that do not match
                           our sentinel prefixes.
        :param interfaces: Always empty -- Windows Firewall has no
                           per-zone interface concept the way
                           firewalld does.
        :param sources: Always empty -- Windows Firewall has no
                        per-zone "source" concept the way firewalld
                        does.
        :param is_default: True when this zone matches the active
                           network's category (i.e. it is the
                           "current" profile).  Computed via
                           ``Get-NetConnectionProfile`` when a
                           snapshot is requested as part of
                           :meth:`WindowsFirewallManager.query_state`.
    """
    zone: str
    services: list = field(default_factory=list)
    ports: list = field(default_factory=list)
    rich_rules: list = field(default_factory=list)
    interfaces: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    is_default: bool = False


@dataclass
class WindowsFirewallState:
    """
        Snapshot of overall Windows Firewall state.

        :param running: True when every profile's ``Enabled`` field is
                        True.  False when any profile is disabled.
        :param default_zone: The active network's category mapped onto
                             a profile name.  Empty string when the
                             host has no active connection.
    """
    running: bool
    default_zone: str = ""


class WindowsFirewallQueryError(Exception):
    """
        Raised when a firewall read fails for a reason other than the
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
            :param stderr: The runner's captured stderr (when
                           relevant).
            :param message: Optional pre-built message used for cases
                            where there is no failed argv to report.
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
            err_msg_lines = ["Firewall query command failed."]
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


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.

        :param value: Raw text.

        :returns: The escaped text.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _normalize_to_list(*, parsed) -> list:
    """
        Normalise a ``ConvertTo-Json`` payload into a list (bare-object
        shape -> single-element list).

        :param parsed: Output of :func:`json.loads`.

        :returns: A list.
    """
    if isinstance(parsed, list):
        rtnval = parsed
        return rtnval
    if isinstance(parsed, dict):
        rtnval = [parsed]
        return rtnval
    rtnval = []
    return rtnval


def _validate_zone(*, zone: str) -> bool:
    """
        Return True when ``zone`` is one of the allowed Windows
        Firewall profile names.

        :param zone: The candidate zone.

        :returns: True when valid.
    """
    if zone is None:
        rtnval = False
        return rtnval
    rtnval = zone in _VALID_ZONES
    return rtnval


def _validate_protocol(*, protocol: str) -> bool:
    """
        Return True when ``protocol`` is one of the supported
        protocol tokens.

        :param protocol: The candidate protocol.

        :returns: True when valid.
    """
    if protocol is None:
        rtnval = False
        return rtnval
    rtnval = protocol.lower() in _VALID_PROTOCOLS
    return rtnval


def _service_display_name(*, service: str) -> str:
    """
        Compose the deterministic display name for a service rule.

        :param service: Service token.

        :returns: ``automojo-svc-<service>``.
    """
    rtnval = "{}{}".format(_SVC_PREFIX, service)
    return rtnval


def _port_display_name(*, port: int, protocol: str) -> str:
    """
        Compose the deterministic display name for a port rule.

        :param port: Port number.
        :param protocol: ``"tcp"`` or ``"udp"``.

        :returns: ``automojo-port-<port>-<protocol>``.
    """
    rtnval = "{}{}-{}".format(_PORT_PREFIX, int(port), protocol.lower())
    return rtnval


def _extract_display_name(*, rule: str) -> Optional[str]:
    """
        Parse a ``-DisplayName 'value'`` token from a rich-rule
        string.

        Accepts both single-quoted (``-DisplayName 'foo'``) and
        double-quoted (``-DisplayName "foo"``) forms, and bare-token
        forms (``-DisplayName foo``).

        :param rule: The rich-rule splatted-parameter string.

        :returns: The display name, or ``None`` when the rule does
                  not carry one.
    """
    if rule is None:
        rtnval: Optional[str] = None
        return rtnval

    single_match = re.search(
        r"-DisplayName\s+'([^']+)'", rule,
    )
    if single_match is not None:
        rtnval = single_match.group(1)
        return rtnval

    double_match = re.search(
        r'-DisplayName\s+"([^"]+)"', rule,
    )
    if double_match is not None:
        rtnval = double_match.group(1)
        return rtnval

    bare_match = re.search(
        r"-DisplayName\s+(\S+)", rule,
    )
    if bare_match is not None:
        rtnval = bare_match.group(1)
        return rtnval

    rtnval = None
    return rtnval


def _profile_field_to_zones(*, raw_value) -> set:
    """
        Convert a ``Get-NetFirewallRule`` ``Profile`` field into the
        set of zone names it covers.

        ``Profile`` may arrive as a string (e.g. ``"Public, Private"``,
        or ``"Any"``), an int (bitmask), or a list.

        :param raw_value: The raw field value.

        :returns: A set of zone names.
    """
    rtnval: set = set()
    if raw_value is None:
        return rtnval

    if isinstance(raw_value, list):
        for item in raw_value:
            if isinstance(item, str):
                token = item.strip()
                if len(token) > 0:
                    rtnval.add(token)
        return rtnval

    if isinstance(raw_value, str):
        text = raw_value.strip()
        if len(text) == 0:
            return rtnval
        for piece in text.split(","):
            piece_clean = piece.strip()
            if len(piece_clean) == 0:
                continue
            rtnval.add(piece_clean)
        return rtnval

    if isinstance(raw_value, int):
        # Bitmask: 0x1=Domain, 0x2=Private, 0x4=Public, 0x7FFFFFFF=Any.
        if raw_value == 0x7FFFFFFF or raw_value == -1:
            rtnval.add("Any")
            return rtnval
        if (raw_value & 0x1) != 0:
            rtnval.add("Domain")
        if (raw_value & 0x2) != 0:
            rtnval.add("Private")
        if (raw_value & 0x4) != 0:
            rtnval.add("Public")
        return rtnval

    return rtnval


def _classify_rule(
    *,
    display_name: str,
    services: list,
    ports: list,
    rich_rules: list,
) -> None:
    """
        Append ``display_name`` to the appropriate output list based
        on its sentinel prefix.

        :param display_name: The rule's display name.
        :param services: Output services list.
        :param ports: Output ports list.
        :param rich_rules: Output rich-rules list.
    """
    if display_name.startswith(_SVC_PREFIX):
        tail = display_name[len(_SVC_PREFIX):]
        if len(tail) > 0:
            services.append(tail)
        return

    if display_name.startswith(_PORT_PREFIX):
        tail = display_name[len(_PORT_PREFIX):]
        if len(tail) > 0:
            ports.append(tail)
        return

    rich_rules.append(display_name)
    return


# ----------------------------------------------------------------------
# WindowsFirewallManager
# ----------------------------------------------------------------------


class WindowsFirewallManager(BaseFeatureManager):
    """
        Windows Firewall manager.  Every operation is dispatched
        through the injected :class:`ICommandRunner`.  Reads do not
        elevate; mutating verbs honour ``config.elevated``.
    """

    result_class = WindowsFirewallActionResult
    status_class = WindowsFirewallActionStatus
    feature_name = "firewall-management"

    # ------------------------------------------------------------------
    # Service writes
    # ------------------------------------------------------------------

    def add_service(
        self,
        service: str,
        zone: str,
    ) -> WindowsFirewallActionResult:
        """
            Add an inbound allow rule for the named service in the
            given zone.

            Resolves ``(port, protocol)`` from the bundled services
            table (or the caller-supplied override).  When the
            service is unknown, returns ``FAILED``.  When the rule
            already exists with the same display name, returns
            ``SKIPPED``.  Honours dry-run.

            :param service: The service token (e.g. ``"ssh"``).
            :param zone: The Windows Firewall profile.

            :returns: The :class:`WindowsFirewallActionResult`.
        """
        zone_failure = self._maybe_reject_zone(
            zone=zone, service_or_port=service,
        )
        if zone_failure is not None:
            return zone_failure

        services_table = self._resolve_services_table()
        if service not in services_table:
            failed = self._build_result(
                status=WindowsFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=None,
                command=[],
                message="unknown_service",
            )
            return failed

        port_proto = services_table[service]
        port = port_proto[0]
        protocol = port_proto[1]
        display_name = _service_display_name(service=service)

        script = self._render_new_netfirewallrule(
            display_name=display_name,
            protocol=protocol,
            local_port=port,
            zone=zone,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=display_name,
                command=command,
            )
            return dry

        if self._rule_exists(display_name=display_name):
            skipped = self._build_result(
                status=WindowsFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=display_name,
                command=command,
                message="rule already exists",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=display_name,
            operation="add_service",
            zone=zone,
            detail_name=display_name,
        )
        return rtnval

    def remove_service(
        self,
        service: str,
        zone: str,
    ) -> WindowsFirewallActionResult:
        """
            Remove the inbound allow rule for the named service.

            Idempotent: when no rule with the matching display name
            exists, returns ``SKIPPED``.  Honours dry-run.

            :param service: The service token.
            :param zone: The zone targeted (echoed onto the result;
                         the actual cmdlet matches by display name
                         only).

            :returns: The :class:`WindowsFirewallActionResult`.
        """
        zone_failure = self._maybe_reject_zone(
            zone=zone, service_or_port=service,
        )
        if zone_failure is not None:
            return zone_failure

        display_name = _service_display_name(service=service)
        script = self._render_remove_netfirewallrule(
            display_name=display_name,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=display_name,
                command=command,
            )
            return dry

        if not self._rule_exists(display_name=display_name):
            skipped = self._build_result(
                status=WindowsFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=display_name,
                command=command,
                message="rule does not exist",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=display_name,
            operation="remove_service",
            zone=zone,
            detail_name=display_name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Port writes
    # ------------------------------------------------------------------

    def add_port(
        self,
        port: int,
        protocol: str,
        zone: str,
    ) -> WindowsFirewallActionResult:
        """
            Add an inbound allow rule for the given port / protocol.

            :param port: TCP/UDP port number.
            :param protocol: ``"tcp"`` or ``"udp"``.
            :param zone: The Windows Firewall profile.

            :returns: The :class:`WindowsFirewallActionResult`.
        """
        zone_failure = self._maybe_reject_zone(
            zone=zone, service_or_port="{}/{}".format(port, protocol),
        )
        if zone_failure is not None:
            return zone_failure

        if not _validate_protocol(protocol=protocol):
            failed = self._build_result(
                status=WindowsFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=None,
                command=[],
                message="invalid_protocol",
            )
            return failed

        normalised_protocol = protocol.lower()
        display_name = _port_display_name(
            port=port, protocol=normalised_protocol,
        )
        script = self._render_new_netfirewallrule(
            display_name=display_name,
            protocol=normalised_protocol,
            local_port=port,
            zone=zone,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=display_name,
                command=command,
            )
            return dry

        if self._rule_exists(display_name=display_name):
            skipped = self._build_result(
                status=WindowsFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=display_name,
                command=command,
                message="rule already exists",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=display_name,
            operation="add_port",
            zone=zone,
            detail_name=display_name,
        )
        return rtnval

    def remove_port(
        self,
        port: int,
        protocol: str,
        zone: str,
    ) -> WindowsFirewallActionResult:
        """
            Remove the inbound allow rule for the given port / protocol.

            :param port: TCP/UDP port number.
            :param protocol: ``"tcp"`` or ``"udp"``.
            :param zone: The zone targeted (echoed onto the result).

            :returns: The :class:`WindowsFirewallActionResult`.
        """
        zone_failure = self._maybe_reject_zone(
            zone=zone, service_or_port="{}/{}".format(port, protocol),
        )
        if zone_failure is not None:
            return zone_failure

        if not _validate_protocol(protocol=protocol):
            failed = self._build_result(
                status=WindowsFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=None,
                command=[],
                message="invalid_protocol",
            )
            return failed

        normalised_protocol = protocol.lower()
        display_name = _port_display_name(
            port=port, protocol=normalised_protocol,
        )
        script = self._render_remove_netfirewallrule(
            display_name=display_name,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=display_name,
                command=command,
            )
            return dry

        if not self._rule_exists(display_name=display_name):
            skipped = self._build_result(
                status=WindowsFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=display_name,
                command=command,
                message="rule does not exist",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=display_name,
            operation="remove_port",
            zone=zone,
            detail_name=display_name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Rich-rule writes
    # ------------------------------------------------------------------

    def add_rich_rule(
        self,
        rule: str,
        zone: str,
    ) -> WindowsFirewallActionResult:
        """
            Add a rich rule.

            ``rule`` is a PowerShell splatted-parameters string passed
            verbatim to ``New-NetFirewallRule`` (e.g.
            ``"-DisplayName 'block-bad' -Direction Inbound -Action
            Block -Protocol TCP -LocalPort 8080
            -RemoteAddress 10.0.0.0/8"``).  The rule MUST contain a
            ``-DisplayName`` clause -- otherwise the manager has no
            stable identifier for idempotency or removal, so the call
            returns ``FAILED``.  Honours dry-run.

            :param rule: The rich-rule parameter string.
            :param zone: The Windows Firewall profile.

            :returns: The :class:`WindowsFirewallActionResult`.
        """
        zone_failure = self._maybe_reject_zone(
            zone=zone, service_or_port=None,
        )
        if zone_failure is not None:
            return zone_failure

        display_name = _extract_display_name(rule=rule)
        if display_name is None or len(display_name) == 0:
            failed = self._build_result(
                status=WindowsFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=None,
                command=[],
                message="display_name_required",
            )
            return failed

        # Avoid emitting a duplicate ``-Profile`` token if the caller
        # already supplied one inside ``rule``.
        if "-Profile" in rule:
            script = "New-NetFirewallRule {}".format(rule)
        else:
            script = "New-NetFirewallRule {} -Profile {}".format(
                rule, zone,
            )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=display_name,
                command=command,
            )
            return dry

        if self._rule_exists(display_name=display_name):
            skipped = self._build_result(
                status=WindowsFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=display_name,
                command=command,
                message="rule already exists",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=display_name,
            operation="add_rich_rule",
            zone=zone,
            detail_name=display_name,
        )
        return rtnval

    def remove_rich_rule(
        self,
        rule: str,
        zone: str,
    ) -> WindowsFirewallActionResult:
        """
            Remove a rich rule by extracting its ``-DisplayName`` and
            dispatching ``Remove-NetFirewallRule -DisplayName '<name>'``.

            :param rule: The rich-rule parameter string.
            :param zone: The zone targeted (echoed onto the result).

            :returns: The :class:`WindowsFirewallActionResult`.
        """
        zone_failure = self._maybe_reject_zone(
            zone=zone, service_or_port=None,
        )
        if zone_failure is not None:
            return zone_failure

        display_name = _extract_display_name(rule=rule)
        if display_name is None or len(display_name) == 0:
            failed = self._build_result(
                status=WindowsFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=None,
                command=[],
                message="display_name_required",
            )
            return failed

        script = self._render_remove_netfirewallrule(
            display_name=display_name,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=display_name,
                command=command,
            )
            return dry

        if not self._rule_exists(display_name=display_name):
            skipped = self._build_result(
                status=WindowsFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=display_name,
                command=command,
                message="rule does not exist",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=display_name,
            operation="remove_rich_rule",
            zone=zone,
            detail_name=display_name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Default-zone / reload
    # ------------------------------------------------------------------

    def set_default_zone(
        self,
        zone: str,
    ) -> WindowsFirewallActionResult:
        """
            Set the default zone.

            Windows has no first-class "default profile" concept --
            profiles activate based on the network category that the
            OS assigns to each connection.  This method therefore
            validates ``zone`` and returns a typed ``SKIPPED`` (for
            valid zones) or ``FAILED`` (for invalid zones) without
            invoking the runner.  Honours dry-run.

            :param zone: The Windows Firewall profile.

            :returns: The :class:`WindowsFirewallActionResult`.
        """
        if not _validate_zone(zone=zone):
            failed = self._build_result(
                status=WindowsFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=None,
                command=[],
                message="invalid_zone",
            )
            return failed

        # Synthesise a placeholder argv so the result still records
        # what the caller's intent was; the runner is never invoked.
        script = (
            "# set_default_zone is not supported on Windows; "
            "profiles activate from NetworkCategory"
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=None,
                command=command,
            )
            return dry

        message = (
            "Windows Firewall profiles activate based on the OS-"
            "assigned NetworkCategory of each connection; there is "
            "no default-profile concept to set."
        )
        rtnval = self._build_result(
            status=WindowsFirewallActionStatus.SKIPPED,
            zone=zone,
            detail_name=None,
            command=command,
            message=message,
        )
        return rtnval

    def reload(self) -> WindowsFirewallActionResult:
        """
            Reload the firewall ruleset.

            Windows Firewall applies rule changes immediately when the
            underlying rule object commits; no reload is required.
            This method returns ``SUCCESS`` with a descriptive
            ``message`` and does NOT invoke the runner.  Honours
            dry-run.

            :returns: The :class:`WindowsFirewallActionResult`.
        """
        script = "# reload is a no-op on Windows Firewall"
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsFirewallActionStatus.DRY_RUN,
                zone=None,
                detail_name=None,
                command=command,
            )
            return dry

        message = (
            "Windows Firewall applies rule changes immediately; no "
            "reload required."
        )
        rtnval = self._build_result(
            status=WindowsFirewallActionStatus.SUCCESS,
            zone=None,
            detail_name=None,
            command=command,
            message=message,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def query_zone(self, zone: str) -> WindowsZoneInfo:
        """
            List rules associated with ``zone``.

            Runs ``Get-NetFirewallRule | Where-Object Profile`` and
            classifies each rule by display-name prefix.  Reads do
            not elevate.

            :param zone: The Windows Firewall profile.

            :returns: The :class:`WindowsZoneInfo`.
            :raises WindowsFirewallQueryError: When the cmdlet fails
                                               or the JSON payload
                                               cannot be parsed.
        """
        if not _validate_zone(zone=zone):
            err_msg_lines = ["Invalid zone for query."]
            err_msg_lines.append("    REQUESTED: {!r}".format(zone))
            err_msg_lines.append("    KNOWN: {}".format(
                ", ".join(_VALID_ZONES),
            ))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsFirewallQueryError(message=err_msg)

        script = (
            "Get-NetFirewallRule "
            "| Select-Object DisplayName, Profile, Enabled, Direction, "
            "Action "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsFirewallQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = self._parse_rules_json(text=run_result.stdout or "")

        services: list = []
        ports: list = []
        rich_rules: list = []
        for record in records:
            display_name_raw = record.get("DisplayName")
            if display_name_raw is None:
                continue
            display_name = str(display_name_raw)
            if len(display_name) == 0:
                continue

            zones = _profile_field_to_zones(
                raw_value=record.get("Profile"),
            )
            if zone not in zones and "Any" not in zones:
                continue

            _classify_rule(
                display_name=display_name,
                services=services,
                ports=ports,
                rich_rules=rich_rules,
            )

        rtnval = WindowsZoneInfo(
            zone=zone,
            services=services,
            ports=ports,
            rich_rules=rich_rules,
            interfaces=[],
            sources=[],
            is_default=False,
        )
        return rtnval

    def query_all_zones(self) -> list:
        """
            Return one :class:`WindowsZoneInfo` per Windows Firewall
            profile (``Domain``, ``Private``, ``Public``).

            :returns: A list of :class:`WindowsZoneInfo`.
        """
        rtnval: list = []
        for zone in _QUERIED_ZONES:
            info = self.query_zone(zone=zone)
            rtnval.append(info)
        return rtnval

    def query_state(self) -> WindowsFirewallState:
        """
            Report overall firewall state.

            Runs ``Get-NetFirewallProfile | ConvertTo-Json`` to
            determine ``running`` (True iff every profile's
            ``Enabled`` field is True), and
            ``Get-NetConnectionProfile | Select -ExpandProperty
            NetworkCategory`` to determine the active network's
            category (mapped onto a profile name for ``default_zone``).

            :returns: The :class:`WindowsFirewallState`.
            :raises WindowsFirewallQueryError: When the firewall-
                                               profile read fails.
        """
        profile_script = (
            "Get-NetFirewallProfile "
            "| Select-Object Name, Enabled "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        profile_command = self._build_powershell_argv(
            script=profile_script,
        )
        profile_result = self._runner.run(
            list(profile_command), elevated=False,
        )
        if profile_result.exit_code != 0:
            raise WindowsFirewallQueryError(
                command=profile_command,
                exit_code=profile_result.exit_code,
                stderr=profile_result.stderr,
            )

        records = self._parse_rules_json(
            text=profile_result.stdout or "",
        )
        if len(records) == 0:
            running_flag = False
        else:
            running_flag = True
            for record in records:
                enabled_raw = record.get("Enabled")
                if enabled_raw is True:
                    continue
                if enabled_raw is False:
                    running_flag = False
                    break
                # ``Enabled`` may arrive as a string token or an int
                # depending on PowerShell version.
                if isinstance(enabled_raw, str):
                    if enabled_raw.strip().lower() == "true":
                        continue
                    running_flag = False
                    break
                if isinstance(enabled_raw, int):
                    if enabled_raw != 0:
                        continue
                    running_flag = False
                    break
                running_flag = False
                break

        default_zone = self._read_active_network_category()

        rtnval = WindowsFirewallState(
            running=running_flag,
            default_zone=default_zone,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsFirewallManagerConfig:
        """
            Return the manager's config typed.

            :returns: The :class:`WindowsFirewallManagerConfig`.
        """
        if isinstance(self._config, WindowsFirewallManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsFirewallManagerConfig(
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

    def _resolve_services_table(self) -> dict:
        """
            Return the active services-to-(port, protocol) table.

            :returns: The bundled table when no override is set;
                      otherwise the override.
        """
        override = self._typed_config().services_table
        if override is None:
            rtnval = _BUNDLED_SERVICES_TABLE
            return rtnval
        rtnval = override
        return rtnval

    def _maybe_reject_zone(
        self,
        *,
        zone: str,
        service_or_port: Optional[str],
    ) -> Optional[WindowsFirewallActionResult]:
        """
            Short-circuit invalid-zone writes with a typed FAILED
            result.

            :param zone: The zone to test.
            :param service_or_port: Service name / port token used
                                    for the result's ``detail`` field.

            :returns: A FAILED result when ``zone`` is invalid;
                      otherwise None.
        """
        if _validate_zone(zone=zone):
            return None

        rtnval = self._build_result(
            status=WindowsFirewallActionStatus.FAILED,
            zone=zone,
            detail_name=service_or_port,
            command=[],
            message="invalid_zone",
        )
        return rtnval

    def _render_new_netfirewallrule(
        self,
        *,
        display_name: str,
        protocol: str,
        local_port: int,
        zone: str,
    ) -> str:
        """
            Render the ``New-NetFirewallRule`` scriptblock for service
            and port writes.

            :param display_name: The rule's display name.
            :param protocol: ``"tcp"`` or ``"udp"``.
            :param local_port: Port number.
            :param zone: Profile selector.

            :returns: The PowerShell scriptblock.
        """
        escaped_name = _ps_escape_single_quoted(value=display_name)
        rtnval = (
            "New-NetFirewallRule -DisplayName '{name}' "
            "-Direction Inbound -Action Allow "
            "-Protocol {proto} -LocalPort {port} "
            "-Profile {zone}"
        ).format(
            name=escaped_name,
            proto=protocol.upper(),
            port=int(local_port),
            zone=zone,
        )
        return rtnval

    def _render_remove_netfirewallrule(
        self,
        *,
        display_name: str,
    ) -> str:
        """
            Render the ``Remove-NetFirewallRule`` scriptblock.

            :param display_name: Display name to match.

            :returns: The PowerShell scriptblock.
        """
        escaped = _ps_escape_single_quoted(value=display_name)
        rtnval = (
            "Remove-NetFirewallRule -DisplayName '{}'"
        ).format(escaped)
        return rtnval

    def _rule_exists(self, *, display_name: str) -> bool:
        """
            Check whether a rule with ``display_name`` is currently
            defined.

            Runs ``Get-NetFirewallRule -DisplayName <name>
            -ErrorAction SilentlyContinue | Select-Object DisplayName |
            ConvertTo-Json -Compress -Depth 5``.  Reads do not
            elevate.

            :param display_name: The display name to probe.

            :returns: True when the rule exists.
        """
        escaped = _ps_escape_single_quoted(value=display_name)
        script = (
            "Get-NetFirewallRule -DisplayName '{}' "
            "-ErrorAction SilentlyContinue "
            "| Select-Object DisplayName "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        stripped = (run_result.stdout or "").strip()
        if len(stripped) == 0:
            rtnval = False
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            rtnval = False
            return rtnval

        records = _normalize_to_list(parsed=parsed)
        for record in records:
            if not isinstance(record, dict):
                continue
            existing_raw = record.get("DisplayName")
            if existing_raw is None:
                continue
            if str(existing_raw) == display_name:
                rtnval = True
                return rtnval

        rtnval = False
        return rtnval

    def _parse_rules_json(self, *, text: str) -> list:
        """
            Parse a ``Get-NetFirewallRule`` / ``Get-NetFirewallProfile``
            JSON payload.

            :param text: Raw stdout text.

            :returns: A list of dicts.  Empty list on parse failure.
        """
        stripped = (text or "").strip()
        if len(stripped) == 0:
            rtnval: list = []
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            rtnval = []
            return rtnval

        rtnval = _normalize_to_list(parsed=parsed)
        return rtnval

    def _read_active_network_category(self) -> str:
        """
            Read the active network's ``NetworkCategory`` and map it
            onto a profile name.

            ``Get-NetConnectionProfile`` may return zero, one, or
            multiple records (multi-homed hosts).  This helper picks
            the first record's category and returns it as the profile
            name.

            :returns: The mapped profile name, or empty string when
                      no active connection is reported.
        """
        script = (
            "Get-NetConnectionProfile "
            "| Select-Object NetworkCategory "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval

        records = self._parse_rules_json(text=run_result.stdout or "")
        if len(records) == 0:
            rtnval = ""
            return rtnval

        first = records[0]
        if not isinstance(first, dict):
            rtnval = ""
            return rtnval

        category_raw = first.get("NetworkCategory")
        if category_raw is None:
            rtnval = ""
            return rtnval

        if isinstance(category_raw, int):
            # ``NetworkCategory`` enum: 0=Public, 1=Private,
            # 2=DomainAuthenticated.
            int_map = {
                0: "Public",
                1: "Private",
                2: "Domain",
            }
            rtnval = int_map.get(category_raw, "")
            return rtnval

        category_text = str(category_raw)
        if category_text == "DomainAuthenticated":
            rtnval = "Domain"
            return rtnval

        if category_text in _VALID_ZONES:
            rtnval = category_text
            return rtnval

        rtnval = category_text
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsFirewallActionStatus,
        zone: Optional[str],
        detail_name: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsFirewallActionResult:
        """
            Construct a :class:`WindowsFirewallActionResult` without
            invoking the runner.

            :param status: Outcome status.
            :param zone: Zone targeted.
            :param detail_name: Rule display name (or service / port
                                token) for the action.
            :param command: Would-have-been argv (copied for capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr.

            :returns: The :class:`WindowsFirewallActionResult`.
        """
        rtnval = WindowsFirewallActionResult(
            status=status,
            detail=detail_name,
            command=list(command),
            stderr=stderr,
            zone=zone,
            detail_name=detail_name,
            message=message,
        )
        return rtnval
