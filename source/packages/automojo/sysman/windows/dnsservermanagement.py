"""
    Windows dns-server-management feature.

    Implements the cross-OS dns-server-management contract for
    Windows using the PowerShell ``DnsServer`` module.  Every
    verb is dispatched through the injected
    :class:`ICommandRunner` so the manager works identically
    under local and SSH execution.

    DnsServer module requirement:
        The ``DnsServer`` PowerShell module ships with the
        Windows Server "DNS Server" role.  It is NOT available
        on Windows 10 / 11 client SKUs by default — operators
        on client SKUs can install the Remote Server
        Administration Tools (RSAT) and target a remote DNS
        server, but the locally-dispatched cmdlets in this
        module will fail with ``ParameterBindingValidationException``
        / ``CommandNotFoundException`` when the ``DnsServer``
        module is missing.

        :class:`WindowsDnsManagerConfig` carries an advisory
        ``requires_dns_server_role`` field documenting this
        requirement.  The manager itself does not probe for
        the role; the post-write cmdlet failure surfaces the
        misconfiguration as a ``FAILED`` result with the
        cmdlet's stderr captured verbatim.

    Verb / tool mapping:

    +---------------------+-----------------------------------------------+
    | Verb                | Windows tool                                  |
    +=====================+===============================================+
    | ``add_zone``        | ``Add-DnsServerPrimaryZone -Name '<zone>'``   |
    |                     | ``-ZoneFile '<zone>.dns'``                    |
    |                     | (or ``-ReplicationScope`` for AD-integrated). |
    +---------------------+-----------------------------------------------+
    | ``remove_zone``     | ``Remove-DnsServerZone -Name '<zone>' -Force``|
    +---------------------+-----------------------------------------------+
    | ``list_zones``      | ``Get-DnsServerZone | Select Name,ZoneType,   |
    |                     | ZoneFile,DynamicUpdate,IsDsIntegrated         |
    |                     | | ConvertTo-Json -Compress -Depth 5``         |
    +---------------------+-----------------------------------------------+
    | ``add_record``      | ``Add-DnsServerResourceRecordA -Name '<host>' |
    |                     | -ZoneName '<zone>' -IPv4Address '<ip>'`` (or  |
    |                     | ``-AAAA``/``-CName``/``-MX``/``-TXT`` etc.).  |
    +---------------------+-----------------------------------------------+
    | ``remove_record``   | ``Remove-DnsServerResourceRecord              |
    |                     | -ZoneName '<zone>' -Name '<host>'             |
    |                     | -RRType '<type>' -Force``.                    |
    +---------------------+-----------------------------------------------+
    | ``list_records``    | ``Get-DnsServerResourceRecord                 |
    |                     | -ZoneName '<zone>'                            |
    |                     | | ConvertTo-Json -Compress -Depth 5``.        |
    +---------------------+-----------------------------------------------+
    | ``reload_zone``     | ``Sync-DnsServerZone -Name '<zone>'``.        |
    +---------------------+-----------------------------------------------+
    | ``get_zone_status`` | ``Get-DnsServerZone -Name '<zone>'            |
    |                     | | ConvertTo-Json -Compress -Depth 5``.        |
    +---------------------+-----------------------------------------------+

    Record-type dispatch:
        :meth:`WindowsDnsServerManager.add_record` selects the
        cmdlet from the spec's ``record_type``:

            ``A``     → ``Add-DnsServerResourceRecordA``
            ``AAAA``  → ``Add-DnsServerResourceRecordAAAA``
            ``CNAME`` → ``Add-DnsServerResourceRecordCName``
            ``MX``    → ``Add-DnsServerResourceRecordMX``
            ``TXT``   → ``Add-DnsServerResourceRecord -Txt``
            ``SRV``   → ``Add-DnsServerResourceRecord -Srv``
            ``PTR``   → ``Add-DnsServerResourceRecordPtr``
            ``NS``    → ``Add-DnsServerResourceRecord -NS``

        Each cmdlet takes type-specific named parameters
        (``-IPv4Address``, ``-IPv6Address``, ``-HostNameAlias``,
        ``-MailExchange`` / ``-Preference``, ``-DescriptiveText``,
        ``-DomainName``, ``-PtrDomainName``).  See
        :func:`_build_add_record_command` for the full
        dispatch table.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so that ``$PROFILE`` cannot
        mutate cmdlet behaviour and PowerShell never blocks
        waiting for interactive input.

    Idempotency:
        * ``add_zone``: probes ``Get-DnsServerZone -Name <name>``
          first; returns ``SKIPPED`` when the zone is already
          registered.
        * ``remove_zone``: returns ``SKIPPED`` when
          ``Get-DnsServerZone -Name <name>`` reports the zone
          is absent.
        * ``add_record``: lists the existing records via
          ``Get-DnsServerResourceRecord`` and skips when an
          identical record (name, type, value) already
          exists.
        * ``remove_record``: skips when no matching record is
          present.
        * ``reload_zone`` and ``get_zone_status`` never skip.

    Elevation policy:
        Write verbs (``add_zone``, ``remove_zone``,
        ``add_record``, ``remove_record``, ``reload_zone``)
        honour ``config.elevated``.  Reads (``list_zones``,
        ``list_records``, ``get_zone_status``) never elevate.
"""

import json as _json
import os

from dataclasses import dataclass
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

_VALID_RECORD_TYPES = (
    "A", "AAAA", "CNAME", "MX", "TXT", "SRV", "PTR", "NS", "SOA",
)


WindowsDnsActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class WindowsDnsManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsDnsServerManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.  Set
                                      to ``"pwsh.exe"`` to
                                      target PowerShell 7+.
        :param requires_dns_server_role: Advisory boolean; the
                                         manager does NOT probe
                                         for the role.  When
                                         ``True``, callers
                                         acknowledge that the
                                         host has the Windows
                                         Server "DNS Server"
                                         role installed and
                                         that the ``DnsServer``
                                         PowerShell module is
                                         available.  Defaults
                                         to ``True``.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    requires_dns_server_role: bool = True


@dataclass
class WindowsDnsActionResult(FeatureActionResult):
    """
        Result of a :class:`WindowsDnsServerManager` write
        verb.

        :param zone: The DNS zone the action targeted.
        :param record_name: The owner-name portion of the
                            record the action targeted, when
                            applicable.
        :param message: Optional human-readable detail for
                        ``SKIPPED`` / ``FAILED`` outcomes.
    """
    zone: Optional[str] = None
    record_name: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsDnsZoneSpec:
    """
        Declarative specification for a Windows DNS zone.

        :param name: The zone name.
        :param zone_type: Zone-kind token.  Defaults to
                          ``"primary"``.  Maps to
                          ``Add-DnsServerPrimaryZone`` for
                          ``"primary"``,
                          ``Add-DnsServerSecondaryZone`` for
                          ``"secondary"``,
                          ``Add-DnsServerStubZone`` for
                          ``"stub"``,
                          ``Add-DnsServerForwarderZone`` for
                          ``"forward"``.
        :param zone_file: Optional ZoneFile name (relative to
                          ``%SystemRoot%\\System32\\dns``).
                          When ``None``, the manager uses
                          ``<name>.dns``.
        :param allow_update: When ``True``, the manager passes
                             ``-DynamicUpdate Secure`` to the
                             ``Add-DnsServerPrimaryZone``
                             cmdlet.  Defaults to ``False``.
        :param replication_scope: When set (one of
                                  ``"Domain"``, ``"Forest"``,
                                  ``"Legacy"``,
                                  ``"Custom"``), the manager
                                  passes
                                  ``-ReplicationScope <scope>``
                                  rather than ``-ZoneFile``,
                                  producing an AD-integrated
                                  zone.
    """
    name: str
    zone_type: str = "primary"
    zone_file: Optional[str] = None
    allow_update: bool = False
    replication_scope: Optional[str] = None


@dataclass
class WindowsDnsZoneInfo:
    """
        One row from
        :meth:`WindowsDnsServerManager.list_zones`.

        :param name: The zone name.
        :param zone_type: Windows DNS zone type token (e.g.,
                          ``"Primary"``, ``"Secondary"``,
                          ``"Stub"``, ``"Forwarder"``).
        :param zone_file: ZoneFile name (relative to
                          ``%SystemRoot%\\System32\\dns``);
                          ``None`` for AD-integrated zones.
        :param record_count: Always ``None`` for the pilot —
                             ``Get-DnsServerZone`` does not
                             report a record count.  Callers
                             who need the count call
                             :meth:`list_records` and inspect
                             the list length.
        :param is_active: True when the zone is enabled (i.e.
                          ``IsAutoCreated == False`` AND the
                          zone is reported as paused).  For
                          the pilot every entry returned by
                          ``Get-DnsServerZone`` is reported as
                          active.
    """
    name: str
    zone_type: str
    zone_file: Optional[str]
    record_count: Optional[int]
    is_active: bool


@dataclass
class WindowsDnsRecordSpec:
    """
        Declarative specification for a single resource
        record.

        :param name: Owner-name token (e.g. ``"www"``,
                     ``"mail"``, or ``"@"`` for the apex).
        :param record_type: One of ``"A"``, ``"AAAA"``,
                            ``"CNAME"``, ``"MX"``, ``"TXT"``,
                            ``"SRV"``, ``"PTR"``, ``"NS"``,
                            or ``"SOA"``.  Selects the
                            ``Add-DnsServerResourceRecord*``
                            cmdlet variant.
        :param value: Right-hand side rdata.  The semantics
                      depend on ``record_type``:

                          A      → IPv4 address text
                          AAAA   → IPv6 address text
                          CNAME  → target FQDN
                          MX     → exchange FQDN
                          TXT    → descriptive text
                          SRV    → target FQDN (port goes
                                   into a SRV-specific helper
                                   that the pilot does NOT
                                   yet expose)
                          PTR    → target FQDN
                          NS     → nameserver FQDN
        :param ttl: Optional TTL in seconds.  When set, the
                    manager passes ``-TimeToLive
                    (New-TimeSpan -Seconds <ttl>)``.
        :param priority: Optional priority field for MX / SRV
                         records.  Maps to ``-Preference`` on
                         MX, ``-Priority`` on SRV.
    """
    name: str
    record_type: str
    value: str
    ttl: Optional[int] = None
    priority: Optional[int] = None


@dataclass
class WindowsDnsRecordInfo:
    """
        One row from
        :meth:`WindowsDnsServerManager.list_records`.

        :param name: Owner-name token (``"HostName"`` from
                     ``Get-DnsServerResourceRecord``).
        :param record_type: Record type token (``"RecordType"``
                            from
                            ``Get-DnsServerResourceRecord``).
        :param value: Rdata text rendered from the
                      ``RecordData`` blob.
        :param ttl: TTL in seconds parsed from ``TimeToLive``.
        :param priority: Priority for MX / SRV records when
                         present in the ``RecordData`` blob;
                         ``None`` otherwise.
        :param serial: Always ``None`` on Windows — Windows
                       does not expose a per-record serial.
                       Provided for cross-OS parity.
    """
    name: str
    record_type: str
    value: str
    ttl: Optional[int] = None
    priority: Optional[int] = None
    serial: Optional[int] = None


@dataclass
class WindowsDnsZoneStatus:
    """
        Result of
        :meth:`WindowsDnsServerManager.get_zone_status`.

        :param name: The zone the status describes.
        :param is_loaded: True when ``Get-DnsServerZone``
                          returns a record (the zone is
                          registered with the DNS server
                          service).
        :param serial: SOA serial parsed from the JSON output
                       when present; ``None`` otherwise.
        :param last_loaded: ``LastZoneUpdate`` field when
                            present.
        :param transferred_from: ``MasterServers`` field when
                                 present.
    """
    name: str
    is_loaded: bool
    serial: Optional[int] = None
    last_loaded: Optional[str] = None
    transferred_from: Optional[str] = None


class WindowsDnsQueryError(Exception):
    """
        Raised when a dns-server read fails for a reason
        other than the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["DNS server query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# WindowsDnsServerManager
# ----------------------------------------------------------------------


class WindowsDnsServerManager(BaseFeatureManager):
    """
        Windows DNS-server manager wrapping the PowerShell
        ``DnsServer`` module.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`.
    """

    result_class = WindowsDnsActionResult
    status_class = WindowsDnsActionStatus
    feature_name = "dns-server-management"

    def __init__(
        self,
        runner,
        config: Optional[WindowsDnsManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to
                           a fresh
                           :class:`WindowsDnsManagerConfig`.
        """
        if config is None:
            config = WindowsDnsManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_zone(
        self,
        name: str,
        spec: WindowsDnsZoneSpec,
    ) -> WindowsDnsActionResult:
        """
            Register a new DNS zone via
            ``Add-DnsServerPrimaryZone`` (or the
            corresponding cmdlet for ``zone_type``).

            Idempotent: returns ``SKIPPED`` when
            ``Get-DnsServerZone -Name <name>`` reports the
            zone already exists.

            :param name: The zone name.
            :param spec: The :class:`WindowsDnsZoneSpec`.

            :returns: The :class:`WindowsDnsActionResult`.
        """
        script = _build_add_zone_script(name=name, spec=spec)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsDnsActionStatus.DRY_RUN,
                zone=name,
                record_name=None,
                command=command,
            )
            return dry

        if self._zone_exists(name=name):
            skipped = self._build_result(
                status=WindowsDnsActionStatus.SKIPPED,
                zone=name,
                record_name=None,
                command=command,
                message="zone already registered",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=command,
            zone=name,
            record_name=None,
        )
        return rtnval

    def remove_zone(self, name: str) -> WindowsDnsActionResult:
        """
            Remove a DNS zone via
            ``Remove-DnsServerZone -Name '<name>' -Force``.

            Idempotent: returns ``SKIPPED`` when
            ``Get-DnsServerZone -Name <name>`` reports the
            zone is absent.

            :param name: The zone name.

            :returns: The :class:`WindowsDnsActionResult`.
        """
        script = (
            "Remove-DnsServerZone -Name '{name}' -Force"
        ).format(name=_ps_quote(value=name))
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsDnsActionStatus.DRY_RUN,
                zone=name,
                record_name=None,
                command=command,
            )
            return dry

        if not self._zone_exists(name=name):
            skipped = self._build_result(
                status=WindowsDnsActionStatus.SKIPPED,
                zone=name,
                record_name=None,
                command=command,
                message="zone already absent",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=command,
            zone=name,
            record_name=None,
        )
        return rtnval

    def add_record(
        self,
        zone: str,
        record: WindowsDnsRecordSpec,
    ) -> WindowsDnsActionResult:
        """
            Add a resource record by dispatching the
            type-specific
            ``Add-DnsServerResourceRecord*`` cmdlet.

            Idempotent: returns ``SKIPPED`` when an identical
            record (same name, type, value) is already
            present.

            :param zone: The zone name.
            :param record: The :class:`WindowsDnsRecordSpec`.

            :returns: The :class:`WindowsDnsActionResult`.
        """
        validation_failure = _validate_record(record=record)
        if validation_failure is not None:
            failed = self._build_result(
                status=WindowsDnsActionStatus.FAILED,
                zone=zone,
                record_name=record.name,
                command=self._build_powershell_argv(script=""),
                message=validation_failure,
                stderr=validation_failure,
            )
            return failed

        script = _build_add_record_command(
            zone=zone, record=record,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsDnsActionStatus.DRY_RUN,
                zone=zone,
                record_name=record.name,
                command=command,
            )
            return dry

        existing_records = self._safe_list_records(zone=zone)
        if _record_already_present(
            records=existing_records, record=record,
        ):
            skipped = self._build_result(
                status=WindowsDnsActionStatus.SKIPPED,
                zone=zone,
                record_name=record.name,
                command=command,
                message="record already present",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=command,
            zone=zone,
            record_name=record.name,
        )
        return rtnval

    def remove_record(
        self,
        zone: str,
        record: WindowsDnsRecordSpec,
    ) -> WindowsDnsActionResult:
        """
            Remove a resource record via
            ``Remove-DnsServerResourceRecord``.

            Idempotent: returns ``SKIPPED`` when no matching
            record is present.

            :param zone: The zone name.
            :param record: The :class:`WindowsDnsRecordSpec`.

            :returns: The :class:`WindowsDnsActionResult`.
        """
        validation_failure = _validate_record(record=record)
        if validation_failure is not None:
            failed = self._build_result(
                status=WindowsDnsActionStatus.FAILED,
                zone=zone,
                record_name=record.name,
                command=self._build_powershell_argv(script=""),
                message=validation_failure,
                stderr=validation_failure,
            )
            return failed

        script = _build_remove_record_command(
            zone=zone, record=record,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsDnsActionStatus.DRY_RUN,
                zone=zone,
                record_name=record.name,
                command=command,
            )
            return dry

        existing_records = self._safe_list_records(zone=zone)
        if not _record_already_present(
            records=existing_records, record=record,
        ):
            skipped = self._build_result(
                status=WindowsDnsActionStatus.SKIPPED,
                zone=zone,
                record_name=record.name,
                command=command,
                message="record not present",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=command,
            zone=zone,
            record_name=record.name,
        )
        return rtnval

    def reload_zone(self, name: str) -> WindowsDnsActionResult:
        """
            Dispatch ``Sync-DnsServerZone -Name '<name>'``
            unconditionally.

            Honours dry-run and elevation; never skips.

            :param name: The zone name.

            :returns: The :class:`WindowsDnsActionResult`.
        """
        script = (
            "Sync-DnsServerZone -Name '{name}'"
        ).format(name=_ps_quote(value=name))
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsDnsActionStatus.DRY_RUN,
                zone=name,
                record_name=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=command,
            zone=name,
            record_name=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_zones(self) -> list:
        """
            Enumerate every DNS zone via
            ``Get-DnsServerZone | ConvertTo-Json``.

            :returns: One :class:`WindowsDnsZoneInfo` per zone.

            :raises WindowsDnsQueryError: When PowerShell
                                          exits non-zero with
                                          a non-empty stderr.
        """
        script = (
            "Get-DnsServerZone "
            "| Select Name,ZoneType,ZoneFile,DynamicUpdate,IsDsIntegrated "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) > 0:
                raise WindowsDnsQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval: list = []
            return rtnval

        rtnval = _parse_zones_json(text=run_result.stdout)
        return rtnval

    def list_records(self, zone: str) -> list:
        """
            Enumerate the resource records in a zone via
            ``Get-DnsServerResourceRecord | ConvertTo-Json``.

            :param zone: The zone name.

            :returns: One :class:`WindowsDnsRecordInfo` per
                      record.

            :raises WindowsDnsQueryError: When PowerShell
                                          exits non-zero with
                                          a non-empty stderr.
        """
        script = (
            "Get-DnsServerResourceRecord -ZoneName '{zone}' "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(zone=_ps_quote(value=zone))
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) > 0:
                raise WindowsDnsQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval: list = []
            return rtnval

        rtnval = _parse_records_json(text=run_result.stdout)
        return rtnval

    def get_zone_status(self, name: str) -> WindowsDnsZoneStatus:
        """
            Query a zone's runtime status via
            ``Get-DnsServerZone -Name '<name>' | ConvertTo-Json``.

            :param name: The zone name.

            :returns: The :class:`WindowsDnsZoneStatus`.

            :raises WindowsDnsQueryError: When PowerShell
                                          exits non-zero with
                                          a non-empty stderr.
        """
        script = (
            "Get-DnsServerZone -Name '{name}' "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(name=_ps_quote(value=name))
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) > 0:
                raise WindowsDnsQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval = WindowsDnsZoneStatus(
                name=name,
                is_loaded=False,
                serial=None,
                last_loaded=None,
                transferred_from=None,
            )
            return rtnval

        parsed = _parse_zone_status_json(text=run_result.stdout)
        rtnval = WindowsDnsZoneStatus(
            name=name,
            is_loaded=parsed["is_loaded"],
            serial=parsed.get("serial"),
            last_loaded=parsed.get("last_loaded"),
            transferred_from=parsed.get("transferred_from"),
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsDnsManagerConfig:
        """
            Return the manager's config typed.

            :returns: The :class:`WindowsDnsManagerConfig`.
        """
        if isinstance(self._config, WindowsDnsManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsDnsManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical PowerShell argv for a
            scriptblock.

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

    def _zone_exists(self, *, name: str) -> bool:
        """
            Probe whether ``Get-DnsServerZone -Name <name>``
            reports the zone is registered.

            :param name: The zone name.

            :returns: True when the cmdlet exits zero.
        """
        script = (
            "Get-DnsServerZone -Name '{name}' "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(name=_ps_quote(value=name))
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        if rtnval:
            stripped = run_result.stdout.strip()
            if len(stripped) == 0:
                rtnval = False
        return rtnval

    def _safe_list_records(self, *, zone: str) -> list:
        """
            Return the runtime record list, swallowing
            :class:`WindowsDnsQueryError` so write paths do
            not propagate read failures.

            :param zone: The zone name.

            :returns: A list of
                      :class:`WindowsDnsRecordInfo`; empty
                      when the read failed.
        """
        try:
            rtnval = self.list_records(zone=zone)
        except WindowsDnsQueryError:
            rtnval = []
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsDnsActionStatus,
        zone: Optional[str],
        record_name: Optional[str],
        command: list,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsDnsActionResult:
        """
            Construct a :class:`WindowsDnsActionResult`
            without invoking the runner.
        """
        rtnval = WindowsDnsActionResult(
            status=status,
            detail=zone,
            command=list(command),
            zone=zone,
            record_name=record_name,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _wrap_run_result(
        self,
        *,
        run_result,
        command: list,
        zone: Optional[str],
        record_name: Optional[str],
    ) -> WindowsDnsActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`WindowsDnsActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = WindowsDnsActionStatus.SUCCESS
        else:
            mapped_status = WindowsDnsActionStatus.FAILED

        rtnval = WindowsDnsActionResult(
            status=mapped_status,
            detail=zone,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            zone=zone,
            record_name=record_name,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PowerShell escaping
# ----------------------------------------------------------------------


def _ps_quote(*, value: str) -> str:
    """
        Escape ``value`` for inclusion inside PowerShell
        single-quoted strings.

        PowerShell single-quoted strings escape an embedded
        single quote by doubling it.  Other characters do not
        require escaping inside the single-quoted form.

        :param value: The raw text.

        :returns: The escaped text (without surrounding
                  quotes).
    """
    if value is None:
        rtnval = ""
        return rtnval
    rtnval = value.replace("'", "''")
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — record validation and dispatch
# ----------------------------------------------------------------------


def _validate_record(*, record: WindowsDnsRecordSpec) -> Optional[str]:
    """
        Validate a record spec.

        :param record: The :class:`WindowsDnsRecordSpec`.

        :returns: ``None`` when valid; an error string when
                  not.
    """
    if record.name is None or len(record.name) == 0:
        rtnval: Optional[str] = "record.name is required"
        return rtnval
    if record.record_type is None or len(record.record_type) == 0:
        rtnval = "record.record_type is required"
        return rtnval
    if record.record_type not in _VALID_RECORD_TYPES:
        rtnval = "record.record_type must be one of {}".format(
            list(_VALID_RECORD_TYPES),
        )
        return rtnval
    if record.value is None or len(record.value) == 0:
        rtnval = "record.value is required"
        return rtnval
    rtnval = None
    return rtnval


def _build_add_zone_script(
    *,
    name: str,
    spec: WindowsDnsZoneSpec,
) -> str:
    """
        Render the ``Add-DnsServer*Zone`` scriptblock.

        :param name: The zone name.
        :param spec: The :class:`WindowsDnsZoneSpec`.

        :returns: The scriptblock string.
    """
    name_token = _ps_quote(value=name)
    if spec.zone_type == "secondary":
        cmdlet = "Add-DnsServerSecondaryZone"
    elif spec.zone_type == "stub":
        cmdlet = "Add-DnsServerStubZone"
    elif spec.zone_type == "forward":
        cmdlet = "Add-DnsServerConditionalForwarderZone"
    else:
        cmdlet = "Add-DnsServerPrimaryZone"

    parts: list = []
    parts.append(cmdlet)
    parts.append("-Name '{}'".format(name_token))

    if (
        spec.replication_scope is not None
        and len(spec.replication_scope) > 0
    ):
        scope_token = _ps_quote(value=spec.replication_scope)
        parts.append("-ReplicationScope '{}'".format(scope_token))
    else:
        if spec.zone_file is not None and len(spec.zone_file) > 0:
            zone_file_token = _ps_quote(value=spec.zone_file)
        else:
            zone_file_token = "{}.dns".format(name_token)
        parts.append("-ZoneFile '{}'".format(zone_file_token))

    if spec.allow_update and cmdlet == "Add-DnsServerPrimaryZone":
        parts.append("-DynamicUpdate Secure")

    rtnval = " ".join(parts)
    return rtnval


def _build_add_record_command(
    *,
    zone: str,
    record: WindowsDnsRecordSpec,
) -> str:
    """
        Render the ``Add-DnsServerResourceRecord*`` scriptblock
        for ``record``.

        :param zone: The zone name.
        :param record: The :class:`WindowsDnsRecordSpec`.

        :returns: The scriptblock string.
    """
    zone_token = _ps_quote(value=zone)
    name_token = _ps_quote(value=record.name)
    value_token = _ps_quote(value=record.value)

    if record.record_type == "A":
        rtnval = (
            "Add-DnsServerResourceRecordA "
            "-ZoneName '{zone}' -Name '{name}' -IPv4Address '{value}'"
        ).format(zone=zone_token, name=name_token, value=value_token)
    elif record.record_type == "AAAA":
        rtnval = (
            "Add-DnsServerResourceRecordAAAA "
            "-ZoneName '{zone}' -Name '{name}' -IPv6Address '{value}'"
        ).format(zone=zone_token, name=name_token, value=value_token)
    elif record.record_type == "CNAME":
        rtnval = (
            "Add-DnsServerResourceRecordCName "
            "-ZoneName '{zone}' -Name '{name}' -HostNameAlias '{value}'"
        ).format(zone=zone_token, name=name_token, value=value_token)
    elif record.record_type == "MX":
        if record.priority is not None:
            preference_value = record.priority
        else:
            preference_value = 10
        rtnval = (
            "Add-DnsServerResourceRecordMX "
            "-ZoneName '{zone}' -Name '{name}' "
            "-MailExchange '{value}' -Preference {pref}"
        ).format(
            zone=zone_token,
            name=name_token,
            value=value_token,
            pref=int(preference_value),
        )
    elif record.record_type == "TXT":
        rtnval = (
            "Add-DnsServerResourceRecord -Txt "
            "-ZoneName '{zone}' -Name '{name}' "
            "-DescriptiveText '{value}'"
        ).format(zone=zone_token, name=name_token, value=value_token)
    elif record.record_type == "SRV":
        if record.priority is not None:
            priority_value = record.priority
        else:
            priority_value = 0
        rtnval = (
            "Add-DnsServerResourceRecord -Srv "
            "-ZoneName '{zone}' -Name '{name}' "
            "-DomainName '{value}' -Priority {pri} "
            "-Weight 0 -Port 0"
        ).format(
            zone=zone_token,
            name=name_token,
            value=value_token,
            pri=int(priority_value),
        )
    elif record.record_type == "PTR":
        rtnval = (
            "Add-DnsServerResourceRecordPtr "
            "-ZoneName '{zone}' -Name '{name}' "
            "-PtrDomainName '{value}'"
        ).format(zone=zone_token, name=name_token, value=value_token)
    elif record.record_type == "NS":
        rtnval = (
            "Add-DnsServerResourceRecord -NS "
            "-ZoneName '{zone}' -Name '{name}' -NameServer '{value}'"
        ).format(zone=zone_token, name=name_token, value=value_token)
    else:
        # Fallback: Generic add via the type-aware cmdlet.  Every
        # rdata token is passed as a positional argument; the
        # validator above keeps unsupported types out of this
        # branch.
        rtnval = (
            "Add-DnsServerResourceRecord "
            "-ZoneName '{zone}' -Name '{name}' "
            "-RRType '{rtype}' -RecordData '{value}'"
        ).format(
            zone=zone_token,
            name=name_token,
            rtype=record.record_type,
            value=value_token,
        )

    if record.ttl is not None:
        rtnval = rtnval + (
            " -TimeToLive (New-TimeSpan -Seconds {})"
        ).format(int(record.ttl))
    return rtnval


def _build_remove_record_command(
    *,
    zone: str,
    record: WindowsDnsRecordSpec,
) -> str:
    """
        Render the ``Remove-DnsServerResourceRecord``
        scriptblock for ``record``.

        :param zone: The zone name.
        :param record: The :class:`WindowsDnsRecordSpec`.

        :returns: The scriptblock string.
    """
    zone_token = _ps_quote(value=zone)
    name_token = _ps_quote(value=record.name)
    rrtype_token = _ps_quote(value=record.record_type)

    rtnval = (
        "Remove-DnsServerResourceRecord "
        "-ZoneName '{zone}' -Name '{name}' "
        "-RRType '{rtype}' -Force"
    ).format(
        zone=zone_token,
        name=name_token,
        rtype=rrtype_token,
    )
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — record idempotency
# ----------------------------------------------------------------------


def _normalise_value(*, value: str) -> str:
    """
        Collapse trailing dots and whitespace inside a value
        token so that the idempotency probe matches Windows's
        canonical FQDN form.

        :param value: The raw value.

        :returns: The normalised value.
    """
    if value is None:
        rtnval = ""
        return rtnval
    parts = value.split()
    joined = " ".join(parts)
    rtnval = joined.rstrip(".")
    return rtnval


def _record_already_present(
    *,
    records: list,
    record: WindowsDnsRecordSpec,
) -> bool:
    """
        Probe whether ``records`` already contains ``record``.

        :param records: A list of
                        :class:`WindowsDnsRecordInfo`.
        :param record: The :class:`WindowsDnsRecordSpec`.

        :returns: True when an identical record is present.
    """
    target_value = _normalise_value(value=record.value)
    rtnval = False
    for existing in records:
        if existing.name != record.name:
            continue
        if existing.record_type != record.record_type:
            continue
        if _normalise_value(value=existing.value) != target_value:
            continue
        rtnval = True
        return rtnval
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — JSON parsers
# ----------------------------------------------------------------------


def _parse_zones_json(*, text: str) -> list:
    """
        Parse the JSON output of
        ``Get-DnsServerZone | ConvertTo-Json`` into a list of
        :class:`WindowsDnsZoneInfo`.

        :param text: The raw JSON stdout.

        :returns: A list of :class:`WindowsDnsZoneInfo`.
    """
    rtnval: list = []
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval

    try:
        decoded = _json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval

    if isinstance(decoded, dict):
        records = [decoded]
    elif isinstance(decoded, list):
        records = decoded
    else:
        return rtnval

    for record in records:
        if not isinstance(record, dict):
            continue
        name_value = record.get("Name", "")
        if not isinstance(name_value, str):
            name_value = str(name_value)
        zone_type_value = record.get("ZoneType", "")
        if not isinstance(zone_type_value, str):
            zone_type_value = str(zone_type_value)
        zone_file_value = record.get("ZoneFile")
        if zone_file_value is not None and not isinstance(
            zone_file_value, str,
        ):
            zone_file_value = str(zone_file_value)

        info = WindowsDnsZoneInfo(
            name=name_value,
            zone_type=zone_type_value,
            zone_file=zone_file_value,
            record_count=None,
            is_active=True,
        )
        rtnval.append(info)
    return rtnval


def _parse_records_json(*, text: str) -> list:
    """
        Parse the JSON output of
        ``Get-DnsServerResourceRecord | ConvertTo-Json``.

        :param text: The raw JSON stdout.

        :returns: A list of :class:`WindowsDnsRecordInfo`.
    """
    rtnval: list = []
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval

    try:
        decoded = _json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval

    if isinstance(decoded, dict):
        records = [decoded]
    elif isinstance(decoded, list):
        records = decoded
    else:
        return rtnval

    for record in records:
        if not isinstance(record, dict):
            continue

        host_name = record.get("HostName", "")
        if not isinstance(host_name, str):
            host_name = str(host_name)

        record_type = record.get("RecordType", "")
        if not isinstance(record_type, str):
            record_type = str(record_type)

        value_text = _extract_record_data_text(record=record)

        ttl_value = _extract_ttl_seconds(record=record)
        priority_value = _extract_priority(record=record)

        info = WindowsDnsRecordInfo(
            name=host_name,
            record_type=record_type,
            value=value_text,
            ttl=ttl_value,
            priority=priority_value,
            serial=None,
        )
        rtnval.append(info)
    return rtnval


def _extract_record_data_text(*, record: dict) -> str:
    """
        Render a record's rdata into a single text value.

        Windows ``Get-DnsServerResourceRecord`` returns a
        ``RecordData`` blob whose shape depends on the record
        type.  This helper picks the canonical field for each
        common type.

        :param record: The decoded JSON record dict.

        :returns: The rdata text.
    """
    record_data = record.get("RecordData")
    if record_data is None:
        rtnval = ""
        return rtnval
    if isinstance(record_data, str):
        rtnval = record_data
        return rtnval
    if not isinstance(record_data, dict):
        rtnval = str(record_data)
        return rtnval

    # Try the common rdata fields in priority order.
    candidate_keys = (
        "IPv4Address",
        "IPv6Address",
        "HostNameAlias",
        "MailExchange",
        "DescriptiveText",
        "DomainName",
        "PtrDomainName",
        "NameServer",
    )
    for key in candidate_keys:
        if key in record_data and record_data[key] is not None:
            value_obj = record_data[key]
            if isinstance(value_obj, str):
                rtnval = value_obj
                return rtnval
            if isinstance(value_obj, list) and len(value_obj) > 0:
                rtnval = str(value_obj[0])
                return rtnval
            rtnval = str(value_obj)
            return rtnval
    rtnval = ""
    return rtnval


def _extract_ttl_seconds(*, record: dict) -> Optional[int]:
    """
        Extract the TTL in seconds from a record's
        ``TimeToLive`` field.

        ``TimeToLive`` may be a JSON object with a
        ``TotalSeconds`` field (PowerShell ``TimeSpan``
        serialised) or a string like
        ``"00:01:00"`` / ``"3600"``.

        :param record: The decoded JSON record dict.

        :returns: The TTL in seconds when extractable;
                  ``None`` otherwise.
    """
    ttl_value = record.get("TimeToLive")
    if ttl_value is None:
        rtnval: Optional[int] = None
        return rtnval
    if isinstance(ttl_value, dict):
        for key in ("TotalSeconds", "Seconds"):
            if key in ttl_value:
                candidate = ttl_value[key]
                if isinstance(candidate, (int, float)):
                    rtnval = int(candidate)
                    return rtnval
                if isinstance(candidate, str):
                    try:
                        rtnval = int(float(candidate))
                        return rtnval
                    except ValueError:
                        continue
        rtnval = None
        return rtnval
    if isinstance(ttl_value, (int, float)):
        rtnval = int(ttl_value)
        return rtnval
    if isinstance(ttl_value, str):
        try:
            rtnval = int(float(ttl_value))
            return rtnval
        except ValueError:
            rtnval = None
            return rtnval
    rtnval = None
    return rtnval


def _extract_priority(*, record: dict) -> Optional[int]:
    """
        Extract the MX / SRV priority from a record.

        :param record: The decoded JSON record dict.

        :returns: The priority when extractable; ``None``
                  otherwise.
    """
    record_data = record.get("RecordData")
    if not isinstance(record_data, dict):
        rtnval: Optional[int] = None
        return rtnval
    for key in ("Preference", "Priority"):
        if key in record_data and record_data[key] is not None:
            candidate = record_data[key]
            if isinstance(candidate, (int, float)):
                rtnval = int(candidate)
                return rtnval
            if isinstance(candidate, str):
                try:
                    rtnval = int(candidate)
                    return rtnval
                except ValueError:
                    continue
    rtnval = None
    return rtnval


def _parse_zone_status_json(*, text: str) -> dict:
    """
        Parse the JSON output of
        ``Get-DnsServerZone -Name '<name>' | ConvertTo-Json``
        into a dict.

        :param text: The raw JSON stdout.

        :returns: A dict with keys ``is_loaded``, ``serial``,
                  ``last_loaded``, ``transferred_from``.
    """
    rtnval: dict = {
        "is_loaded": False,
        "serial": None,
        "last_loaded": None,
        "transferred_from": None,
    }
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval

    try:
        decoded = _json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval

    if isinstance(decoded, list):
        if len(decoded) == 0:
            return rtnval
        record = decoded[0]
    elif isinstance(decoded, dict):
        record = decoded
    else:
        return rtnval

    if not isinstance(record, dict):
        return rtnval

    rtnval["is_loaded"] = True

    serial_value = record.get("SerialNumber")
    if isinstance(serial_value, int):
        rtnval["serial"] = serial_value
    elif isinstance(serial_value, str):
        try:
            rtnval["serial"] = int(serial_value)
        except ValueError:
            rtnval["serial"] = None

    last_loaded_value = record.get("LastZoneUpdate")
    if isinstance(last_loaded_value, str):
        rtnval["last_loaded"] = last_loaded_value

    masters_value = record.get("MasterServers")
    if isinstance(masters_value, list) and len(masters_value) > 0:
        rtnval["transferred_from"] = ", ".join(
            str(token) for token in masters_value
        )
    elif isinstance(masters_value, str):
        rtnval["transferred_from"] = masters_value

    return rtnval
