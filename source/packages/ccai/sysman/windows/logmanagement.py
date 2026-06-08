"""
    Windows log-management feature.

    Implements the cross-OS log-management contract for Windows using
    PowerShell's ``Get-WinEvent`` cmdlet for Event Log reads,
    ``wevtutil`` for archive / clear / size-policy commands, and
    ``Clear-EventLog`` / ``Limit-EventLog`` for the classic-channel
    fast-path.

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner` (``powershell.exe`` or ``wevtutil.exe``
        invocations).  This module does NOT consume an
        :class:`IFileOperator` -- archive paths, when produced, are
        derived from the manager's configured ``archive_dir`` and
        passed verbatim to ``wevtutil epl``, which creates the file
        for us.

    Source surface:
        ``name`` is a Windows Event Log channel name -- e.g.,
        ``"Application"``, ``"System"``, or modern channel names such
        as ``"Microsoft-Windows-Security-Auditing/Operational"``.

    Elevation policy:
        Reads never elevate.  Mutating verbs honour
        ``config.elevated``.  The ``Security`` channel is a notable
        elevation exception on real Windows hosts: the operating
        system rejects ``Get-WinEvent -LogName Security`` for non-
        elevated callers.  This manager DOES NOT promote ``Security``
        reads to ``elevated=True`` automatically -- callers running
        against ``Security`` should construct the manager with
        ``config.elevated=True`` and accept that the runner is
        responsible for being launched with an Administrator token
        (mirroring the policy described in ``servicemanagement.py``).
        The ``Security`` channel elevation gap is recorded as a
        follow-up in ``specs/3-log-management/tasks.md``.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so that ``$PROFILE`` cannot mutate
        cmdlet behaviour and PowerShell never blocks waiting for
        interactive input on a runner that has no controlling
        terminal.  Reads use ``| ConvertTo-Json -Compress -Depth 5``
        for a single robust serialization that round-trips across
        PowerShell 5.1 and PowerShell 7+.

    Idempotency guards:
        * ``rotate_log`` and ``truncate_log`` are operational verbs
          and have no idempotency guard.
        * ``configure_rotation`` reads the current channel via
          ``Get-WinEvent -ListLog <name>`` and skips when the
          existing ``MaximumSizeInBytes`` and ``LogMode`` already
          match the requested policy.

    Modern vs Classic channels:
        * Classic channels (``Application``, ``System``, ``Security``,
          ``Setup``) accept ``Clear-EventLog`` and ``Limit-EventLog``.
        * Modern channels (e.g.
          ``Microsoft-Windows-Security-Auditing/Operational``) only
          accept ``wevtutil cl`` / ``wevtutil sl``.
        * This manager dispatches ``Clear-EventLog`` /
          ``Limit-EventLog`` for classic channels and falls back to
          ``wevtutil`` for everything else, classified by the
          presence of a ``/`` or ``-`` token characteristic of modern
          channel paths.
"""

import json
import os

from dataclasses import dataclass
from typing import Iterator, Optional

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

_WEVTUTIL = "wevtutil.exe"

# Default base directory for ``wevtutil epl`` archive output.  Resolved
# server-side by ``wevtutil`` (via the ``%ProgramData%`` environment
# variable) when the path is passed verbatim; the manager also exposes
# the resolved path on ``WindowsLogActionResult.archive_path`` so the
# caller can correlate the archive with the action.
_DEFAULT_ARCHIVE_DIR = r"%ProgramData%\CodeCraftAI\eventlogs"

# Classic Windows event-log names.  These are the only channels that
# accept ``Clear-EventLog`` / ``Limit-EventLog``; modern channels
# require ``wevtutil`` exclusively.
_CLASSIC_CHANNEL_NAMES = (
    "Application",
    "System",
    "Security",
    "Setup",
    "ForwardedEvents",
)


WindowsLogActionStatus = ActionStatus


@dataclass
class WindowsLogManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsLogManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell executable
                                      used to run cmdlets.  Defaults to
                                      ``"powershell.exe"`` (PATH-resolved
                                      Windows PowerShell 5.1).  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
        :param archive_dir: Directory under which ``wevtutil epl``
                            archive files are written by
                            :meth:`WindowsLogManager.rotate_log`.
                            Defaults to
                            ``%ProgramData%\\CodeCraftAI\\eventlogs``.
        :param tail_poll_interval: Seconds between
                                   ``Get-WinEvent -MaxEvents 1`` polls
                                   in :meth:`WindowsLogManager.tail_log`.
                                   Defaults to 1.0.  Reserved for the
                                   live-host integration tests; the
                                   pure-mock unit tests drive ``tail_log``
                                   with ``follow=False``.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    archive_dir: str = _DEFAULT_ARCHIVE_DIR
    tail_poll_interval: float = 1.0


@dataclass
class WindowsLogActionResult(FeatureActionResult):
    """
        Result of a log-management write operation.

        :param name: The Windows Event Log channel name targeted, or
                     ``None`` for verbs that are not channel-scoped.
        :param archive_path: For :meth:`WindowsLogManager.rotate_log`,
                             the path passed to ``wevtutil epl``.
                             ``None`` for other verbs.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    archive_path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsLogEntry:
    """
        One entry parsed from ``Get-WinEvent | ConvertTo-Json``.

        :param time_created: The ``TimeCreated`` field, surfaced as the
                             original ISO-8601 string emitted by
                             ``ConvertTo-Json``.  We keep this as a
                             string (rather than ``datetime``) so that
                             ``tail_log`` and ``get_log`` round-trip
                             losslessly across PowerShell versions
                             whose serializers emit subtly different
                             precision.
        :param level: The numeric ``Level`` field, when reported.
                      ``None`` when absent.
        :param provider_name: The ``ProviderName`` field, when
                              reported.  ``None`` when absent.
        :param id: The numeric ``Id`` field, when reported.  ``None``
                   when absent.
        :param message: The ``Message`` field.  Empty string when
                        absent.
    """
    time_created: str
    level: Optional[int] = None
    provider_name: Optional[str] = None
    id: Optional[int] = None
    message: str = ""


@dataclass
class WindowsLogEntries:
    """
        Result of :meth:`WindowsLogManager.get_log`.

        :param name: The channel queried.
        :param entries: One :class:`WindowsLogEntry` per parsed event.
        :param truncated: True when the parsed entry count equals the
                          requested ``lines`` (callers can re-query
                          with a larger window when this is True).
    """
    name: str
    entries: list
    truncated: bool = False


@dataclass
class WindowsLogInfo:
    """
        One row from ``Get-WinEvent -ListLog * | ConvertTo-Json``.

        :param name: The ``LogName`` field.
        :param log_mode: ``"Circular"``, ``"AutoBackup"``, or
                         ``"Retain"``.  Empty string when absent.
        :param max_size_bytes: The ``MaximumSizeInBytes`` field, when
                               reported.  ``None`` when absent.
        :param record_count: The ``RecordCount`` field, when reported.
                             ``None`` when absent.
    """
    name: str
    log_mode: str = ""
    max_size_bytes: Optional[int] = None
    record_count: Optional[int] = None


@dataclass
class WindowsRotationPolicy:
    """
        Rotation policy applied via ``wevtutil sl``.

        :param size_kb: Maximum log size, in kilobytes.  Multiplied by
                        1024 before being passed to ``wevtutil sl
                        /ms:<bytes>``.  ``None`` leaves the existing
                        size in place.
        :param retain: True selects circular retention
                       (``wevtutil sl /rt:true``); False selects the
                       "archive then overwrite" mode
                       (``wevtutil sl /rt:false``).
    """
    size_kb: Optional[int] = None
    retain: bool = True


class WindowsLogQueryError(Exception):
    """
        Raised when a log-management read fails for an unrecoverable
        reason (non-zero exit + unrecognised stderr from PowerShell, or
        a JSON payload that fails to parse).
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
            err_msg_lines = ["Log-management query command failed."]
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


def _is_classic_channel(*, name: str) -> bool:
    """
        Determine whether ``name`` is a Windows classic event-log
        channel.  Classic channels accept ``Clear-EventLog`` and
        ``Limit-EventLog``; modern channels do not.

        :param name: Channel name.

        :returns: True when ``name`` is a classic channel.
    """
    if name is None or len(name) == 0:
        rtnval = False
        return rtnval

    rtnval = name in _CLASSIC_CHANNEL_NAMES
    return rtnval


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


def _record_to_log_entry(*, record: dict) -> WindowsLogEntry:
    """
        Convert one parsed ``Get-WinEvent`` record into a
        :class:`WindowsLogEntry`.

        :param record: Parsed JSON object for a single event.

        :returns: The :class:`WindowsLogEntry`.
    """
    time_raw = record.get("TimeCreated")
    if time_raw is None:
        time_str = ""
    else:
        time_str = str(time_raw)

    level_raw = record.get("Level")
    if isinstance(level_raw, int):
        level_value: Optional[int] = level_raw
    elif isinstance(level_raw, str) and len(level_raw) > 0:
        try:
            level_value = int(level_raw)
        except ValueError:
            level_value = None
    else:
        level_value = None

    provider_raw = record.get("ProviderName")
    if provider_raw is None or len(str(provider_raw)) == 0:
        provider_value: Optional[str] = None
    else:
        provider_value = str(provider_raw)

    id_raw = record.get("Id")
    if isinstance(id_raw, int):
        id_value: Optional[int] = id_raw
    elif isinstance(id_raw, str) and len(id_raw) > 0:
        try:
            id_value = int(id_raw)
        except ValueError:
            id_value = None
    else:
        id_value = None

    message_raw = record.get("Message")
    if message_raw is None:
        message_value = ""
    else:
        message_value = str(message_raw)

    rtnval = WindowsLogEntry(
        time_created=time_str,
        level=level_value,
        provider_name=provider_value,
        id=id_value,
        message=message_value,
    )
    return rtnval


def _record_to_log_info(*, record: dict) -> Optional[WindowsLogInfo]:
    """
        Convert one parsed ``Get-WinEvent -ListLog`` record into a
        :class:`WindowsLogInfo`.

        :param record: Parsed JSON object.

        :returns: The :class:`WindowsLogInfo`, or None when the record
                  has no ``LogName`` field.
    """
    name_raw = record.get("LogName")
    if name_raw is None or len(str(name_raw)) == 0:
        rtnval: Optional[WindowsLogInfo] = None
        return rtnval

    log_mode_raw = record.get("LogMode")
    if log_mode_raw is None:
        log_mode_value = ""
    elif isinstance(log_mode_raw, int):
        log_mode_map = {0: "Circular", 1: "AutoBackup", 2: "Retain"}
        log_mode_value = log_mode_map.get(log_mode_raw, "")
    else:
        log_mode_value = str(log_mode_raw)

    max_size_raw = record.get("MaximumSizeInBytes")
    if isinstance(max_size_raw, int):
        max_size_value: Optional[int] = max_size_raw
    elif isinstance(max_size_raw, str) and len(max_size_raw) > 0:
        try:
            max_size_value = int(max_size_raw)
        except ValueError:
            max_size_value = None
    else:
        max_size_value = None

    record_count_raw = record.get("RecordCount")
    if isinstance(record_count_raw, int):
        record_count_value: Optional[int] = record_count_raw
    elif isinstance(record_count_raw, str) and len(record_count_raw) > 0:
        try:
            record_count_value = int(record_count_raw)
        except ValueError:
            record_count_value = None
    else:
        record_count_value = None

    rtnval = WindowsLogInfo(
        name=str(name_raw),
        log_mode=log_mode_value,
        max_size_bytes=max_size_value,
        record_count=record_count_value,
    )
    return rtnval


def _build_get_log_script(
    *,
    name: str,
    lines: int,
    since: Optional[str],
) -> str:
    """
        Build the ``Get-WinEvent`` scriptblock for
        :meth:`WindowsLogManager.get_log`.

        :param name: Channel name.
        :param lines: Maximum number of events to fetch.
        :param since: Optional ISO-8601 timestamp passed via
                      ``-FilterHashtable @{StartTime=...}``.  When
                      ``None``, the simpler ``-LogName <name>
                      -MaxEvents <N>`` form is used.

        :returns: The PowerShell scriptblock.
    """
    escaped_name = _ps_escape_single_quoted(value=name)
    if since is None or len(since) == 0:
        rtnval = (
            "Get-WinEvent -LogName '{name}' -MaxEvents {lines} "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(name=escaped_name, lines=int(lines))
        return rtnval

    escaped_since = _ps_escape_single_quoted(value=since)
    rtnval = (
        "Get-WinEvent -FilterHashtable "
        "@{{LogName='{name}';StartTime=[datetime]'{since}'}} "
        "-MaxEvents {lines} "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(name=escaped_name, since=escaped_since, lines=int(lines))
    return rtnval


def _build_list_logs_script() -> str:
    """
        Build the scriptblock for :meth:`WindowsLogManager.list_logs`.

        :returns: The PowerShell scriptblock.
    """
    rtnval = (
        "Get-WinEvent -ListLog * "
        "| Select-Object LogName, LogMode, MaximumSizeInBytes, "
        "RecordCount "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    return rtnval


def _build_listlog_one_script(*, name: str) -> str:
    """
        Build the per-channel ``Get-WinEvent -ListLog <name>`` probe
        used by ``configure_rotation`` for idempotency.

        :param name: Channel name.

        :returns: The PowerShell scriptblock.
    """
    escaped = _ps_escape_single_quoted(value=name)
    rtnval = (
        "Get-WinEvent -ListLog '{}' "
        "| Select-Object LogName, LogMode, MaximumSizeInBytes, "
        "RecordCount "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(escaped)
    return rtnval


def _stderr_looks_no_events(*, stderr: str) -> bool:
    """
        Heuristically detect a ``Get-WinEvent`` "no events" stderr.

        :param stderr: Raw stderr text.

        :returns: True when the stderr clearly indicates the channel
                  is empty (rather than missing).
    """
    if stderr is None:
        rtnval = False
        return rtnval
    lower = stderr.lower()
    if "no events were found" in lower:
        rtnval = True
        return rtnval
    if "noeventsfound" in lower.replace(" ", ""):
        rtnval = True
        return rtnval
    rtnval = False
    return rtnval


# ----------------------------------------------------------------------
# WindowsLogManager
# ----------------------------------------------------------------------


class WindowsLogManager(BaseFeatureManager):
    """
        Windows Event Log manager.  Every operation is dispatched
        through the injected :class:`ICommandRunner`.  Reads do not
        elevate; mutating verbs honour ``config.elevated``.
    """

    result_class = WindowsLogActionResult
    status_class = WindowsLogActionStatus
    feature_name = "log-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_log(
        self,
        name: str,
        lines: int = 100,
        since: Optional[str] = None,
    ) -> WindowsLogEntries:
        """
            Read up to ``lines`` recent events from a channel.

            Runs ``Get-WinEvent -LogName <name> -MaxEvents <lines>``
            (or the ``-FilterHashtable`` form when ``since`` is
            supplied) and parses the resulting ``ConvertTo-Json``
            payload.  Reads do not elevate.  An empty channel (stderr
            "No events were found") yields an empty
            :class:`WindowsLogEntries` rather than raising.

            :param name: The Windows Event Log channel name.
            :param lines: Maximum number of events to fetch.  Defaults
                          to 100.
            :param since: Optional ISO-8601 timestamp filter applied
                          via ``-FilterHashtable``.

            :returns: The :class:`WindowsLogEntries`.
            :raises WindowsLogQueryError: On non-zero ``Get-WinEvent``
                                          exits that are not
                                          recognisably "no events"
                                          stderrs, or when the JSON
                                          payload fails to parse.
        """
        script = _build_get_log_script(
            name=name, lines=lines, since=since,
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            if _stderr_looks_no_events(stderr=run_result.stderr or ""):
                rtnval = WindowsLogEntries(
                    name=name, entries=[], truncated=False,
                )
                return rtnval
            raise WindowsLogQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        stripped = (run_result.stdout or "").strip()
        if len(stripped) == 0:
            rtnval = WindowsLogEntries(
                name=name, entries=[], truncated=False,
            )
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError) as parse_error:
            err_msg_lines = ["Failed to parse Get-WinEvent JSON payload."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    STDOUT: {}".format(stripped))
            err_msg_lines.append("    PARSE_ERROR: {}".format(parse_error))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsLogQueryError(message=err_msg) from None

        records = _normalize_to_list(parsed=parsed)
        entries: list = []
        for record in records:
            if not isinstance(record, dict):
                continue
            entry = _record_to_log_entry(record=record)
            entries.append(entry)

        truncated_flag = len(entries) >= int(lines)
        rtnval = WindowsLogEntries(
            name=name, entries=entries, truncated=truncated_flag,
        )
        return rtnval

    def tail_log(
        self,
        name: str,
        follow: bool = True,
        max_polls: Optional[int] = None,
    ) -> Iterator[WindowsLogEntry]:
        """
            Yield events from a channel as they appear.

            Implementation note: Windows Event Log has no native
            streaming API exposed through PowerShell.  This method
            therefore implements ``tail_log`` as a polling loop that
            issues ``Get-WinEvent -LogName <name> -MaxEvents 1`` and
            yields the result each iteration.  Callers must accept
            that this is a polling-vs-streaming approximation: events
            occurring more rapidly than the poll interval can be
            coalesced into a single most-recent record.

            :param name: The Windows Event Log channel name.
            :param follow: When True, the iterator polls indefinitely.
                           When False, the iterator yields the single
                           most-recent event (if any) and terminates.
            :param max_polls: Optional safety bound on the number of
                              poll iterations.  Reserved for unit
                              tests that drive the iterator with a
                              live runner; production callers leave
                              this as ``None``.

            :returns: An iterator of :class:`WindowsLogEntry`.
            :raises WindowsLogQueryError: On the first failed poll.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = (
            "Get-WinEvent -LogName '{}' -MaxEvents 1 "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        poll_count = 0
        while True:
            run_result = self._runner.run(list(command), elevated=False)

            if run_result.exit_code != 0:
                if _stderr_looks_no_events(stderr=run_result.stderr or ""):
                    if not follow:
                        return
                    poll_count = poll_count + 1
                    if max_polls is not None and poll_count >= max_polls:
                        return
                    continue
                raise WindowsLogQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )

            stripped = (run_result.stdout or "").strip()
            if len(stripped) > 0:
                try:
                    parsed = json.loads(stripped)
                except (ValueError, TypeError):
                    parsed = None
                if parsed is not None:
                    records = _normalize_to_list(parsed=parsed)
                    for record in records:
                        if not isinstance(record, dict):
                            continue
                        yield _record_to_log_entry(record=record)

            if not follow:
                return

            poll_count = poll_count + 1
            if max_polls is not None and poll_count >= max_polls:
                return

    def list_logs(self) -> list:
        """
            Enumerate all event-log channels on the host.

            Runs ``Get-WinEvent -ListLog * | ConvertTo-Json -Compress
            -Depth 5``.  Reads do not elevate.

            :returns: A list of :class:`WindowsLogInfo`.
            :raises WindowsLogQueryError: On non-zero exit or
                                          unparseable JSON.
        """
        script = _build_list_logs_script()
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsLogQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        stripped = (run_result.stdout or "").strip()
        if len(stripped) == 0:
            rtnval: list = []
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError) as parse_error:
            err_msg_lines = [
                "Failed to parse Get-WinEvent -ListLog JSON payload.",
            ]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    STDOUT: {}".format(stripped))
            err_msg_lines.append("    PARSE_ERROR: {}".format(parse_error))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsLogQueryError(message=err_msg) from None

        records = _normalize_to_list(parsed=parsed)
        rtnval = []
        for record in records:
            if not isinstance(record, dict):
                continue
            info = _record_to_log_info(record=record)
            if info is not None:
                rtnval.append(info)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def rotate_log(
        self,
        name: str,
        archive_path: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> WindowsLogActionResult:
        """
            Archive a channel via ``wevtutil epl <name> "<archive>"
            /q:"*"`` and then clear it via ``wevtutil cl <name>``.
            No idempotency guard -- rotation is operational.  Honours
            dry-run.

            :param name: The Windows Event Log channel name.
            :param archive_path: Optional override for the archive
                                 file path.  When ``None``, the path
                                 is computed as ``<archive_dir>\\<name
                                 with slashes replaced>-<timestamp>.evtx``.
            :param timestamp: Optional pre-formatted timestamp token
                              used in the default archive path.  When
                              ``None``, a placeholder ``rotated``
                              token is used so the call site remains
                              deterministic in tests; production
                              callers should supply a real timestamp
                              (e.g.
                              ``datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")``).

            :returns: The :class:`WindowsLogActionResult`.  Carries
                      the archive path on ``archive_path`` for both
                      SUCCESS and DRY_RUN.
        """
        resolved_archive = self._resolve_archive_path(
            name=name,
            archive_path=archive_path,
            timestamp=timestamp,
        )

        epl_command = self._build_wevtutil_argv(
            args=["epl", name, resolved_archive, "/q:*"],
        )
        cl_command = self._build_wevtutil_argv(args=["cl", name])

        if self._config.dry_run:
            joined = epl_command + ["&&"] + cl_command
            dry = self._build_result(
                status=WindowsLogActionStatus.DRY_RUN,
                name=name,
                command=joined,
                archive_path=resolved_archive,
            )
            return dry

        epl_result = self._runner.run(
            list(epl_command),
            elevated=self._config.elevated,
        )
        if epl_result.exit_code != 0:
            failed = WindowsLogActionResult(
                status=WindowsLogActionStatus.FAILED,
                detail=name,
                exit_code=epl_result.exit_code,
                stdout=epl_result.stdout,
                stderr=epl_result.stderr,
                command=list(epl_command),
                name=name,
                archive_path=resolved_archive,
                message="wevtutil_epl_failed",
            )
            return failed

        cl_result = self._runner.run(
            list(cl_command),
            elevated=self._config.elevated,
        )
        if cl_result.exit_code != 0:
            failed = WindowsLogActionResult(
                status=WindowsLogActionStatus.FAILED,
                detail=name,
                exit_code=cl_result.exit_code,
                stdout=cl_result.stdout,
                stderr=cl_result.stderr,
                command=list(cl_command),
                name=name,
                archive_path=resolved_archive,
                message="wevtutil_cl_failed",
            )
            return failed

        rtnval = WindowsLogActionResult(
            status=WindowsLogActionStatus.SUCCESS,
            detail=name,
            exit_code=cl_result.exit_code,
            stdout=cl_result.stdout,
            stderr=cl_result.stderr,
            command=list(cl_command),
            name=name,
            archive_path=resolved_archive,
        )
        return rtnval

    def truncate_log(self, name: str) -> WindowsLogActionResult:
        """
            Clear a channel.  Classic channels (``Application``,
            ``System``, ``Security``, ``Setup``, ``ForwardedEvents``)
            dispatch ``Clear-EventLog -LogName <name>``; modern
            channels dispatch ``wevtutil cl <name>``.  No idempotency
            guard.  Honours dry-run.

            :param name: The Windows Event Log channel name.

            :returns: The :class:`WindowsLogActionResult`.
        """
        if _is_classic_channel(name=name):
            escaped = _ps_escape_single_quoted(value=name)
            script = "Clear-EventLog -LogName '{}'".format(escaped)
            command = self._build_powershell_argv(script=script)
        else:
            command = self._build_wevtutil_argv(args=["cl", name])

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsLogActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation="truncate_log",
            name=name,
        )
        return rtnval

    def configure_rotation(
        self,
        name: str,
        policy: WindowsRotationPolicy,
    ) -> WindowsLogActionResult:
        """
            Apply a rotation policy.  For classic channels the manager
            uses ``Limit-EventLog -LogName <name> -MaximumSize
            <size_kb>``; for modern channels (and as a unified
            fallback when the caller specifies ``retain``), the
            manager uses ``wevtutil sl <name> /ms:<bytes> /rt:<bool>``.

            Idempotent: when the existing channel's
            ``MaximumSizeInBytes`` and ``LogMode`` already match the
            requested policy, returns ``SKIPPED``.  Honours dry-run.

            :param name: The Windows Event Log channel name.
            :param policy: The :class:`WindowsRotationPolicy`.

            :returns: The :class:`WindowsLogActionResult`.
        """
        if policy is None:
            failed = self._build_result(
                status=WindowsLogActionStatus.FAILED,
                name=name,
                command=[],
                message="policy_required",
            )
            return failed

        size_kb = policy.size_kb
        if size_kb is None:
            failed = self._build_result(
                status=WindowsLogActionStatus.FAILED,
                name=name,
                command=[],
                message="size_kb_required",
            )
            return failed
        if int(size_kb) <= 0:
            failed = self._build_result(
                status=WindowsLogActionStatus.FAILED,
                name=name,
                command=[],
                message="size_kb_must_be_positive",
            )
            return failed

        size_bytes = int(size_kb) * 1024
        if policy.retain:
            rt_token = "true"
        else:
            rt_token = "false"

        command = self._build_wevtutil_argv(
            args=[
                "sl",
                name,
                "/ms:{}".format(size_bytes),
                "/rt:{}".format(rt_token),
            ],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsLogActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        existing_info = self._read_log_info(name=name)
        if existing_info is not None:
            if self._info_matches_policy(
                info=existing_info, policy=policy,
            ):
                skipped = self._build_result(
                    status=WindowsLogActionStatus.SKIPPED,
                    name=name,
                    command=command,
                    message="rotation policy already in desired state",
                )
                return skipped

        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation="configure_rotation",
            name=name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsLogManagerConfig:
        """
            Return the manager's config typed.

            :returns: The :class:`WindowsLogManagerConfig`.
        """
        if isinstance(self._config, WindowsLogManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsLogManagerConfig(
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

    def _build_wevtutil_argv(self, *, args: list[str]) -> list[str]:
        """
            Build a ``wevtutil.exe`` argv with the supplied arguments.

            :param args: The wevtutil arguments (verb plus operands).

            :returns: The full argv list.
        """
        rtnval = [_WEVTUTIL]
        for arg in args:
            rtnval.append(str(arg))
        return rtnval

    def _resolve_archive_path(
        self,
        *,
        name: str,
        archive_path: Optional[str],
        timestamp: Optional[str],
    ) -> str:
        """
            Compute the archive path for :meth:`rotate_log`.

            :param name: Channel name.
            :param archive_path: Optional caller override.
            :param timestamp: Optional caller-supplied timestamp token.

            :returns: The archive path string.
        """
        if archive_path is not None and len(archive_path) > 0:
            rtnval = archive_path
            return rtnval

        archive_dir = self._typed_config().archive_dir
        # Channel names can contain ``/`` (modern channels) which is
        # not a valid filename character; replace with ``-``.
        safe_name = name.replace("/", "-").replace("\\", "-")

        if timestamp is None or len(timestamp) == 0:
            stamp = "rotated"
        else:
            stamp = timestamp

        rtnval = "{dir}\\{name}-{stamp}.evtx".format(
            dir=archive_dir, name=safe_name, stamp=stamp,
        )
        return rtnval

    def _read_log_info(
        self,
        *,
        name: str,
    ) -> Optional[WindowsLogInfo]:
        """
            Probe ``Get-WinEvent -ListLog <name>`` and return the
            parsed :class:`WindowsLogInfo` (or None on miss).  Used by
            ``configure_rotation`` for idempotency.

            :param name: Channel name.

            :returns: The parsed info, or ``None`` when the channel
                      cannot be read or the payload is empty.
        """
        script = _build_listlog_one_script(name=name)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[WindowsLogInfo] = None
            return rtnval

        stripped = (run_result.stdout or "").strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            rtnval = None
            return rtnval

        records = _normalize_to_list(parsed=parsed)
        if len(records) == 0:
            rtnval = None
            return rtnval

        first = records[0]
        if not isinstance(first, dict):
            rtnval = None
            return rtnval

        rtnval = _record_to_log_info(record=first)
        return rtnval

    def _info_matches_policy(
        self,
        *,
        info: WindowsLogInfo,
        policy: WindowsRotationPolicy,
    ) -> bool:
        """
            Compare an existing :class:`WindowsLogInfo` against a
            requested :class:`WindowsRotationPolicy`.

            Match criteria:
                * ``info.max_size_bytes == policy.size_kb * 1024``
                * ``info.log_mode == "Circular"`` when
                  ``policy.retain`` is True; one of ``"AutoBackup"``
                  / ``"Retain"`` otherwise.

            :param info: Parsed channel info.
            :param policy: Requested policy.

            :returns: True when the existing channel already matches.
        """
        if policy.size_kb is None:
            rtnval = False
            return rtnval

        wanted_bytes = int(policy.size_kb) * 1024
        if info.max_size_bytes is None:
            rtnval = False
            return rtnval
        if info.max_size_bytes != wanted_bytes:
            rtnval = False
            return rtnval

        existing_mode = info.log_mode or ""
        if policy.retain:
            if existing_mode != "Circular":
                rtnval = False
                return rtnval
        else:
            if existing_mode not in ("AutoBackup", "Retain"):
                rtnval = False
                return rtnval

        rtnval = True
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsLogActionStatus,
        name: Optional[str],
        command: list[str],
        archive_path: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsLogActionResult:
        """
            Construct a :class:`WindowsLogActionResult` without
            invoking the runner.

            :param status: Outcome status.
            :param name: Channel name.
            :param command: Would-have-been argv (copied for capture).
            :param archive_path: Optional archive path.
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr.

            :returns: The :class:`WindowsLogActionResult`.
        """
        rtnval = WindowsLogActionResult(
            status=status,
            detail=name,
            command=list(command),
            stderr=stderr,
            name=name,
            archive_path=archive_path,
            message=message,
        )
        return rtnval
