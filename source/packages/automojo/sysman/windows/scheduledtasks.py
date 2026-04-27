"""
    Windows scheduled-tasks feature.

    Implements the cross-OS scheduled-tasks contract for Windows using
    the PowerShell ``ScheduledTasks`` module (``Register-ScheduledTask``,
    ``Unregister-ScheduledTask``, ``Enable-ScheduledTask``,
    ``Disable-ScheduledTask``, ``Start-ScheduledTask``,
    ``Get-ScheduledTask``).

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner` (``powershell.exe`` invocations).  This
        module does NOT consume an :class:`IFileOperator`.

    Owner-scope mapping:
        ``WindowsTaskOwner.SYSTEM`` -> ``Register-ScheduledTask`` with
            ``-Principal (New-ScheduledTaskPrincipal -UserId
            'NT AUTHORITY\\SYSTEM' -RunLevel Highest)``.  Tasks live in
            Task Scheduler at the root path ``\\<name>``.
        ``WindowsTaskOwner.USER`` -> ``Register-ScheduledTask`` with
            ``-Principal (New-ScheduledTaskPrincipal -UserId '<user>'
            -LogonType S4U -RunLevel <run_level>)``.  Requires
            ``target_user`` from the config or the spec.

    Elevation policy:
        SYSTEM-owner writes elevate when ``config.elevated`` is True.
        USER-owner writes never elevate (matching the Ubuntu / macOS
        siblings).  Reads do not elevate.

    Schedule grammar (deliberately limited):
        * ``"daily HH:MM"`` -> ``New-ScheduledTaskTrigger -Daily -At
          "HH:MM"``.
        * ``"weekly <day> HH:MM"`` -> ``New-ScheduledTaskTrigger -Weekly
          -DaysOfWeek <day> -At "HH:MM"``.
        * ``"every <N>min"`` -> ``-Once -At (Get-Date)
          -RepetitionInterval (New-TimeSpan -Minutes <N>)
          -RepetitionDuration ([TimeSpan]::MaxValue)``.
        * ``"every <N>hour"``/``"hours"`` -> same with ``-Hours``.

        Anything else is rejected by :func:`_translate_schedule`, which
        returns an error string surfaced as a ``FAILED`` result with
        ``message="invalid_schedule"`` from
        :meth:`WindowsScheduledTasksManager.create_task` BEFORE the
        runner is invoked.

    Idempotency:
        ``create_task`` skips when the existing task's
        ``Actions[0].Execute`` argv, ``Triggers[0]`` schedule, and
        ``Principal.UserId`` already match the spec.  Other write verbs
        skip when state already matches (``State == "Disabled"`` for
        ``disable_task``, ``State != "Disabled"`` for ``enable_task``,
        absence for ``delete_task``).  ``run_task_now`` never skips.
"""

import json
import os
import re

from dataclasses import dataclass
from enum import Enum
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

# Allowed Windows task names: alphanumeric plus dot, hyphen, underscore.
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

_SYSTEM_PRINCIPAL_USER = "NT AUTHORITY\\SYSTEM"

_RUN_LEVEL_HIGHEST = "Highest"
_RUN_LEVEL_LIMITED = "Limited"

_VALID_RUN_LEVELS = (_RUN_LEVEL_HIGHEST, _RUN_LEVEL_LIMITED)


WindowsScheduledTaskActionStatus = ActionStatus


class WindowsTaskOwner(Enum):
    """
        Owner-scope selector for scheduled-task verbs.

        ``SYSTEM`` -> task registered with ``NT AUTHORITY\\SYSTEM`` as
        the principal.  Requires elevation to register.
        ``USER`` -> task registered for ``target_user`` with ``S4U``
        logon type.  Does not elevate when dispatched by this manager
        (matching the Ubuntu / macOS siblings).
    """
    SYSTEM = "system"
    USER   = "user"


@dataclass
class WindowsScheduledTasksManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsScheduledTasksManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell executable.
                                      Defaults to ``"powershell.exe"``.
        :param target_user: Default username for USER-scope writes.
                            Used when a :class:`WindowsTaskSpec` does
                            not carry its own ``user`` field.  Required
                            for any USER-scope verb when the spec does
                            not override it.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    target_user: Optional[str] = None


@dataclass
class WindowsScheduledTaskActionResult(FeatureActionResult):
    """
        Result of a scheduled-task write operation.

        :param name: Task name.
        :param owner: Owner scope targeted.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    owner: Optional[WindowsTaskOwner] = None
    message: Optional[str] = None


@dataclass
class WindowsTaskSpec:
    """
        Specification for a Windows scheduled task.

        :param name: Task name (becomes the ``-TaskName`` argument).
        :param command: Argument vector executed by the task.  The
                        first element is the executable; remaining
                        elements are joined with single spaces and
                        passed as the ``-Argument`` value to
                        ``New-ScheduledTaskAction``.
        :param schedule: Schedule string in the limited grammar
                         documented at module top.
        :param working_dir: Optional ``-WorkingDirectory`` for
                            ``New-ScheduledTaskAction``.
        :param user: Optional override for the principal user.  When
                     ``owner`` is USER, takes precedence over
                     ``WindowsScheduledTasksManagerConfig.target_user``.
        :param env: Optional environment variables.  Surfaced via
                    ``Set-ScheduledTask`` follow-up only when present
                    (Windows scheduled-task settings do not directly
                    accept env vars on ``New-ScheduledTaskSettingsSet``;
                    we record them on the spec for callers that wish to
                    extend the renderer).
        :param owner: Owner scope selector.
        :param description: Optional ``-Description`` echoed onto the
                            ``Register-ScheduledTask`` invocation.
        :param run_level: ``"Highest"`` (default) or ``"Limited"`` for
                          ``-RunLevel`` on the principal.
    """
    name: str
    command: list[str]
    schedule: str
    working_dir: Optional[str] = None
    user: Optional[str] = None
    env: Optional[dict[str, str]] = None
    owner: WindowsTaskOwner = WindowsTaskOwner.SYSTEM
    description: Optional[str] = None
    run_level: str = _RUN_LEVEL_HIGHEST


@dataclass
class WindowsTaskInfo:
    """
        Current state of a single Windows scheduled task.

        :param name: Task name.
        :param owner: Owner scope inferred from the principal.
        :param enabled: ``True`` when ``Settings.Enabled`` is reported
                        as true / when ``State`` is ``"Ready"`` or
                        ``"Running"``.
        :param state: One of ``"Ready"``, ``"Running"``, ``"Disabled"``,
                      ``"Queued"``, ``"Unknown"``, or any other string
                      reported by ``Get-ScheduledTask``.
        :param last_run: Optional last-run timestamp string.
        :param next_run: Optional next-run timestamp string.
        :param schedule: Echoed back from the spec when known; ``None``
                         for tasks the manager did not create.
        :param executable: ``Actions[0].Execute`` field, when reported.
    """
    name: str
    owner: WindowsTaskOwner
    enabled: bool = False
    state: str = "Unknown"
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    schedule: Optional[str] = None
    executable: Optional[str] = None


class WindowsScheduledTaskQueryError(Exception):
    """
        Raised when a scheduled-task read fails for a reason other
        than the target simply being absent.
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
            err_msg_lines = ["Scheduled-task query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(
                " ".join(self.command),
            ))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers — schedule translation
# ----------------------------------------------------------------------


_VALID_DAYS_OF_WEEK = (
    "Sunday", "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday",
)


def _translate_schedule(*, schedule: str) -> dict:
    """
        Translate a schedule string into a dict describing the
        corresponding ``New-ScheduledTaskTrigger`` arguments.

        Pure function so it can be unit-tested in isolation.

        Returned shape is one of:

            * ``{"kind": "daily", "time": "HH:MM"}``
            * ``{"kind": "weekly", "day": "Monday", "time": "HH:MM"}``
            * ``{"kind": "every", "unit": "Minutes", "amount": <int>}``
            * ``{"kind": "every", "unit": "Hours", "amount": <int>}``

        :param schedule: Schedule string.

        :returns: Translation dict.
        :raises ValueError: When the input does not match the grammar.
    """
    if schedule is None:
        err_msg = "schedule must not be None"
        raise ValueError(err_msg)

    candidate = schedule.strip()
    if len(candidate) == 0:
        err_msg = "schedule must not be empty"
        raise ValueError(err_msg)

    lower = candidate.lower()

    daily_match = re.match(
        r"^daily\s+(\d{1,2}):(\d{1,2})$", lower,
    )
    if daily_match is not None:
        hour = int(daily_match.group(1))
        minute = int(daily_match.group(2))
        if hour < 0 or hour > 23:
            err_msg = "daily hour must be in [0, 23]: {}".format(hour)
            raise ValueError(err_msg)
        if minute < 0 or minute > 59:
            err_msg = "daily minute must be in [0, 59]: {}".format(minute)
            raise ValueError(err_msg)
        time_str = "{:02d}:{:02d}".format(hour, minute)
        rtnval = {"kind": "daily", "time": time_str}
        return rtnval

    weekly_match = re.match(
        r"^weekly\s+([a-zA-Z]+)\s+(\d{1,2}):(\d{1,2})$", lower,
    )
    if weekly_match is not None:
        day_lower = weekly_match.group(1)
        hour = int(weekly_match.group(2))
        minute = int(weekly_match.group(3))
        # Map the lowercase token back to the canonical mixed-case name
        # accepted by ``-DaysOfWeek``.
        day_name: Optional[str] = None
        for canonical in _VALID_DAYS_OF_WEEK:
            if canonical.lower() == day_lower:
                day_name = canonical
                break
        if day_name is None:
            err_msg = "invalid day-of-week token: {!r}".format(day_lower)
            raise ValueError(err_msg)
        if hour < 0 or hour > 23:
            err_msg = "weekly hour must be in [0, 23]: {}".format(hour)
            raise ValueError(err_msg)
        if minute < 0 or minute > 59:
            err_msg = "weekly minute must be in [0, 59]: {}".format(minute)
            raise ValueError(err_msg)
        time_str = "{:02d}:{:02d}".format(hour, minute)
        rtnval = {"kind": "weekly", "day": day_name, "time": time_str}
        return rtnval

    every_match = re.match(
        r"^every\s+(\d+)\s*(min|mins|minute|minutes|hour|hours)$", lower,
    )
    if every_match is not None:
        amount = int(every_match.group(1))
        unit_token = every_match.group(2)
        if amount <= 0:
            err_msg = "interval amount must be positive: {}".format(amount)
            raise ValueError(err_msg)
        if unit_token in ("min", "mins", "minute", "minutes"):
            unit = "Minutes"
        else:
            unit = "Hours"
        rtnval = {"kind": "every", "unit": unit, "amount": amount}
        return rtnval

    err_msg = (
        "unrecognised schedule {!r}; supported grammar: "
        "'daily HH:MM', 'weekly <day> HH:MM', 'every <N>min', "
        "'every <N>hour'"
    ).format(schedule)
    raise ValueError(err_msg)


def _render_trigger_fragment(*, schedule_dict: dict) -> str:
    """
        Render a ``New-ScheduledTaskTrigger`` fragment from a
        translated schedule dict.

        :param schedule_dict: Output of :func:`_translate_schedule`.

        :returns: The PowerShell fragment.
    """
    kind = schedule_dict.get("kind")
    if kind == "daily":
        time_str = schedule_dict["time"]
        rtnval = "New-ScheduledTaskTrigger -Daily -At '{}'".format(time_str)
        return rtnval
    if kind == "weekly":
        time_str = schedule_dict["time"]
        day = schedule_dict["day"]
        rtnval = (
            "New-ScheduledTaskTrigger -Weekly -DaysOfWeek {} -At '{}'"
        ).format(day, time_str)
        return rtnval
    if kind == "every":
        unit = schedule_dict["unit"]
        amount = int(schedule_dict["amount"])
        rtnval = (
            "New-ScheduledTaskTrigger -Once -At (Get-Date) "
            "-RepetitionInterval (New-TimeSpan -{} {}) "
            "-RepetitionDuration ([TimeSpan]::MaxValue)"
        ).format(unit, amount)
        return rtnval

    err_msg = "unhandled schedule kind: {!r}".format(kind)
    raise ValueError(err_msg)


def _render_action_fragment(*, spec: "WindowsTaskSpec") -> str:
    """
        Render a ``New-ScheduledTaskAction`` fragment for ``spec``.

        :param spec: The task spec.

        :returns: The PowerShell fragment.
    """
    if spec.command is None or len(spec.command) == 0:
        err_msg = "spec.command must contain at least one element"
        raise ValueError(err_msg)

    executable = spec.command[0]
    arguments = spec.command[1:]

    escaped_exec = _ps_escape_single_quoted(value=executable)
    fragment = "New-ScheduledTaskAction -Execute '{}'".format(escaped_exec)

    if len(arguments) > 0:
        joined = " ".join(arguments)
        escaped_args = _ps_escape_single_quoted(value=joined)
        fragment = fragment + " -Argument '{}'".format(escaped_args)

    if spec.working_dir is not None and len(spec.working_dir) > 0:
        escaped_wd = _ps_escape_single_quoted(value=spec.working_dir)
        fragment = fragment + " -WorkingDirectory '{}'".format(escaped_wd)

    rtnval = fragment
    return rtnval


def _render_principal_fragment(
    *,
    owner: "WindowsTaskOwner",
    user: Optional[str],
    run_level: str,
) -> str:
    """
        Render a ``New-ScheduledTaskPrincipal`` fragment.

        SYSTEM owner uses ``NT AUTHORITY\\SYSTEM``.  USER owner uses
        the supplied ``user`` with logon type ``S4U`` so the task can
        run when the user is not logged in.

        :param owner: Owner scope.
        :param user: Username to use for USER scope (ignored for SYSTEM).
        :param run_level: ``"Highest"`` or ``"Limited"``.

        :returns: The PowerShell fragment.
        :raises ValueError: When ``owner`` is USER and no ``user`` is
                            supplied, or when ``run_level`` is invalid.
    """
    if run_level not in _VALID_RUN_LEVELS:
        err_msg = (
            "invalid run_level {!r}; expected one of {}"
        ).format(run_level, _VALID_RUN_LEVELS)
        raise ValueError(err_msg)

    if owner == WindowsTaskOwner.SYSTEM:
        rtnval = (
            "New-ScheduledTaskPrincipal -UserId '{}' -RunLevel {}"
        ).format(_SYSTEM_PRINCIPAL_USER, run_level)
        return rtnval

    if owner == WindowsTaskOwner.USER:
        if user is None or len(user) == 0:
            err_msg = (
                "USER-scope tasks require a non-empty user name "
                "(via spec.user or config.target_user)"
            )
            raise ValueError(err_msg)
        escaped_user = _ps_escape_single_quoted(value=user)
        rtnval = (
            "New-ScheduledTaskPrincipal -UserId '{}' -LogonType S4U "
            "-RunLevel {}"
        ).format(escaped_user, run_level)
        return rtnval

    err_msg = "unhandled task owner: {!r}".format(owner)
    raise ValueError(err_msg)


def _render_register_script(
    *,
    spec: "WindowsTaskSpec",
    schedule_dict: dict,
    principal_user: Optional[str],
) -> str:
    """
        Render the multi-step PowerShell scriptblock that registers
        the task.

        :param spec: The task spec.
        :param schedule_dict: Output of :func:`_translate_schedule`.
        :param principal_user: Resolved principal user (for USER
                               scope).  ``None`` for SYSTEM scope.

        :returns: The full PowerShell scriptblock.
    """
    action_fragment = _render_action_fragment(spec=spec)
    trigger_fragment = _render_trigger_fragment(schedule_dict=schedule_dict)
    principal_fragment = _render_principal_fragment(
        owner=spec.owner,
        user=principal_user,
        run_level=spec.run_level,
    )

    escaped_name = _ps_escape_single_quoted(value=spec.name)
    description = spec.description
    if description is not None and len(description) > 0:
        escaped_desc = _ps_escape_single_quoted(value=description)
        description_arg = " -Description '{}'".format(escaped_desc)
    else:
        description_arg = ""

    lines: list[str] = []
    lines.append("$action = {}".format(action_fragment))
    lines.append("$trigger = {}".format(trigger_fragment))
    lines.append("$principal = {}".format(principal_fragment))
    lines.append(
        "$settings = New-ScheduledTaskSettingsSet "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
    )
    lines.append(
        "Register-ScheduledTask -TaskName '{name}' -Action $action "
        "-Trigger $trigger -Principal $principal -Settings $settings "
        "-Force{desc}".format(name=escaped_name, desc=description_arg),
    )
    rtnval = "; ".join(lines)
    return rtnval


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.

        :param value: Raw text.

        :returns: The escaped text.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _ensure_valid_name(*, name: str) -> None:
    """
        Raise ``ValueError`` when ``name`` contains disallowed
        characters.

        :param name: Candidate task name.
        :raises ValueError: When ``name`` does not match the allowed
                            character class.
    """
    if name is None or len(name) == 0:
        err_msg = "task name must be non-empty"
        raise ValueError(err_msg)
    if _VALID_NAME.match(name) is None:
        err_msg = "invalid task name: {!r}".format(name)
        raise ValueError(err_msg)
    return


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _normalize_to_list(*, parsed):
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


def _parse_get_scheduledtask(*, text: str) -> list[dict]:
    """
        Parse the JSON output of ``Get-ScheduledTask | ConvertTo-Json``.

        :param text: Raw stdout text.

        :returns: One dict per parsed task.  Returns an empty list
                  when the payload is absent or unparseable.
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


def _record_owner(*, record: dict) -> WindowsTaskOwner:
    """
        Infer the :class:`WindowsTaskOwner` from a parsed
        ``Get-ScheduledTask`` record.

        ``Principal.UserId == "NT AUTHORITY\\SYSTEM"`` (and equivalent
        well-known SIDs) maps to SYSTEM; everything else maps to USER.

        :param record: Parsed JSON object.

        :returns: The owner scope.
    """
    principal = record.get("Principal")
    if not isinstance(principal, dict):
        rtnval = WindowsTaskOwner.USER
        return rtnval

    user_id_raw = principal.get("UserId")
    if user_id_raw is None:
        rtnval = WindowsTaskOwner.USER
        return rtnval

    user_id_text = str(user_id_raw)
    upper = user_id_text.upper()
    if "SYSTEM" in upper or upper in ("S-1-5-18", "LOCALSYSTEM"):
        rtnval = WindowsTaskOwner.SYSTEM
        return rtnval

    rtnval = WindowsTaskOwner.USER
    return rtnval


def _record_to_taskinfo(
    *,
    record: dict,
    requested_name: Optional[str],
) -> WindowsTaskInfo:
    """
        Convert a parsed ``Get-ScheduledTask`` record into a
        :class:`WindowsTaskInfo`.

        :param record: Parsed JSON object.
        :param requested_name: Task name the caller asked for.  Used
                               when the record's ``TaskName`` field is
                               missing.

        :returns: The :class:`WindowsTaskInfo`.
    """
    name_raw = record.get("TaskName")
    if name_raw is None or len(str(name_raw)) == 0:
        if requested_name is None:
            name = ""
        else:
            name = requested_name
    else:
        name = str(name_raw)

    state_raw = record.get("State")
    if state_raw is None:
        state = "Unknown"
    elif isinstance(state_raw, int):
        state_map = {
            0: "Unknown",
            1: "Disabled",
            2: "Queued",
            3: "Ready",
            4: "Running",
        }
        state = state_map.get(state_raw, "Unknown")
    else:
        state = str(state_raw)

    if state == "Disabled":
        enabled = False
    else:
        # ``Settings.Enabled`` is the canonical truth when the
        # serialised record carries it.
        enabled_raw = None
        settings = record.get("Settings")
        if isinstance(settings, dict):
            enabled_raw = settings.get("Enabled")
        if enabled_raw is None:
            enabled = state in ("Ready", "Running", "Queued")
        else:
            enabled = bool(enabled_raw)

    actions = record.get("Actions")
    executable: Optional[str] = None
    if isinstance(actions, list) and len(actions) > 0:
        first = actions[0]
        if isinstance(first, dict):
            exec_raw = first.get("Execute")
            if exec_raw is not None and len(str(exec_raw)) > 0:
                executable = str(exec_raw)
    elif isinstance(actions, dict):
        exec_raw = actions.get("Execute")
        if exec_raw is not None and len(str(exec_raw)) > 0:
            executable = str(exec_raw)

    last_run_raw = record.get("LastRunTime")
    if last_run_raw is None or len(str(last_run_raw)) == 0:
        last_run: Optional[str] = None
    else:
        last_run = str(last_run_raw)

    next_run_raw = record.get("NextRunTime")
    if next_run_raw is None or len(str(next_run_raw)) == 0:
        next_run: Optional[str] = None
    else:
        next_run = str(next_run_raw)

    owner = _record_owner(record=record)

    rtnval = WindowsTaskInfo(
        name=name,
        owner=owner,
        enabled=enabled,
        state=state,
        last_run=last_run,
        next_run=next_run,
        schedule=None,
        executable=executable,
    )
    return rtnval


def _stderr_looks_not_found(*, stderr: str) -> bool:
    """
        Heuristically detect a ``Get-ScheduledTask`` "not found" stderr.

        :param stderr: Raw stderr text.

        :returns: True when the stderr clearly indicates the task is
                  unknown.
    """
    lower = stderr.lower()
    if "no msft_scheduledtask objects found" in lower:
        rtnval = True
        return rtnval
    if "the system cannot find the file specified" in lower:
        rtnval = True
        return rtnval
    if "objectnotfound" in lower:
        rtnval = True
        return rtnval
    rtnval = False
    return rtnval


# ----------------------------------------------------------------------
# WindowsScheduledTasksManager
# ----------------------------------------------------------------------


class WindowsScheduledTasksManager(BaseFeatureManager):
    """
        Windows scheduled-task manager.  Every operation is dispatched
        through the injected :class:`ICommandRunner`.  SYSTEM-scope
        writes elevate when ``config.elevated`` is True; USER-scope
        writes never elevate (matching Ubuntu / macOS).
    """

    result_class = WindowsScheduledTaskActionResult
    status_class = WindowsScheduledTaskActionStatus
    feature_name = "scheduled-tasks"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def create_task(
        self,
        spec: WindowsTaskSpec,
    ) -> WindowsScheduledTaskActionResult:
        """
            Create or update a scheduled task.

            Composes a multi-step PowerShell scriptblock that builds the
            ``$action``, ``$trigger``, ``$principal``, and ``$settings``
            objects and dispatches ``Register-ScheduledTask -Force``.
            Honours dry-run.

            Idempotent: when the existing task's ``Actions[0].Execute``
            and ``Principal.UserId`` already match the spec, returns
            ``SKIPPED``.

            :param spec: The :class:`WindowsTaskSpec`.

            :returns: The :class:`WindowsScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=spec.name)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsScheduledTaskActionStatus.FAILED,
                name=spec.name,
                owner=spec.owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        try:
            schedule_dict = _translate_schedule(schedule=spec.schedule)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsScheduledTaskActionStatus.FAILED,
                name=spec.name,
                owner=spec.owner,
                command=[],
                message="invalid_schedule",
                stderr=str(exc),
            )
            return failed

        try:
            principal_user = self._resolve_principal_user(spec=spec)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsScheduledTaskActionStatus.FAILED,
                name=spec.name,
                owner=spec.owner,
                command=[],
                message="user_required_for_user_scope",
                stderr=str(exc),
            )
            return failed

        try:
            script = _render_register_script(
                spec=spec,
                schedule_dict=schedule_dict,
                principal_user=principal_user,
            )
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsScheduledTaskActionStatus.FAILED,
                name=spec.name,
                owner=spec.owner,
                command=[],
                message="invalid_spec",
                stderr=str(exc),
            )
            return failed

        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsScheduledTaskActionStatus.DRY_RUN,
                name=spec.name,
                owner=spec.owner,
                command=command,
            )
            return dry

        # Idempotency: skip when the registered task's Actions[0].Execute
        # and Principal.UserId already match the spec.
        existing_record = self._read_task_record(name=spec.name)
        if existing_record is not None:
            if self._record_matches_spec(
                record=existing_record,
                spec=spec,
                principal_user=principal_user,
            ):
                skipped = self._build_result(
                    status=WindowsScheduledTaskActionStatus.SKIPPED,
                    name=spec.name,
                    owner=spec.owner,
                    command=command,
                    message="task already in desired state",
                )
                return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=spec.name,
            owner=spec.owner,
        )
        return rtnval

    def delete_task(
        self,
        name: str,
        owner: WindowsTaskOwner,
    ) -> WindowsScheduledTaskActionResult:
        """
            Delete a scheduled task via ``Unregister-ScheduledTask
            -TaskName <name> -Confirm:$false``.

            Idempotent: when ``Get-ScheduledTask`` reports the task
            absent, returns ``SKIPPED``.

            :param name: Task name.
            :param owner: Owner scope (echoed onto the result; the
                          actual cmdlet is owner-agnostic).

            :returns: The :class:`WindowsScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        escaped = _ps_escape_single_quoted(value=name)
        script = (
            "Unregister-ScheduledTask -TaskName '{}' -Confirm:$false"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=command,
            )
            return dry

        existing_record = self._read_task_record(name=name)
        if existing_record is None:
            skipped = self._build_result(
                status=WindowsScheduledTaskActionStatus.SKIPPED,
                name=name,
                owner=owner,
                command=command,
                message="task does not exist",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            owner=owner,
        )
        return rtnval

    def enable_task(
        self,
        name: str,
        owner: WindowsTaskOwner,
    ) -> WindowsScheduledTaskActionResult:
        """
            Enable a scheduled task via ``Enable-ScheduledTask
            -TaskName <name>``.

            Idempotent: when ``Get-ScheduledTask`` reports the task
            already in a non-disabled state, returns ``SKIPPED``.
            FAILED with ``message="task_not_found"`` when the task
            does not exist.

            :param name: Task name.
            :param owner: Owner scope (echoed onto the result).

            :returns: The :class:`WindowsScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        escaped = _ps_escape_single_quoted(value=name)
        script = "Enable-ScheduledTask -TaskName '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=command,
            )
            return dry

        existing_record = self._read_task_record(name=name)
        if existing_record is None:
            failed = self._build_result(
                status=WindowsScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=command,
                message="task_not_found",
            )
            return failed

        info = _record_to_taskinfo(
            record=existing_record, requested_name=name,
        )
        if info.enabled and info.state != "Disabled":
            skipped = self._build_result(
                status=WindowsScheduledTaskActionStatus.SKIPPED,
                name=name,
                owner=owner,
                command=command,
                message="task is already enabled",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            owner=owner,
        )
        return rtnval

    def disable_task(
        self,
        name: str,
        owner: WindowsTaskOwner,
    ) -> WindowsScheduledTaskActionResult:
        """
            Disable a scheduled task via ``Disable-ScheduledTask
            -TaskName <name>``.

            Idempotent: when ``Get-ScheduledTask`` reports the task
            already in the ``Disabled`` state, returns ``SKIPPED``.
            FAILED with ``message="task_not_found"`` when the task
            does not exist.

            :param name: Task name.
            :param owner: Owner scope (echoed onto the result).

            :returns: The :class:`WindowsScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        escaped = _ps_escape_single_quoted(value=name)
        script = "Disable-ScheduledTask -TaskName '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=command,
            )
            return dry

        existing_record = self._read_task_record(name=name)
        if existing_record is None:
            failed = self._build_result(
                status=WindowsScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=command,
                message="task_not_found",
            )
            return failed

        info = _record_to_taskinfo(
            record=existing_record, requested_name=name,
        )
        if info.state == "Disabled" or not info.enabled:
            skipped = self._build_result(
                status=WindowsScheduledTaskActionStatus.SKIPPED,
                name=name,
                owner=owner,
                command=command,
                message="task is already disabled",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            owner=owner,
        )
        return rtnval

    def run_task_now(
        self,
        name: str,
        owner: WindowsTaskOwner,
    ) -> WindowsScheduledTaskActionResult:
        """
            Run a task immediately via ``Start-ScheduledTask -TaskName
            <name>``.

            No idempotency guard — the contract specifies an
            unconditional run.  Honours dry-run.

            :param name: Task name.
            :param owner: Owner scope (echoed onto the result).

            :returns: The :class:`WindowsScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=WindowsScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        escaped = _ps_escape_single_quoted(value=name)
        script = "Start-ScheduledTask -TaskName '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=command,
            )
            return dry

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            owner=owner,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_task(
        self,
        name: str,
        owner: WindowsTaskOwner,
    ) -> Optional[WindowsTaskInfo]:
        """
            Read state for a single task via ``Get-ScheduledTask
            -TaskName <name> | ConvertTo-Json -Compress -Depth 5``.
            Reads do not elevate.

            :param name: Task name.
            :param owner: Owner scope (echoed onto the result; the
                          inferred owner from ``Principal.UserId`` is
                          NOT validated against this).

            :returns: The :class:`WindowsTaskInfo`, or ``None`` when
                      the task does not exist.
            :raises WindowsScheduledTaskQueryError: When
                                                    ``Get-ScheduledTask``
                                                    fails for an
                                                    unexpected reason.
        """
        _ensure_valid_name(name=name)

        record = self._read_task_record(name=name, raise_on_unknown=True)
        if record is None:
            rtnval: Optional[WindowsTaskInfo] = None
            return rtnval

        info = _record_to_taskinfo(record=record, requested_name=name)
        # The ``owner`` argument is informational; we trust the
        # principal-derived owner inferred from the JSON.
        rtnval = info
        return rtnval

    def list_tasks(self) -> list[WindowsTaskInfo]:
        """
            Enumerate scheduled tasks via ``Get-ScheduledTask |
            ConvertTo-Json -Compress -Depth 5``.  Reads do not elevate.

            :returns: One :class:`WindowsTaskInfo` per registered task.
            :raises WindowsScheduledTaskQueryError: When the cmdlet
                                                    fails.
        """
        script = (
            "Get-ScheduledTask "
            "| Select-Object TaskName, State, Actions, Triggers, "
            "Principal, Settings, LastRunTime, NextRunTime "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsScheduledTaskQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_get_scheduledtask(text=run_result.stdout)
        rtnval: list[WindowsTaskInfo] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            info = _record_to_taskinfo(record=record, requested_name=None)
            rtnval.append(info)
        return rtnval

    def validate_schedule(self, schedule: str) -> bool:
        """
            Pure-Python syntactic validation for a schedule string.
            No runner / file-operator calls.

            :param schedule: Schedule string.

            :returns: True when the input matches the supported
                      grammar; False otherwise.
        """
        try:
            _translate_schedule(schedule=schedule)
        except ValueError:
            rtnval = False
            return rtnval
        rtnval = True
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsScheduledTasksManagerConfig:
        """
            Return the manager's config typed.

            :returns: The typed configuration.
        """
        if isinstance(self._config, WindowsScheduledTasksManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsScheduledTasksManagerConfig(
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

    def _resolve_principal_user(
        self,
        *,
        spec: WindowsTaskSpec,
    ) -> Optional[str]:
        """
            Resolve the principal user for ``spec``.

            For SYSTEM scope, returns ``None`` (the renderer hard-codes
            ``NT AUTHORITY\\SYSTEM``).  For USER scope, returns the
            spec's ``user`` field, falling back to the config's
            ``target_user``.

            :param spec: The task spec.

            :returns: The resolved principal user, or ``None`` for
                      SYSTEM scope.
            :raises ValueError: When USER scope is requested and no
                                user is available.
        """
        if spec.owner == WindowsTaskOwner.SYSTEM:
            rtnval: Optional[str] = None
            return rtnval

        if spec.owner == WindowsTaskOwner.USER:
            if spec.user is not None and len(spec.user) > 0:
                rtnval = spec.user
                return rtnval
            config_user = self._typed_config().target_user
            if config_user is not None and len(config_user) > 0:
                rtnval = config_user
                return rtnval
            err_msg_lines = [
                "USER-scope scheduled tasks require either spec.user or "
                "WindowsScheduledTasksManagerConfig.target_user.",
            ]
            err_msg_lines.append("    spec.user: {!r}".format(spec.user))
            err_msg_lines.append(
                "    config.target_user: {!r}".format(config_user),
            )
            err_msg = os.linesep.join(err_msg_lines)
            raise ValueError(err_msg)

        err_msg = "unhandled task owner: {!r}".format(spec.owner)
        raise ValueError(err_msg)

    def _read_task_record(
        self,
        *,
        name: str,
        raise_on_unknown: bool = False,
    ) -> Optional[dict]:
        """
            Probe ``Get-ScheduledTask -TaskName <name>`` and return the
            parsed JSON record.

            :param name: Task name.
            :param raise_on_unknown: When True, unexpected non-zero
                                     exits raise
                                     :class:`WindowsScheduledTaskQueryError`.
                                     When False (the default for
                                     idempotency probes), unexpected
                                     failures collapse to ``None``.

            :returns: The parsed dict, or ``None`` when the task is
                      absent.
            :raises WindowsScheduledTaskQueryError: When
                                                    ``raise_on_unknown``
                                                    is True and the
                                                    cmdlet fails for an
                                                    unexpected reason.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = (
            "Get-ScheduledTask -TaskName '{}' "
            "| Select-Object TaskName, State, Actions, Triggers, "
            "Principal, Settings, LastRunTime, NextRunTime "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            if _stderr_looks_not_found(stderr=run_result.stderr or ""):
                rtnval: Optional[dict] = None
                return rtnval
            if raise_on_unknown:
                raise WindowsScheduledTaskQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval = None
            return rtnval

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            if raise_on_unknown:
                err_msg_lines = [
                    "Failed to parse Get-ScheduledTask JSON payload.",
                ]
                err_msg_lines.append("    COMMAND: {}".format(
                    " ".join(command),
                ))
                err_msg_lines.append("    STDOUT: {}".format(stripped))
                err_msg = os.linesep.join(err_msg_lines)
                raise WindowsScheduledTaskQueryError(message=err_msg) from None
            rtnval = None
            return rtnval

        if isinstance(parsed, list):
            if len(parsed) == 0:
                rtnval = None
                return rtnval
            first = parsed[0]
            if not isinstance(first, dict):
                rtnval = None
                return rtnval
            rtnval = first
            return rtnval

        if isinstance(parsed, dict):
            rtnval = parsed
            return rtnval

        rtnval = None
        return rtnval

    def _record_matches_spec(
        self,
        *,
        record: dict,
        spec: WindowsTaskSpec,
        principal_user: Optional[str],
    ) -> bool:
        """
            Compare a parsed ``Get-ScheduledTask`` record against a
            spec for the create-task idempotency guard.

            We compare the ``Actions[0].Execute`` argv plus the
            ``Principal.UserId``.  Trigger comparison is limited to
            "the trigger array is non-empty" because PowerShell
            serialises ``Triggers`` as a deeply nested object whose
            shape varies by trigger type; matching exactly would
            require a much larger surface that is not worth the
            maintenance cost for an idempotency hint.

            :param record: Parsed JSON object.
            :param spec: The desired spec.
            :param principal_user: Resolved principal user (or ``None``
                                   for SYSTEM scope).

            :returns: True when the record matches.
        """
        actions = record.get("Actions")
        first_action: Optional[dict] = None
        if isinstance(actions, list) and len(actions) > 0:
            candidate = actions[0]
            if isinstance(candidate, dict):
                first_action = candidate
        elif isinstance(actions, dict):
            first_action = actions

        if first_action is None:
            rtnval = False
            return rtnval

        existing_exec_raw = first_action.get("Execute")
        if existing_exec_raw is None:
            rtnval = False
            return rtnval
        existing_exec = str(existing_exec_raw)

        if spec.command is None or len(spec.command) == 0:
            rtnval = False
            return rtnval
        wanted_exec = spec.command[0]
        if existing_exec != wanted_exec:
            rtnval = False
            return rtnval

        existing_args_raw = first_action.get("Arguments")
        if existing_args_raw is None:
            existing_args = ""
        else:
            existing_args = str(existing_args_raw)

        wanted_args = " ".join(spec.command[1:])
        if existing_args != wanted_args:
            rtnval = False
            return rtnval

        principal = record.get("Principal")
        if not isinstance(principal, dict):
            rtnval = False
            return rtnval

        existing_user_raw = principal.get("UserId")
        if existing_user_raw is None:
            rtnval = False
            return rtnval
        existing_user = str(existing_user_raw)

        if spec.owner == WindowsTaskOwner.SYSTEM:
            wanted_user_upper = _SYSTEM_PRINCIPAL_USER.upper()
            if "SYSTEM" not in existing_user.upper():
                rtnval = False
                return rtnval
        else:
            if principal_user is None:
                rtnval = False
                return rtnval
            if existing_user.lower() != principal_user.lower():
                rtnval = False
                return rtnval

        triggers = record.get("Triggers")
        has_trigger = False
        if isinstance(triggers, list) and len(triggers) > 0:
            has_trigger = True
        elif isinstance(triggers, dict):
            has_trigger = True
        if not has_trigger:
            rtnval = False
            return rtnval

        rtnval = True
        return rtnval

    def _elevation_for_owner(self, *, owner: WindowsTaskOwner) -> bool:
        """
            Decide whether to elevate a write for ``owner``.

            SYSTEM-owner writes inherit ``config.elevated``; USER-owner
            writes never elevate.

            :param owner: Owner scope.

            :returns: True when the runner should elevate.
        """
        if owner == WindowsTaskOwner.SYSTEM:
            rtnval = self._config.elevated
            return rtnval
        rtnval = False
        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        name: str,
        owner: WindowsTaskOwner,
    ) -> WindowsScheduledTaskActionResult:
        """
            Dispatch a write command, applying the owner-based
            elevation policy.

            :param command: Full argv.
            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`WindowsScheduledTaskActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._elevation_for_owner(owner=owner),
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsScheduledTaskActionStatus.SUCCESS
        else:
            mapped_status = WindowsScheduledTaskActionStatus.FAILED

        rtnval = WindowsScheduledTaskActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            owner=owner,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsScheduledTaskActionStatus,
        name: Optional[str],
        owner: Optional[WindowsTaskOwner],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsScheduledTaskActionResult:
        """
            Construct a :class:`WindowsScheduledTaskActionResult`
            without invoking the runner.

            :param status: Outcome status.
            :param name: Task name.
            :param owner: Owner scope.
            :param command: Would-have-been argv (copied for capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`WindowsScheduledTaskActionResult`.
        """
        rtnval = WindowsScheduledTaskActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            owner=owner,
            message=message,
            stderr=stderr,
        )
        return rtnval
