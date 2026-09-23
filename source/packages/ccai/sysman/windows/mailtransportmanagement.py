"""
    Windows mail-transport-management feature.

    Implements the cross-OS mail-transport-management contract for
    Windows.  **Windows has no built-in MTA equivalent.**  After the
    deprecation of the IIS SMTP feature on consumer Windows SKUs,
    Microsoft's recommendation for outbound mail is a third-party
    relay (hMailServer, MailEnable) or an external SMTP gateway.
    This pilot is a **best-effort** implementation that documents the
    gap prominently, returns ``FAILED`` with a stable
    ``windows_no_built_in_mta`` detail string for every write when
    the host is configured as ``mta_kind=NONE`` (the default), and
    routes through ``Send-MailMessage`` and the IIS SMTP queue
    directory when the operator opts into ``mta_kind=IIS_SMTP``.

    Pilot scope:

    +-----------------------+----------------------------------------+
    | ``mta_kind``          | Coverage                               |
    +=======================+========================================+
    | ``NONE`` (default)    | Reads return synthetic empty           |
    |                       | shapes; writes return FAILED with      |
    |                       | ``detail="windows_no_built_in_mta"``.  |
    +-----------------------+----------------------------------------+
    | ``IIS_SMTP``          | Best-effort.  ``get_mta_config`` reads |
    |                       | via ``Get-WmiObject IIsSmtpServer``;   |
    |                       | ``test_mail_delivery`` uses            |
    |                       | ``Send-MailMessage``;                  |
    |                       | ``get_queue_status`` counts files      |
    |                       | under                                  |
    |                       | ``%SystemRoot%\\System32\\inetsrv\\``  |
    |                       | ``mailroot\\Queue\\``;                 |
    |                       | ``flush_queue`` does                   |
    |                       | ``Restart-Service SMTPSVC``.           |
    |                       | Aliases are not directly supported on  |
    |                       | IIS SMTP — alias verbs return FAILED.  |
    +-----------------------+----------------------------------------+
    | ``THIRD_PARTY``       | Placeholder.  Writes return            |
    |                       | FAILED with                            |
    |                       | ``detail="windows_third_party_mta``    |
    |                       | ``_not_implemented"``.  Reads return   |
    |                       | synthetic empty shapes.                |
    +-----------------------+----------------------------------------+

    Production guidance:
        Pilot is best-effort on Windows; full enterprise MTA
        management is **out of scope**.  Operators who require
        production MTA capability SHOULD run their MTA on Linux
        (Ubuntu / RHEL) or macOS where the Postfix command suite is
        available natively.  Windows hosts that need outbound mail
        should be configured to relay through a Linux / macOS MTA
        rather than running a local server.

    File I/O policy:
        Every command runs through the injected
        :class:`ICommandRunner`.  This module does NOT consume an
        :class:`IFileOperator`.

    Elevation policy:
        Every write verb (``set_mta_config``, ``add_alias``,
        ``remove_alias``, ``test_mail_delivery``, ``flush_queue``)
        honours ``config.elevated``.  Reads (``get_mta_config``,
        ``list_aliases``, ``get_queue_status``) never elevate.
"""

import os
import re

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ccai.sysman.common.featurebase import (
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

_DEFAULT_IIS_SMTP_QUEUE_DIR = (
    r"%SystemRoot%\System32\inetsrv\mailroot\Queue"
)

_DETAIL_NO_MTA = "windows_no_built_in_mta"
_DETAIL_THIRD_PARTY_NOT_IMPL = "windows_third_party_mta_not_implemented"
_DETAIL_IIS_SMTP_NO_ALIASES = (
    "windows_iis_smtp_aliases_unsupported"
)


WindowsMtaActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------


class WindowsMtaKind(Enum):
    """
        Discriminator for the Windows MTA flavour the manager should
        target.

        :cvar NONE: Default.  Windows has no built-in MTA installed.
                    Writes return FAILED; reads return synthetic
                    empty shapes.
        :cvar IIS_SMTP: Legacy IIS SMTP feature.  Available only on
                        Windows Server SKUs through the
                        ``Web-Common-Http`` -> ``SMTP-Server`` role
                        feature.  Best-effort coverage.
        :cvar THIRD_PARTY: Operator runs a third-party MTA
                           (hMailServer / MailEnable / etc.).
                           Pilot does NOT provide command suites for
                           any specific third-party server — this is
                           a placeholder for future extension.
    """
    NONE = "none"
    IIS_SMTP = "iis_smtp"
    THIRD_PARTY = "third_party"


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class WindowsMtaManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsMailTransportManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param mta_kind: The MTA flavour the manager should target.
                         Default is :data:`WindowsMtaKind.NONE`,
                         reflecting Windows' lack of a built-in MTA.
        :param iis_smtp_queue_dir: Path to the IIS SMTP queue
                                   directory used by
                                   :meth:`get_queue_status` when
                                   ``mta_kind=IIS_SMTP``.  Default
                                   is ``%SystemRoot%\\System32\\
                                   inetsrv\\mailroot\\Queue``.
        :param powershell_executable: PowerShell executable.
                                      Defaults to ``"powershell.exe"``.
        :param iis_smtp_service_name: Windows service name for the
                                      IIS SMTP daemon.  Defaults to
                                      ``"SMTPSVC"``.
        :param iis_smtp_localhost: Hostname / IP used by
                                   :meth:`test_mail_delivery` when
                                   ``mta_kind=IIS_SMTP``.  Defaults
                                   to ``"localhost"``.
    """
    mta_kind: WindowsMtaKind = WindowsMtaKind.NONE
    iis_smtp_queue_dir: str = _DEFAULT_IIS_SMTP_QUEUE_DIR
    powershell_executable: str = _DEFAULT_POWERSHELL
    iis_smtp_service_name: str = "SMTPSVC"
    iis_smtp_localhost: str = "localhost"


@dataclass
class WindowsMtaActionResult(FeatureActionResult):
    """
        Result of a mail-transport write operation.

        :param setting: The directive name targeted, when applicable.
        :param local: The local alias name targeted, when applicable.
    """
    setting: Optional[str] = None
    local: Optional[str] = None


@dataclass
class WindowsMtaConfig:
    """
        Synthetic Postfix-style directive snapshot for cross-OS
        contract conformance.

        On Windows there is no native concept of "Postfix main.cf
        directives" — IIS SMTP exposes a different schema entirely
        through WMI.  The manager populates the named fields
        best-effort from ``Get-WmiObject IIsSmtpServer`` when
        ``mta_kind=IIS_SMTP``; otherwise every named field is
        ``None`` and the ``mta_kind`` field discriminates.

        :param mta_kind: The active MTA flavour at read time.
        :param myhostname: Host name; populated from the
                           ``FullyQualifiedDomainName`` IIS SMTP
                           setting when available.
        :param mydomain: Always ``None`` on Windows — the IIS SMTP
                         schema has no direct equivalent.
        :param myorigin: Always ``None`` on Windows.
        :param relayhost: Populated from the
                          ``SmartHostType`` / ``SmartHost`` IIS SMTP
                          settings when available.
        :param mynetworks: Always ``None`` on Windows — IIS SMTP
                           uses a different IP-restriction surface.
        :param inet_interfaces: Always ``None`` on Windows.
        :param inet_protocols: Always ``None`` on Windows.
        :param mailbox_size_limit: Populated from the
                                   ``MaxMessageSize`` IIS SMTP
                                   setting when available.
        :param message_size_limit: Mirrors ``mailbox_size_limit``
                                   on Windows — IIS SMTP exposes a
                                   single ``MaxMessageSize``
                                   setting.
        :param extras: Every other IIS SMTP property surfaced by the
                       WMI probe.
        :param detail: Human-readable diagnostic explaining the
                       state — e.g.,
                       ``"Windows has no built-in MTA configured"``
                       when ``mta_kind=NONE``.
    """
    mta_kind: WindowsMtaKind = WindowsMtaKind.NONE
    myhostname: Optional[str] = None
    mydomain: Optional[str] = None
    myorigin: Optional[str] = None
    relayhost: Optional[str] = None
    mynetworks: Optional[str] = None
    inet_interfaces: Optional[str] = None
    inet_protocols: Optional[str] = None
    mailbox_size_limit: Optional[int] = None
    message_size_limit: Optional[int] = None
    extras: dict = field(default_factory=dict)
    detail: Optional[str] = None


@dataclass
class WindowsMtaAliasInfo:
    """
        A single aliases entry.  Always empty on Windows.

        :param local: The local alias.
        :param remote: The remote target list.
    """
    local: str
    remote: str


@dataclass
class WindowsMtaQueueStatus:
    """
        Aggregated mail-queue status.

        :param pending_count: Number of pending messages.
        :param deferred_count: Number of deferred messages.
        :param total_size_bytes: Aggregate queue size in bytes.
        :param queue_age_oldest: Always ``None`` on Windows — the
                                 IIS SMTP queue directory does not
                                 expose per-message arrival times
                                 cheaply.
    """
    pending_count: int = 0
    deferred_count: int = 0
    total_size_bytes: Optional[int] = None
    queue_age_oldest: Optional[str] = None


class WindowsMtaQueryError(Exception):
    """
        Raised when a mail-transport read fails for an unrecoverable
        reason.
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

        err_msg_lines = ["Mail transport query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsMailTransportManager(BaseFeatureManager):
    """
        Windows mail-transport-management manager.  Operates against
        the configured :class:`WindowsMtaKind` flavour through the
        injected :class:`ICommandRunner`.

        Default behaviour (`mta_kind=NONE`) returns FAILED on every
        write with the stable ``windows_no_built_in_mta`` detail
        string.  This is the documented Windows surface for the
        cross-OS contract.
    """

    result_class = WindowsMtaActionResult
    status_class = WindowsMtaActionStatus
    feature_name = "mail-transport-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_mta_config(self) -> WindowsMtaConfig:
        """
            Read the current MTA configuration.

            For ``mta_kind=NONE`` (default) returns a synthetic
            :class:`WindowsMtaConfig` with every named field ``None``
            and ``detail="Windows has no built-in MTA configured"``.

            For ``mta_kind=IIS_SMTP`` invokes a PowerShell pipeline
            that calls ``Get-WmiObject IIsSmtpServer`` and emits the
            relevant properties as JSON; the JSON is parsed into the
            named fields best-effort.

            For ``mta_kind=THIRD_PARTY`` returns a synthetic shape
            with ``detail="Windows third-party MTA management is not
            implemented in the pilot"``.

            :returns: The :class:`WindowsMtaConfig`.
        """
        kind = self._typed_config().mta_kind

        if kind == WindowsMtaKind.NONE:
            rtnval = WindowsMtaConfig(
                mta_kind=WindowsMtaKind.NONE,
                detail="Windows has no built-in MTA configured",
            )
            return rtnval

        if kind == WindowsMtaKind.THIRD_PARTY:
            rtnval = WindowsMtaConfig(
                mta_kind=WindowsMtaKind.THIRD_PARTY,
                detail=(
                    "Windows third-party MTA management is not "
                    "implemented in the pilot"
                ),
            )
            return rtnval

        # IIS_SMTP path.
        script = _build_iissmtp_get_script()
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            # Best-effort read.  Surface stderr but do not raise
            # to keep parity with the contract's "no MTA installed"
            # synthetic-shape behaviour.
            rtnval = WindowsMtaConfig(
                mta_kind=WindowsMtaKind.IIS_SMTP,
                detail="IIS SMTP probe failed: {}".format(
                    run_result.stderr.strip(),
                ),
            )
            return rtnval

        rtnval = _parse_iissmtp_get_output(text=run_result.stdout)
        return rtnval

    def list_aliases(self) -> list:
        """
            Enumerate aliases.

            On Windows every supported MTA flavour returns an empty
            list — IIS SMTP has no native aliases concept and the
            ``THIRD_PARTY`` placeholder does not implement aliases.

            :returns: Always an empty ``list``.
        """
        rtnval: list = []
        return rtnval

    def get_queue_status(self) -> WindowsMtaQueueStatus:
        """
            Read the mail-queue status.

            For ``mta_kind=NONE`` / ``THIRD_PARTY`` returns a zeroed
            status.

            For ``mta_kind=IIS_SMTP`` counts files under the
            configured ``iis_smtp_queue_dir`` via a PowerShell
            pipeline that invokes ``Get-ChildItem`` and emits a
            JSON summary (file count + aggregate length).

            :returns: The :class:`WindowsMtaQueueStatus`.
        """
        kind = self._typed_config().mta_kind

        if kind != WindowsMtaKind.IIS_SMTP:
            rtnval = WindowsMtaQueueStatus()
            return rtnval

        script = _build_iissmtp_queue_status_script(
            queue_dir=self._typed_config().iis_smtp_queue_dir,
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = WindowsMtaQueueStatus()
            return rtnval

        rtnval = _parse_iissmtp_queue_status_output(
            text=run_result.stdout,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_mta_config(
        self,
        spec: WindowsMtaConfig,
    ) -> WindowsMtaActionResult:
        """
            Apply a directive snapshot.

            Behaviour by kind:

            * ``NONE``: returns FAILED with
              ``detail="windows_no_built_in_mta"``.
            * ``THIRD_PARTY``: returns FAILED with
              ``detail="windows_third_party_mta_not_implemented"``.
            * ``IIS_SMTP``: best-effort.  Builds a PowerShell
              pipeline that invokes ``Get-WmiObject`` to fetch the
              live IIS SMTP server, sets each requested property,
              and calls ``.Put_()``.  Surfaces FAILED on PowerShell
              non-zero.

            :param spec: The :class:`WindowsMtaConfig` snapshot.

            :returns: The :class:`WindowsMtaActionResult`.
        """
        kind = self._typed_config().mta_kind

        if kind == WindowsMtaKind.NONE:
            failed = self._build_no_mta_failed_result(
                detail="set_mta_config",
            )
            return failed

        if kind == WindowsMtaKind.THIRD_PARTY:
            failed = self._build_third_party_failed_result(
                detail="set_mta_config",
            )
            return failed

        if self._config.dry_run:
            placeholder_command = [
                self._typed_config().powershell_executable,
                _PS_NOPROFILE,
                _PS_NONINTERACTIVE,
                _PS_COMMAND,
                "<set-iis-smtp-config>",
            ]
            dry = WindowsMtaActionResult(
                status=WindowsMtaActionStatus.DRY_RUN,
                detail="set_mta_config",
                command=placeholder_command,
                setting=None,
                local=None,
            )
            return dry

        script = _build_iissmtp_set_script(spec=spec)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            detail="set_mta_config",
            local=None,
            setting=None,
        )
        return rtnval

    def add_alias(
        self,
        local: str,
        remote: str,
    ) -> WindowsMtaActionResult:
        """
            Add an alias.

            Aliases are not natively supported on any Windows MTA
            flavour the pilot covers.  Returns FAILED unconditionally
            with a stable detail string per kind.

            :param local: The local alias.
            :param remote: The remote target list.

            :returns: The :class:`WindowsMtaActionResult`.
        """
        kind = self._typed_config().mta_kind

        if kind == WindowsMtaKind.NONE:
            failed = self._build_no_mta_failed_result(
                detail="add_alias:{}".format(local), local=local,
            )
            return failed

        if kind == WindowsMtaKind.THIRD_PARTY:
            failed = self._build_third_party_failed_result(
                detail="add_alias:{}".format(local), local=local,
            )
            return failed

        # IIS_SMTP: aliases are not exposed.
        failed = WindowsMtaActionResult(
            status=WindowsMtaActionStatus.FAILED,
            detail="add_alias:{}".format(local),
            stderr=_DETAIL_IIS_SMTP_NO_ALIASES,
            command=None,
            local=local,
            setting=None,
        )
        return failed

    def remove_alias(
        self,
        local: str,
    ) -> WindowsMtaActionResult:
        """
            Remove an alias.

            Same surface as :meth:`add_alias` — always FAILED with a
            stable detail string.

            :param local: The local alias.

            :returns: The :class:`WindowsMtaActionResult`.
        """
        kind = self._typed_config().mta_kind

        if kind == WindowsMtaKind.NONE:
            failed = self._build_no_mta_failed_result(
                detail="remove_alias:{}".format(local), local=local,
            )
            return failed

        if kind == WindowsMtaKind.THIRD_PARTY:
            failed = self._build_third_party_failed_result(
                detail="remove_alias:{}".format(local), local=local,
            )
            return failed

        failed = WindowsMtaActionResult(
            status=WindowsMtaActionStatus.FAILED,
            detail="remove_alias:{}".format(local),
            stderr=_DETAIL_IIS_SMTP_NO_ALIASES,
            command=None,
            local=local,
            setting=None,
        )
        return failed

    def test_mail_delivery(
        self,
        to: str,
        from_addr: str,
        subject: str,
    ) -> WindowsMtaActionResult:
        """
            Send a probe message.

            Behaviour by kind:

            * ``NONE``: FAILED with
              ``detail="windows_no_built_in_mta"``.
            * ``THIRD_PARTY``: FAILED with
              ``detail="windows_third_party_mta_not_implemented"``.
            * ``IIS_SMTP``: PowerShell ``Send-MailMessage`` against
              the configured ``iis_smtp_localhost`` (default
              ``localhost``).  Honours dry-run.

            :param to: Recipient address.
            :param from_addr: Sender address.
            :param subject: Subject text.

            :returns: The :class:`WindowsMtaActionResult`.
        """
        kind = self._typed_config().mta_kind
        action_detail = "test_mail_delivery:{}".format(to)

        if kind == WindowsMtaKind.NONE:
            failed = self._build_no_mta_failed_result(
                detail=action_detail,
            )
            return failed

        if kind == WindowsMtaKind.THIRD_PARTY:
            failed = self._build_third_party_failed_result(
                detail=action_detail,
            )
            return failed

        script = _build_send_mailmessage_script(
            smtp_server=self._typed_config().iis_smtp_localhost,
            to=to,
            from_addr=from_addr,
            subject=subject,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = WindowsMtaActionResult(
                status=WindowsMtaActionStatus.DRY_RUN,
                detail=action_detail,
                command=list(command),
                local=None,
                setting=None,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            detail=action_detail,
            local=None,
            setting=None,
        )
        return rtnval

    def flush_queue(self) -> WindowsMtaActionResult:
        """
            Flush the queue.

            Behaviour by kind:

            * ``NONE``: FAILED.
            * ``THIRD_PARTY``: FAILED.
            * ``IIS_SMTP``: best-effort.  Dispatches
              ``Restart-Service <iis_smtp_service_name>`` (default
              ``SMTPSVC``).  Restarting the service forces the IIS
              SMTP queue to retry delivery.  Honours dry-run.

            :returns: The :class:`WindowsMtaActionResult`.
        """
        kind = self._typed_config().mta_kind
        action_detail = "flush_queue"

        if kind == WindowsMtaKind.NONE:
            failed = self._build_no_mta_failed_result(
                detail=action_detail,
            )
            return failed

        if kind == WindowsMtaKind.THIRD_PARTY:
            failed = self._build_third_party_failed_result(
                detail=action_detail,
            )
            return failed

        script = "Restart-Service -Name '{}' -Force".format(
            self._typed_config().iis_smtp_service_name,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = WindowsMtaActionResult(
                status=WindowsMtaActionStatus.DRY_RUN,
                detail=action_detail,
                command=list(command),
                local=None,
                setting=None,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            detail=action_detail,
            local=None,
            setting=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsMtaManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed :class:`WindowsMtaManagerConfig`.
        """
        rtnval = self._config
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the PowerShell argv used by every IIS SMTP path.

            :param script: The PowerShell command to run.

            :returns: The argv.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _build_no_mta_failed_result(
        self,
        *,
        detail: str,
        local: Optional[str] = None,
        setting: Optional[str] = None,
    ) -> WindowsMtaActionResult:
        """
            Build a FAILED result with the stable
            ``windows_no_built_in_mta`` detail string.

            :param detail: The verb-level identifier (e.g.
                           ``"set_mta_config"``).
            :param local: Alias local name, when applicable.
            :param setting: Directive name, when applicable.

            :returns: The :class:`WindowsMtaActionResult`.
        """
        rtnval = WindowsMtaActionResult(
            status=WindowsMtaActionStatus.FAILED,
            detail=detail,
            stderr=_DETAIL_NO_MTA,
            command=None,
            local=local,
            setting=setting,
        )
        return rtnval

    def _build_third_party_failed_result(
        self,
        *,
        detail: str,
        local: Optional[str] = None,
        setting: Optional[str] = None,
    ) -> WindowsMtaActionResult:
        """
            Build a FAILED result with the stable
            ``windows_third_party_mta_not_implemented`` detail
            string.

            :param detail: The verb-level identifier.
            :param local: Alias local name, when applicable.
            :param setting: Directive name, when applicable.

            :returns: The :class:`WindowsMtaActionResult`.
        """
        rtnval = WindowsMtaActionResult(
            status=WindowsMtaActionStatus.FAILED,
            detail=detail,
            stderr=_DETAIL_THIRD_PARTY_NOT_IMPL,
            command=None,
            local=local,
            setting=setting,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        detail: Optional[str],
        local: Optional[str],
        setting: Optional[str],
    ) -> WindowsMtaActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`WindowsMtaActionResult`.

            :param run_result: The runner's :class:`RunResult`.
            :param command: The argv that ran.
            :param detail: Short identifier.
            :param local: Alias local name, when applicable.
            :param setting: Directive name, when applicable.

            :returns: The :class:`WindowsMtaActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = WindowsMtaActionStatus.SUCCESS
        else:
            mapped_status = WindowsMtaActionStatus.FAILED

        rtnval = WindowsMtaActionResult(
            status=mapped_status,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            local=local,
            setting=setting,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — IIS SMTP scripts
# ----------------------------------------------------------------------


def _build_iissmtp_get_script() -> str:
    """
        Build the PowerShell script used by
        :meth:`WindowsMailTransportManager.get_mta_config` when
        ``mta_kind=IIS_SMTP``.

        Calls ``Get-WmiObject -Class IIsSmtpServerSetting -Namespace
        root/MicrosoftIISv2`` and emits a JSON object with the
        ``FullyQualifiedDomainName``, ``SmartHost``,
        ``MaxMessageSize``, ``RelayIpList``, and
        ``ServerBindings`` fields surfaced.

        :returns: The PowerShell command text.
    """
    rtnval = (
        "$s = Get-WmiObject -Class IIsSmtpServerSetting "
        "-Namespace 'root/MicrosoftIISv2' "
        "| Select-Object -First 1; "
        "if ($null -eq $s) { '{}'; exit 0 } "
        "$out = @{ "
        "FullyQualifiedDomainName = $s.FullyQualifiedDomainName; "
        "SmartHost = $s.SmartHost; "
        "MaxMessageSize = $s.MaxMessageSize; "
        "ServerBindings = $s.ServerBindings "
        "}; "
        "$out | ConvertTo-Json -Compress"
    )
    return rtnval


def _build_iissmtp_set_script(*, spec: WindowsMtaConfig) -> str:
    """
        Build the PowerShell script used by
        :meth:`WindowsMailTransportManager.set_mta_config` when
        ``mta_kind=IIS_SMTP``.

        For each spec field that maps to a known IIS SMTP property,
        emit ``$s.<Property> = <value>`` followed by a single
        ``$s.Put_()`` to commit.  Properties without an IIS SMTP
        equivalent are skipped (and recorded in the script as
        comments so the audit trail surfaces them).

        :param spec: The desired :class:`WindowsMtaConfig`.

        :returns: The PowerShell command text.
    """
    statements: list = [
        (
            "$s = Get-WmiObject -Class IIsSmtpServerSetting "
            "-Namespace 'root/MicrosoftIISv2' "
            "| Select-Object -First 1"
        ),
        "if ($null -eq $s) { Write-Error 'IIS SMTP not installed'; exit 1 }",
    ]

    if spec.myhostname is not None:
        statements.append(
            "$s.FullyQualifiedDomainName = '{}'".format(
                _ps_escape(text=spec.myhostname),
            ),
        )

    if spec.relayhost is not None:
        statements.append(
            "$s.SmartHost = '{}'".format(
                _ps_escape(text=spec.relayhost),
            ),
        )

    if spec.message_size_limit is not None:
        statements.append(
            "$s.MaxMessageSize = {}".format(
                int(spec.message_size_limit),
            ),
        )
    elif spec.mailbox_size_limit is not None:
        statements.append(
            "$s.MaxMessageSize = {}".format(
                int(spec.mailbox_size_limit),
            ),
        )

    statements.append("$s.Put_()")
    rtnval = "; ".join(statements)
    return rtnval


def _build_iissmtp_queue_status_script(*, queue_dir: str) -> str:
    """
        Build the PowerShell script used by
        :meth:`WindowsMailTransportManager.get_queue_status` when
        ``mta_kind=IIS_SMTP``.

        Counts files under ``queue_dir`` and aggregates the byte
        total via ``Measure-Object``.  Emits a JSON object with
        ``Count`` and ``Sum`` fields.

        :param queue_dir: Path to the IIS SMTP queue directory.

        :returns: The PowerShell command text.
    """
    rtnval = (
        "$items = Get-ChildItem -LiteralPath '{}' -File "
        "-ErrorAction SilentlyContinue; "
        "$count = ($items | Measure-Object).Count; "
        "$sum = ($items | Measure-Object -Property Length -Sum).Sum; "
        "$out = @{{ Count = $count; Sum = $sum }}; "
        "$out | ConvertTo-Json -Compress"
    ).format(_ps_escape(text=queue_dir))
    return rtnval


def _build_send_mailmessage_script(
    *,
    smtp_server: str,
    to: str,
    from_addr: str,
    subject: str,
) -> str:
    """
        Build the PowerShell script used by
        :meth:`WindowsMailTransportManager.test_mail_delivery` when
        ``mta_kind=IIS_SMTP``.

        Calls ``Send-MailMessage`` with the supplied addresses and
        subject; the body is a fixed audit-tag line.

        :param smtp_server: SMTP server hostname / IP.
        :param to: Recipient address.
        :param from_addr: Sender address.
        :param subject: Subject text.

        :returns: The PowerShell command text.
    """
    rtnval = (
        "Send-MailMessage "
        "-SmtpServer '{server}' "
        "-To '{to}' "
        "-From '{frm}' "
        "-Subject '{subject}' "
        "-Body 'codecraftai-sysman mail-transport probe'"
    ).format(
        server=_ps_escape(text=smtp_server),
        to=_ps_escape(text=to),
        frm=_ps_escape(text=from_addr),
        subject=_ps_escape(text=subject),
    )
    return rtnval


def _ps_escape(*, text: str) -> str:
    """
        Escape a string for embedding inside a PowerShell single-
        quoted literal.

        PowerShell single-quoted literals only need single-quote
        doubling for embedding; backticks and dollars are not
        interpreted inside single-quoted literals.

        :param text: Input string.

        :returns: The escaped string.
    """
    rtnval = text.replace("'", "''")
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — IIS SMTP parsers
# ----------------------------------------------------------------------


_NUMBER_PATTERN = re.compile(r"-?\d+")


def _parse_iissmtp_get_output(*, text: str) -> WindowsMtaConfig:
    """
        Parse the JSON emitted by :func:`_build_iissmtp_get_script`
        into a :class:`WindowsMtaConfig`.

        Uses a tolerant key=value extractor rather than ``json``
        directly so that minor schema changes (or empty objects) do
        not raise.  This matches the pattern used elsewhere in the
        Windows package for PowerShell-output parsers.

        :param text: The captured stdout.

        :returns: The :class:`WindowsMtaConfig`.
    """
    rtnval = WindowsMtaConfig(mta_kind=WindowsMtaKind.IIS_SMTP)

    if len(text.strip()) == 0:
        rtnval.detail = "IIS SMTP probe returned empty output"
        return rtnval

    fqdn = _extract_json_value(text=text, key="FullyQualifiedDomainName")
    if fqdn is not None:
        rtnval.myhostname = fqdn

    smart_host = _extract_json_value(text=text, key="SmartHost")
    if smart_host is not None and len(smart_host) > 0:
        rtnval.relayhost = smart_host

    max_size_text = _extract_json_value(text=text, key="MaxMessageSize")
    if max_size_text is not None:
        match = _NUMBER_PATTERN.search(max_size_text)
        if match is not None:
            try:
                size_int = int(match.group(0))
            except ValueError:
                size_int = None
            if size_int is not None:
                rtnval.message_size_limit = size_int
                rtnval.mailbox_size_limit = size_int

    bindings = _extract_json_value(text=text, key="ServerBindings")
    if bindings is not None and len(bindings) > 0:
        rtnval.extras["ServerBindings"] = bindings

    return rtnval


def _parse_iissmtp_queue_status_output(
    *,
    text: str,
) -> WindowsMtaQueueStatus:
    """
        Parse the JSON emitted by
        :func:`_build_iissmtp_queue_status_script`.

        The script emits ``{"Count":N,"Sum":B}``.  The pilot maps
        ``Count`` to ``pending_count`` (IIS SMTP does not partition
        active vs deferred via the queue directory cheaply — every
        file in the queue is a pending message awaiting delivery).
        ``deferred_count`` stays at zero.

        :param text: The captured stdout.

        :returns: The :class:`WindowsMtaQueueStatus`.
    """
    rtnval = WindowsMtaQueueStatus()

    if len(text.strip()) == 0:
        return rtnval

    count_text = _extract_json_value(text=text, key="Count")
    if count_text is not None:
        match = _NUMBER_PATTERN.search(count_text)
        if match is not None:
            try:
                count_int = int(match.group(0))
            except ValueError:
                count_int = 0
            rtnval.pending_count = count_int

    sum_text = _extract_json_value(text=text, key="Sum")
    if sum_text is not None:
        match = _NUMBER_PATTERN.search(sum_text)
        if match is not None:
            try:
                sum_int = int(match.group(0))
            except ValueError:
                sum_int = None
            if sum_int is not None:
                rtnval.total_size_bytes = sum_int

    return rtnval


_JSON_VALUE_PATTERN = re.compile(
    r'"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*'
    r'(?:"(?P<svalue>(?:[^"\\]|\\.)*)"|(?P<nvalue>-?\d+(?:\.\d+)?|null|true|false))',
)


def _extract_json_value(*, text: str, key: str) -> Optional[str]:
    """
        Tolerant extractor for ``"<key>": <value>`` pairs in a JSON
        blob.  Returns the matched value verbatim; numeric values are
        returned as their text representation.

        :param text: The JSON blob.
        :param key: The key whose value to extract.

        :returns: The matched value text, or ``None`` when the key is
                  absent / the value is ``null``.
    """
    for match in _JSON_VALUE_PATTERN.finditer(text):
        if match.group("key") != key:
            continue
        svalue = match.group("svalue")
        if svalue is not None:
            rtnval: Optional[str] = svalue
            return rtnval
        nvalue = match.group("nvalue")
        if nvalue is None:
            rtnval = None
            return rtnval
        if nvalue == "null":
            rtnval = None
            return rtnval
        rtnval = nvalue
        return rtnval

    rtnval = None
    return rtnval
