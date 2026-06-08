"""
    Windows user-group-management feature.

    Implements the cross-OS user-group-management contract for Windows
    using the PowerShell ``Microsoft.PowerShell.LocalAccounts`` module
    (``New-LocalUser``, ``Set-LocalUser``, ``Rename-LocalUser``,
    ``Remove-LocalUser``, ``Disable-LocalUser`` / ``Enable-LocalUser``,
    ``New-LocalGroup``, ``Remove-LocalGroup``, ``Add-LocalGroupMember`` /
    ``Remove-LocalGroupMember``, ``Get-LocalUser``, ``Get-LocalGroup``,
    ``Get-LocalGroupMember``).  Every verb is dispatched through the
    injected :class:`ICommandRunner` so the manager works identically
    under local and SSH execution.

    File I/O policy:
        Account state lives in the Windows SAM (Security Accounts
        Manager); reads and writes go through ``LocalAccounts`` cmdlets.
        This module does NOT consume an :class:`IFileOperator`; every
        read / write / probe flows through the runner so dry-run,
        elevation, and aspect composition compose uniformly.

    Elevation policy:
        Every write verb (``create_user``, ``delete_user``,
        ``modify_user``, ``set_password``, ``lock_account``,
        ``unlock_account``, ``create_group``, ``delete_group``,
        ``add_to_group``, ``remove_from_group``) honours
        ``config.elevated``.  Reads (``get_user``, ``list_users``,
        ``list_groups``, idempotency probes) never elevate.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so that ``$PROFILE`` cannot mutate
        cmdlet behaviour and PowerShell never blocks waiting for
        interactive input on a runner that has no controlling terminal.

    Reads use ``| ConvertTo-Json -Compress -Depth 5`` for a single
    robust serialization that round-trips across PowerShell 5.1 and
    PowerShell 7+.

    Password handling:
        Plaintext passwords appear inside the PowerShell scriptblock
        passed to ``-Command`` (wrapped in
        ``ConvertTo-SecureString -AsPlainText -Force '<pw>'``).  The
        argv recorded on the result is sanitised: every
        ``ConvertTo-SecureString -AsPlainText -Force '...'`` literal
        password value is replaced with the literal ``"<redacted>"``
        before the argv is captured on
        :class:`WindowsUserGroupActionResult`, so the plaintext password
        never leaks into audit captures.  This mirrors macOS's
        :class:`MacosUserGroupManager` redaction contract.
"""

import json
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

# Sentinel placed into the recorded argv anywhere a password value
# would otherwise appear.
_REDACTED = "<redacted>"

# Pattern used by :func:`_sanitise_script_passwords` to locate
# ``ConvertTo-SecureString -AsPlainText -Force '<pw>'`` invocations
# inside a PowerShell scriptblock so the embedded plaintext password
# can be replaced with the redaction sentinel before the argv is
# recorded on a result.
_SECURE_STRING_PATTERN = re.compile(
    r"ConvertTo-SecureString\s+-AsPlainText\s+-Force\s+'((?:[^']|'')*)'",
)


WindowsUserGroupActionStatus = ActionStatus


class WindowsPrincipalKind(Enum):
    """
        Principal kind echoed onto every
        :class:`WindowsUserGroupActionResult`.
    """
    USER = "user"
    GROUP = "group"


@dataclass
class WindowsUserGroupManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsUserGroupManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell executable
                                      used to run cmdlets.  Defaults to
                                      ``"powershell.exe"`` (PATH-resolved
                                      Windows PowerShell 5.1).  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL


@dataclass
class WindowsUserGroupActionResult(FeatureActionResult):
    """
        Result of a user-or-group write operation.

        :param name: The user or group name targeted.  ``None`` for
                     verbs that are not name-scoped.
        :param kind: Whether the principal is a user or a group.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    kind: Optional[WindowsPrincipalKind] = None
    message: Optional[str] = None


@dataclass
class WindowsUserSpec:
    """
        Specification for creating a local user via ``New-LocalUser``.

        :param name: Login name.
        :param password: Optional initial plaintext password.  When set,
                         a ``ConvertTo-SecureString -AsPlainText -Force``
                         is emitted to wrap the value as a SecureString
                         before passing it to ``New-LocalUser
                         -Password``.  When None, ``-NoPassword`` is
                         emitted (Windows requires either ``-Password``
                         or ``-NoPassword``).
        :param full_name: Optional ``-FullName`` value.
        :param description: Optional ``-Description`` value.
        :param account_never_expires: When True, ``-AccountNeverExpires``
                                      is appended.
        :param password_never_expires: When True,
                                       ``-PasswordNeverExpires`` is
                                       appended.
        :param groups: Optional supplementary groups to add the user to
                       via ``Add-LocalGroupMember`` after creation.
    """
    name: str
    password: Optional[str] = None
    full_name: Optional[str] = None
    description: Optional[str] = None
    account_never_expires: bool = False
    password_never_expires: bool = False
    groups: Optional[list[str]] = None


@dataclass
class WindowsUserChanges:
    """
        Specification for modifying an existing local user account.
        Every field is optional; only fields that are not ``None`` (or
        not False for the boolean toggles, when paired with current
        observed state) cause a write to be emitted.

        :param new_name: New login name (handled via
                         ``Rename-LocalUser``).
        :param full_name: New ``-FullName`` value (``Set-LocalUser``).
        :param description: New ``-Description`` value
                            (``Set-LocalUser``).
        :param password: New plaintext password.  When set, a
                         ``ConvertTo-SecureString -AsPlainText -Force``
                         wrap is emitted and the resulting SecureString
                         is passed to ``Set-LocalUser -Password``.
        :param account_never_expires: When set, controls the
                                      ``-AccountNeverExpires`` flag on
                                      ``Set-LocalUser``.
        :param password_never_expires: When set, controls the
                                       ``-PasswordNeverExpires`` flag
                                       on ``Set-LocalUser``.
        :param groups: Supplementary group set.  When set, group
                       membership is reconciled via
                       ``Add-LocalGroupMember`` /
                       ``Remove-LocalGroupMember``.
        :param append_groups: When True with ``groups`` set, the manager
                              only adds the user to listed groups.
                              Otherwise the user's current memberships
                              that are NOT in ``groups`` are revoked
                              first.
    """
    new_name: Optional[str] = None
    full_name: Optional[str] = None
    description: Optional[str] = None
    password: Optional[str] = None
    account_never_expires: Optional[bool] = None
    password_never_expires: Optional[bool] = None
    groups: Optional[list[str]] = None
    append_groups: bool = False


@dataclass
class WindowsUserInfo:
    """
        Information about an existing local user as reported by
        ``Get-LocalUser`` plus per-user ``Get-LocalGroupMember``
        traversal.

        :param name: Login name (``Name`` field).
        :param sid: SID string (``SID.Value``).
        :param enabled: ``Enabled`` flag from ``Get-LocalUser``.  When
                        False the account is "locked" in the
                        cross-OS-contract sense.
        :param full_name: ``FullName`` field, or ``None`` when absent.
        :param description: ``Description`` field, or ``None``.
        :param last_logon: ``LastLogon`` field rendered as a string, or
                           ``None`` when never logged on.
        :param groups: Group names the user belongs to (computed by
                       enumerating ``Get-LocalGroup`` and probing
                       ``Get-LocalGroupMember`` for each group).
        :param password_never_expires: ``PasswordNeverExpires`` field.
        :param account_expires: ``AccountExpires`` field rendered as a
                                string, or ``None`` when never expires.
    """
    name: str
    sid: str
    enabled: bool
    full_name: Optional[str] = None
    description: Optional[str] = None
    last_logon: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    password_never_expires: bool = False
    account_expires: Optional[str] = None


@dataclass
class WindowsGroupSpec:
    """
        Specification for creating a local group via ``New-LocalGroup``.

        :param name: Group name.
        :param description: Optional ``-Description`` value.
    """
    name: str
    description: Optional[str] = None


@dataclass
class WindowsGroupInfo:
    """
        Information about an existing local group.

        :param name: Group name.
        :param sid: SID string.
        :param description: Group description, or ``None`` when absent.
        :param members: Member principal names (each rendered as a
                        bare account name; the domain prefix, when
                        present, is preserved verbatim).
    """
    name: str
    sid: str
    description: Optional[str] = None
    members: list[str] = field(default_factory=list)


class WindowsUserGroupQueryError(Exception):
    """
        Raised when a user / group read fails for an unrecoverable
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
            err_msg_lines = ["User/group query command failed."]
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

        :param value: The raw text.

        :returns: The escaped text suitable for embedding between
                  single quotes in a PowerShell scriptblock.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _sanitise_script_passwords(*, script: str) -> str:
    """
        Replace every ``ConvertTo-SecureString -AsPlainText -Force
        '<pw>'`` literal in ``script`` with the same wrapper carrying
        ``'<redacted>'`` instead of the plaintext password.

        Used to redact passwords from the recorded argv before the
        result is returned.  The substitution leaves the rest of the
        scriptblock intact so audit reviewers can still see exactly
        which cmdlet was invoked.

        :param script: The PowerShell scriptblock text.

        :returns: A new string with every embedded plaintext password
                  replaced by ``"<redacted>"``.
    """
    rtnval = _SECURE_STRING_PATTERN.sub(
        "ConvertTo-SecureString -AsPlainText -Force '{}'".format(_REDACTED),
        script,
    )
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


def _extract_sid(*, raw_value) -> str:
    """
        Pull the string SID out of the ``SID`` field of a parsed
        ``Get-LocalUser`` / ``Get-LocalGroup`` JSON record.

        ``ConvertTo-Json`` serializes the .NET ``SecurityIdentifier``
        object as a dict with a ``Value`` key (PS 5.1) or as a plain
        string (PS 7+).  Both shapes are tolerated.

        :param raw_value: The raw ``SID`` field value.

        :returns: The SID as a string, or ``""`` when the field is
                  absent / unrecognised.
    """
    if raw_value is None:
        rtnval = ""
        return rtnval
    if isinstance(raw_value, str):
        rtnval = raw_value
        return rtnval
    if isinstance(raw_value, dict):
        value_field = raw_value.get("Value")
        if value_field is None:
            rtnval = ""
            return rtnval
        rtnval = str(value_field)
        return rtnval
    rtnval = str(raw_value)
    return rtnval


def _extract_optional_string(*, raw_value) -> Optional[str]:
    """
        Return ``raw_value`` as a string, collapsing ``None`` / empty
        strings to ``None``.

        :param raw_value: The raw field value.

        :returns: A non-empty string, or ``None``.
    """
    if raw_value is None:
        rtnval: Optional[str] = None
        return rtnval
    text = str(raw_value)
    if len(text) == 0:
        rtnval = None
        return rtnval
    rtnval = text
    return rtnval


def _extract_bool(*, raw_value) -> bool:
    """
        Coerce a JSON-decoded value into a bool.

        ``ConvertTo-Json`` emits booleans natively in PowerShell 7+ but
        as ``"True"`` / ``"False"`` strings under some legacy paths.
        This helper accepts both.

        :param raw_value: The raw field value.

        :returns: True / False; defaults to False on unparseable input.
    """
    if isinstance(raw_value, bool):
        rtnval = raw_value
        return rtnval
    if isinstance(raw_value, str):
        text_lower = raw_value.strip().lower()
        if text_lower in ("true", "1", "yes"):
            rtnval = True
            return rtnval
        rtnval = False
        return rtnval
    if isinstance(raw_value, int):
        rtnval = raw_value != 0
        return rtnval
    rtnval = False
    return rtnval


def _parse_get_localuser(*, text: str) -> list[dict]:
    """
        Parse the JSON output of ``Get-LocalUser | ConvertTo-Json``.

        :param text: Raw stdout.

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


def _parse_get_localgroup(*, text: str) -> list[dict]:
    """
        Parse the JSON output of ``Get-LocalGroup | ConvertTo-Json``.

        :param text: Raw stdout.

        :returns: A list of dicts (bare-object shape normalised).
    """
    rtnval = _parse_get_localuser(text=text)
    return rtnval


def _parse_get_localgroupmember(*, text: str) -> list[str]:
    """
        Parse the JSON output of ``Get-LocalGroupMember | ConvertTo-Json``.

        Each record exposes a ``Name`` field rendered as
        ``"<domain>\\<account>"`` (or just ``<account>`` for built-in
        groups).  This parser preserves the value verbatim — callers
        that need to compare against a bare ``user`` argument should
        strip the domain prefix themselves.

        :param text: Raw stdout.

        :returns: List of member name strings.
    """
    stripped = text.strip()
    if len(stripped) == 0:
        rtnval: list[str] = []
        return rtnval

    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = []
        return rtnval

    records = _normalize_to_list(parsed=parsed)
    rtnval = []
    for record in records:
        if not isinstance(record, dict):
            continue
        name_raw = record.get("Name")
        if name_raw is None:
            continue
        name_text = str(name_raw)
        if len(name_text) == 0:
            continue
        rtnval.append(name_text)
    return rtnval


def _strip_domain_prefix(*, raw_name: str) -> str:
    """
        Return the bare account name from a ``"<domain>\\<account>"``
        compound name.  Names without a backslash are returned
        unchanged.

        :param raw_name: The compound or bare name.

        :returns: The bare account name.
    """
    last_backslash = raw_name.rfind("\\")
    if last_backslash < 0:
        rtnval = raw_name
        return rtnval
    rtnval = raw_name[last_backslash + 1:]
    return rtnval


def _record_to_user_info(
    *,
    record: dict,
    fallback_name: str,
    groups: list[str],
) -> WindowsUserInfo:
    """
        Convert a parsed ``Get-LocalUser`` JSON record into a
        :class:`WindowsUserInfo`.

        :param record: Parsed JSON object.
        :param fallback_name: Name to use when ``Name`` is absent.
        :param groups: The list of groups computed for the user
                       (typically by enumerating ``Get-LocalGroup`` and
                       probing membership).

        :returns: The :class:`WindowsUserInfo`.
    """
    name_raw = record.get("Name")
    if name_raw is None or len(str(name_raw)) == 0:
        name_value = fallback_name
    else:
        name_value = str(name_raw)

    sid_value = _extract_sid(raw_value=record.get("SID"))
    enabled_value = _extract_bool(raw_value=record.get("Enabled"))
    full_name_value = _extract_optional_string(
        raw_value=record.get("FullName"),
    )
    description_value = _extract_optional_string(
        raw_value=record.get("Description"),
    )
    last_logon_value = _extract_optional_string(
        raw_value=record.get("LastLogon"),
    )
    pne_value = _extract_bool(raw_value=record.get("PasswordNeverExpires"))
    account_expires_value = _extract_optional_string(
        raw_value=record.get("AccountExpires"),
    )

    rtnval = WindowsUserInfo(
        name=name_value,
        sid=sid_value,
        enabled=enabled_value,
        full_name=full_name_value,
        description=description_value,
        last_logon=last_logon_value,
        groups=list(groups),
        password_never_expires=pne_value,
        account_expires=account_expires_value,
    )
    return rtnval


def _record_to_group_info(
    *,
    record: dict,
    fallback_name: str,
    members: list[str],
) -> WindowsGroupInfo:
    """
        Convert a parsed ``Get-LocalGroup`` JSON record into a
        :class:`WindowsGroupInfo`.

        :param record: Parsed JSON object.
        :param fallback_name: Name to use when ``Name`` is absent.
        :param members: The list of member names (already enumerated
                        via ``Get-LocalGroupMember``).

        :returns: The :class:`WindowsGroupInfo`.
    """
    name_raw = record.get("Name")
    if name_raw is None or len(str(name_raw)) == 0:
        name_value = fallback_name
    else:
        name_value = str(name_raw)

    sid_value = _extract_sid(raw_value=record.get("SID"))
    description_value = _extract_optional_string(
        raw_value=record.get("Description"),
    )

    rtnval = WindowsGroupInfo(
        name=name_value,
        sid=sid_value,
        description=description_value,
        members=list(members),
    )
    return rtnval


# ----------------------------------------------------------------------
# WindowsUserGroupManager
# ----------------------------------------------------------------------


class WindowsUserGroupManager(BaseFeatureManager):
    """
        Windows local-account manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`; reads use
        ``Get-LocalUser``, ``Get-LocalGroup``, and
        ``Get-LocalGroupMember``, while writes use ``New-LocalUser``,
        ``Set-LocalUser``, ``Rename-LocalUser``, ``Remove-LocalUser``,
        ``Disable-LocalUser`` / ``Enable-LocalUser``,
        ``New-LocalGroup`` / ``Remove-LocalGroup``, and
        ``Add-LocalGroupMember`` / ``Remove-LocalGroupMember``.
    """

    result_class = WindowsUserGroupActionResult
    status_class = WindowsUserGroupActionStatus
    feature_name = "user-group-management"

    # ------------------------------------------------------------------
    # User write verbs
    # ------------------------------------------------------------------

    def create_user(
        self,
        spec: WindowsUserSpec,
    ) -> WindowsUserGroupActionResult:
        """
            Create a local user via ``New-LocalUser``.

            When ``spec.password`` is set, the password is wrapped via
            ``ConvertTo-SecureString -AsPlainText -Force '<pw>'`` inside
            the same scriptblock and passed to ``-Password``; otherwise
            ``-NoPassword`` is emitted.  When ``spec.groups`` is set,
            each group is added via a chained ``Add-LocalGroupMember``
            call after the create.  Idempotent: skipped when
            ``Get-LocalUser -Name <name>`` already succeeds.

            The recorded ``command`` field on the returned result has
            every embedded plaintext password replaced with
            ``"<redacted>"`` so the password never leaks into audit
            captures.

            :param spec: The :class:`WindowsUserSpec` describing the
                         user.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        full_script = self._build_create_user_script(spec=spec)
        full_command = self._build_powershell_argv(script=full_script)
        recorded_command = self._sanitise_command_passwords(
            command=full_command,
        )

        if self._config.dry_run:
            dry = self._build_user_result(
                status=WindowsUserGroupActionStatus.DRY_RUN,
                name=spec.name,
                command=recorded_command,
            )
            return dry

        if self._user_exists(name=spec.name):
            skipped = self._build_user_result(
                status=WindowsUserGroupActionStatus.SKIPPED,
                name=spec.name,
                command=recorded_command,
                message="user already exists",
            )
            return skipped

        rtnval = self._dispatch_user_write(
            full_command=full_command,
            recorded_command=recorded_command,
            name=spec.name,
        )
        return rtnval

    def delete_user(
        self,
        name: str,
    ) -> WindowsUserGroupActionResult:
        """
            Delete a local user via ``Remove-LocalUser -Name <name>``.
            Idempotent: skipped when the user does not exist.

            :param name: The login name to delete.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = "Remove-LocalUser -Name '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_user_result(
                status=WindowsUserGroupActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        if not self._user_exists(name=name):
            skipped = self._build_user_result(
                status=WindowsUserGroupActionStatus.SKIPPED,
                name=name,
                command=command,
                message="user does not exist",
            )
            return skipped

        rtnval = self._dispatch_user_write(
            full_command=command,
            recorded_command=command,
            name=name,
        )
        return rtnval

    def modify_user(
        self,
        name: str,
        changes: WindowsUserChanges,
    ) -> WindowsUserGroupActionResult:
        """
            Modify a local user via one or more ``Set-LocalUser`` /
            ``Rename-LocalUser`` / ``Add-LocalGroupMember`` /
            ``Remove-LocalGroupMember`` calls.

            Idempotent per-attribute: only attributes whose current
            value differs from the requested value cause a write.  When
            no attribute requires a write, returns ``SKIPPED``.

            The recorded ``command`` field on the returned result is
            the FIRST emitted argv (or, when nothing needed changing,
            an empty list); embedded plaintext passwords are
            ``<redacted>``.  ``detail`` always carries the user's name.

            :param name: The current login name to modify.
            :param changes: The :class:`WindowsUserChanges` describing
                            the desired new state.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        if self._config.dry_run:
            dry_command = self._build_first_modify_command(
                name=name, changes=changes,
            )
            recorded = self._sanitise_command_passwords(command=dry_command)
            dry = self._build_user_result(
                status=WindowsUserGroupActionStatus.DRY_RUN,
                name=name,
                command=recorded,
            )
            return dry

        existing = self.get_user(name=name)
        if existing is None:
            failed = self._build_user_result(
                status=WindowsUserGroupActionStatus.FAILED,
                name=name,
                command=self._build_powershell_argv(
                    script="Get-LocalUser -Name '{}'".format(
                        _ps_escape_single_quoted(value=name),
                    ),
                ),
                message="user does not exist",
                stderr="user does not exist",
            )
            return failed

        plan = self._build_modify_plan(
            name=name, changes=changes, current=existing,
        )

        if len(plan) == 0:
            skipped = self._build_user_result(
                status=WindowsUserGroupActionStatus.SKIPPED,
                name=name,
                command=[],
                message="user already matches requested state",
            )
            return skipped

        first_full_command = list(plan[0])
        first_recorded_command = self._sanitise_command_passwords(
            command=first_full_command,
        )

        last_run = None
        for full_command in plan:
            this_run = self._runner.run(
                list(full_command),
                elevated=self._config.elevated,
            )
            last_run = this_run
            if this_run.exit_code != 0:
                failed = WindowsUserGroupActionResult(
                    status=WindowsUserGroupActionStatus.FAILED,
                    detail=name,
                    exit_code=this_run.exit_code,
                    stdout=this_run.stdout,
                    stderr=this_run.stderr,
                    command=self._sanitise_command_passwords(
                        command=full_command,
                    ),
                    name=name,
                    kind=WindowsPrincipalKind.USER,
                    message="modify_user step failed",
                )
                return failed

        # ``last_run`` is non-None at this point because ``plan`` is
        # non-empty.
        rtnval = WindowsUserGroupActionResult(
            status=WindowsUserGroupActionStatus.SUCCESS,
            detail=name,
            exit_code=last_run.exit_code,
            stdout=last_run.stdout,
            stderr=last_run.stderr,
            command=first_recorded_command,
            name=name,
            kind=WindowsPrincipalKind.USER,
        )
        return rtnval

    def set_password(
        self,
        user: str,
        password: str,
    ) -> WindowsUserGroupActionResult:
        """
            Reset ``user``'s password via ``Set-LocalUser -Password``.

            Renders a single PowerShell scriptblock that wraps
            ``password`` with ``ConvertTo-SecureString -AsPlainText
            -Force '<pw>'`` and passes the resulting SecureString to
            ``Set-LocalUser -Password``.  The recorded argv replaces
            every embedded plaintext password with ``"<redacted>"`` so
            the plaintext password never lands in audit captures.  No
            idempotency guard — the current password hash is not
            externally observable.

            :param user: The login name whose password to set.
            :param password: The plaintext password to install.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        full_script = self._build_set_password_script(
            user=user, password=password,
        )
        full_command = self._build_powershell_argv(script=full_script)
        recorded_command = self._sanitise_command_passwords(
            command=full_command,
        )

        if self._config.dry_run:
            dry = self._build_user_result(
                status=WindowsUserGroupActionStatus.DRY_RUN,
                name=user,
                command=recorded_command,
            )
            return dry

        rtnval = self._dispatch_user_write(
            full_command=full_command,
            recorded_command=recorded_command,
            name=user,
        )
        return rtnval

    def lock_account(
        self,
        user: str,
    ) -> WindowsUserGroupActionResult:
        """
            Disable ``user``'s account via ``Disable-LocalUser -Name
            <user>`` (Windows uses ``Enabled=False`` as the lock
            equivalent).  Idempotent: skipped when ``Get-LocalUser``
            already reports ``Enabled=False``.

            :param user: The login name to lock.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        escaped = _ps_escape_single_quoted(value=user)
        script = "Disable-LocalUser -Name '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_user_result(
                status=WindowsUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        existing = self._get_user_record(name=user)
        if existing is not None and not existing.enabled:
            skipped = self._build_user_result(
                status=WindowsUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="account is already disabled",
            )
            return skipped

        rtnval = self._dispatch_user_write(
            full_command=command,
            recorded_command=command,
            name=user,
        )
        return rtnval

    def unlock_account(
        self,
        user: str,
    ) -> WindowsUserGroupActionResult:
        """
            Enable ``user``'s account via ``Enable-LocalUser -Name
            <user>``.  Idempotent: skipped when ``Get-LocalUser``
            already reports ``Enabled=True``.

            :param user: The login name to unlock.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        escaped = _ps_escape_single_quoted(value=user)
        script = "Enable-LocalUser -Name '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_user_result(
                status=WindowsUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        existing = self._get_user_record(name=user)
        if existing is not None and existing.enabled:
            skipped = self._build_user_result(
                status=WindowsUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="account is already enabled",
            )
            return skipped

        rtnval = self._dispatch_user_write(
            full_command=command,
            recorded_command=command,
            name=user,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Group write verbs
    # ------------------------------------------------------------------

    def create_group(
        self,
        spec: WindowsGroupSpec,
    ) -> WindowsUserGroupActionResult:
        """
            Create a local group via ``New-LocalGroup``.  Skipped when
            the group already exists.

            :param spec: The :class:`WindowsGroupSpec` describing the
                         group.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        escaped_name = _ps_escape_single_quoted(value=spec.name)
        if spec.description is None:
            script = "New-LocalGroup -Name '{}'".format(escaped_name)
        else:
            escaped_desc = _ps_escape_single_quoted(value=spec.description)
            script = (
                "New-LocalGroup -Name '{}' -Description '{}'"
            ).format(escaped_name, escaped_desc)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_group_result(
                status=WindowsUserGroupActionStatus.DRY_RUN,
                name=spec.name,
                command=command,
            )
            return dry

        if self._group_exists(name=spec.name):
            skipped = self._build_group_result(
                status=WindowsUserGroupActionStatus.SKIPPED,
                name=spec.name,
                command=command,
                message="group already exists",
            )
            return skipped

        rtnval = self._dispatch_group_write(command=command, name=spec.name)
        return rtnval

    def delete_group(
        self,
        name: str,
    ) -> WindowsUserGroupActionResult:
        """
            Delete a local group via ``Remove-LocalGroup -Name <name>``.
            Skipped when the group does not exist.

            :param name: The group name to delete.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = "Remove-LocalGroup -Name '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_group_result(
                status=WindowsUserGroupActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        if not self._group_exists(name=name):
            skipped = self._build_group_result(
                status=WindowsUserGroupActionStatus.SKIPPED,
                name=name,
                command=command,
                message="group does not exist",
            )
            return skipped

        rtnval = self._dispatch_group_write(command=command, name=name)
        return rtnval

    def add_to_group(
        self,
        user: str,
        group: str,
    ) -> WindowsUserGroupActionResult:
        """
            Add ``user`` to ``group`` via ``Add-LocalGroupMember
            -Group <group> -Member <user>``.  Skipped when the user is
            already a member.

            :param user: The login name to add.
            :param group: The group name to add to.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        command = self._build_add_to_group_command(
            user=user, group=group,
        )

        if self._config.dry_run:
            dry = self._build_user_result(
                status=WindowsUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        if not self._group_exists(name=group):
            failed = self._build_user_result(
                status=WindowsUserGroupActionStatus.FAILED,
                name=user,
                command=command,
                message="group does not exist",
                stderr="group does not exist",
            )
            return failed

        if self._user_is_member(user=user, group=group):
            skipped = self._build_user_result(
                status=WindowsUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="user is already a member of the group",
            )
            return skipped

        rtnval = self._dispatch_user_write(
            full_command=command,
            recorded_command=command,
            name=user,
        )
        return rtnval

    def remove_from_group(
        self,
        user: str,
        group: str,
    ) -> WindowsUserGroupActionResult:
        """
            Remove ``user`` from ``group`` via ``Remove-LocalGroupMember
            -Group <group> -Member <user>``.  Skipped when the user is
            not a member.

            :param user: The login name to remove.
            :param group: The group name to remove from.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        command = self._build_remove_from_group_command(
            user=user, group=group,
        )

        if self._config.dry_run:
            dry = self._build_user_result(
                status=WindowsUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        if not self._group_exists(name=group):
            failed = self._build_user_result(
                status=WindowsUserGroupActionStatus.FAILED,
                name=user,
                command=command,
                message="group does not exist",
                stderr="group does not exist",
            )
            return failed

        if not self._user_is_member(user=user, group=group):
            skipped = self._build_user_result(
                status=WindowsUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="user is not a member of the group",
            )
            return skipped

        rtnval = self._dispatch_user_write(
            full_command=command,
            recorded_command=command,
            name=user,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_user(
        self,
        name: str,
    ) -> Optional[WindowsUserInfo]:
        """
            Look up a user by name.

            Runs ``Get-LocalUser -Name <name> | ConvertTo-Json -Compress
            -Depth 5`` for the scalar attributes, then enumerates
            ``Get-LocalGroup`` and probes ``Get-LocalGroupMember`` for
            each group to compute the user's group memberships.  Reads
            do not elevate.

            :param name: The login name to look up.

            :returns: The :class:`WindowsUserInfo`, or ``None`` when no
                      such user exists.
        """
        existing = self._get_user_record(name=name)
        if existing is None:
            rtnval: Optional[WindowsUserInfo] = None
            return rtnval

        groups = self._enumerate_user_groups(user=name)
        existing.groups = groups
        rtnval = existing
        return rtnval

    def list_users(self) -> list[WindowsUserInfo]:
        """
            Enumerate every local user.

            Runs ``Get-LocalUser | ConvertTo-Json -Compress -Depth 5``,
            parses each row, and materialises a :class:`WindowsUserInfo`
            for each.  Reads do not elevate.

            :returns: One :class:`WindowsUserInfo` per parsed entry.
            :raises WindowsUserGroupQueryError: When the cmdlet fails
                                                or the JSON payload is
                                                unparseable.
        """
        script = (
            "Get-LocalUser "
            "| Select-Object Name, SID, Enabled, FullName, Description, "
            "LastLogon, PasswordNeverExpires, AccountExpires "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsUserGroupQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_get_localuser(text=run_result.stdout)
        rtnval: list[WindowsUserInfo] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            name_raw = record.get("Name")
            if name_raw is None:
                continue
            user_name = str(name_raw)
            if len(user_name) == 0:
                continue
            groups = self._enumerate_user_groups(user=user_name)
            info = _record_to_user_info(
                record=record, fallback_name=user_name, groups=groups,
            )
            rtnval.append(info)
        return rtnval

    def get_group(
        self,
        name: str,
    ) -> Optional[WindowsGroupInfo]:
        """
            Look up a group by name via ``Get-LocalGroup -Name <name>``
            plus ``Get-LocalGroupMember -Group <name>``.  Reads do not
            elevate.

            :param name: The group name to look up.

            :returns: The :class:`WindowsGroupInfo`, or ``None``.
        """
        record = self._get_group_record(name=name)
        if record is None:
            rtnval: Optional[WindowsGroupInfo] = None
            return rtnval

        members = self._enumerate_group_members(group=name)
        rtnval = _record_to_group_info(
            record=record, fallback_name=name, members=members,
        )
        return rtnval

    def list_groups(self) -> list[WindowsGroupInfo]:
        """
            Enumerate every local group via ``Get-LocalGroup |
            ConvertTo-Json -Compress -Depth 5``.  Reads do not elevate.

            :returns: One :class:`WindowsGroupInfo` per parsed entry.
                      ``members`` is left empty by this listing call;
                      callers needing per-group members should call
                      :meth:`get_group` for each entry of interest.
            :raises WindowsUserGroupQueryError: When the cmdlet fails
                                                or the JSON payload is
                                                unparseable.
        """
        script = (
            "Get-LocalGroup "
            "| Select-Object Name, SID, Description "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsUserGroupQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_get_localgroup(text=run_result.stdout)
        rtnval: list[WindowsGroupInfo] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            name_raw = record.get("Name")
            if name_raw is None:
                continue
            group_name = str(name_raw)
            if len(group_name) == 0:
                continue
            info = _record_to_group_info(
                record=record, fallback_name=group_name, members=[],
            )
            rtnval.append(info)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv / dispatch / result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsUserGroupManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`WindowsUserGroupManagerConfig`, falling back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.

            :returns: The typed config.
        """
        if isinstance(self._config, WindowsUserGroupManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsUserGroupManagerConfig(
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

    def _sanitise_command_passwords(
        self,
        *,
        command: list[str],
    ) -> list[str]:
        """
            Return a copy of ``command`` whose final element (the
            PowerShell scriptblock) has every embedded plaintext
            password replaced with ``"<redacted>"``.

            :param command: The raw argv (as it would be sent to the
                            runner).

            :returns: A new list with passwords redacted.  When the
                      argv is empty or the last element is not a
                      ``-Command`` scriptblock, returns an unchanged
                      copy.
        """
        if len(command) == 0:
            rtnval: list[str] = []
            return rtnval
        rtnval = list(command)
        last_index = len(rtnval) - 1
        rtnval[last_index] = _sanitise_script_passwords(
            script=rtnval[last_index],
        )
        return rtnval

    def _build_create_user_script(self, *, spec: WindowsUserSpec) -> str:
        """
            Build the PowerShell scriptblock for ``create_user``.

            Renders a ``New-LocalUser`` invocation with the password
            (when set) wrapped via ``ConvertTo-SecureString`` plus, when
            ``spec.groups`` is set, a chained ``Add-LocalGroupMember``
            statement per group.

            :param spec: The :class:`WindowsUserSpec`.

            :returns: The scriptblock text.
        """
        escaped_name = _ps_escape_single_quoted(value=spec.name)
        statements: list[str] = []

        new_user_fragments: list[str] = []
        new_user_fragments.append(
            "New-LocalUser -Name '{}'".format(escaped_name),
        )

        if spec.password is None:
            new_user_fragments.append("-NoPassword")
            password_prelude = ""
        else:
            escaped_pw = _ps_escape_single_quoted(value=spec.password)
            password_prelude = (
                "$pw = ConvertTo-SecureString -AsPlainText -Force '{}'; "
            ).format(escaped_pw)
            new_user_fragments.append("-Password $pw")

        if spec.full_name is not None:
            escaped_full = _ps_escape_single_quoted(value=spec.full_name)
            new_user_fragments.append(
                "-FullName '{}'".format(escaped_full),
            )

        if spec.description is not None:
            escaped_desc = _ps_escape_single_quoted(value=spec.description)
            new_user_fragments.append(
                "-Description '{}'".format(escaped_desc),
            )

        if spec.account_never_expires:
            new_user_fragments.append("-AccountNeverExpires")

        statements.append(password_prelude + " ".join(new_user_fragments))

        if spec.password_never_expires:
            statements.append(
                "Set-LocalUser -Name '{}' -PasswordNeverExpires $true".format(
                    escaped_name,
                ),
            )

        if spec.groups is not None and len(spec.groups) > 0:
            for group_name in spec.groups:
                escaped_group = _ps_escape_single_quoted(value=group_name)
                statements.append(
                    "Add-LocalGroupMember -Group '{}' -Member '{}'".format(
                        escaped_group, escaped_name,
                    ),
                )

        rtnval = "; ".join(statements)
        return rtnval

    def _build_set_password_script(
        self,
        *,
        user: str,
        password: str,
    ) -> str:
        """
            Build the PowerShell scriptblock for ``set_password``.

            :param user: The login name.
            :param password: Plaintext password.

            :returns: The scriptblock text.
        """
        escaped_user = _ps_escape_single_quoted(value=user)
        escaped_pw = _ps_escape_single_quoted(value=password)
        rtnval = (
            "$pw = ConvertTo-SecureString -AsPlainText -Force '{pw}'; "
            "Set-LocalUser -Name '{user}' -Password $pw"
        ).format(pw=escaped_pw, user=escaped_user)
        return rtnval

    def _build_add_to_group_command(
        self,
        *,
        user: str,
        group: str,
    ) -> list[str]:
        """
            Build the ``Add-LocalGroupMember`` argv.
        """
        escaped_user = _ps_escape_single_quoted(value=user)
        escaped_group = _ps_escape_single_quoted(value=group)
        script = (
            "Add-LocalGroupMember -Group '{}' -Member '{}'"
        ).format(escaped_group, escaped_user)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_remove_from_group_command(
        self,
        *,
        user: str,
        group: str,
    ) -> list[str]:
        """
            Build the ``Remove-LocalGroupMember`` argv.
        """
        escaped_user = _ps_escape_single_quoted(value=user)
        escaped_group = _ps_escape_single_quoted(value=group)
        script = (
            "Remove-LocalGroupMember -Group '{}' -Member '{}'"
        ).format(escaped_group, escaped_user)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_first_modify_command(
        self,
        *,
        name: str,
        changes: WindowsUserChanges,
    ) -> list[str]:
        """
            Compute the FIRST argv that :meth:`modify_user` would emit
            without consulting current state.  Used purely to populate
            ``command`` on the dry-run result.

            :param name: The current login name.
            :param changes: The requested changes.

            :returns: A representative argv; ``[]`` when ``changes``
                      requests nothing.
        """
        escaped_name = _ps_escape_single_quoted(value=name)

        if changes.new_name is not None:
            escaped_new = _ps_escape_single_quoted(value=changes.new_name)
            script = "Rename-LocalUser -Name '{}' -NewName '{}'".format(
                escaped_name, escaped_new,
            )
            rtnval = self._build_powershell_argv(script=script)
            return rtnval

        set_localuser = self._maybe_build_set_localuser_script(
            name=name, changes=changes,
        )
        if set_localuser is not None:
            rtnval = self._build_powershell_argv(script=set_localuser)
            return rtnval

        if changes.groups is not None and len(changes.groups) > 0:
            rtnval = self._build_add_to_group_command(
                user=name, group=changes.groups[0],
            )
            return rtnval

        rtnval = []
        return rtnval

    def _maybe_build_set_localuser_script(
        self,
        *,
        name: str,
        changes: WindowsUserChanges,
    ) -> Optional[str]:
        """
            Build a ``Set-LocalUser`` scriptblock for the scalar
            attributes set in ``changes``, or ``None`` when no scalar
            attributes are requested.

            :param name: The login name.
            :param changes: The requested changes.

            :returns: The scriptblock text, or ``None``.
        """
        escaped_name = _ps_escape_single_quoted(value=name)
        fragments: list[str] = []
        prelude = ""

        if changes.password is not None:
            escaped_pw = _ps_escape_single_quoted(value=changes.password)
            prelude = (
                "$pw = ConvertTo-SecureString -AsPlainText -Force '{}'; "
            ).format(escaped_pw)
            fragments.append("-Password $pw")

        if changes.full_name is not None:
            escaped_full = _ps_escape_single_quoted(value=changes.full_name)
            fragments.append("-FullName '{}'".format(escaped_full))

        if changes.description is not None:
            escaped_desc = _ps_escape_single_quoted(
                value=changes.description,
            )
            fragments.append("-Description '{}'".format(escaped_desc))

        if changes.account_never_expires is True:
            fragments.append("-AccountNeverExpires")

        if changes.password_never_expires is not None:
            if changes.password_never_expires:
                fragments.append("-PasswordNeverExpires $true")
            else:
                fragments.append("-PasswordNeverExpires $false")

        if len(fragments) == 0:
            rtnval: Optional[str] = None
            return rtnval

        rtnval = (
            prelude
            + "Set-LocalUser -Name '{}' ".format(escaped_name)
            + " ".join(fragments)
        )
        return rtnval

    def _build_modify_plan(
        self,
        *,
        name: str,
        changes: WindowsUserChanges,
        current: WindowsUserInfo,
    ) -> list[list[str]]:
        """
            Compute the ordered list of full argvs needed to bring
            ``current`` into line with ``changes``.  Skips fields that
            already match.

            :param name: The current login name.
            :param changes: The requested changes.
            :param current: The user's currently-observed info.

            :returns: One full argv per write operation.
        """
        plan: list[list[str]] = []
        escaped_name = _ps_escape_single_quoted(value=name)

        # Rename comes first so subsequent Set-LocalUser invocations
        # target the new name.
        active_name = name
        if changes.new_name is not None and changes.new_name != current.name:
            escaped_new = _ps_escape_single_quoted(value=changes.new_name)
            rename_script = (
                "Rename-LocalUser -Name '{}' -NewName '{}'"
            ).format(escaped_name, escaped_new)
            plan.append(self._build_powershell_argv(script=rename_script))
            active_name = changes.new_name

        # Build a Set-LocalUser invocation for the scalar diffs.
        set_fragments: list[str] = []
        prelude = ""

        if changes.password is not None:
            # No idempotency probe for passwords.
            escaped_pw = _ps_escape_single_quoted(value=changes.password)
            prelude = (
                "$pw = ConvertTo-SecureString -AsPlainText -Force '{}'; "
            ).format(escaped_pw)
            set_fragments.append("-Password $pw")

        if (
            changes.full_name is not None
            and changes.full_name != (current.full_name or "")
        ):
            escaped_full = _ps_escape_single_quoted(value=changes.full_name)
            set_fragments.append("-FullName '{}'".format(escaped_full))

        if (
            changes.description is not None
            and changes.description != (current.description or "")
        ):
            escaped_desc = _ps_escape_single_quoted(
                value=changes.description,
            )
            set_fragments.append("-Description '{}'".format(escaped_desc))

        # ``-AccountNeverExpires`` is a flag, not a -bool toggle on
        # Set-LocalUser; only emit when the caller explicitly wants it
        # set.  There is no observed-state field exposed on
        # WindowsUserInfo for the inverse, so we always emit when set.
        if changes.account_never_expires is True:
            set_fragments.append("-AccountNeverExpires")

        if (
            changes.password_never_expires is not None
            and changes.password_never_expires != current.password_never_expires
        ):
            if changes.password_never_expires:
                set_fragments.append("-PasswordNeverExpires $true")
            else:
                set_fragments.append("-PasswordNeverExpires $false")

        if len(set_fragments) > 0:
            escaped_active = _ps_escape_single_quoted(value=active_name)
            set_script = (
                prelude
                + "Set-LocalUser -Name '{}' ".format(escaped_active)
                + " ".join(set_fragments)
            )
            plan.append(self._build_powershell_argv(script=set_script))

        # Group-membership reconciliation.
        if changes.groups is not None:
            desired = list(changes.groups)
            observed = set(current.groups)
            if changes.append_groups:
                for group_name in desired:
                    if group_name in observed:
                        continue
                    plan.append(
                        self._build_add_to_group_command(
                            user=active_name, group=group_name,
                        ),
                    )
            else:
                desired_set = set(desired)
                for group_name in desired:
                    if group_name in observed:
                        continue
                    plan.append(
                        self._build_add_to_group_command(
                            user=active_name, group=group_name,
                        ),
                    )
                for observed_name in current.groups:
                    if observed_name in desired_set:
                        continue
                    plan.append(
                        self._build_remove_from_group_command(
                            user=active_name, group=observed_name,
                        ),
                    )

        return plan

    def _dispatch_user_write(
        self,
        *,
        full_command: list[str],
        recorded_command: list[str],
        name: str,
    ) -> WindowsUserGroupActionResult:
        """
            Dispatch a user-scope write command and wrap its outcome.

            :param full_command: The argv actually sent to the runner
                                 (may carry plaintext password).
            :param recorded_command: The argv recorded on the result
                                     (passwords redacted).
            :param name: The user name.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        run_result = self._runner.run(
            list(full_command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsUserGroupActionStatus.SUCCESS
        else:
            mapped_status = WindowsUserGroupActionStatus.FAILED

        rtnval = WindowsUserGroupActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(recorded_command),
            name=name,
            kind=WindowsPrincipalKind.USER,
        )
        return rtnval

    def _dispatch_group_write(
        self,
        *,
        command: list[str],
        name: str,
    ) -> WindowsUserGroupActionResult:
        """
            Dispatch a group-scope write command and wrap its outcome.

            :param command: The full argv (no redaction needed; group
                            verbs do not carry passwords).
            :param name: The group name.

            :returns: The :class:`WindowsUserGroupActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsUserGroupActionStatus.SUCCESS
        else:
            mapped_status = WindowsUserGroupActionStatus.FAILED

        rtnval = WindowsUserGroupActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            kind=WindowsPrincipalKind.GROUP,
        )
        return rtnval

    def _build_user_result(
        self,
        *,
        status: WindowsUserGroupActionStatus,
        name: str,
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsUserGroupActionResult:
        """
            Construct a user-kind
            :class:`WindowsUserGroupActionResult` without invoking the
            runner.
        """
        rtnval = WindowsUserGroupActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            kind=WindowsPrincipalKind.USER,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _build_group_result(
        self,
        *,
        status: WindowsUserGroupActionStatus,
        name: str,
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsUserGroupActionResult:
        """
            Construct a group-kind
            :class:`WindowsUserGroupActionResult` without invoking the
            runner.
        """
        rtnval = WindowsUserGroupActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            kind=WindowsPrincipalKind.GROUP,
            message=message,
            stderr=stderr,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — read probes
    # ------------------------------------------------------------------

    def _user_exists(self, *, name: str) -> bool:
        """
            Probe ``Get-LocalUser -Name <name>`` for existence.  Reads
            do not elevate.

            :param name: The login name.

            :returns: True when the cmdlet exits zero.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = (
            "Get-LocalUser -Name '{}' "
            "| Select-Object Name "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _group_exists(self, *, name: str) -> bool:
        """
            Probe ``Get-LocalGroup -Name <name>`` for existence.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = (
            "Get-LocalGroup -Name '{}' "
            "| Select-Object Name "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _get_user_record(
        self,
        *,
        name: str,
    ) -> Optional[WindowsUserInfo]:
        """
            Run ``Get-LocalUser -Name <name> | ConvertTo-Json`` and
            parse the result into a partially-populated
            :class:`WindowsUserInfo` (groups field is left empty).

            :param name: The login name.

            :returns: The :class:`WindowsUserInfo`, or ``None`` when
                      the read fails.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = (
            "Get-LocalUser -Name '{}' "
            "| Select-Object Name, SID, Enabled, FullName, Description, "
            "LastLogon, PasswordNeverExpires, AccountExpires "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[WindowsUserInfo] = None
            return rtnval

        records = _parse_get_localuser(text=run_result.stdout)
        if len(records) == 0:
            rtnval = None
            return rtnval

        rtnval = _record_to_user_info(
            record=records[0], fallback_name=name, groups=[],
        )
        return rtnval

    def _get_group_record(
        self,
        *,
        name: str,
    ) -> Optional[dict]:
        """
            Run ``Get-LocalGroup -Name <name> | ConvertTo-Json``.

            :param name: The group name.

            :returns: The parsed JSON record, or ``None`` when the
                      read fails or the payload is empty.
        """
        escaped = _ps_escape_single_quoted(value=name)
        script = (
            "Get-LocalGroup -Name '{}' "
            "| Select-Object Name, SID, Description "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval

        records = _parse_get_localgroup(text=run_result.stdout)
        if len(records) == 0:
            rtnval = None
            return rtnval
        rtnval = records[0]
        return rtnval

    def _enumerate_user_groups(self, *, user: str) -> list[str]:
        """
            Return the list of group names that ``user`` is a member of
            by enumerating ``Get-LocalGroup`` and checking
            ``Get-LocalGroupMember`` for each group.

            :param user: The login name.

            :returns: Group-name list.  Empty when the lookup fails.
        """
        list_script = (
            "Get-LocalGroup "
            "| Select-Object Name "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        list_command = self._build_powershell_argv(script=list_script)
        list_run = self._runner.run(list(list_command), elevated=False)
        if list_run.exit_code != 0:
            rtnval: list[str] = []
            return rtnval

        records = _parse_get_localgroup(text=list_run.stdout)
        rtnval = []
        for record in records:
            if not isinstance(record, dict):
                continue
            group_name_raw = record.get("Name")
            if group_name_raw is None:
                continue
            group_name = str(group_name_raw)
            if len(group_name) == 0:
                continue
            if self._user_is_member(user=user, group=group_name):
                rtnval.append(group_name)
        return rtnval

    def _enumerate_group_members(self, *, group: str) -> list[str]:
        """
            Run ``Get-LocalGroupMember -Group <group> | ConvertTo-Json``
            and return the list of bare account names.

            :param group: The group name.

            :returns: List of bare account names; empty on failure.
        """
        escaped = _ps_escape_single_quoted(value=group)
        script = (
            "Get-LocalGroupMember -Group '{}' "
            "| Select-Object Name "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list[str] = []
            return rtnval

        compound_names = _parse_get_localgroupmember(text=run_result.stdout)
        rtnval = []
        for compound in compound_names:
            bare = _strip_domain_prefix(raw_name=compound)
            rtnval.append(bare)
        return rtnval

    def _user_is_member(
        self,
        *,
        user: str,
        group: str,
    ) -> bool:
        """
            Probe ``Get-LocalGroupMember -Group <group>`` for a member
            whose bare name (after domain-prefix stripping) matches
            ``user``.

            :param user: The login name to search for.
            :param group: The group name.

            :returns: True when the user is a member.
        """
        members = self._enumerate_group_members(group=group)
        rtnval = user in members
        return rtnval
