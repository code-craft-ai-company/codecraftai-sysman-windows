"""
    Windows audit-management feature.

    Implements the cross-OS audit-management contract for Windows
    using ``auditpol`` for audit-policy rule management,
    ``Get-WinEvent`` (PowerShell) for the Security event channel
    queries, and ``wevtutil`` for channel toggling.

    Audit-model divergence:
        Windows audit policy is **subcategory-based**.  Each rule is
        a tuple ``(subcategory, success: bool, failure: bool)`` where
        ``subcategory`` is one of the documented advanced-audit
        subcategory names (``"Logon"``, ``"Object Access"``,
        ``"Process Creation"``, etc.).  ``auditpol /set
        /subcategory:<name> /success:enable|disable
        /failure:enable|disable`` writes one rule; ``auditpol /get
        /category:* /r`` reads the complete policy as a CSV.  This
        is markedly different from Linux's line-based rules and
        macOS's class-based flags — the design document below
        captures the cross-OS concept mapping in detail.

    File I/O policy:
        Every command runs through the injected
        :class:`ICommandRunner`.  This module does NOT consume an
        :class:`IFileOperator`.

    Elevation policy:
        Every write verb (``add_rule``, ``remove_rule``,
        ``enable_auditing``, ``disable_auditing``) honours
        ``config.elevated``.  Reads (``list_rules``,
        ``get_audit_status``, ``search_events``) MUST be passed
        ``elevated=True`` to read the Windows Security channel —
        the contract carves out a documented exception here.  See
        :meth:`WindowsAuditManager.search_events` for the surface
        the manager uses.

    Surface choices:
        * ``add_rule(spec)`` issues ``auditpol /set
          /subcategory:<spec.subcategory> /success:<...>
          /failure:<...>``.  Idempotency probes ``auditpol /get`` and
          skips when the (success, failure) tuple already matches.
        * ``remove_rule(rule_id)`` issues ``auditpol /set
          /subcategory:<rule_id> /success:disable /failure:disable``.
          Idempotency skips when both flags are already disabled.
        * ``list_rules()`` parses the CSV output of
          ``auditpol /get /category:* /r``.
        * ``enable_auditing()`` enables the ``Security`` event log
          channel via ``wevtutil sl Security /e:true`` (and any
          other channels in
          ``WindowsAuditManagerConfig.default_log_channel``).  The
          ``EventLog`` Windows service is always-running so the
          manager does NOT attempt to start it; it returns SUCCESS
          when the channel is enabled, SKIPPED when already enabled.
        * ``disable_auditing()`` is a destructive operation.
          When ``config.allow_destructive`` is False the verb
          returns ``FAILED`` with a descriptive message.  When
          True the verb issues ``auditpol /clear /y`` to reset
          every audit-policy subcategory.
        * ``get_audit_status()`` parses ``auditpol /get /category:*
          /r`` into a single :class:`WindowsAuditStatus` aggregating
          the per-subcategory counts.
        * ``search_events(query)`` issues a
          ``Get-WinEvent -LogName <channel> -FilterHashtable
          @{...}`` PowerShell pipeline against the Security
          channel and parses the JSON output.
"""

import os

from dataclasses import dataclass, field
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_AUDITPOL = "auditpol"
_WEVTUTIL = "wevtutil"

_DEFAULT_POWERSHELL = "powershell.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

_DEFAULT_LOG_CHANNEL = "Security"

_VALID_SETTINGS = (
    "No Auditing",
    "Success",
    "Failure",
    "Success and Failure",
)


WindowsAuditActionStatus = ActionStatus


@dataclass
class WindowsAuditManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsAuditManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.
        :param default_log_channel: Default event-log channel name
                                    used by ``enable_auditing`` and
                                    ``search_events``.  Defaults to
                                    ``"Security"``.
        :param allow_destructive: Master switch for
                                  ``disable_auditing``.  When False
                                  (default), ``disable_auditing``
                                  returns ``FAILED`` rather than
                                  invoking ``auditpol /clear``.
                                  Callers MUST opt in explicitly to
                                  blow away the audit policy.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    default_log_channel: str = _DEFAULT_LOG_CHANNEL
    allow_destructive: bool = False


@dataclass
class WindowsAuditActionResult(FeatureActionResult):
    """
        Result of an audit-management write operation.

        :param rule_id: The rule identifier targeted, when applicable.
                        On Windows this mirrors the subcategory name.
        :param rule_key: The rule key (mirrors ``rule_id``) for
                         contract conformance.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    rule_id: Optional[str] = None
    rule_key: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsAuditRuleSpec:
    """
        Structured input for :meth:`WindowsAuditManager.add_rule` /
        :meth:`WindowsAuditManager.remove_rule`.

        Windows audit policy is keyed by subcategory.  Each rule is
        a triple ``(subcategory, success, failure)`` where the
        boolean fields control whether the corresponding event
        bucket is recorded.

        :param subcategory: The advanced-audit subcategory name
                            (e.g., ``"Logon"``,
                            ``"Object Access"``, ``"Process
                            Creation"``).  Acts as the abstract
                            ``rule_id``.
        :param success: When True, ``auditpol /set
                        /success:enable``.  When False,
                        ``/success:disable``.
        :param failure: When True, ``auditpol /set
                        /failure:enable``.  When False,
                        ``/failure:disable``.
    """
    subcategory: str
    success: bool = True
    failure: bool = True


@dataclass
class WindowsAuditRuleInfo:
    """
        One audit rule observed in ``auditpol /get /category:* /r``.

        :param rule_id: The subcategory name (acts as the rule
                        identifier).
        :param subcategory: Mirrors ``rule_id``.
        :param category: The parent category name (e.g.
                         ``"Logon/Logoff"`` for the ``Logon``
                         subcategory).
        :param success: True when the inclusion setting includes
                        ``"Success"``.
        :param failure: True when the inclusion setting includes
                        ``"Failure"``.
        :param expression: The raw inclusion-setting text from the
                           CSV (e.g. ``"Success and Failure"``).
        :param subsystem: Always ``"subcategory"`` for
                          ``auditpol``-derived rules.
        :param source: Always ``"auditpol"``.
    """
    rule_id: str
    subcategory: str
    category: str
    success: bool
    failure: bool
    expression: str
    subsystem: str = "subcategory"
    source: str = "auditpol"


@dataclass
class WindowsAuditStatus:
    """
        Aggregated audit-policy status.

        :param enabled: True when at least one subcategory's
                        inclusion setting is non-empty (i.e., not
                        ``"No Auditing"``).
        :param running: True (the Windows EventLog service is
                        always-running on healthy systems).  This
                        field is provided for contract conformance.
        :param pid: Always ``None`` on Windows.
        :param failure_mode: Always ``None`` on Windows.
        :param rate_limit: Always ``None`` on Windows.
        :param backlog_limit: Always ``None`` on Windows.
        :param backlog: Always ``None`` on Windows.
        :param lost_events: Always ``None`` on Windows.
        :param locked: Always False on Windows (the audit subsystem
                       has no immutable mode comparable to Linux's
                       ``enabled 2``).
        :param rule_count: Number of subcategories observed in the
                           CSV output.
        :param subcategory_counts: Map of inclusion-setting text to
                                   the number of subcategories that
                                   carry it (e.g., ``{"No Auditing":
                                   12, "Success and Failure": 4}``).
        :param raw: The raw CSV stdout for debuggability.
    """
    enabled: bool
    running: bool
    pid: Optional[int]
    failure_mode: Optional[str]
    rate_limit: Optional[int]
    backlog_limit: Optional[int]
    backlog: Optional[int]
    lost_events: Optional[int]
    locked: bool
    rule_count: int
    subcategory_counts: dict
    raw: str


@dataclass
class WindowsAuditEventQuery:
    """
        Structured query for :meth:`WindowsAuditManager.search_events`.

        :param channel: Event log channel name.  Defaults to
                        ``WindowsAuditManagerConfig.default_log_channel``
                        (``"Security"``) when ``None``.
        :param event_id: When set, filters by ``Id`` in the
                         ``Get-WinEvent -FilterHashtable`` payload.
        :param level: When set, filters by ``Level`` (1=Critical,
                      2=Error, 3=Warning, 4=Info, 5=Verbose).
        :param principal: Reserved for future ``UserId`` filtering.
        :param keyword: Substring filter applied client-side
                        against the decoded message body.
        :param since: When set, emits ``StartTime=<since>`` in the
                      filter hashtable.  Caller must supply a
                      PowerShell-parseable datetime literal (e.g.,
                      ``"2024-04-22T10:00:00"``).
        :param until: When set, emits ``EndTime=<until>``.
        :param raw_filter: When set, replaces the structured
                           hashtable with a verbatim ``-FilterXPath``
                           or ``-FilterHashtable`` expression.
        :param limit: Cap on the number of events returned.
    """
    channel: Optional[str] = None
    event_id: Optional[int] = None
    level: Optional[int] = None
    principal: Optional[str] = None
    keyword: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None
    raw_filter: Optional[str] = None
    limit: Optional[int] = None


@dataclass
class WindowsAuditEvent:
    """
        One parsed Get-WinEvent record.

        :param timestamp: The ``TimeCreated`` field as a string.
        :param event_id: The ``Id`` field.
        :param category: The ``LogName`` field.
        :param severity: The ``LevelDisplayName`` field.
        :param message: The ``Message`` field, possibly truncated
                        to a short summary.
        :param raw: The full Message body.
    """
    timestamp: str
    event_id: Optional[int]
    category: Optional[str]
    severity: Optional[str]
    message: str
    raw: str


class WindowsAuditQueryError(Exception):
    """
        Raised when an audit read fails for an unrecoverable reason.
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

        err_msg_lines = ["Audit query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsAuditImmutableError(Exception):
    """
        Reserved for callers who want a typed exception when a
        write is rejected.  Windows does not expose an immutable
        audit mode, but this type exists for cross-OS conformance.
    """

    def __init__(self, *, detail: str) -> None:
        """
            :param detail: Human-readable description.
        """
        self.detail = detail
        err_msg_lines = ["Audit subsystem is in immutable mode."]
        err_msg_lines.append("    DETAIL: {}".format(detail))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsAuditManager(BaseFeatureManager):
    """
        Windows audit-management manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`.
    """

    result_class = WindowsAuditActionResult
    status_class = WindowsAuditActionStatus
    feature_name = "audit-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_rules(self) -> list:
        """
            Enumerate the audit-policy subcategories via
            ``auditpol /get /category:* /r``.

            :returns: One :class:`WindowsAuditRuleInfo` per
                      subcategory.
            :raises WindowsAuditQueryError: When ``auditpol`` exits
                                            non-zero with non-empty
                                            stderr.
        """
        command = [_AUDITPOL, "/get", "/category:*", "/r"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise WindowsAuditQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_auditpol_csv(text=run_result.stdout)
        return rtnval

    def get_audit_status(self) -> WindowsAuditStatus:
        """
            Aggregate the audit-policy state into a single status
            blob.  Calls ``auditpol /get /category:* /r`` and rolls
            the per-subcategory rows up into counts.

            :returns: The :class:`WindowsAuditStatus`.
            :raises WindowsAuditQueryError: When ``auditpol`` exits
                                            non-zero with non-empty
                                            stderr.
        """
        command = [_AUDITPOL, "/get", "/category:*", "/r"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise WindowsAuditQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rules = _parse_auditpol_csv(text=run_result.stdout)
        counts: dict = {}
        any_enabled = False
        for rule in rules:
            counts[rule.expression] = counts.get(rule.expression, 0) + 1
            if rule.expression != "No Auditing" and len(rule.expression) > 0:
                any_enabled = True

        rtnval = WindowsAuditStatus(
            enabled=any_enabled,
            running=True,
            pid=None,
            failure_mode=None,
            rate_limit=None,
            backlog_limit=None,
            backlog=None,
            lost_events=None,
            locked=False,
            rule_count=len(rules),
            subcategory_counts=counts,
            raw=run_result.stdout,
        )
        return rtnval

    def search_events(
        self,
        query: WindowsAuditEventQuery,
    ) -> list:
        """
            Search the Windows Security event channel via
            ``Get-WinEvent``.

            The contract carves out an exception for Windows: reading
            the Security event log requires elevation.  The manager
            therefore passes ``elevated=True`` for this verb only —
            ``list_rules`` and ``get_audit_status`` continue to call
            with ``elevated=False`` because ``auditpol /get`` does
            not require elevation on a standard install.

            :param query: The :class:`WindowsAuditEventQuery`.

            :returns: One :class:`WindowsAuditEvent` per parsed
                      record, capped at ``query.limit`` when set.
            :raises WindowsAuditQueryError: When PowerShell exits
                                            non-zero with non-empty
                                            stderr.
        """
        if query.channel is not None and len(query.channel) > 0:
            channel_name = query.channel
        else:
            channel_name = self._typed_config().default_log_channel

        script = _build_get_winevent_script(
            channel=channel_name, query=query,
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=True)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise WindowsAuditQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        events = _parse_winevent_json(
            text=run_result.stdout,
            keyword=query.keyword,
            limit=query.limit,
        )
        return events

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_rule(
        self,
        spec: WindowsAuditRuleSpec,
    ) -> WindowsAuditActionResult:
        """
            Add (or modify) an audit-policy rule.

            Dispatches ``auditpol /set /subcategory:<name>
            /success:<enable|disable> /failure:<enable|disable>``.

            Idempotency: probes ``auditpol /get`` and skips when the
            existing inclusion setting matches the desired
            (success, failure) tuple.

            :param spec: The :class:`WindowsAuditRuleSpec`.

            :returns: The :class:`WindowsAuditActionResult`.
        """
        command = _build_auditpol_set(spec=spec)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAuditActionStatus.DRY_RUN,
                rule_id=spec.subcategory,
                rule_key=spec.subcategory,
                command=command,
            )
            return dry

        existing = self._safe_runtime_rules()
        if _spec_already_in_effect(rules=existing, spec=spec):
            skipped = self._build_result(
                status=WindowsAuditActionStatus.SKIPPED,
                rule_id=spec.subcategory,
                rule_key=spec.subcategory,
                command=command,
                message="audit policy already in desired state",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsAuditActionStatus.SUCCESS
        else:
            mapped_status = WindowsAuditActionStatus.FAILED

        rtnval = WindowsAuditActionResult(
            status=mapped_status,
            detail=spec.subcategory,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            rule_id=spec.subcategory,
            rule_key=spec.subcategory,
        )
        return rtnval

    def remove_rule(
        self,
        rule_id: str,
    ) -> WindowsAuditActionResult:
        """
            Remove an audit-policy rule by setting both ``success``
            and ``failure`` to disabled.

            :param rule_id: The subcategory name.

            :returns: The :class:`WindowsAuditActionResult`.
        """
        spec = WindowsAuditRuleSpec(
            subcategory=rule_id, success=False, failure=False,
        )
        command = _build_auditpol_set(spec=spec)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAuditActionStatus.DRY_RUN,
                rule_id=rule_id,
                rule_key=rule_id,
                command=command,
            )
            return dry

        existing = self._safe_runtime_rules()
        if _spec_already_in_effect(rules=existing, spec=spec):
            skipped = self._build_result(
                status=WindowsAuditActionStatus.SKIPPED,
                rule_id=rule_id,
                rule_key=rule_id,
                command=command,
                message="audit policy already disabled for subcategory",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsAuditActionStatus.SUCCESS
        else:
            mapped_status = WindowsAuditActionStatus.FAILED

        rtnval = WindowsAuditActionResult(
            status=mapped_status,
            detail=rule_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            rule_id=rule_id,
            rule_key=rule_id,
        )
        return rtnval

    def enable_auditing(self) -> WindowsAuditActionResult:
        """
            Enable the configured event-log channel.

            Dispatches ``wevtutil sl <default_log_channel>
            /e:true``.  The Windows EventLog service itself is
            always-running and is not restarted.

            Idempotency: probes the channel state via
            ``wevtutil gl <channel>`` and skips when ``enabled:
            true``.

            :returns: The :class:`WindowsAuditActionResult`.
        """
        cfg = self._typed_config()
        command = [_WEVTUTIL, "sl", cfg.default_log_channel, "/e:true"]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAuditActionStatus.DRY_RUN,
                rule_id=None,
                rule_key=None,
                command=command,
            )
            return dry

        if self._channel_already_enabled(channel=cfg.default_log_channel):
            skipped = self._build_result(
                status=WindowsAuditActionStatus.SKIPPED,
                rule_id=None,
                rule_key=None,
                command=command,
                message="event-log channel already enabled",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsAuditActionStatus.SUCCESS
        else:
            mapped_status = WindowsAuditActionStatus.FAILED

        rtnval = WindowsAuditActionResult(
            status=mapped_status,
            detail=cfg.default_log_channel,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            rule_id=None,
            rule_key=None,
        )
        return rtnval

    def disable_auditing(self) -> WindowsAuditActionResult:
        """
            Disable auditing by clearing every audit-policy
            subcategory.

            This is **destructive** — it issues
            ``auditpol /clear /y`` which resets every subcategory
            to ``No Auditing``.  The verb returns ``FAILED`` unless
            ``config.allow_destructive`` is True.

            :returns: The :class:`WindowsAuditActionResult`.
        """
        cfg = self._typed_config()
        command = [_AUDITPOL, "/clear", "/y"]

        if not cfg.allow_destructive:
            failed = self._build_result(
                status=WindowsAuditActionStatus.FAILED,
                rule_id=None,
                rule_key=None,
                command=command,
                message=(
                    "disable_auditing is destructive; set "
                    "config.allow_destructive=True to opt in"
                ),
                stderr="allow_destructive=False",
            )
            return failed

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAuditActionStatus.DRY_RUN,
                rule_id=None,
                rule_key=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsAuditActionStatus.SUCCESS
        else:
            mapped_status = WindowsAuditActionStatus.FAILED

        rtnval = WindowsAuditActionResult(
            status=mapped_status,
            detail="disable",
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            rule_id=None,
            rule_key=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsAuditManagerConfig:
        """
            Return the manager's config typed.

            :returns: The :class:`WindowsAuditManagerConfig`.
        """
        if isinstance(self._config, WindowsAuditManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsAuditManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
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

    def _safe_runtime_rules(self) -> list:
        """
            Return the runtime rule list, swallowing
            :class:`WindowsAuditQueryError` so write paths do not
            propagate read failures.

            :returns: A list of :class:`WindowsAuditRuleInfo`;
                      empty when the read failed.
        """
        try:
            rtnval = self.list_rules()
        except WindowsAuditQueryError:
            rtnval = []
        return rtnval

    def _channel_already_enabled(self, *, channel: str) -> bool:
        """
            Probe whether an event-log channel is enabled via
            ``wevtutil gl <channel>``.

            :param channel: The channel name.

            :returns: True when the ``enabled: true`` line is
                      present in stdout; False otherwise.
        """
        command = [_WEVTUTIL, "gl", channel]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        for raw_line in run_result.stdout.splitlines():
            line = raw_line.strip().lower()
            if line.startswith("enabled:"):
                _, _, value = line.partition(":")
                if value.strip() == "true":
                    rtnval = True
                    return rtnval
                rtnval = False
                return rtnval
        rtnval = False
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsAuditActionStatus,
        rule_id: Optional[str],
        rule_key: Optional[str],
        command: list,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsAuditActionResult:
        """
            Construct a :class:`WindowsAuditActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param rule_id: The rule identifier to echo onto the
                            result.
            :param rule_key: The rule key to echo onto the result.
            :param command: The would-have-been argv.
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`WindowsAuditActionResult`.
        """
        rtnval = WindowsAuditActionResult(
            status=status,
            detail=rule_id,
            command=list(command),
            rule_id=rule_id,
            rule_key=rule_key,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _bool_to_auditpol_flag(*, value: bool) -> str:
    """
        Translate a boolean into ``"enable"`` / ``"disable"`` for
        ``auditpol /set``.

        :param value: The flag value.

        :returns: ``"enable"`` for True; ``"disable"`` for False.
    """
    if value:
        rtnval = "enable"
    else:
        rtnval = "disable"
    return rtnval


def _build_auditpol_set(*, spec: WindowsAuditRuleSpec) -> list:
    """
        Render an ``auditpol /set`` argv from a rule spec.

        :param spec: The :class:`WindowsAuditRuleSpec`.

        :returns: The argv as a list.
    """
    success_token = "/success:{}".format(
        _bool_to_auditpol_flag(value=spec.success),
    )
    failure_token = "/failure:{}".format(
        _bool_to_auditpol_flag(value=spec.failure),
    )
    rtnval = [
        _AUDITPOL, "/set",
        "/subcategory:{}".format(spec.subcategory),
        success_token,
        failure_token,
    ]
    return rtnval


def _split_csv_line(*, line: str) -> list:
    """
        Split a CSV line, honouring double-quoted fields.

        :param line: One line of CSV text.

        :returns: One token per field.
    """
    rtnval: list = []
    current = []
    in_quotes = False
    for ch in line:
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch == "," and not in_quotes:
            rtnval.append("".join(current))
            current = []
            continue
        current.append(ch)
    rtnval.append("".join(current))
    return rtnval


def _normalise_inclusion_setting(*, value: str) -> tuple:
    """
        Translate the ``Inclusion Setting`` text into a
        ``(success, failure)`` tuple.

        :param value: The inclusion setting (e.g. ``"Success and
                      Failure"``, ``"No Auditing"``).

        :returns: A two-tuple of booleans.
    """
    lowered = value.strip().lower()
    if lowered == "no auditing" or len(lowered) == 0:
        rtnval = (False, False)
        return rtnval
    if lowered == "success and failure":
        rtnval = (True, True)
        return rtnval
    if lowered == "success":
        rtnval = (True, False)
        return rtnval
    if lowered == "failure":
        rtnval = (False, True)
        return rtnval
    # Unknown setting; treat both flags as False so that
    # idempotency probes do not over-claim coverage.
    rtnval = (False, False)
    return rtnval


def _parse_auditpol_csv(*, text: str) -> list:
    """
        Parse the CSV stdout of ``auditpol /get /category:* /r``.

        ``auditpol /r`` emits a header row followed by one row per
        subcategory.  The header columns vary slightly across
        Windows builds, but the canonical schema is:

            Machine Name,Policy Target,Subcategory,Subcategory GUID,
            Inclusion Setting,Exclusion Setting

        On builds that emit a top-level "Category" column the
        parser maps that into :class:`WindowsAuditRuleInfo.category`.

        :param text: The CSV stdout.

        :returns: One :class:`WindowsAuditRuleInfo` per subcategory.
    """
    rtnval: list = []
    if len(text.strip()) == 0:
        return rtnval

    lines: list = []
    for raw_line in text.splitlines():
        if len(raw_line.strip()) == 0:
            continue
        lines.append(raw_line)

    if len(lines) == 0:
        return rtnval

    header_tokens = _split_csv_line(line=lines[0])
    column_index: dict = {}
    for index, token in enumerate(header_tokens):
        column_index[token.strip().lower()] = index

    sub_idx = column_index.get("subcategory")
    inclusion_idx = column_index.get("inclusion setting")
    category_idx = column_index.get("category")

    if sub_idx is None or inclusion_idx is None:
        return rtnval

    for raw_line in lines[1:]:
        tokens = _split_csv_line(line=raw_line)
        if len(tokens) <= sub_idx or len(tokens) <= inclusion_idx:
            continue

        subcategory_name = tokens[sub_idx].strip()
        inclusion_text = tokens[inclusion_idx].strip()

        if category_idx is not None and len(tokens) > category_idx:
            category_text = tokens[category_idx].strip()
        else:
            category_text = ""

        success_bool, failure_bool = _normalise_inclusion_setting(
            value=inclusion_text,
        )

        info = WindowsAuditRuleInfo(
            rule_id=subcategory_name,
            subcategory=subcategory_name,
            category=category_text,
            success=success_bool,
            failure=failure_bool,
            expression=inclusion_text,
        )
        rtnval.append(info)
    return rtnval


def _spec_already_in_effect(
    *,
    rules: list,
    spec: WindowsAuditRuleSpec,
) -> bool:
    """
        Idempotency probe.

        :param rules: The list of :class:`WindowsAuditRuleInfo`.
        :param spec: The desired :class:`WindowsAuditRuleSpec`.

        :returns: True when a rule for ``spec.subcategory`` exists
                  whose ``(success, failure)`` tuple matches the
                  spec; False otherwise.
    """
    rtnval = False
    for rule in rules:
        if rule.subcategory != spec.subcategory:
            continue
        if rule.success == spec.success and rule.failure == spec.failure:
            rtnval = True
            return rtnval
        # Subcategory present but state mismatched.
        rtnval = False
        return rtnval
    return rtnval


def _build_get_winevent_script(
    *,
    channel: str,
    query: WindowsAuditEventQuery,
) -> str:
    """
        Build a PowerShell ``Get-WinEvent`` scriptblock from the
        structured query.

        :param channel: Event log channel name.
        :param query: The :class:`WindowsAuditEventQuery`.

        :returns: A scriptblock string.
    """
    if query.raw_filter is not None and len(query.raw_filter) > 0:
        rtnval = (
            "Get-WinEvent -LogName '{channel}' {filter} "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(channel=channel, filter=query.raw_filter)
        return rtnval

    hashtable_parts: list = []
    hashtable_parts.append("LogName='{}'".format(channel))
    if query.event_id is not None:
        hashtable_parts.append("Id={}".format(query.event_id))
    if query.level is not None:
        hashtable_parts.append("Level={}".format(query.level))
    if query.since is not None and len(query.since) > 0:
        hashtable_parts.append("StartTime='{}'".format(query.since))
    if query.until is not None and len(query.until) > 0:
        hashtable_parts.append("EndTime='{}'".format(query.until))

    hashtable_body = ";".join(hashtable_parts)

    if query.limit is not None:
        max_clause = " -MaxEvents {}".format(query.limit)
    else:
        max_clause = ""

    rtnval = (
        "Get-WinEvent -FilterHashtable @{{{hashtable}}}{max_clause} "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(hashtable=hashtable_body, max_clause=max_clause)
    return rtnval


def _parse_winevent_json(
    *,
    text: str,
    keyword: Optional[str],
    limit: Optional[int],
) -> list:
    """
        Parse the JSON output of ``Get-WinEvent | ConvertTo-Json``
        into :class:`WindowsAuditEvent` rows.

        ``ConvertTo-Json`` emits either a single object (for a
        scalar pipeline result) or a JSON array.  This parser
        normalises both shapes via best-effort line-walking that
        does not require the host to ship a JSON dependency: it
        looks for the canonical ``"TimeCreated"``, ``"Id"``,
        ``"LevelDisplayName"``, ``"LogName"``, and ``"Message"``
        keys.

        :param text: The raw JSON stdout.
        :param keyword: Optional substring filter applied
                        client-side against the message body.
        :param limit: Cap on the number of events returned.

        :returns: One :class:`WindowsAuditEvent` per parsed record.
    """
    import json as _json

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

        timestamp = ""
        if "TimeCreated" in record and record["TimeCreated"] is not None:
            timestamp = str(record["TimeCreated"])

        event_id_raw = record.get("Id")
        event_id_value: Optional[int] = None
        if isinstance(event_id_raw, int):
            event_id_value = event_id_raw
        elif isinstance(event_id_raw, str):
            try:
                event_id_value = int(event_id_raw)
            except ValueError:
                event_id_value = None

        category_raw = record.get("LogName")
        if isinstance(category_raw, str):
            category_value: Optional[str] = category_raw
        else:
            category_value = None

        severity_raw = record.get("LevelDisplayName")
        if isinstance(severity_raw, str):
            severity_value: Optional[str] = severity_raw
        else:
            severity_value = None

        message_raw = record.get("Message", "")
        if isinstance(message_raw, str):
            message_value = message_raw
        else:
            message_value = str(message_raw)

        if keyword is not None and len(keyword) > 0:
            if keyword not in message_value:
                continue

        event = WindowsAuditEvent(
            timestamp=timestamp,
            event_id=event_id_value,
            category=category_value,
            severity=severity_value,
            message=message_value[:200],
            raw=message_value,
        )
        rtnval.append(event)
        if limit is not None and len(rtnval) >= limit:
            return rtnval
    return rtnval
