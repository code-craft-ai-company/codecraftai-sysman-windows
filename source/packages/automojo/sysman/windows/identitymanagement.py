"""
    Windows identity-management feature.

    Implements the cross-OS identity-management contract for Windows
    using PowerShell cmdlets: ``Add-Computer`` / ``Remove-Computer``
    for domain join / unjoin, ``Get-WmiObject Win32_ComputerSystem``
    for membership query, ``Get-ADUser`` / ``Get-ADGroup`` (RSAT
    Active Directory module) for directory-backed principal
    resolution, with a ``Get-LocalUser`` / ``Get-LocalGroup`` fallback
    when RSAT is not installed, and ``klist purge`` for cache
    invalidation.

    Verb / tool mapping:

    +-----------------------------+-------------------------------------+
    | Verb                        | Windows tool (PowerShell)           |
    +=============================+=====================================+
    | ``join_realm``              | ``Add-Computer -DomainName <d>      |
    |                             | -Credential <cred> [-OUPath <ou>]   |
    |                             | [-Server <dc>] -Force -Restart:     |
    |                             | $false`` (password as SecureString  |
    |                             | inside scriptblock; redacted in     |
    |                             | recorded argv)                      |
    +-----------------------------+-------------------------------------+
    | ``leave_realm``             | ``Remove-Computer                   |
    |                             | -UnjoinDomainCredential <cred>      |
    |                             | -PassThru -Force -Restart:$false``  |
    +-----------------------------+-------------------------------------+
    | ``get_realm_status``        | ``Get-WmiObject Win32_ComputerSystem|
    |                             | | Select Domain,PartOfDomain,       |
    |                             | DomainRole | ConvertTo-Json``       |
    +-----------------------------+-------------------------------------+
    | ``list_joined_realms``      | Same as get_realm_status            |
    |                             | (single-domain reality)             |
    +-----------------------------+-------------------------------------+
    | ``sync_users``              | ``klist purge -li 0x3e7`` (system   |
    |                             | LUID) — best-effort                 |
    +-----------------------------+-------------------------------------+
    | ``resolve_user``            | ``Get-ADUser -Identity <u>          |
    |                             | -Properties * | ConvertTo-Json``    |
    |                             | (RSAT) OR ``Get-LocalUser`` fallback|
    +-----------------------------+-------------------------------------+
    | ``resolve_group``           | ``Get-ADGroup -Identity <g>         |
    |                             | -Properties * | ConvertTo-Json``    |
    |                             | (RSAT) OR ``Get-LocalGroup`` fallback|
    +-----------------------------+-------------------------------------+

    File I/O policy:
        Domain-join state lives in the registry / `Lsass`; reads and
        writes go through PowerShell cmdlets.  This module does NOT
        consume an :class:`IFileOperator`.

    Single-domain reality:
        Windows hosts are joined to exactly one Active Directory
        domain at a time.  ``list_joined_realms`` therefore returns
        a list of zero or one :class:`WindowsRealmInfo` records.

    Elevation policy:
        Every write verb (``join_realm``, ``leave_realm``,
        ``sync_users``) honours ``config.elevated``.  Reads
        (``get_realm_status``, ``list_joined_realms``,
        ``resolve_user``, ``resolve_group``) never elevate.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so that ``$PROFILE`` cannot mutate
        cmdlet behaviour and PowerShell never blocks waiting for
        interactive input on a runner that has no controlling
        terminal.

    Password handling:
        Plaintext passwords appear inside the PowerShell scriptblock
        as ``ConvertTo-SecureString -AsPlainText -Force '<pw>'``.
        The argv recorded on the result is sanitised: every embedded
        plaintext password value is replaced with ``<redacted>``
        before the argv is captured on
        :class:`WindowsIdentityActionResult`.  This mirrors the
        pattern from :class:`WindowsUserGroupManager`.

    RSAT availability:
        ``Get-ADUser`` and ``Get-ADGroup`` require the RSAT Active
        Directory PowerShell module, which is not installed by
        default on Windows Server Core, Windows 10 / 11 Home, or
        many automation images.  When ``config.fallback_to_local``
        is True (the default) and the AD cmdlet is unavailable (the
        scriptblock fails with the canonical "term … is not
        recognized" error), the manager falls back to
        ``Get-LocalUser`` / ``Get-LocalGroup`` and reports
        ``realm=None`` on the result.
"""

import json
import os
import re

from dataclasses import dataclass, field
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

# Sentinel placed into the recorded argv anywhere a password value
# would otherwise appear.
_REDACTED = "<redacted>"

# Pattern used to locate
# ``ConvertTo-SecureString -AsPlainText -Force '<pw>'`` invocations
# inside a PowerShell scriptblock so the embedded plaintext password
# can be replaced with the redaction sentinel before the argv is
# recorded on a result.
_SECURE_STRING_PATTERN = re.compile(
    r"ConvertTo-SecureString\s+-AsPlainText\s+-Force\s+'((?:[^']|'')*)'",
)

# Token that ``Get-ADUser`` / ``Get-ADGroup`` produce when the RSAT
# AD module is not loaded.  Match a representative substring of the
# canonical PowerShell error.
_RSAT_MISSING_MARKERS = (
    "is not recognized as the name of a cmdlet",
    "could not be loaded",
    "Active Directory module",
)


WindowsIdentityActionStatus = ActionStatus


class WindowsRealmKind(Enum):
    """
        Identity back-end kinds visible on Windows.

        :cvar ACTIVE_DIRECTORY: Microsoft Active Directory domain.
        :cvar WORKGROUP: The host is in a workgroup (no domain bind).
        :cvar UNKNOWN: ``Win32_ComputerSystem`` reported a domain role
                       value that cannot be classified.
    """
    ACTIVE_DIRECTORY = "active_directory"
    WORKGROUP = "workgroup"
    UNKNOWN = "unknown"


@dataclass
class WindowsIdentityManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsIdentityManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
        :param requires_rsat: Advisory flag — when True the manager
                              records the RSAT requirement on
                              `WindowsRealmInfo.notes`-style fields
                              (currently unused).  Future follow-ups
                              may use this to gate AD cmdlet usage.
        :param fallback_to_local: When True (default), AD-cmdlet
                                  failures that look like RSAT-missing
                                  errors trigger a fallback to
                                  ``Get-LocalUser`` / ``Get-LocalGroup``.
                                  Set False to keep the failure
                                  visible.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    requires_rsat: bool = True
    fallback_to_local: bool = True


@dataclass
class WindowsIdentityActionResult(FeatureActionResult):
    """
        Result of an identity-management write operation.

        :param realm: The realm acted upon, when relevant.
        :param username: The admin username acted upon, when relevant.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    realm: Optional[str] = None
    username: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsRealmJoinSpec:
    """
        Specification for joining a domain via ``Add-Computer``.

        :param realm: The Active Directory domain name (e.g.
                      ``"ad.example.com"``).
        :param admin_user: Administrator login.  Combined with
                           ``admin_password`` to build a
                           PSCredential.  Should be in
                           ``DOMAIN\\user`` or ``user@DOMAIN`` form
                           when joining a different forest.
        :param admin_password: Administrator password.  Lands inside
                               the scriptblock as
                               ``ConvertTo-SecureString
                               -AsPlainText -Force '<pw>'``; redacted
                               in the recorded argv.
        :param computer_name: Optional ``-NewName`` override (renames
                              the computer during the join).
        :param ou_path: Optional ``-OUPath`` value.
        :param domain_controller: Optional ``-Server`` override.
    """
    realm: str
    admin_user: str
    admin_password: str
    computer_name: Optional[str] = None
    ou_path: Optional[str] = None
    domain_controller: Optional[str] = None


@dataclass
class WindowsRealmInfo:
    """
        Information about the realm the host is currently joined to.

        :param realm: Realm / domain name.
        :param realm_kind: Classified back-end kind.
        :param is_default: True for the bound AD domain (the only
                           configured realm on a Windows host).
        :param domain_controller: Reserved.  ``Win32_ComputerSystem``
                                  does not surface a single canonical
                                  DC; this field is always ``None``.
        :param last_sync: Reserved; ``None``.
    """
    realm: str
    realm_kind: WindowsRealmKind
    is_default: bool = False
    domain_controller: Optional[str] = None
    last_sync: Optional[str] = None


@dataclass
class WindowsRealmStatus:
    """
        Aggregate realm-join status of the host.

        :param joined: True when ``Win32_ComputerSystem.PartOfDomain``
                       is True.
        :param current_realm: The domain name when joined; the
                              workgroup name when not.
        :param realm_kind: ``ACTIVE_DIRECTORY`` when joined,
                           ``WORKGROUP`` otherwise.
        :param computer_name: ``Win32_ComputerSystem.Name`` value, or
                              ``None`` when absent.
    """
    joined: bool
    current_realm: Optional[str] = None
    realm_kind: Optional[WindowsRealmKind] = None
    computer_name: Optional[str] = None


@dataclass
class WindowsUserResolveResult:
    """
        Result of resolving a user against AD (with local fallback).

        :param username: The supplied username.
        :param uid: ``UidNumber`` AD attribute when present (rare
                    — typically populated by AD-with-Identity-
                    Management for Unix); ``None`` otherwise.
        :param gid: ``GidNumber`` AD attribute when present.
        :param home: ``HomeDirectory`` AD attribute (or ``HomeDir``
                     for local accounts), or ``None``.
        :param shell: ``LoginShell`` AD attribute, or ``None``.
        :param realm: Realm the user belongs to when resolved via
                      ``Get-ADUser``; ``None`` when resolved via the
                      local fallback.
        :param groups: ``MemberOf`` AD attribute parsed into bare
                       CN components when AD-resolved; empty for
                       local fallback.
        :param found: True when a record was returned.
    """
    username: str
    uid: Optional[int] = None
    gid: Optional[int] = None
    home: Optional[str] = None
    shell: Optional[str] = None
    realm: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    found: bool = False


@dataclass
class WindowsGroupResolveResult:
    """
        Result of resolving a group against AD (with local fallback).

        :param groupname: The supplied groupname.
        :param gid: ``GidNumber`` AD attribute when present.
        :param members: ``Members`` AD attribute parsed into bare CN
                        components, or local member names from the
                        ``Get-LocalGroupMember`` fallback.
        :param realm: Realm the group belongs to when resolved via
                      ``Get-ADGroup``; ``None`` when resolved via the
                      local fallback.
        :param found: True when a record was returned.
    """
    groupname: str
    gid: Optional[int] = None
    members: list[str] = field(default_factory=list)
    realm: Optional[str] = None
    found: bool = False


class WindowsIdentityQueryError(Exception):
    """
        Raised when an identity-management read fails for a reason
        other than the target simply being absent (or RSAT being
        unavailable when ``fallback_to_local`` is True).
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
        self.command: list[str] = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Identity-management query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
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

        :returns: The escaped text.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _sanitise_script_passwords(*, script: str) -> str:
    """
        Replace every ``ConvertTo-SecureString -AsPlainText -Force
        '<pw>'`` literal in ``script`` with the same wrapper carrying
        ``'<redacted>'`` instead of the plaintext password.

        :param script: The PowerShell scriptblock text.

        :returns: A new string with passwords redacted.
    """
    rtnval = _SECURE_STRING_PATTERN.sub(
        "ConvertTo-SecureString -AsPlainText -Force '{}'".format(_REDACTED),
        script,
    )
    return rtnval


def _normalize_to_list(*, parsed):
    """
        Normalise a ``ConvertTo-Json`` payload into a list.

        :param parsed: Output of :func:`json.loads`.

        :returns: A list.  ``None`` and other non-list / non-dict
                  types collapse to an empty list.
    """
    if isinstance(parsed, list):
        rtnval = parsed
        return rtnval
    if isinstance(parsed, dict):
        rtnval = [parsed]
        return rtnval
    rtnval = []
    return rtnval


def _looks_like_rsat_missing(*, stderr: str) -> bool:
    """
        Return True when ``stderr`` contains a marker that strongly
        suggests the RSAT Active Directory module is not loaded.

        :param stderr: The runner's captured stderr.

        :returns: True when at least one marker substring is present.
    """
    if stderr is None:
        rtnval = False
        return rtnval
    for marker in _RSAT_MISSING_MARKERS:
        if marker in stderr:
            rtnval = True
            return rtnval
    rtnval = False
    return rtnval


def _strip_dn_to_cn(*, dn: str) -> str:
    """
        Reduce a Distinguished Name to its leading ``CN=`` component.

        ``Get-ADUser -Properties *`` reports ``MemberOf`` as a list of
        DNs (``"CN=Admins,OU=Groups,DC=ad,DC=example,DC=com"``).
        Callers typically need just the CN; this helper strips the
        rest.  When the input does not start with ``CN=`` it is
        returned unchanged.

        :param dn: The raw DN string.

        :returns: The bare CN value, or the original ``dn`` when no
                  ``CN=`` prefix is found.
    """
    cleaned = dn.strip()
    upper_prefix = cleaned[:3].upper()
    if upper_prefix != "CN=":
        rtnval = cleaned
        return rtnval

    body = cleaned[3:]
    comma_position = body.find(",")
    if comma_position < 0:
        rtnval = body
        return rtnval

    rtnval = body[:comma_position]
    return rtnval


# ----------------------------------------------------------------------
# WindowsIdentityManager
# ----------------------------------------------------------------------


class WindowsIdentityManager(BaseFeatureManager):
    """
        Windows identity / domain-join manager.  Every verb is
        dispatched through the injected :class:`ICommandRunner`.
        Reads use ``Get-WmiObject Win32_ComputerSystem``,
        ``Get-ADUser`` / ``Get-ADGroup`` (with local fallback);
        writes use ``Add-Computer`` / ``Remove-Computer`` and
        ``klist purge``.
    """

    result_class = WindowsIdentityActionResult
    status_class = WindowsIdentityActionStatus
    feature_name = "identity-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def join_realm(
        self,
        spec: WindowsRealmJoinSpec,
    ) -> WindowsIdentityActionResult:
        """
            Join the host to ``spec.realm`` via ``Add-Computer``.

            The admin password is wrapped inside the scriptblock as
            ``ConvertTo-SecureString -AsPlainText -Force '<pw>'`` and
            combined with ``spec.admin_user`` to build a
            ``PSCredential``.  The recorded argv replaces every
            embedded plaintext password with ``<redacted>``.
            Idempotent: when ``Win32_ComputerSystem`` already shows
            the host joined to ``spec.realm``, returns ``SKIPPED``.

            :param spec: The :class:`WindowsRealmJoinSpec`.

            :returns: The :class:`WindowsIdentityActionResult`.
        """
        full_script = self._build_join_script(spec=spec)
        full_command = self._build_powershell_argv(script=full_script)
        recorded_command = self._sanitise_command_passwords(
            command=full_command,
        )

        if self._config.dry_run:
            dry = self._build_realm_result(
                status=WindowsIdentityActionStatus.DRY_RUN,
                realm=spec.realm,
                username=spec.admin_user,
                command=recorded_command,
            )
            return dry

        if self._is_realm_joined(realm=spec.realm):
            skipped = self._build_realm_result(
                status=WindowsIdentityActionStatus.SKIPPED,
                realm=spec.realm,
                username=spec.admin_user,
                command=recorded_command,
                message="host already joined to realm",
            )
            return skipped

        run_result = self._runner.run(
            list(full_command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsIdentityActionStatus.SUCCESS
        else:
            mapped_status = WindowsIdentityActionStatus.FAILED

        rtnval = WindowsIdentityActionResult(
            status=mapped_status,
            detail=spec.realm,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(recorded_command),
            realm=spec.realm,
            username=spec.admin_user,
        )
        return rtnval

    def leave_realm(
        self,
        admin_user: str,
        admin_password: str,
        realm: Optional[str] = None,
    ) -> WindowsIdentityActionResult:
        """
            Unjoin the host via ``Remove-Computer``.

            Windows requires admin credentials for the unjoin step in
            order to remove the computer account from the directory.
            The credential pair is therefore an explicit argument set
            rather than reusing a stored one.

            Idempotent: when ``Win32_ComputerSystem.PartOfDomain`` is
            False, returns ``SKIPPED`` without invoking
            ``Remove-Computer``.

            :param admin_user: Administrator login.
            :param admin_password: Administrator password (redacted in
                                   the recorded argv).
            :param realm: Optional realm guard.  When supplied and the
                          current realm differs (case-insensitive),
                          returns ``SKIPPED``.

            :returns: The :class:`WindowsIdentityActionResult`.
        """
        full_script = self._build_leave_script(
            admin_user=admin_user,
            admin_password=admin_password,
        )
        full_command = self._build_powershell_argv(script=full_script)
        recorded_command = self._sanitise_command_passwords(
            command=full_command,
        )

        if self._config.dry_run:
            dry = self._build_realm_result(
                status=WindowsIdentityActionStatus.DRY_RUN,
                realm=realm,
                username=admin_user,
                command=recorded_command,
            )
            return dry

        current_status = self.get_realm_status()
        if not current_status.joined:
            skipped = self._build_realm_result(
                status=WindowsIdentityActionStatus.SKIPPED,
                realm=realm,
                username=admin_user,
                command=recorded_command,
                message="host is not joined to any realm",
            )
            return skipped
        if realm is not None and current_status.current_realm is not None:
            if current_status.current_realm.lower() != realm.lower():
                skipped = self._build_realm_result(
                    status=WindowsIdentityActionStatus.SKIPPED,
                    realm=realm,
                    username=admin_user,
                    command=recorded_command,
                    message="host is joined to a different realm",
                )
                return skipped

        run_result = self._runner.run(
            list(full_command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsIdentityActionStatus.SUCCESS
        else:
            mapped_status = WindowsIdentityActionStatus.FAILED

        rtnval = WindowsIdentityActionResult(
            status=mapped_status,
            detail=realm,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(recorded_command),
            realm=realm,
            username=admin_user,
        )
        return rtnval

    def sync_users(
        self,
    ) -> WindowsIdentityActionResult:
        """
            Purge the per-system Kerberos ticket cache via
            ``klist purge -li 0x3e7`` so the next AD lookup
            re-acquires a TGT against the updated directory.

            Not idempotent — every call re-purges.

            :returns: The :class:`WindowsIdentityActionResult`.
        """
        script = "klist purge -li 0x3e7"
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_realm_result(
                status=WindowsIdentityActionStatus.DRY_RUN,
                realm=None,
                username=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsIdentityActionStatus.SUCCESS
        else:
            mapped_status = WindowsIdentityActionStatus.FAILED

        rtnval = WindowsIdentityActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            realm=None,
            username=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_realm_status(self) -> WindowsRealmStatus:
        """
            Return the host's current domain-join status by parsing
            ``Get-WmiObject Win32_ComputerSystem``.

            Reads do not elevate.

            :returns: The :class:`WindowsRealmStatus`.
            :raises WindowsIdentityQueryError: When the WMI query
                                               exits non-zero.
        """
        record = self._win32_computersystem()
        rtnval = _materialise_realm_status(record=record)
        return rtnval

    def list_joined_realms(self) -> list[WindowsRealmInfo]:
        """
            Return the realm the host is currently joined to as a
            single-element list (or empty when in a workgroup).

            :returns: Zero or one :class:`WindowsRealmInfo` records.
            :raises WindowsIdentityQueryError: When the underlying
                                               query fails.
        """
        status = self.get_realm_status()
        if not status.joined:
            rtnval: list[WindowsRealmInfo] = []
            return rtnval
        if status.current_realm is None:
            rtnval = []
            return rtnval

        info = WindowsRealmInfo(
            realm=status.current_realm,
            realm_kind=status.realm_kind or WindowsRealmKind.UNKNOWN,
            is_default=True,
            domain_controller=None,
            last_sync=None,
        )
        rtnval = [info]
        return rtnval

    def resolve_user(
        self,
        username: str,
    ) -> WindowsUserResolveResult:
        """
            Resolve a username via ``Get-ADUser`` (RSAT) with a
            ``Get-LocalUser`` fallback.  Reads do not elevate.

            :param username: The username to resolve.

            :returns: The :class:`WindowsUserResolveResult`.
        """
        ad_record = self._get_aduser_record(username=username)
        if ad_record is not None:
            rtnval = _materialise_user_resolve_from_ad(
                username=username, record=ad_record,
            )
            return rtnval

        if self._typed_config().fallback_to_local:
            local_record = self._get_localuser_record(username=username)
            if local_record is not None:
                rtnval = _materialise_user_resolve_from_local(
                    username=username, record=local_record,
                )
                return rtnval

        rtnval = WindowsUserResolveResult(
            username=username,
            found=False,
        )
        return rtnval

    def resolve_group(
        self,
        groupname: str,
    ) -> WindowsGroupResolveResult:
        """
            Resolve a groupname via ``Get-ADGroup`` (RSAT) with a
            ``Get-LocalGroup`` fallback.  Reads do not elevate.

            :param groupname: The groupname to resolve.

            :returns: The :class:`WindowsGroupResolveResult`.
        """
        ad_record = self._get_adgroup_record(groupname=groupname)
        if ad_record is not None:
            rtnval = _materialise_group_resolve_from_ad(
                groupname=groupname, record=ad_record,
            )
            return rtnval

        if self._typed_config().fallback_to_local:
            local_record = self._get_localgroup_record(groupname=groupname)
            if local_record is not None:
                rtnval = _materialise_group_resolve_from_local(
                    groupname=groupname, record=local_record,
                )
                return rtnval

        rtnval = WindowsGroupResolveResult(
            groupname=groupname,
            found=False,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv / script builders
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsIdentityManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`WindowsIdentityManagerConfig`, falling back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, WindowsIdentityManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsIdentityManagerConfig(
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
            password replaced with ``<redacted>``.

            :param command: The raw argv.

            :returns: A new list with passwords redacted.
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

    def _build_join_script(self, *, spec: WindowsRealmJoinSpec) -> str:
        """
            Build the PowerShell scriptblock for ``join_realm``.

            Renders a ``$cred = New-Object PSCredential ...`` prelude
            followed by ``Add-Computer -DomainName <realm>
            -Credential $cred ... -Force -Restart:$false``.

            :param spec: The :class:`WindowsRealmJoinSpec`.

            :returns: The scriptblock text.
        """
        escaped_realm = _ps_escape_single_quoted(value=spec.realm)
        escaped_user = _ps_escape_single_quoted(value=spec.admin_user)
        escaped_pw = _ps_escape_single_quoted(value=spec.admin_password)

        prelude = (
            "$pw = ConvertTo-SecureString -AsPlainText -Force '{pw}'; "
            "$cred = New-Object System.Management.Automation.PSCredential('{user}', $pw); "
        ).format(pw=escaped_pw, user=escaped_user)

        fragments: list[str] = []
        fragments.append("Add-Computer")
        fragments.append("-DomainName '{}'".format(escaped_realm))
        fragments.append("-Credential $cred")

        if spec.computer_name is not None:
            escaped_cn = _ps_escape_single_quoted(value=spec.computer_name)
            fragments.append("-NewName '{}'".format(escaped_cn))

        if spec.ou_path is not None:
            escaped_ou = _ps_escape_single_quoted(value=spec.ou_path)
            fragments.append("-OUPath '{}'".format(escaped_ou))

        if spec.domain_controller is not None:
            escaped_dc = _ps_escape_single_quoted(
                value=spec.domain_controller,
            )
            fragments.append("-Server '{}'".format(escaped_dc))

        fragments.append("-Force")
        fragments.append("-Restart:$false")

        rtnval = prelude + " ".join(fragments)
        return rtnval

    def _build_leave_script(
        self,
        *,
        admin_user: str,
        admin_password: str,
    ) -> str:
        """
            Build the PowerShell scriptblock for ``leave_realm``.

            :param admin_user: Administrator login.
            :param admin_password: Administrator password.

            :returns: The scriptblock text.
        """
        escaped_user = _ps_escape_single_quoted(value=admin_user)
        escaped_pw = _ps_escape_single_quoted(value=admin_password)

        rtnval = (
            "$pw = ConvertTo-SecureString -AsPlainText -Force '{pw}'; "
            "$cred = New-Object System.Management.Automation.PSCredential('{user}', $pw); "
            "Remove-Computer -UnjoinDomainCredential $cred "
            "-PassThru -Force -Restart:$false"
        ).format(pw=escaped_pw, user=escaped_user)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — dispatch + result construction
    # ------------------------------------------------------------------

    def _build_realm_result(
        self,
        *,
        status: WindowsIdentityActionStatus,
        realm: Optional[str],
        username: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsIdentityActionResult:
        """
            Construct a :class:`WindowsIdentityActionResult` without
            invoking the runner.
        """
        rtnval = WindowsIdentityActionResult(
            status=status,
            detail=realm,
            command=list(command),
            realm=realm,
            username=username,
            message=message,
            stderr=stderr,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — read probes
    # ------------------------------------------------------------------

    def _is_realm_joined(self, *, realm: str) -> bool:
        """
            Probe ``Win32_ComputerSystem`` and check whether
            ``PartOfDomain`` is True and ``Domain`` matches ``realm``
            (case-insensitive).

            :param realm: The realm of interest.

            :returns: True when joined to ``realm``.  Query failures
                      collapse to False so the caller's join attempt
                      proceeds normally.
        """
        try:
            status = self.get_realm_status()
        except WindowsIdentityQueryError:
            rtnval = False
            return rtnval

        if not status.joined:
            rtnval = False
            return rtnval
        if status.current_realm is None:
            rtnval = False
            return rtnval

        rtnval = status.current_realm.lower() == realm.lower()
        return rtnval

    def _win32_computersystem(self) -> dict:
        """
            Run ``Get-WmiObject Win32_ComputerSystem | Select Domain,
            PartOfDomain, DomainRole, Name | ConvertTo-Json`` and
            return the parsed dict.

            :returns: The parsed JSON record.
            :raises WindowsIdentityQueryError: When the query fails.
        """
        script = (
            "Get-WmiObject Win32_ComputerSystem "
            "| Select-Object Domain, PartOfDomain, DomainRole, Name "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise WindowsIdentityQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        text = run_result.stdout.strip()
        if len(text) == 0:
            rtnval: dict = {}
            return rtnval

        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            raise WindowsIdentityQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr="Win32_ComputerSystem JSON parse failed",
            )

        records = _normalize_to_list(parsed=parsed)
        if len(records) == 0:
            rtnval = {}
            return rtnval

        first = records[0]
        if not isinstance(first, dict):
            rtnval = {}
            return rtnval

        rtnval = first
        return rtnval

    def _get_aduser_record(self, *, username: str) -> Optional[dict]:
        """
            Run ``Get-ADUser -Identity <u> -Properties * |
            ConvertTo-Json`` and return the parsed dict, or ``None``
            on missing-RSAT or missing-user errors.

            :param username: The username to look up.

            :returns: The parsed record, or ``None``.
        """
        escaped = _ps_escape_single_quoted(value=username)
        script = (
            "Get-ADUser -Identity '{}' -Properties * "
            "| Select-Object SamAccountName, DistinguishedName, "
            "UserPrincipalName, MemberOf, HomeDirectory, "
            "uidNumber, gidNumber, loginShell "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval

        text = run_result.stdout.strip()
        if len(text) == 0:
            rtnval = None
            return rtnval

        # Some error paths print the canonical missing-RSAT marker on
        # stderr but exit zero through a wrapper; defensively check.
        if _looks_like_rsat_missing(stderr=run_result.stderr):
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(text)
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

        rtnval = first
        return rtnval

    def _get_adgroup_record(self, *, groupname: str) -> Optional[dict]:
        """
            Run ``Get-ADGroup -Identity <g> -Properties * |
            ConvertTo-Json``.

            :param groupname: The groupname to look up.

            :returns: The parsed record, or ``None``.
        """
        escaped = _ps_escape_single_quoted(value=groupname)
        script = (
            "Get-ADGroup -Identity '{}' -Properties * "
            "| Select-Object SamAccountName, DistinguishedName, "
            "Members, gidNumber "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval

        text = run_result.stdout.strip()
        if len(text) == 0:
            rtnval = None
            return rtnval
        if _looks_like_rsat_missing(stderr=run_result.stderr):
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(text)
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

        rtnval = first
        return rtnval

    def _get_localuser_record(self, *, username: str) -> Optional[dict]:
        """
            Run ``Get-LocalUser -Name <u> | ConvertTo-Json``.

            :param username: The username to look up.

            :returns: The parsed record, or ``None``.
        """
        escaped = _ps_escape_single_quoted(value=username)
        script = (
            "Get-LocalUser -Name '{}' "
            "| Select-Object Name, SID, Enabled, FullName, Description "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval

        text = run_result.stdout.strip()
        if len(text) == 0:
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(text)
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

        rtnval = first
        return rtnval

    def _get_localgroup_record(
        self,
        *,
        groupname: str,
    ) -> Optional[dict]:
        """
            Run ``Get-LocalGroup -Name <g> | ConvertTo-Json``.

            :param groupname: The groupname to look up.

            :returns: The parsed record (with a synthetic ``Members``
                      list pulled from ``Get-LocalGroupMember``), or
                      ``None``.
        """
        escaped = _ps_escape_single_quoted(value=groupname)
        script = (
            "Get-LocalGroup -Name '{name}' "
            "| Select-Object Name, SID, Description "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(name=escaped)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval

        text = run_result.stdout.strip()
        if len(text) == 0:
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(text)
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

        # Augment with member names from a follow-up query.
        member_script = (
            "Get-LocalGroupMember -Group '{}' "
            "| Select-Object Name "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(escaped)
        member_command = self._build_powershell_argv(script=member_script)
        member_run = self._runner.run(list(member_command), elevated=False)
        if member_run.exit_code == 0:
            member_text = member_run.stdout.strip()
            if len(member_text) > 0:
                try:
                    parsed_members = json.loads(member_text)
                except (ValueError, TypeError):
                    parsed_members = []
                member_records = _normalize_to_list(parsed=parsed_members)
                names: list[str] = []
                for record in member_records:
                    if not isinstance(record, dict):
                        continue
                    name_raw = record.get("Name")
                    if name_raw is None:
                        continue
                    name_text = str(name_raw)
                    if len(name_text) == 0:
                        continue
                    names.append(name_text)
                first["__Members"] = names

        rtnval = first
        return rtnval


# ----------------------------------------------------------------------
# Module-level materialisers
# ----------------------------------------------------------------------


def _materialise_realm_status(*, record: dict) -> WindowsRealmStatus:
    """
        Convert a parsed ``Win32_ComputerSystem`` record into a
        :class:`WindowsRealmStatus`.

        :param record: Parsed JSON record.

        :returns: The :class:`WindowsRealmStatus`.
    """
    if record is None or len(record) == 0:
        rtnval = WindowsRealmStatus(joined=False)
        return rtnval

    part_of_domain_raw = record.get("PartOfDomain")
    if isinstance(part_of_domain_raw, bool):
        part_of_domain = part_of_domain_raw
    elif isinstance(part_of_domain_raw, str):
        part_of_domain = part_of_domain_raw.strip().lower() in (
            "true", "1", "yes",
        )
    else:
        part_of_domain = False

    domain_raw = record.get("Domain")
    if domain_raw is None:
        domain_value: Optional[str] = None
    else:
        text = str(domain_raw).strip()
        if len(text) == 0:
            domain_value = None
        else:
            domain_value = text

    name_raw = record.get("Name")
    if name_raw is None:
        computer_value: Optional[str] = None
    else:
        text = str(name_raw).strip()
        if len(text) == 0:
            computer_value = None
        else:
            computer_value = text

    if part_of_domain:
        kind_value: Optional[WindowsRealmKind] = (
            WindowsRealmKind.ACTIVE_DIRECTORY
        )
        joined_value = True
    else:
        # When in a workgroup, the Domain field is the workgroup name.
        kind_value = WindowsRealmKind.WORKGROUP
        joined_value = False

    rtnval = WindowsRealmStatus(
        joined=joined_value,
        current_realm=domain_value,
        realm_kind=kind_value,
        computer_name=computer_value,
    )
    return rtnval


def _materialise_user_resolve_from_ad(
    *,
    username: str,
    record: dict,
) -> WindowsUserResolveResult:
    """
        Convert a parsed ``Get-ADUser`` JSON record into a
        :class:`WindowsUserResolveResult`.

        :param username: The supplied username.
        :param record: Parsed AD record.

        :returns: The :class:`WindowsUserResolveResult`.
    """
    realm_value = _extract_realm_from_dn(
        dn=record.get("DistinguishedName"),
    )

    uid_raw = record.get("uidNumber")
    if isinstance(uid_raw, int):
        uid_value: Optional[int] = uid_raw
    elif isinstance(uid_raw, str) and len(uid_raw.strip()) > 0:
        try:
            uid_value = int(uid_raw.strip())
        except ValueError:
            uid_value = None
    else:
        uid_value = None

    gid_raw = record.get("gidNumber")
    if isinstance(gid_raw, int):
        gid_value: Optional[int] = gid_raw
    elif isinstance(gid_raw, str) and len(gid_raw.strip()) > 0:
        try:
            gid_value = int(gid_raw.strip())
        except ValueError:
            gid_value = None
    else:
        gid_value = None

    home_raw = record.get("HomeDirectory")
    if home_raw is None or len(str(home_raw)) == 0:
        home_value: Optional[str] = None
    else:
        home_value = str(home_raw)

    shell_raw = record.get("loginShell")
    if shell_raw is None or len(str(shell_raw)) == 0:
        shell_value: Optional[str] = None
    else:
        shell_value = str(shell_raw)

    member_of_raw = record.get("MemberOf")
    groups: list[str] = []
    if isinstance(member_of_raw, list):
        for entry in member_of_raw:
            if not isinstance(entry, str):
                continue
            cn_value = _strip_dn_to_cn(dn=entry)
            if len(cn_value) > 0:
                groups.append(cn_value)
    elif isinstance(member_of_raw, str):
        cn_value = _strip_dn_to_cn(dn=member_of_raw)
        if len(cn_value) > 0:
            groups.append(cn_value)

    rtnval = WindowsUserResolveResult(
        username=username,
        uid=uid_value,
        gid=gid_value,
        home=home_value,
        shell=shell_value,
        realm=realm_value,
        groups=groups,
        found=True,
    )
    return rtnval


def _materialise_user_resolve_from_local(
    *,
    username: str,
    record: dict,
) -> WindowsUserResolveResult:
    """
        Convert a parsed ``Get-LocalUser`` JSON record into a
        :class:`WindowsUserResolveResult`.

        :param username: The supplied username.
        :param record: Parsed local-user record.

        :returns: The :class:`WindowsUserResolveResult` with
                  ``realm=None`` (local accounts have no realm).
    """
    rtnval = WindowsUserResolveResult(
        username=username,
        uid=None,
        gid=None,
        home=None,
        shell=None,
        realm=None,
        groups=[],
        found=True,
    )
    return rtnval


def _materialise_group_resolve_from_ad(
    *,
    groupname: str,
    record: dict,
) -> WindowsGroupResolveResult:
    """
        Convert a parsed ``Get-ADGroup`` JSON record into a
        :class:`WindowsGroupResolveResult`.

        :param groupname: The supplied groupname.
        :param record: Parsed AD record.

        :returns: The :class:`WindowsGroupResolveResult`.
    """
    realm_value = _extract_realm_from_dn(
        dn=record.get("DistinguishedName"),
    )

    gid_raw = record.get("gidNumber")
    if isinstance(gid_raw, int):
        gid_value: Optional[int] = gid_raw
    elif isinstance(gid_raw, str) and len(gid_raw.strip()) > 0:
        try:
            gid_value = int(gid_raw.strip())
        except ValueError:
            gid_value = None
    else:
        gid_value = None

    members_raw = record.get("Members")
    members: list[str] = []
    if isinstance(members_raw, list):
        for entry in members_raw:
            if not isinstance(entry, str):
                continue
            cn_value = _strip_dn_to_cn(dn=entry)
            if len(cn_value) > 0:
                members.append(cn_value)
    elif isinstance(members_raw, str):
        cn_value = _strip_dn_to_cn(dn=members_raw)
        if len(cn_value) > 0:
            members.append(cn_value)

    rtnval = WindowsGroupResolveResult(
        groupname=groupname,
        gid=gid_value,
        members=members,
        realm=realm_value,
        found=True,
    )
    return rtnval


def _materialise_group_resolve_from_local(
    *,
    groupname: str,
    record: dict,
) -> WindowsGroupResolveResult:
    """
        Convert a parsed ``Get-LocalGroup`` JSON record (augmented
        with a ``__Members`` list pulled from
        ``Get-LocalGroupMember``) into a
        :class:`WindowsGroupResolveResult`.

        :param groupname: The supplied groupname.
        :param record: Parsed local-group record.

        :returns: The :class:`WindowsGroupResolveResult` with
                  ``realm=None``.
    """
    members_raw = record.get("__Members")
    if isinstance(members_raw, list):
        members: list[str] = []
        for entry in members_raw:
            if not isinstance(entry, str):
                continue
            if len(entry) == 0:
                continue
            members.append(entry)
    else:
        members = []

    rtnval = WindowsGroupResolveResult(
        groupname=groupname,
        gid=None,
        members=members,
        realm=None,
        found=True,
    )
    return rtnval


def _extract_realm_from_dn(*, dn) -> Optional[str]:
    """
        Pull the AD realm out of a DistinguishedName by stitching
        together the ``DC=`` components.

        ``"CN=alice,OU=Users,DC=ad,DC=example,DC=com"`` becomes
        ``"ad.example.com"``.

        :param dn: The raw DistinguishedName (or ``None``).

        :returns: The realm name, or ``None`` when no ``DC=``
                  components are present.
    """
    if dn is None:
        rtnval: Optional[str] = None
        return rtnval

    text = str(dn).strip()
    if len(text) == 0:
        rtnval = None
        return rtnval

    parts = text.split(",")
    dc_components: list[str] = []
    for part in parts:
        cleaned = part.strip()
        if cleaned[:3].upper() == "DC=":
            value = cleaned[3:]
            if len(value) > 0:
                dc_components.append(value)

    if len(dc_components) == 0:
        rtnval = None
        return rtnval

    rtnval = ".".join(dc_components)
    return rtnval
