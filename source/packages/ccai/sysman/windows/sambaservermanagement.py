"""
    Windows samba-server-management feature.

    Implements the cross-OS samba-server-management contract for
    Windows using the PowerShell ``SmbShare`` / ``SmbShareAccess``
    cmdlet family (``New-SmbShare`` / ``Remove-SmbShare`` /
    ``Get-SmbShare`` / ``Grant-SmbShareAccess`` /
    ``Revoke-SmbShareAccess`` / ``Get-SmbShareAccess``).  Every
    cmdlet is dispatched through the injected
    :class:`ICommandRunner` so the manager works identically under
    local and SSH execution.

    Native SMB vs Samba distinction:
        Windows ships its own SMB server as part of the base
        operating system; the Samba project's binary is NOT used.
        There is no ``smb.conf``, no ``smbpasswd`` user database,
        and no ``pdbedit`` enumeration tool.  Authentication
        against an SMB share routes through the local account
        database (or a domain controller for domain-joined hosts).
        ``add_samba_user`` and ``remove_samba_user`` are therefore
        delegated to ``user-group-management`` and return
        ``status=FAILED`` with an explanatory ``message`` and
        ``detail``.  ``list_samba_users`` returns the SMB-share
        access map (``Get-SmbShareAccess``) as the closest
        available analogue.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Windows tool (PowerShell)         |
    +=============================+===================================+
    | ``add_share``               | ``New-SmbShare -Name '<n>'        |
    |                             | -Path '<p>' -Description '<d>'    |
    |                             | -FullAccess <users>``             |
    +-----------------------------+-----------------------------------+
    | ``remove_share``            | ``Remove-SmbShare -Name '<n>'     |
    |                             | -Force``                          |
    +-----------------------------+-----------------------------------+
    | ``list_shares``             | ``Get-SmbShare \\| ConvertTo-Json |
    |                             | -Compress -Depth 5``              |
    +-----------------------------+-----------------------------------+
    | ``get_share``               | ``Get-SmbShare -Name '<n>'        |
    |                             | \\| ConvertTo-Json``              |
    +-----------------------------+-----------------------------------+
    | ``set_share_permissions``   | ``Grant-SmbShareAccess`` /        |
    |                             | ``Revoke-SmbShareAccess`` per     |
    |                             | principal                         |
    +-----------------------------+-----------------------------------+
    | ``reload_config``           | NO-OP SUCCESS — Windows commits   |
    |                             | SMB share changes synchronously   |
    +-----------------------------+-----------------------------------+
    | ``add_samba_user``          | DELEGATED → user-group-management |
    +-----------------------------+-----------------------------------+
    | ``remove_samba_user``       | DELEGATED → user-group-management |
    +-----------------------------+-----------------------------------+
    | ``list_samba_users``        | ``Get-SmbShareAccess -Name '*'    |
    |                             | \\| ConvertTo-Json``              |
    +-----------------------------+-----------------------------------+

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<scriptblock>"]`` so that ``$PROFILE`` cannot
        mutate cmdlet behaviour and PowerShell never blocks
        waiting for interactive input.

    Reload semantics:
        Windows has no per-share reload cmdlet — ``New-SmbShare`` /
        ``Remove-SmbShare`` / ``Grant-SmbShareAccess`` /
        ``Revoke-SmbShareAccess`` update the daemon's runtime
        state synchronously.  ``reload_config`` therefore returns
        ``status=SUCCESS`` immediately without invoking the runner;
        the recorded ``command`` field is a sanitised placeholder
        scriptblock.

    Idempotency:
        * ``add_share`` probes ``Get-SmbShare -Name <name>`` first;
          returns ``SKIPPED`` when a share with that name already
          maps to the same path.
        * ``remove_share`` looks the share up by name; returns
          ``SKIPPED`` when no share is registered.
        * ``set_share_permissions`` reads the existing access map
          via ``Get-SmbShareAccess`` and suppresses grants /
          revokes that already match.
        * ``list_shares`` / ``get_share`` / ``list_samba_users``
          never skip.
        * ``reload_config`` never skips — it always reports
          SUCCESS / DRY_RUN.

    Credential handling:
        ``add_samba_user`` is delegated and never invokes the
        runner with the password.  The recorded ``command`` field
        is a sanitised placeholder PowerShell scriptblock
        documenting the would-be tool but containing neither the
        password nor any password-equivalent.

    Elevation policy:
        Write verbs honour ``config.elevated`` when they actually
        dispatch.  Reads (``list_shares``, ``get_share``,
        ``list_samba_users``) and idempotency probes
        (``Get-SmbShare -Name``, ``Get-SmbShareAccess``) never
        elevate.
"""

import json as _json
import os

from dataclasses import dataclass, field
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

_RELOAD_PLACEHOLDER_SCRIPT = (
    "# samba reload_config is a no-op on Windows; "
    "SMB share writes commit synchronously."
)

_DELEGATION_DETAIL_ADD = (
    "add_samba_user delegated to user-group-management"
)
_DELEGATION_DETAIL_REMOVE = (
    "remove_samba_user delegated to user-group-management"
)
_DELEGATION_MESSAGE = (
    "Windows SMB authentication uses the local / domain user "
    "directory directly; create or remove the underlying "
    "account through the user-group-management feature before "
    "publishing or withdrawing share access."
)
_DELEGATION_PLACEHOLDER_ADD = "# delegated: New-LocalUser"
_DELEGATION_PLACEHOLDER_REMOVE = "# delegated: Remove-LocalUser"


WindowsSambaActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


def _empty_str_list() -> list:
    """
        Build a fresh empty list of strings.

        :returns: An empty list.
    """
    rtnval: list = []
    return rtnval


@dataclass
class WindowsSambaManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsSambaServerManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
        :param delegate_user_mgmt: When ``True`` (the default),
                                   ``add_samba_user`` and
                                   ``remove_samba_user`` return
                                   ``FAILED`` with a delegation
                                   message directing callers to
                                   the ``user-group-management``
                                   feature.  Reserved for future
                                   opt-in to ``New-LocalUser``
                                   dispatch.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    delegate_user_mgmt: bool = True


@dataclass
class WindowsSambaActionResult(FeatureActionResult):
    """
        Result of a samba-server write operation.

        :param share_name: The share name the action targeted, or
                           ``None``.
        :param username: The user the action targeted, or ``None``.
        :param message: Optional human-readable detail for SKIPPED /
                        FAILED outcomes.
    """
    share_name: Optional[str] = None
    username: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsSambaShareSpec:
    """
        Declarative spec for a single Windows SMB share.

        :param name: The SMB-visible share name.
        :param path: The disk path the share is rooted at (e.g.,
                     ``"D:\\Shares\\Public"``).
        :param description: Optional free-form description.
                            Rendered as the
                            ``-Description '<value>'`` parameter.
        :param read_only: Whether the share is read-only.  When
                          ``True`` the rendered cmdlet uses
                          ``-ReadAccess`` for the ``valid_users``
                          set instead of ``-FullAccess``.  Default
                          ``False``.
        :param valid_users: Optional list of users granted full
                            access.  When ``None`` and the spec is
                            not read-only, the rendered cmdlet
                            falls back to ``-FullAccess Everyone``
                            (matching macOS / Linux's
                            ``guest_ok=False`` semantics requires
                            an explicit ACL).
        :param read_list: Optional list of users granted read
                          access.  Mapped to ``-ReadAccess``.
        :param write_list: Optional list of users granted change
                           access.  Mapped to ``-ChangeAccess``.
        :param browseable: Whether the share is browseable.
                           Mapped to ``-FolderEnumerationMode
                           Unrestricted`` when ``True`` and
                           ``AccessBased`` when ``False``.  Default
                           ``True``.
        :param guest_ok: Whether guest access is permitted.  When
                         ``True`` ``Everyone`` is appended to the
                         full-access set.  Default ``False``.
    """
    name: str
    path: str
    description: Optional[str] = None
    read_only: bool = False
    valid_users: Optional[list] = None
    read_list: Optional[list] = None
    write_list: Optional[list] = None
    browseable: bool = True
    guest_ok: bool = False


@dataclass
class WindowsSambaShareInfo:
    """
        One parsed share record from ``Get-SmbShare``.

        :param name: The share name.
        :param path: The disk path the share is rooted at.
        :param description: The share description (``Description``
                            field), or ``None`` when absent.
        :param read_only: Best-effort read-only flag derived from
                          the share's access map.
        :param valid_users: Configured full-access users, or
                            ``None`` when the access map has none.
        :param read_list: Configured read-access users, or ``None``.
        :param write_list: Configured change-access users, or
                           ``None``.
        :param browseable: ``True`` when ``FolderEnumerationMode``
                           is ``Unrestricted``.
        :param guest_ok: ``True`` when ``Everyone`` has any access.
        :param is_active: Always ``True`` for entries returned by
                          ``Get-SmbShare`` (the cmdlet only reports
                          live shares).
        :param connection_count: ``CurrentUsers`` field from
                                 ``Get-SmbShare`` when reported,
                                 else ``None``.
    """
    name: str
    path: str
    description: Optional[str]
    read_only: bool
    valid_users: Optional[list]
    read_list: Optional[list]
    write_list: Optional[list]
    browseable: bool
    guest_ok: bool
    is_active: bool
    connection_count: Optional[int]


@dataclass
class WindowsSambaShareAcl:
    """
        Share-level ACL spec for
        :meth:`WindowsSambaServerManager.set_share_permissions`.

        :param read_users: Users granted ``Read`` access.
        :param write_users: Users granted ``Change`` access.
        :param full_access_users: Users granted ``Full`` access.
        :param denied_users: Users explicitly revoked.
    """
    read_users: list = field(default_factory=_empty_str_list)
    write_users: list = field(default_factory=_empty_str_list)
    full_access_users: list = field(default_factory=_empty_str_list)
    denied_users: list = field(default_factory=_empty_str_list)


@dataclass
class WindowsSambaUserInfo:
    """
        One parsed user record derived from
        ``Get-SmbShareAccess``.

        :param username: The principal account name.
        :param enabled: ``True`` when the principal has at least
                        one ``Allow`` access entry across any
                        share.  ``False`` when the principal
                        appears only as a ``Deny`` entry.
        :param last_password_change: Always ``None`` on Windows
                                     (the SMB cmdlets do not report
                                     password metadata).
        :param description: A space-separated list of access rights
                            held by the principal (e.g., ``"Read,
                            Change"``), or ``None`` when the
                            principal is denied-only.
    """
    username: str
    enabled: bool
    last_password_change: Optional[str]
    description: Optional[str]


class WindowsSambaQueryError(Exception):
    """
        Raised when a Windows Samba read fails for a reason other
        than the target simply being absent.
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

        err_msg_lines = ["Windows Samba query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsSambaServerManager(BaseFeatureManager):
    """
        Windows samba-server-management manager wrapping the
        PowerShell ``SmbShare`` / ``SmbShareAccess`` cmdlet family.
    """

    result_class = WindowsSambaActionResult
    status_class = WindowsSambaActionStatus
    feature_name = "samba-server-management"

    def __init__(
        self,
        runner,
        config: Optional[WindowsSambaManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh
                           :class:`WindowsSambaManagerConfig`.
        """
        if config is None:
            config = WindowsSambaManagerConfig()
        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_shares(self) -> list:
        """
            Enumerate every registered SMB share via
            ``Get-SmbShare | ConvertTo-Json``.

            :returns: A ``list[WindowsSambaShareInfo]``.

            :raises WindowsSambaQueryError: When PowerShell exits
                                            non-zero with non-empty
                                            stderr.
        """
        script = (
            "Get-SmbShare | ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) > 0:
                raise WindowsSambaQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval: list = []
            return rtnval

        access_map = self._read_share_access_map_safe()
        rtnval = _parse_shares_json(
            text=run_result.stdout, access_map=access_map,
        )
        return rtnval

    def get_share(
        self,
        name: str,
    ) -> Optional[WindowsSambaShareInfo]:
        """
            Look up a single share by name.

            :param name: The share name.

            :returns: The :class:`WindowsSambaShareInfo`, or
                      ``None`` when no matching share exists.
        """
        script = (
            "Get-SmbShare -Name '{}' "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(_ps_quote(value=name))
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval: Optional[WindowsSambaShareInfo] = None
            return rtnval

        access_map = self._read_share_access_map_safe()
        records = _parse_shares_json(
            text=run_result.stdout, access_map=access_map,
        )
        for entry in records:
            if entry.name == name:
                rtnval = entry
                return rtnval
        rtnval = None
        return rtnval

    def list_samba_users(self) -> list:
        """
            Enumerate the SMB-share access map via
            ``Get-SmbShareAccess -Name '*' | ConvertTo-Json``.
            Returns one :class:`WindowsSambaUserInfo` per unique
            principal observed across the access map.

            :returns: A ``list[WindowsSambaUserInfo]``.

            :raises WindowsSambaQueryError: When PowerShell exits
                                            non-zero with non-empty
                                            stderr.
        """
        script = (
            "Get-SmbShareAccess -Name '*' "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) > 0:
                raise WindowsSambaQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval: list = []
            return rtnval

        records = _decode_share_access_records(text=run_result.stdout)
        rtnval = _users_from_access_records(records=records)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — share CRUD
    # ------------------------------------------------------------------

    def add_share(
        self,
        spec: WindowsSambaShareSpec,
    ) -> WindowsSambaActionResult:
        """
            Register a new SMB share via ``New-SmbShare``.

            Idempotent: returns ``SKIPPED`` when a share with the
            same name and path is already registered.  Honours
            dry-run.

            :param spec: The :class:`WindowsSambaShareSpec`.

            :returns: The :class:`WindowsSambaActionResult`.
        """
        script = _build_new_smb_share_script(spec=spec)
        command = self._build_powershell_argv(script=script)

        if self._share_exists_with_same_path(
            name=spec.name, path=spec.path,
        ):
            skipped = self._build_result(
                status=WindowsSambaActionStatus.SKIPPED,
                share_name=spec.name,
                username=None,
                command=command,
                message="share already at desired state",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSambaActionStatus.DRY_RUN,
                share_name=spec.name,
                username=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            share_name=spec.name,
            username=None,
        )
        return rtnval

    def remove_share(self, name: str) -> WindowsSambaActionResult:
        """
            Remove an SMB share via ``Remove-SmbShare -Force``.

            Idempotent: returns ``SKIPPED`` when no share is
            registered under ``name``.  Honours dry-run.

            :param name: The share name.

            :returns: The :class:`WindowsSambaActionResult`.
        """
        script = _build_remove_smb_share_script(share_name=name)
        command = self._build_powershell_argv(script=script)

        if not self._share_exists_by_name(name=name):
            skipped = self._build_result(
                status=WindowsSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=command,
                message="share not present",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSambaActionStatus.DRY_RUN,
                share_name=name,
                username=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            share_name=name,
            username=None,
        )
        return rtnval

    def set_share_permissions(
        self,
        name: str,
        acl: WindowsSambaShareAcl,
    ) -> WindowsSambaActionResult:
        """
            Update the share-level ACL via the
            ``Grant-SmbShareAccess`` / ``Revoke-SmbShareAccess``
            cmdlets.

            Each principal in ``acl.read_users`` /
            ``acl.write_users`` / ``acl.full_access_users`` is
            granted the corresponding access right; each principal
            in ``acl.denied_users`` is revoked.  Existing entries
            that already match are suppressed.

            Idempotent: returns ``SKIPPED`` when no share is
            registered under ``name`` or when the existing access
            map already matches the requested ACL.  Honours
            dry-run.

            :param name: The share name.
            :param acl: The :class:`WindowsSambaShareAcl`.

            :returns: The :class:`WindowsSambaActionResult`.
        """
        if not self._share_exists_by_name(name=name):
            placeholder_script = (
                "Get-SmbShareAccess -Name '{}' "
                "| ConvertTo-Json -Compress -Depth 5"
            ).format(_ps_quote(value=name))
            placeholder = self._build_powershell_argv(
                script=placeholder_script,
            )
            skipped = self._build_result(
                status=WindowsSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=placeholder,
                message="share not present",
            )
            return skipped

        existing_access = self._read_share_access_for_share(name=name)
        change_set = _compute_acl_change_set(
            existing=existing_access, acl=acl,
        )
        if len(change_set) == 0:
            placeholder_script = (
                "Get-SmbShareAccess -Name '{}' "
                "| ConvertTo-Json -Compress -Depth 5"
            ).format(_ps_quote(value=name))
            placeholder = self._build_powershell_argv(
                script=placeholder_script,
            )
            skipped = self._build_result(
                status=WindowsSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=placeholder,
                message="acl already at desired value",
            )
            return skipped

        first_command = self._build_powershell_argv(
            script=change_set[0],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSambaActionStatus.DRY_RUN,
                share_name=name,
                username=None,
                command=first_command,
            )
            return dry

        last_run = None
        last_command = first_command
        for change_script in change_set:
            command = self._build_powershell_argv(script=change_script)
            last_command = command
            run_result = self._runner.run(
                list(command),
                elevated=self._config.elevated,
            )
            last_run = run_result
            if run_result.exit_code != 0:
                failed = WindowsSambaActionResult(
                    status=WindowsSambaActionStatus.FAILED,
                    detail="set_share_permissions:{}".format(name),
                    exit_code=run_result.exit_code,
                    stdout=run_result.stdout,
                    stderr=run_result.stderr,
                    command=list(command),
                    share_name=name,
                    username=None,
                )
                return failed

        if last_run is None:
            placeholder_script = (
                "Get-SmbShareAccess -Name '{}' "
                "| ConvertTo-Json -Compress -Depth 5"
            ).format(_ps_quote(value=name))
            placeholder = self._build_powershell_argv(
                script=placeholder_script,
            )
            skipped = self._build_result(
                status=WindowsSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=placeholder,
                message="no acl changes required",
            )
            return skipped

        rtnval = WindowsSambaActionResult(
            status=WindowsSambaActionStatus.SUCCESS,
            detail="set_share_permissions:{}".format(name),
            exit_code=last_run.exit_code,
            stdout=last_run.stdout,
            stderr=last_run.stderr,
            command=list(last_command),
            share_name=name,
            username=None,
        )
        return rtnval

    def reload_config(self) -> WindowsSambaActionResult:
        """
            Reload the SMB server configuration.

            On Windows this is a NO-OP that returns ``SUCCESS``
            immediately without invoking the runner — the SMB
            cmdlets commit changes synchronously, so there is no
            per-share reload analogue to Linux's ``smbcontrol`` or
            macOS's ``launchctl kickstart``.  The recorded
            ``command`` field is a sanitised placeholder
            scriptblock so callers can inspect the no-op.

            Honours dry-run (returns DRY_RUN with the same
            placeholder argv).

            :returns: The :class:`WindowsSambaActionResult`.
        """
        command = self._build_powershell_argv(
            script=_RELOAD_PLACEHOLDER_SCRIPT,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSambaActionStatus.DRY_RUN,
                share_name=None,
                username=None,
                command=command,
            )
            return dry

        rtnval = WindowsSambaActionResult(
            status=WindowsSambaActionStatus.SUCCESS,
            detail="reload_config:no-op",
            exit_code=0,
            stdout="",
            stderr="",
            command=list(command),
            share_name=None,
            username=None,
            message=(
                "Windows commits SMB share changes synchronously; "
                "reload_config is a no-op."
            ),
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — Samba user management (delegated)
    # ------------------------------------------------------------------

    def add_samba_user(
        self,
        name: str,
        password: str,
    ) -> WindowsSambaActionResult:
        """
            Add a Samba user.  On Windows this surface is the
            local / domain account directory and is delegated to
            ``user-group-management``.  Returns ``FAILED`` with a
            delegation message.

            The ``password`` parameter is accepted for API parity
            with Ubuntu but is NEVER forwarded to the runner.  The
            recorded ``command`` field is a sanitised placeholder
            PowerShell scriptblock that documents the would-be
            tool but contains neither the password nor any
            password-equivalent.

            :param name: The user's login name.
            :param password: The plaintext password.  Discarded
                             without traversing the runner.

            :returns: The :class:`WindowsSambaActionResult`.
        """
        _ = password

        sanitised_command = self._build_powershell_argv(
            script=_DELEGATION_PLACEHOLDER_ADD,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSambaActionStatus.DRY_RUN,
                share_name=None,
                username=name,
                command=sanitised_command,
                message=_DELEGATION_MESSAGE,
            )
            dry.detail = _DELEGATION_DETAIL_ADD
            return dry

        failed = WindowsSambaActionResult(
            status=WindowsSambaActionStatus.FAILED,
            detail=_DELEGATION_DETAIL_ADD,
            exit_code=None,
            stdout=None,
            stderr=_DELEGATION_MESSAGE,
            command=list(sanitised_command),
            share_name=None,
            username=name,
            message=_DELEGATION_MESSAGE,
        )
        return failed

    def remove_samba_user(
        self,
        name: str,
    ) -> WindowsSambaActionResult:
        """
            Remove a Samba user.  Delegated to
            ``user-group-management``; returns ``FAILED`` with the
            delegation message.

            :param name: The user's login name.

            :returns: The :class:`WindowsSambaActionResult`.
        """
        sanitised_command = self._build_powershell_argv(
            script=_DELEGATION_PLACEHOLDER_REMOVE,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSambaActionStatus.DRY_RUN,
                share_name=None,
                username=name,
                command=sanitised_command,
                message=_DELEGATION_MESSAGE,
            )
            dry.detail = _DELEGATION_DETAIL_REMOVE
            return dry

        failed = WindowsSambaActionResult(
            status=WindowsSambaActionStatus.FAILED,
            detail=_DELEGATION_DETAIL_REMOVE,
            exit_code=None,
            stdout=None,
            stderr=_DELEGATION_MESSAGE,
            command=list(sanitised_command),
            share_name=None,
            username=name,
            message=_DELEGATION_MESSAGE,
        )
        return failed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsSambaManagerConfig:
        """
            Return the manager's config narrowed to the typed
            subclass.

            :returns: The :class:`WindowsSambaManagerConfig`.
        """
        if isinstance(self._config, WindowsSambaManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsSambaManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical PowerShell argv for a scriptblock.

            :param script: The PowerShell scriptblock text.

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

    def _share_exists_with_same_path(
        self,
        *,
        name: str,
        path: str,
    ) -> bool:
        """
            Probe whether a share with the given name and path is
            registered.

            :param name: The share name.
            :param path: The expected disk path.

            :returns: ``True`` when an existing share matches both.
        """
        record = self._lookup_share_by_name_via_runner(name=name)
        if record is None:
            rtnval = False
            return rtnval
        if record.path == path:
            rtnval = True
            return rtnval
        rtnval = False
        return rtnval

    def _share_exists_by_name(self, *, name: str) -> bool:
        """
            Probe whether a share with the given name is
            registered.

            :param name: The share name.

            :returns: ``True`` when the share exists.
        """
        record = self._lookup_share_by_name_via_runner(name=name)
        rtnval = record is not None
        return rtnval

    def _lookup_share_by_name_via_runner(
        self,
        *,
        name: str,
    ) -> Optional[WindowsSambaShareInfo]:
        """
            Probe ``Get-SmbShare -Name '<name>'`` and decode the
            single-record JSON.

            :param name: The share name.

            :returns: The matching info record, or ``None``.
        """
        script = (
            "Get-SmbShare -Name '{}' "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(_ps_quote(value=name))
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[WindowsSambaShareInfo] = None
            return rtnval
        access_map = self._read_share_access_map_safe()
        records = _parse_shares_json(
            text=run_result.stdout, access_map=access_map,
        )
        for entry in records:
            if entry.name == name:
                rtnval = entry
                return rtnval
        rtnval = None
        return rtnval

    def _read_share_access_map_safe(self) -> dict:
        """
            Build a ``{share_name: list[record]}`` map of every
            access entry across every share.  Failures here
            (cmdlet missing, no shares yet) return an empty dict.

            :returns: The access map.
        """
        script = (
            "Get-SmbShareAccess -Name '*' "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: dict = {}
            return rtnval
        records = _decode_share_access_records(text=run_result.stdout)
        rtnval = {}
        for record in records:
            share_name = record.get("Name")
            if not isinstance(share_name, str):
                continue
            if share_name not in rtnval:
                rtnval[share_name] = []
            rtnval[share_name].append(record)
        return rtnval

    def _read_share_access_for_share(
        self,
        *,
        name: str,
    ) -> list:
        """
            Read the access entries for a single share.

            :param name: The share name.

            :returns: A ``list[dict]`` of decoded JSON records.
        """
        script = (
            "Get-SmbShareAccess -Name '{}' "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(_ps_quote(value=name))
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = _decode_share_access_records(text=run_result.stdout)
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsSambaActionStatus,
        share_name: Optional[str],
        username: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> WindowsSambaActionResult:
        """
            Build a result without invoking the runner.
        """
        if share_name is not None:
            detail_text = share_name
        elif username is not None:
            detail_text = username
        else:
            detail_text = None
        rtnval = WindowsSambaActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            share_name=share_name,
            username=username,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        share_name: Optional[str],
        username: Optional[str],
    ) -> WindowsSambaActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`WindowsSambaActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = WindowsSambaActionStatus.SUCCESS
        else:
            mapped_status = WindowsSambaActionStatus.FAILED

        if share_name is not None:
            detail_text = share_name
        elif username is not None:
            detail_text = username
        else:
            detail_text = None

        rtnval = WindowsSambaActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            share_name=share_name,
            username=username,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PowerShell escaping
# ----------------------------------------------------------------------


def _ps_quote(*, value: str) -> str:
    """
        Escape ``value`` for inclusion inside PowerShell single-
        quoted strings.

        PowerShell single-quoted strings escape an embedded single
        quote by doubling it.

        :param value: The raw text.

        :returns: The escaped text (without surrounding quotes).
    """
    if value is None:
        rtnval = ""
        return rtnval
    rtnval = value.replace("'", "''")
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — scriptblock builders
# ----------------------------------------------------------------------


def _build_new_smb_share_script(
    *,
    spec: WindowsSambaShareSpec,
) -> str:
    """
        Render the ``New-SmbShare`` scriptblock for the spec.

        :param spec: The :class:`WindowsSambaShareSpec`.

        :returns: The scriptblock string.
    """
    name_token = _ps_quote(value=spec.name)
    path_token = _ps_quote(value=spec.path)
    parts: list = []
    parts.append(
        "New-SmbShare -Name '{name}' -Path '{path}'".format(
            name=name_token, path=path_token,
        ),
    )
    if spec.description is not None and len(spec.description) > 0:
        parts.append(
            "-Description '{}'".format(_ps_quote(value=spec.description)),
        )

    if spec.read_only:
        if spec.valid_users is not None and len(spec.valid_users) > 0:
            parts.append(
                "-ReadAccess {}".format(
                    _format_principal_csv(values=spec.valid_users),
                ),
            )
        else:
            parts.append("-ReadAccess 'Everyone'")
    else:
        if spec.valid_users is not None and len(spec.valid_users) > 0:
            parts.append(
                "-FullAccess {}".format(
                    _format_principal_csv(values=spec.valid_users),
                ),
            )
        elif spec.guest_ok:
            parts.append("-FullAccess 'Everyone'")

    if spec.read_list is not None and len(spec.read_list) > 0:
        parts.append(
            "-ReadAccess {}".format(
                _format_principal_csv(values=spec.read_list),
            ),
        )
    if spec.write_list is not None and len(spec.write_list) > 0:
        parts.append(
            "-ChangeAccess {}".format(
                _format_principal_csv(values=spec.write_list),
            ),
        )

    if spec.browseable:
        parts.append("-FolderEnumerationMode Unrestricted")
    else:
        parts.append("-FolderEnumerationMode AccessBased")

    rtnval = " ".join(parts)
    return rtnval


def _build_remove_smb_share_script(*, share_name: str) -> str:
    """
        Render the ``Remove-SmbShare -Force`` scriptblock.

        :param share_name: The share name.

        :returns: The scriptblock string.
    """
    name_token = _ps_quote(value=share_name)
    rtnval = (
        "Remove-SmbShare -Name '{name}' -Force"
    ).format(name=name_token)
    return rtnval


def _build_grant_smb_share_access_script(
    *,
    share_name: str,
    account: str,
    access_right: str,
) -> str:
    """
        Render the ``Grant-SmbShareAccess`` scriptblock.

        :param share_name: The share name.
        :param account: The principal account name.
        :param access_right: One of ``"Read"``, ``"Change"``,
                             ``"Full"``.

        :returns: The scriptblock string.
    """
    name_token = _ps_quote(value=share_name)
    account_token = _ps_quote(value=account)
    rtnval = (
        "Grant-SmbShareAccess -Name '{name}' "
        "-AccountName '{account}' "
        "-AccessRight {access_right} -Force"
    ).format(
        name=name_token,
        account=account_token,
        access_right=access_right,
    )
    return rtnval


def _build_revoke_smb_share_access_script(
    *,
    share_name: str,
    account: str,
) -> str:
    """
        Render the ``Revoke-SmbShareAccess`` scriptblock.

        :param share_name: The share name.
        :param account: The principal account name.

        :returns: The scriptblock string.
    """
    name_token = _ps_quote(value=share_name)
    account_token = _ps_quote(value=account)
    rtnval = (
        "Revoke-SmbShareAccess -Name '{name}' "
        "-AccountName '{account}' -Force"
    ).format(name=name_token, account=account_token)
    return rtnval


def _format_principal_csv(*, values: list) -> str:
    """
        Render a list of principal names as a comma-separated
        single-quoted set acceptable to ``-FullAccess`` /
        ``-ReadAccess`` / ``-ChangeAccess``.

        :param values: The principals.

        :returns: A string of the form ``'user1','user2'``.
    """
    quoted: list = []
    for value in values:
        quoted.append("'{}'".format(_ps_quote(value=value)))
    rtnval = ",".join(quoted)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — JSON parsers
# ----------------------------------------------------------------------


def _decode_json(*, text: str):
    """
        Decode a PowerShell ``ConvertTo-Json`` payload, normalising
        the single-record-vs-list shape divergence.  Returns ``[]``
        when the payload is empty or invalid.

        :param text: The raw JSON text.

        :returns: A list of records.
    """
    stripped = text.strip()
    if len(stripped) == 0:
        rtnval: list = []
        return rtnval

    try:
        decoded = _json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = []
        return rtnval

    if isinstance(decoded, dict):
        rtnval = [decoded]
        return rtnval
    if isinstance(decoded, list):
        rtnval = decoded
        return rtnval
    rtnval = []
    return rtnval


def _parse_shares_json(
    *,
    text: str,
    access_map: dict,
) -> list:
    """
        Parse the JSON output of ``Get-SmbShare`` into a list of
        :class:`WindowsSambaShareInfo`.

        :param text: The raw JSON stdout.
        :param access_map: A ``{share_name: list[access_record]}``
                           map used to populate the per-share ACL
                           fields.

        :returns: A list of share info records.
    """
    rtnval: list = []
    records = _decode_json(text=text)
    for record in records:
        if not isinstance(record, dict):
            continue
        info = _build_share_info(
            record=record, access_map=access_map,
        )
        if info is None:
            continue
        rtnval.append(info)
    return rtnval


def _build_share_info(
    *,
    record: dict,
    access_map: dict,
) -> Optional[WindowsSambaShareInfo]:
    """
        Build a :class:`WindowsSambaShareInfo` from a decoded JSON
        record.

        :param record: The decoded JSON dict.
        :param access_map: The pre-fetched access map.

        :returns: The populated info, or ``None`` when essential
                  fields are missing.
    """
    name_value = record.get("Name")
    if name_value is None:
        rtnval: Optional[WindowsSambaShareInfo] = None
        return rtnval
    if not isinstance(name_value, str):
        share_name = str(name_value)
    else:
        share_name = name_value

    path_value = record.get("Path")
    if path_value is None:
        rtnval = None
        return rtnval
    if not isinstance(path_value, str):
        path = str(path_value)
    else:
        path = path_value

    description_value = record.get("Description")
    if isinstance(description_value, str) and len(description_value) > 0:
        description: Optional[str] = description_value
    else:
        description = None

    folder_mode = record.get("FolderEnumerationMode")
    if folder_mode == "Unrestricted" or folder_mode == 0:
        browseable = True
    else:
        browseable = False

    current_users = record.get("CurrentUsers")
    if isinstance(current_users, int):
        connection_count: Optional[int] = current_users
    else:
        connection_count = None

    access_records = access_map.get(share_name, [])
    full_access: list = []
    read_access: list = []
    change_access: list = []
    has_everyone = False
    for access in access_records:
        account = access.get("AccountName")
        right = access.get("AccessRight")
        access_type = access.get("AccessControlType")
        if account is None or right is None:
            continue
        if access_type == "Deny" or access_type == 1:
            continue
        if account == "Everyone":
            has_everyone = True
        if right == "Full" or right == 0:
            full_access.append(account)
            continue
        if right == "Change" or right == 1:
            change_access.append(account)
            continue
        if right == "Read" or right == 2:
            read_access.append(account)
            continue

    if len(full_access) == 0:
        valid_users: Optional[list] = None
    else:
        valid_users = full_access
    if len(read_access) == 0:
        read_list: Optional[list] = None
    else:
        read_list = read_access
    if len(change_access) == 0:
        write_list: Optional[list] = None
    else:
        write_list = change_access

    if (
        len(full_access) == 0
        and len(change_access) == 0
        and len(read_access) > 0
    ):
        read_only = True
    else:
        read_only = False

    rtnval = WindowsSambaShareInfo(
        name=share_name,
        path=path,
        description=description,
        read_only=read_only,
        valid_users=valid_users,
        read_list=read_list,
        write_list=write_list,
        browseable=browseable,
        guest_ok=has_everyone,
        is_active=True,
        connection_count=connection_count,
    )
    return rtnval


def _decode_share_access_records(*, text: str) -> list:
    """
        Decode the ``Get-SmbShareAccess`` JSON output into a list
        of dicts.

        :param text: The raw JSON stdout.

        :returns: A list of decoded records.
    """
    records = _decode_json(text=text)
    rtnval: list = []
    for record in records:
        if isinstance(record, dict):
            rtnval.append(record)
    return rtnval


def _users_from_access_records(*, records: list) -> list:
    """
        Reduce the access map to one
        :class:`WindowsSambaUserInfo` per unique account.

        :param records: A list of decoded ``Get-SmbShareAccess``
                        records.

        :returns: A list of user info records.
    """
    by_account: dict = {}
    for record in records:
        account = record.get("AccountName")
        if account is None:
            continue
        if not isinstance(account, str):
            account = str(account)
        right = record.get("AccessRight")
        if right is None:
            right_text = ""
        elif isinstance(right, int):
            if right == 0:
                right_text = "Full"
            elif right == 1:
                right_text = "Change"
            elif right == 2:
                right_text = "Read"
            else:
                right_text = str(right)
        else:
            right_text = str(right)

        access_type = record.get("AccessControlType")
        is_allow = access_type != "Deny" and access_type != 1

        if account not in by_account:
            by_account[account] = {
                "rights": [],
                "has_allow": False,
            }
        if right_text not in by_account[account]["rights"]:
            by_account[account]["rights"].append(right_text)
        if is_allow:
            by_account[account]["has_allow"] = True

    rtnval: list = []
    for account, summary in by_account.items():
        rights = list(summary["rights"])
        if len(rights) > 0:
            description: Optional[str] = ", ".join(rights)
        else:
            description = None
        info = WindowsSambaUserInfo(
            username=account,
            enabled=summary["has_allow"],
            last_password_change=None,
            description=description,
        )
        rtnval.append(info)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — ACL change-set computation
# ----------------------------------------------------------------------


def _compute_acl_change_set(
    *,
    existing: list,
    acl: WindowsSambaShareAcl,
) -> list:
    """
        Compute the ``Grant-SmbShareAccess`` /
        ``Revoke-SmbShareAccess`` scriptblocks needed to bring
        ``existing`` into line with ``acl``.

        Existing access entries that already match the requested
        ACL are suppressed.  Existing entries that conflict (e.g.,
        a principal currently granted ``Change`` who should be
        ``Read``) are replaced with a fresh grant; the cmdlet
        treats the second grant as an update.

        :param existing: The decoded
                         ``Get-SmbShareAccess`` records.
        :param acl: The desired :class:`WindowsSambaShareAcl`.

        :returns: A list of PowerShell scriptblock strings.
    """
    existing_allow_map: dict = {}
    existing_deny_set: set = set()
    for record in existing:
        account = record.get("AccountName")
        if not isinstance(account, str):
            continue
        right = record.get("AccessRight")
        if isinstance(right, int):
            if right == 0:
                right_text = "Full"
            elif right == 1:
                right_text = "Change"
            elif right == 2:
                right_text = "Read"
            else:
                right_text = ""
        elif isinstance(right, str):
            right_text = right
        else:
            right_text = ""
        access_type = record.get("AccessControlType")
        if access_type == "Deny" or access_type == 1:
            existing_deny_set.add(account)
            continue
        existing_allow_map[account] = right_text

    share_name = ""
    for record in existing:
        share_value = record.get("Name")
        if isinstance(share_value, str):
            share_name = share_value
            break

    rtnval: list = []

    desired_grants = (
        ("Full", acl.full_access_users),
        ("Change", acl.write_users),
        ("Read", acl.read_users),
    )
    for right_text, principals in desired_grants:
        for principal in principals:
            if existing_allow_map.get(principal) == right_text:
                continue
            rtnval.append(
                _build_grant_smb_share_access_script(
                    share_name=share_name,
                    account=principal,
                    access_right=right_text,
                ),
            )

    for principal in acl.denied_users:
        if principal in existing_deny_set:
            continue
        rtnval.append(
            _build_revoke_smb_share_access_script(
                share_name=share_name,
                account=principal,
            ),
        )

    return rtnval
