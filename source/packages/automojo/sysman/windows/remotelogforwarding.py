"""
    Windows remote-log-forwarding feature.

    Implements the cross-OS remote-log-forwarding contract for
    Windows using Windows Event Forwarding (WEF) subscriptions
    managed via ``wecutil.exe``.  Every verb is dispatched
    through an :class:`ICommandRunner` so the manager works
    identically under local and SSH execution.

    WEF vs. syslog — read this first:
        Windows Event Forwarding carries Event Log records
        between Windows hosts only.  WEF rides on
        WS-Management (WinRM) and ships native ``EventLog``
        records — NOT syslog records.

        Operators who need to ship to a non-Windows collector
        (rsyslog, syslog-ng, Splunk, Elastic, Datadog) MUST
        deploy a third-party shipper such as Winlogbeat or
        NXLog alongside this module.  This module exists for
        the WEF use case (collector-side subscription
        authoring) and does NOT attempt to bridge the syslog
        gap.

    Subscription mode:
        This manager implements the collector-side
        configuration in **Source-Initiated** mode.  Source
        hosts push events to the collector based on the
        subscription document the manager registers.
        Configuring the source-side WinRM listener (typically
        via Group Policy or DSC) is out of scope.

    File I/O policy:
        Every read / write is performed through the injected
        :class:`ICommandRunner` (``powershell.exe`` /
        ``wecutil.exe`` invocations).  Subscription XML is
        written to ``<wec_subscription_dir>\\<id>.xml`` via a
        ``Set-Content`` PowerShell scriptblock.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so that ``$PROFILE`` cannot
        mutate cmdlet behaviour and PowerShell never blocks
        waiting for interactive input.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        and ``test_forwarder`` never elevate.

    Documented limitations:
        * Only Source-Initiated mode is rendered.
        * Only single-XPath ``<Query>`` blocks are rendered.
        * No syslog-protocol output.
        * ``RuntimeStatus`` parsing for ``last_event_time`` /
          ``error_count`` / ``bytes_sent`` is best-effort.
"""

import os
import time

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
_DEFAULT_WECUTIL = "wecutil.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

_DEFAULT_WEC_SUBSCRIPTION_DIR = r"C:\ProgramData\Automojo\WEC"
_DEFAULT_TEST_EVENT_SOURCE = "automojo-test"
_DEFAULT_TEST_EVENT_ID = 4242

_PROTOCOL_HTTP = "http"
_PROTOCOL_HTTPS = "https"
_VALID_PROTOCOLS = frozenset({_PROTOCOL_HTTP, _PROTOCOL_HTTPS})

_WEF_NAMESPACE = (
    "http://schemas.microsoft.com/2006/03/windows/events/subscription"
)
_WEF_LOG_URI = (
    "http://schemas.microsoft.com/wbem/wsman/1/windows/EventLog"
)


WindowsForwarderActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class WindowsForwarderManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`WindowsRemoteLogForwardingManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.
                                      Set to ``"pwsh.exe"`` to
                                      target PowerShell 7+.
        :param wecutil_executable: Path to ``wecutil.exe``.
                                   Defaults to
                                   ``"wecutil.exe"``.
        :param wec_subscription_dir: Directory under which
                                     subscription XML documents
                                     are written.  Defaults to
                                     ``C:\\ProgramData\\Automojo\\WEC``.
        :param test_event_source: Event Log source registered
                                  by ``test_forwarder``.
                                  Defaults to ``"automojo-test"``.
        :param test_event_id: Event ID written by
                              ``test_forwarder``.  Defaults to
                              ``4242``.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    wecutil_executable: str = _DEFAULT_WECUTIL
    wec_subscription_dir: str = _DEFAULT_WEC_SUBSCRIPTION_DIR
    test_event_source: str = _DEFAULT_TEST_EVENT_SOURCE
    test_event_id: int = _DEFAULT_TEST_EVENT_ID


@dataclass
class WindowsForwarderActionResult(FeatureActionResult):
    """
        Result of a
        :class:`WindowsRemoteLogForwardingManager` write verb.

        :param forwarder_id: The forwarder identifier targeted.
        :param path: The on-disk subscription XML path the action
                     targeted, when applicable.
        :param message: Optional human-readable detail for
                        ``SKIPPED`` / ``FAILED`` outcomes.
    """
    forwarder_id: Optional[str] = None
    path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsForwarderSpec:
    """
        Declarative specification for a single WEF subscription.

        :param id: File-name-friendly identifier.  Used as both
                   the WEF ``SubscriptionId`` and the on-disk
                   XML basename.
        :param host: Address recorded in the subscription's
                     ``<Address>`` element.  For source-initiated
                     subscriptions this is typically the
                     collector's own hostname.
        :param port: WinRM port.  ``5985`` selects ``HTTP``;
                     ``5986`` selects ``HTTPS``.  Defaults to
                     5985.
        :param protocol: ``"http"`` or ``"https"``.  Defaults to
                         ``"http"``.
        :param query: XPath query embedded in the subscription's
                      ``<Query>`` element.  Defaults to ``"*"``.
        :param description: Free-text description rendered in
                            the subscription's ``<Description>``
                            element.  Defaults to ``""``.
        :param log_file: ``LogFile`` element value identifying
                         the local channel that captures the
                         forwarded events.  Defaults to
                         ``"ForwardedEvents"``.
        :param tls_ca_cert: Accepted for cross-OS parity but
                            not consumed by the renderer.
                            ``Subscription`` elements reference
                            certificates by thumbprint, which is
                            out of scope for this pilot.
        :param enabled: When ``False``, the rendered XML's
                        ``<Enabled>`` element is set to ``false``
                        so the subscription registers in a
                        disabled state.
    """
    id: str
    host: str
    port: int = 5985
    protocol: str = _PROTOCOL_HTTP
    query: str = "*"
    description: str = ""
    log_file: str = "ForwardedEvents"
    tls_ca_cert: Optional[str] = None
    enabled: bool = True


@dataclass
class WindowsForwarderInfo:
    """
        One row from
        :meth:`WindowsRemoteLogForwardingManager.list_forwarders`.

        :param id: Subscription identifier.
        :param host: Address recorded on the subscription.
        :param port: WinRM port (5985 for HTTP, 5986 for HTTPS).
        :param protocol: ``"http"`` or ``"https"`` derived from
                         the ``TransportName`` element.
        :param enabled: True when ``wecutil gs`` reports
                        ``Enabled: true``.
        :param path: Absolute path of the on-disk subscription
                     XML, when known.
        :param selector_or_query: XPath query parsed from the
                                  subscription's ``Query``
                                  block.  ``None`` when the
                                  parser could not extract it.
    """
    id: str
    host: str
    port: int
    protocol: str
    enabled: bool
    path: Optional[str]
    selector_or_query: Optional[str]


@dataclass
class WindowsForwarderStatus:
    """
        Result of
        :meth:`WindowsRemoteLogForwardingManager.get_forwarder_status`.

        :param id: The subscription identifier queried.
        :param enabled: True when ``wecutil gs`` reports
                        ``Enabled: true``.  False when the
                        subscription is not registered.
        :param last_event_time: Best-effort parse of the
                                ``RuntimeStatus`` block; ``None``
                                when not present.
        :param error_count: Best-effort parse of the
                            ``RuntimeStatus`` block; ``None``
                            when not present.
        :param bytes_sent: Best-effort parse of the
                           ``RuntimeStatus`` block; ``None``
                           when not present.
    """
    id: str
    enabled: bool
    last_event_time: Optional[str] = None
    error_count: Optional[int] = None
    bytes_sent: Optional[int] = None


@dataclass
class WindowsForwarderTestResult(FeatureActionResult):
    """
        Result of
        :meth:`WindowsRemoteLogForwardingManager.test_forwarder`.

        :param forwarder_id: The subscription identifier the
                             probe targeted.
        :param test_message_id: The unique identifier embedded
                                in the dispatched
                                ``Write-EventLog`` message.
    """
    forwarder_id: str = ""
    test_message_id: Optional[str] = None


class WindowsForwarderQueryError(Exception):
    """
        Raised when a remote-log-forwarding read fails for a
        reason other than the subscription simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
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

        err_msg_lines = ["Forwarder query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# WindowsRemoteLogForwardingManager
# ----------------------------------------------------------------------


class WindowsRemoteLogForwardingManager(BaseFeatureManager):
    """
        Windows Event Forwarding subscription manager.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`.  See the module docstring for
        the prominent WEF-vs-syslog distinction: this manager
        covers WEF subscriptions, NOT syslog forwarding.
    """

    result_class = WindowsForwarderActionResult
    status_class = WindowsForwarderActionStatus
    feature_name = "remote-log-forwarding"

    def __init__(
        self,
        runner,
        config: Optional[WindowsForwarderManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh
                           :class:`WindowsForwarderManagerConfig`
                           when omitted.
        """
        if config is None:
            config = WindowsForwarderManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_forwarder(
        self,
        spec: WindowsForwarderSpec,
    ) -> WindowsForwarderActionResult:
        """
            Render a WEF subscription XML for ``spec``, write it
            to the subscription directory, and register it with
            the Windows Event Collector service.

            Idempotent: returns ``SKIPPED`` when the on-disk XML
            matches the rendered desired XML byte-for-byte.

            :param spec: The :class:`WindowsForwarderSpec` to
                         render.

            :returns: The :class:`WindowsForwarderActionResult`.
        """
        validation_failure = _validate_spec(spec=spec)
        if validation_failure is not None:
            failed = self._build_result(
                status=WindowsForwarderActionStatus.FAILED,
                forwarder_id=spec.id,
                path=None,
                command=[
                    self._typed_config().powershell_executable,
                ],
                message=validation_failure,
                stderr=validation_failure,
            )
            return failed

        cfg = self._typed_config()
        xml_path = self._subscription_path(forwarder_id=spec.id)
        desired_xml = _render_subscription_xml(spec=spec)

        write_script = _build_write_xml_script(
            xml_path=xml_path,
            xml_body=desired_xml,
            wec_subscription_dir=cfg.wec_subscription_dir,
        )
        write_command = [
            cfg.powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            write_script,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsForwarderActionStatus.DRY_RUN,
                forwarder_id=spec.id,
                path=xml_path,
                command=write_command,
            )
            return dry

        existing_xml = self._read_xml(path=xml_path)
        if existing_xml is not None and existing_xml == desired_xml:
            skipped = self._build_result(
                status=WindowsForwarderActionStatus.SKIPPED,
                forwarder_id=spec.id,
                path=xml_path,
                command=write_command,
                message="forwarder already matches desired content",
            )
            return skipped

        write_result = self._runner.run(
            list(write_command),
            elevated=cfg.elevated,
        )
        if write_result.exit_code != 0:
            failed = WindowsForwarderActionResult(
                status=WindowsForwarderActionStatus.FAILED,
                detail=spec.id,
                exit_code=write_result.exit_code,
                stdout=write_result.stdout,
                stderr=write_result.stderr,
                command=list(write_command),
                forwarder_id=spec.id,
                path=xml_path,
                message="Set-Content failed; subscription XML not written",
            )
            return failed

        register_command = [
            cfg.wecutil_executable, "cs", xml_path,
        ]
        register_result = self._runner.run(
            list(register_command),
            elevated=cfg.elevated,
        )
        if register_result.exit_code != 0:
            failed = WindowsForwarderActionResult(
                status=WindowsForwarderActionStatus.FAILED,
                detail=spec.id,
                exit_code=register_result.exit_code,
                stdout=register_result.stdout,
                stderr=register_result.stderr,
                command=list(register_command),
                forwarder_id=spec.id,
                path=xml_path,
                message="wecutil cs failed; subscription not registered",
            )
            return failed

        rtnval = WindowsForwarderActionResult(
            status=WindowsForwarderActionStatus.SUCCESS,
            detail=spec.id,
            exit_code=register_result.exit_code,
            stdout=register_result.stdout,
            stderr=register_result.stderr,
            command=list(register_command),
            forwarder_id=spec.id,
            path=xml_path,
        )
        return rtnval

    def remove_forwarder(self, id: str) -> WindowsForwarderActionResult:
        """
            Delete a WEF subscription by id.

            Idempotent: returns ``SKIPPED`` when the
            subscription is not registered AND the on-disk XML
            is missing.

            :param id: The subscription identifier.

            :returns: The :class:`WindowsForwarderActionResult`.
        """
        cfg = self._typed_config()
        xml_path = self._subscription_path(forwarder_id=id)

        registered = self._is_registered(forwarder_id=id)
        on_disk_xml = self._read_xml(path=xml_path)

        delete_command = [cfg.wecutil_executable, "ds", id]

        if not registered and on_disk_xml is None:
            skipped = self._build_result(
                status=WindowsForwarderActionStatus.SKIPPED,
                forwarder_id=id,
                path=xml_path,
                command=delete_command,
                message="subscription already absent",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsForwarderActionStatus.DRY_RUN,
                forwarder_id=id,
                path=xml_path,
                command=delete_command,
            )
            return dry

        if registered:
            delete_result = self._runner.run(
                list(delete_command),
                elevated=cfg.elevated,
            )
            if delete_result.exit_code != 0:
                failed = WindowsForwarderActionResult(
                    status=WindowsForwarderActionStatus.FAILED,
                    detail=id,
                    exit_code=delete_result.exit_code,
                    stdout=delete_result.stdout,
                    stderr=delete_result.stderr,
                    command=list(delete_command),
                    forwarder_id=id,
                    path=xml_path,
                    message="wecutil ds failed; subscription still registered",
                )
                return failed
            captured_result = delete_result
        else:
            captured_result = None

        if on_disk_xml is not None:
            cleanup_script = _build_remove_xml_script(xml_path=xml_path)
            cleanup_command = [
                cfg.powershell_executable,
                _PS_NOPROFILE,
                _PS_NONINTERACTIVE,
                _PS_COMMAND,
                cleanup_script,
            ]
            cleanup_result = self._runner.run(
                list(cleanup_command),
                elevated=cfg.elevated,
            )
            if cleanup_result.exit_code != 0:
                failed = WindowsForwarderActionResult(
                    status=WindowsForwarderActionStatus.FAILED,
                    detail=id,
                    exit_code=cleanup_result.exit_code,
                    stdout=cleanup_result.stdout,
                    stderr=cleanup_result.stderr,
                    command=list(cleanup_command),
                    forwarder_id=id,
                    path=xml_path,
                    message="Remove-Item failed; on-disk XML not deleted",
                )
                return failed

        if captured_result is not None:
            exit_code = captured_result.exit_code
            stdout = captured_result.stdout
            stderr = captured_result.stderr
        else:
            exit_code = 0
            stdout = ""
            stderr = ""

        rtnval = WindowsForwarderActionResult(
            status=WindowsForwarderActionStatus.SUCCESS,
            detail=id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=list(delete_command),
            forwarder_id=id,
            path=xml_path,
        )
        return rtnval

    def enable_forwarder(self, id: str) -> WindowsForwarderActionResult:
        """
            Enable a registered WEF subscription via
            ``wecutil ss <id> /e:true``.

            Idempotent: returns ``SKIPPED`` when the
            subscription is already enabled.

            :param id: The subscription identifier.

            :returns: The :class:`WindowsForwarderActionResult`.
        """
        rtnval = self._toggle_subscription_state(
            forwarder_id=id, enabled=True,
        )
        return rtnval

    def disable_forwarder(self, id: str) -> WindowsForwarderActionResult:
        """
            Disable a registered WEF subscription via
            ``wecutil ss <id> /e:false``.

            Idempotent: returns ``SKIPPED`` when the
            subscription is already disabled.

            :param id: The subscription identifier.

            :returns: The :class:`WindowsForwarderActionResult`.
        """
        rtnval = self._toggle_subscription_state(
            forwarder_id=id, enabled=False,
        )
        return rtnval

    def test_forwarder(self, id: str) -> WindowsForwarderTestResult:
        """
            Write a test event into the Application channel via
            a PowerShell ``Write-EventLog`` scriptblock.

            Dispatches without elevation.  No idempotency
            guard.  Never raises on dispatch failure.

            :param id: The subscription identifier the test is
                       targeted at.

            :returns: The :class:`WindowsForwarderTestResult`.
        """
        cfg = self._typed_config()
        timestamp = int(time.time())
        message_id = "forwarder-test-{}".format(timestamp)

        script = _build_test_event_script(
            source=cfg.test_event_source,
            event_id=cfg.test_event_id,
            message=message_id,
        )
        command = [
            cfg.powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]

        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 0:
            mapped_status = WindowsForwarderActionStatus.SUCCESS
        else:
            mapped_status = WindowsForwarderActionStatus.FAILED

        rtnval = WindowsForwarderTestResult(
            status=mapped_status,
            detail=id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            forwarder_id=id,
            test_message_id=message_id,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_forwarders(self) -> list[WindowsForwarderInfo]:
        """
            Enumerate WEF subscriptions registered on this
            host.

            Dispatches ``wecutil.exe es`` without elevation.
            For each subscription name in the listing, dispatches
            ``wecutil.exe gs <id>`` and parses ``Enabled``,
            ``TransportName``, the address, and the XPath query.

            :returns: One :class:`WindowsForwarderInfo` per
                      registered subscription.

            :raises WindowsForwarderQueryError: When ``wecutil
                                                 es`` exits
                                                 non-zero with a
                                                 non-empty
                                                 stderr.
        """
        cfg = self._typed_config()
        es_command = [cfg.wecutil_executable, "es"]
        es_result = self._runner.run(list(es_command), elevated=False)

        if es_result.exit_code != 0 and len(es_result.stderr.strip()) > 0:
            raise WindowsForwarderQueryError(
                command=es_command,
                exit_code=es_result.exit_code,
                stderr=es_result.stderr,
            )

        rtnval: list[WindowsForwarderInfo] = []
        for raw_line in es_result.stdout.splitlines():
            entry = raw_line.strip()
            if len(entry) == 0:
                continue
            sub_id = entry
            gs_command = [cfg.wecutil_executable, "gs", sub_id]
            gs_result = self._runner.run(
                list(gs_command), elevated=False,
            )
            if gs_result.exit_code != 0:
                continue
            (
                enabled,
                transport,
                address,
                query,
                _last_event,
                _error_count,
                _bytes_sent,
            ) = _parse_wecutil_gs_output(text=gs_result.stdout)

            xml_path = self._subscription_path(forwarder_id=sub_id)
            if transport.lower() == "https":
                protocol_value = _PROTOCOL_HTTPS
                port_value = 5986
            else:
                protocol_value = _PROTOCOL_HTTP
                port_value = 5985

            info = WindowsForwarderInfo(
                id=sub_id,
                host=address,
                port=port_value,
                protocol=protocol_value,
                enabled=enabled,
                path=xml_path,
                selector_or_query=query,
            )
            rtnval.append(info)

        return rtnval

    def get_forwarder_status(self, id: str) -> WindowsForwarderStatus:
        """
            Report a subscription's current state.

            Dispatches ``wecutil.exe gs <id>`` without
            elevation.  When the subscription is not registered,
            returns ``enabled=False`` with ``None`` runtime
            fields.

            :param id: The subscription identifier.

            :returns: The :class:`WindowsForwarderStatus`.
        """
        cfg = self._typed_config()
        command = [cfg.wecutil_executable, "gs", id]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval = WindowsForwarderStatus(
                id=id,
                enabled=False,
                last_event_time=None,
                error_count=None,
                bytes_sent=None,
            )
            return rtnval

        (
            enabled,
            _transport,
            _address,
            _query,
            last_event,
            error_count,
            bytes_sent,
        ) = _parse_wecutil_gs_output(text=run_result.stdout)

        rtnval = WindowsForwarderStatus(
            id=id,
            enabled=enabled,
            last_event_time=last_event,
            error_count=error_count,
            bytes_sent=bytes_sent,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _toggle_subscription_state(
        self,
        *,
        forwarder_id: str,
        enabled: bool,
    ) -> WindowsForwarderActionResult:
        """
            Flip the enabled state of a registered subscription.

            :param forwarder_id: The subscription identifier.
            :param enabled: True to enable; False to disable.

            :returns: The :class:`WindowsForwarderActionResult`.
        """
        cfg = self._typed_config()
        if enabled:
            flag_token = "/e:true"
        else:
            flag_token = "/e:false"

        command = [cfg.wecutil_executable, "ss", forwarder_id, flag_token]

        current_status = self.get_forwarder_status(id=forwarder_id)
        is_registered = self._is_registered(forwarder_id=forwarder_id)

        if not is_registered:
            failed = self._build_result(
                status=WindowsForwarderActionStatus.FAILED,
                forwarder_id=forwarder_id,
                path=self._subscription_path(forwarder_id=forwarder_id),
                command=command,
                message="subscription not registered",
                stderr="subscription_not_registered",
            )
            return failed

        if current_status.enabled == enabled:
            if enabled:
                msg = "subscription already enabled"
            else:
                msg = "subscription already disabled"
            skipped = self._build_result(
                status=WindowsForwarderActionStatus.SKIPPED,
                forwarder_id=forwarder_id,
                path=self._subscription_path(forwarder_id=forwarder_id),
                command=command,
                message=msg,
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsForwarderActionStatus.DRY_RUN,
                forwarder_id=forwarder_id,
                path=self._subscription_path(forwarder_id=forwarder_id),
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=cfg.elevated,
        )

        if run_result.exit_code != 0:
            failed = WindowsForwarderActionResult(
                status=WindowsForwarderActionStatus.FAILED,
                detail=forwarder_id,
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(command),
                forwarder_id=forwarder_id,
                path=self._subscription_path(forwarder_id=forwarder_id),
                message="wecutil ss failed",
            )
            return failed

        rtnval = WindowsForwarderActionResult(
            status=WindowsForwarderActionStatus.SUCCESS,
            detail=forwarder_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            forwarder_id=forwarder_id,
            path=self._subscription_path(forwarder_id=forwarder_id),
        )
        return rtnval

    def _is_registered(self, *, forwarder_id: str) -> bool:
        """
            Probe whether a subscription is registered with the
            Windows Event Collector service.

            :param forwarder_id: The subscription identifier.

            :returns: True when ``wecutil gs <id>`` exits zero.
        """
        cfg = self._typed_config()
        command = [cfg.wecutil_executable, "gs", forwarder_id]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _subscription_path(self, *, forwarder_id: str) -> str:
        """
            Build the absolute path to the on-disk subscription
            XML for ``forwarder_id``.

            :param forwarder_id: The subscription identifier.

            :returns: ``<wec_subscription_dir>\\<id>.xml``.
        """
        cfg = self._typed_config()
        rtnval = "{}\\{}.xml".format(
            cfg.wec_subscription_dir, forwarder_id,
        )
        return rtnval

    def _typed_config(self) -> WindowsForwarderManagerConfig:
        """
            Return the manager's config typed as
            :class:`WindowsForwarderManagerConfig`.

            :returns: The active config typed.
        """
        if isinstance(self._config, WindowsForwarderManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsForwarderManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _read_xml(self, *, path: str) -> Optional[str]:
        """
            Read a subscription XML file via PowerShell
            ``Get-Content``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None``
                      when the file is missing or unreadable.
        """
        cfg = self._typed_config()
        script = _build_read_xml_script(xml_path=path)
        command = [
            cfg.powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        if run_result.stdout is None or len(run_result.stdout) == 0:
            rtnval = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsForwarderActionStatus,
        forwarder_id: Optional[str],
        path: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsForwarderActionResult:
        """
            Construct a :class:`WindowsForwarderActionResult`
            without invoking the runner.

            :param status: The outcome status.
            :param forwarder_id: The subscription identifier to
                                 echo onto the result.
            :param path: The on-disk path to echo onto the
                         result.
            :param command: The would-have-been argv (copied for
                            capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`WindowsForwarderActionResult`.
        """
        rtnval = WindowsForwarderActionResult(
            status=status,
            detail=forwarder_id,
            command=list(command),
            forwarder_id=forwarder_id,
            path=path,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level renderers / helpers
# ----------------------------------------------------------------------


def _validate_spec(*, spec: WindowsForwarderSpec) -> Optional[str]:
    """
        Validate a spec before the renderer runs.

        :param spec: The :class:`WindowsForwarderSpec` to
                     validate.

        :returns: ``None`` when the spec is valid; an error
                  message string when it is not.
    """
    if spec.id is None or len(spec.id) == 0:
        rtnval: Optional[str] = "spec.id is required"
        return rtnval
    if spec.host is None or len(spec.host) == 0:
        rtnval = "spec.host is required"
        return rtnval
    if spec.port is None:
        rtnval = "spec.port is required"
        return rtnval
    if spec.port <= 0:
        rtnval = "spec.port must be positive"
        return rtnval
    if spec.protocol not in _VALID_PROTOCOLS:
        rtnval = "spec.protocol must be one of {}".format(
            sorted(_VALID_PROTOCOLS),
        )
        return rtnval

    rtnval = None
    return rtnval


def _xml_escape(*, value: str) -> str:
    """
        Escape XML special characters.

        :param value: Raw text.

        :returns: The XML-safe text.
    """
    if value is None:
        rtnval = ""
        return rtnval
    rtnval = value
    rtnval = rtnval.replace("&", "&amp;")
    rtnval = rtnval.replace("<", "&lt;")
    rtnval = rtnval.replace(">", "&gt;")
    rtnval = rtnval.replace("\"", "&quot;")
    return rtnval


def _render_subscription_xml(*, spec: WindowsForwarderSpec) -> str:
    """
        Render a :class:`WindowsForwarderSpec` into a WEF
        subscription XML body.

        Output is byte-stable for matching inputs so that the
        idempotency comparison in
        :meth:`WindowsRemoteLogForwardingManager.add_forwarder`
        works.

        :param spec: The :class:`WindowsForwarderSpec` to render.

        :returns: The full XML document as a string ending in
                  ``\\n``.
    """
    if spec.protocol == _PROTOCOL_HTTPS:
        transport = "HTTPS"
    else:
        transport = "HTTP"

    if spec.enabled:
        enabled_text = "true"
    else:
        enabled_text = "false"

    description_text = _xml_escape(value=spec.description)
    query_text = _xml_escape(value=spec.query)
    log_file_text = _xml_escape(value=spec.log_file)
    sub_id_text = _xml_escape(value=spec.id)

    inner_query = (
        "&lt;QueryList&gt;"
        "&lt;Query Id=\"0\"&gt;"
        "&lt;Select Path=\"*\"&gt;{}&lt;/Select&gt;"
        "&lt;/Query&gt;"
        "&lt;/QueryList&gt;"
    ).format(query_text)

    body_lines: list[str] = []
    body_lines.append("<?xml version=\"1.0\" encoding=\"UTF-8\"?>")
    body_lines.append(
        "<Subscription xmlns=\"{}\">".format(_WEF_NAMESPACE),
    )
    body_lines.append(
        "    <SubscriptionId>{}</SubscriptionId>".format(sub_id_text),
    )
    body_lines.append(
        "    <SubscriptionType>SourceInitiated</SubscriptionType>",
    )
    body_lines.append(
        "    <Description>{}</Description>".format(description_text),
    )
    body_lines.append(
        "    <Enabled>{}</Enabled>".format(enabled_text),
    )
    body_lines.append("    <Uri>{}</Uri>".format(_WEF_LOG_URI))
    body_lines.append(
        "    <ConfigurationMode>Custom</ConfigurationMode>",
    )
    body_lines.append("    <Query>{}</Query>".format(inner_query))
    body_lines.append(
        "    <ReadExistingEvents>false</ReadExistingEvents>",
    )
    body_lines.append(
        "    <TransportName>{}</TransportName>".format(transport),
    )
    body_lines.append(
        "    <LogFile>{}</LogFile>".format(log_file_text),
    )
    body_lines.append(
        "    <ContentFormat>RenderedText</ContentFormat>",
    )
    body_lines.append("</Subscription>")

    rtnval = "\n".join(body_lines) + "\n"
    return rtnval


def _build_write_xml_script(
    *,
    xml_path: str,
    xml_body: str,
    wec_subscription_dir: str,
) -> str:
    """
        Build a PowerShell scriptblock that creates the
        subscription directory (if missing) and writes the XML
        body via ``Set-Content -Encoding UTF8``.

        :param xml_path: Target on-disk XML path.
        :param xml_body: Rendered XML document.
        :param wec_subscription_dir: Parent directory of
                                     ``xml_path``.

        :returns: The scriptblock string.
    """
    escaped_dir = wec_subscription_dir.replace("'", "''")
    escaped_path = xml_path.replace("'", "''")
    escaped_body = xml_body.replace("'", "''")

    script_lines: list[str] = []
    script_lines.append(
        "if (-not (Test-Path -Path '{}')) {{".format(escaped_dir),
    )
    script_lines.append(
        "    New-Item -ItemType Directory -Path '{}' "
        "-Force | Out-Null".format(escaped_dir),
    )
    script_lines.append("}")
    script_lines.append(
        "Set-Content -Path '{}' -Value '{}' "
        "-Encoding UTF8 -NoNewline".format(escaped_path, escaped_body),
    )
    rtnval = "; ".join(script_lines)
    return rtnval


def _build_read_xml_script(*, xml_path: str) -> str:
    """
        Build a PowerShell scriptblock that reads the
        subscription XML.

        :param xml_path: The on-disk XML path.

        :returns: The scriptblock string.
    """
    escaped_path = xml_path.replace("'", "''")
    rtnval = (
        "if (Test-Path -Path '{0}') "
        "{{ Get-Content -Path '{0}' -Raw }} "
        "else {{ exit 1 }}"
    ).format(escaped_path)
    return rtnval


def _build_remove_xml_script(*, xml_path: str) -> str:
    """
        Build a PowerShell scriptblock that deletes the
        on-disk subscription XML.

        :param xml_path: The on-disk XML path.

        :returns: The scriptblock string.
    """
    escaped_path = xml_path.replace("'", "''")
    rtnval = (
        "if (Test-Path -Path '{0}') "
        "{{ Remove-Item -Path '{0}' -Force }}"
    ).format(escaped_path)
    return rtnval


def _build_test_event_script(
    *,
    source: str,
    event_id: int,
    message: str,
) -> str:
    """
        Build the ``Write-EventLog`` scriptblock for
        :meth:`WindowsRemoteLogForwardingManager.test_forwarder`.

        Registers the event source on demand via
        ``New-EventLog`` when the source does not yet exist.

        :param source: Event Log source name.
        :param event_id: Event ID.
        :param message: Event message text.

        :returns: The scriptblock string.
    """
    escaped_source = source.replace("'", "''")
    escaped_message = message.replace("'", "''")
    script_lines: list[str] = []
    script_lines.append(
        "if (-not "
        "[System.Diagnostics.EventLog]::SourceExists('{}')) "
        "{{ New-EventLog -LogName Application -Source '{}' }}".format(
            escaped_source, escaped_source,
        ),
    )
    script_lines.append(
        "Write-EventLog -LogName Application -Source '{}' "
        "-EventId {} -EntryType Information -Message '{}'".format(
            escaped_source, int(event_id), escaped_message,
        ),
    )
    rtnval = "; ".join(script_lines)
    return rtnval


def _parse_wecutil_gs_output(*, text: str) -> tuple:
    """
        Parse the indented key-value output of
        ``wecutil.exe gs <id>`` into a tuple.

        ``wecutil gs`` produces a hierarchical block such as:

        .. code-block::

            Subscription Id: collector1
            SubscriptionType: SourceInitiated
            Enabled: true
            ...
            TransportName: HTTP
            ContentFormat: RenderedText
            Locale: en-US
            ...
            EventSources:
                EventSource[0]:
                    Address: source1.example.com
                    Enabled: true
            ...
            RuntimeStatus:
                LastError: 0
                EventSources:
                    EventSource[0]:
                        LastEventTime: 2024-04-01T00:00:00Z
                        TotalBytes: 12345

        The parser is permissive: it walks every key-equals-value
        / key-colon-value line and extracts the fields it
        recognises.

        :param text: Raw stdout from ``wecutil gs``.

        :returns: A seven-tuple
                  ``(enabled, transport, address, query,
                  last_event_time, error_count, bytes_sent)``.
    """
    enabled = False
    transport = ""
    address = ""
    query = ""
    last_event_time: Optional[str] = None
    error_count: Optional[int] = None
    bytes_sent: Optional[int] = None

    if text is None or len(text) == 0:
        rtnval = (
            enabled, transport, address, query,
            last_event_time, error_count, bytes_sent,
        )
        return rtnval

    saw_enabled = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if ":" not in stripped:
            continue

        key, _, value = stripped.partition(":")
        key_clean = key.strip()
        value_clean = value.strip()

        if not saw_enabled and key_clean.lower() == "enabled":
            enabled = value_clean.lower() == "true"
            saw_enabled = True
            continue
        if key_clean.lower() == "transportname":
            transport = value_clean
            continue
        if (
            key_clean.lower() == "address"
            and len(address) == 0
        ):
            address = value_clean
            continue
        if (
            key_clean.lower() in ("query", "queryxpath")
            and len(query) == 0
        ):
            query = value_clean
            continue
        if key_clean.lower() == "lasteventtime":
            last_event_time = value_clean
            continue
        if key_clean.lower() == "lasterror":
            try:
                error_count = int(value_clean)
            except ValueError:
                error_count = None
            continue
        if key_clean.lower() == "totalbytes":
            try:
                bytes_sent = int(value_clean)
            except ValueError:
                bytes_sent = None
            continue

    rtnval = (
        enabled, transport, address, query,
        last_event_time, error_count, bytes_sent,
    )
    return rtnval
