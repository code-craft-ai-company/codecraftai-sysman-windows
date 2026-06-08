"""
    Windows nfs-server-management feature.

    Implements the cross-OS nfs-server-management contract for
    Windows using the PowerShell ``NFS`` module (``New-NfsShare`` /
    ``Remove-NfsShare`` / ``Set-NfsShare`` / ``Get-NfsShare``) and
    the ``NfsService`` Windows service.  Every verb is dispatched
    through the injected :class:`ICommandRunner` so the manager
    works identically under local and SSH execution.

    NFS Server role requirement:
        The ``NFS`` PowerShell module ships with the Windows Server
        "Server for NFS" role
        (``Install-WindowsFeature -Name FS-NFS-Service``).  It is
        NOT available on Windows 10 / 11 client SKUs by default —
        operators on client SKUs can install the Remote Server
        Administration Tools (RSAT) and target a remote NFS server,
        but the locally-dispatched cmdlets in this module will fail
        with ``CommandNotFoundException`` when the module is
        missing.

        :class:`WindowsNfsManagerConfig` carries an advisory
        ``requires_nfs_server_role`` field documenting this
        requirement.  The manager itself does not probe for the
        role; the post-write cmdlet failure surfaces the
        misconfiguration as a ``FAILED`` result with the cmdlet's
        stderr captured verbatim.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Windows tool                      |
    +=============================+===================================+
    | ``add_export``              | ``New-NfsShare -Name '<n>'        |
    |                             | -Path '<p>' -Authentication 'sys' |
    |                             | -EnableUnmappedAccess $false      |
    |                             | -Permission ReadWrite             |
    |                             | -AllowRootAccess $false``         |
    +-----------------------------+-----------------------------------+
    | ``remove_export``           | ``Remove-NfsShare -Name '<n>'     |
    |                             | -Force``                          |
    +-----------------------------+-----------------------------------+
    | ``list_exports``            | ``Get-NfsShare \\| ConvertTo-Json |
    |                             | -Compress -Depth 5``              |
    +-----------------------------+-----------------------------------+
    | ``reload_exports``          | ``Restart-Service NfsService``    |
    +-----------------------------+-----------------------------------+
    | ``get_export_status``       | ``Get-NfsShare -Name '<n>' \\|    |
    |                             | ConvertTo-Json``                  |
    +-----------------------------+-----------------------------------+
    | ``set_export_options``      | ``Set-NfsShare -Name '<n>'        |
    |                             | <named-args>``                    |
    +-----------------------------+-----------------------------------+

    Cross-OS exports-syntax divergence:
        Unlike Linux (``/etc/exports`` parenthesised options) and
        macOS (``/etc/exports`` dash-prefixed options), Windows
        keeps the share table in the registry and addresses every
        share by **share name** rather than by path.  This module
        therefore requires ``WindowsNfsExportSpec.share_name`` for
        ``add_export`` and looks up the share name from the path
        for the other write verbs via ``Get-NfsShare``.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<scriptblock>"]`` so that ``$PROFILE`` cannot
        mutate cmdlet behaviour and PowerShell never blocks waiting
        for interactive input.

    Reload semantics:
        Windows has no per-share reload cmdlet — ``New-NfsShare`` /
        ``Remove-NfsShare`` update the daemon's runtime state
        synchronously.  ``reload_exports`` therefore invokes
        ``Restart-Service NfsService`` (service-wide) for contract
        parity.  Callers should rarely need to invoke this verb
        because the per-share writes already commit synchronously.

    Idempotency:
        * ``add_export`` probes ``Get-NfsShare -Name <name>`` first;
          returns ``SKIPPED`` when a share with that name already
          maps to the same path.
        * ``remove_export`` looks the share up by path; returns
          ``SKIPPED`` when no share advertises ``path``.
        * ``set_export_options`` looks the share up by path and
          compares each option against the existing share fields;
          returns ``SKIPPED`` when every option already matches.
        * ``list_exports`` and ``get_export_status`` never skip.
        * ``reload_exports`` never skips — operational and
          unconditional.

    share-name addressability:
        ``add_export(spec)`` requires ``spec.share_name``; an empty
        or ``None`` ``share_name`` produces ``FAILED`` with
        ``message="share_name is required for Windows NFS exports"``
        without invoking the runner.  The other write verbs derive
        the share name from ``path`` via ``Get-NfsShare`` and return
        ``SKIPPED`` when no share is found.

    Elevation policy:
        Write verbs (``add_export``, ``remove_export``,
        ``reload_exports``, ``set_export_options``) honour
        ``config.elevated``.  Reads (``list_exports``,
        ``get_export_status``) and idempotency probes
        (``Get-NfsShare -Name``) never elevate.
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

_DEFAULT_SERVICE_NAME = "NfsService"

_DEFAULT_CLIENT_OPTIONS = (
    "ReadWrite",
    "Authentication=sys",
)


WindowsNfsActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


def _default_options_list() -> list:
    """
        Build the default per-client options list.

        :returns: A fresh list of the safe-default options.
    """
    rtnval = list(_DEFAULT_CLIENT_OPTIONS)
    return rtnval


@dataclass
class WindowsNfsManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsNfsServerManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
        :param requires_nfs_server_role: Advisory boolean.  The
                                         manager does NOT probe for
                                         the Server-for-NFS role.
                                         Default ``True``.
        :param service_name: Windows service name for the NFS
                             daemon, used by
                             :meth:`WindowsNfsServerManager.reload_exports`.
                             Default ``"NfsService"``.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    requires_nfs_server_role: bool = True
    service_name: str = _DEFAULT_SERVICE_NAME


@dataclass
class WindowsNfsActionResult(FeatureActionResult):
    """
        Result of an nfs-server write operation.

        :param path: The disk path the share was rooted at, when
                     applicable.
        :param share_name: The NFS-visible share name the action
                           targeted, when applicable.
        :param message: Optional human-readable detail for SKIPPED /
                        FAILED outcomes.
    """
    path: Optional[str] = None
    share_name: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsNfsClientSpec:
    """
        A single client matcher for a Windows NFS share.

        Note: The pilot does NOT translate per-client options into
        ``Grant-NfsSharePermission`` cmdlets.  The fields are
        preserved for cross-OS parity and for future per-client
        permission grants.

        :param host: Client address matcher (hostname, IP literal,
                     or ``"*"``).  Default ``"*"``.
        :param options: Per-client permission tokens.  Default
                        ``["ReadWrite", "Authentication=sys"]``.
    """
    host: str = "*"
    options: list = field(default_factory=_default_options_list)


@dataclass
class WindowsNfsExportSpec:
    """
        Declarative spec for a single Windows NFS share.

        :param path: The disk path the share is rooted at (e.g.,
                     ``"D:\\Shares\\Public"``).
        :param clients: One or more client matchers (cross-OS
                        parity; the pilot does not translate these
                        into per-client cmdlet dispatches).
        :param share_name: The NFS-visible share name.  REQUIRED
                           for ``add_export``; used by every other
                           write verb after a path → share-name
                           lookup.
    """
    path: str
    clients: list
    share_name: Optional[str] = None


@dataclass
class WindowsNfsExportInfo:
    """
        One parsed share entry from ``Get-NfsShare``.

        :param path: The disk path the share is rooted at.
        :param share_name: The NFS-visible share name.
        :param clients: One :class:`WindowsNfsClientSpec` per client
                        permission rule (best-effort; the pilot
                        records a single wildcard client matching
                        the share's permission/authentication
                        fields).
        :param is_active: Always ``True`` for entries returned by
                          ``Get-NfsShare`` (the cmdlet only reports
                          live shares).
    """
    path: str
    share_name: Optional[str]
    clients: list
    is_active: bool


@dataclass
class WindowsNfsExportStatus:
    """
        Result of :meth:`WindowsNfsServerManager.get_export_status`.

        :param path: The disk path the status describes.
        :param is_active: True when the path matches a registered
                          share.
        :param client_count: 1 when active, 0 when inactive
                             (``Get-NfsShare`` is share-scoped, not
                             client-scoped).
        :param flags: List of token strings recovered from the
                      share's permission / authentication fields
                      (e.g., ``["ReadWrite", "Authentication=sys"]``).
    """
    path: str
    is_active: bool
    client_count: int
    flags: list


class WindowsNfsQueryError(Exception):
    """
        Raised when a Windows NFS read fails for a reason other
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

        err_msg_lines = ["Windows NFS query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsNfsServerManager(BaseFeatureManager):
    """
        Windows nfs-server-management manager wrapping the
        PowerShell ``NFS`` module.
    """

    result_class = WindowsNfsActionResult
    status_class = WindowsNfsActionStatus
    feature_name = "nfs-server-management"

    def __init__(
        self,
        runner,
        config: Optional[WindowsNfsManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh :class:`WindowsNfsManagerConfig`.
        """
        if config is None:
            config = WindowsNfsManagerConfig()
        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_exports(self) -> list:
        """
            Enumerate every registered NFS share via
            ``Get-NfsShare | ConvertTo-Json``.

            :returns: A ``list[WindowsNfsExportInfo]``.

            :raises WindowsNfsQueryError: When PowerShell exits
                                          non-zero with non-empty
                                          stderr.
        """
        script = (
            "Get-NfsShare | ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) > 0:
                raise WindowsNfsQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval: list = []
            return rtnval

        rtnval = _parse_shares_json(text=run_result.stdout)
        return rtnval

    def get_export_status(self, path: str) -> WindowsNfsExportStatus:
        """
            Query whether a share is registered for ``path``.

            :param path: The disk path to query.

            :returns: The :class:`WindowsNfsExportStatus`.

            :raises WindowsNfsQueryError: When ``Get-NfsShare``
                                          exits non-zero with
                                          non-empty stderr.
        """
        shares = self.list_exports()
        match = _lookup_share_by_path(shares=shares, path=path)
        if match is None:
            rtnval = WindowsNfsExportStatus(
                path=path,
                is_active=False,
                client_count=0,
                flags=[],
            )
            return rtnval

        flag_list: list = []
        for client in match.clients:
            for opt in client.options:
                if opt not in flag_list:
                    flag_list.append(opt)
            break

        rtnval = WindowsNfsExportStatus(
            path=path,
            is_active=True,
            client_count=1,
            flags=flag_list,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_export(
        self,
        spec: WindowsNfsExportSpec,
    ) -> WindowsNfsActionResult:
        """
            Register a new NFS share via ``New-NfsShare``.

            Idempotent: returns ``SKIPPED`` when a share with the
            same name and path already exists.  Honours dry-run.

            :param spec: The :class:`WindowsNfsExportSpec`.  The
                         spec MUST carry a non-empty
                         ``share_name``.

            :returns: The :class:`WindowsNfsActionResult`.
        """
        if spec.share_name is None or len(spec.share_name) == 0:
            failed_command = self._build_powershell_argv(script="")
            failed = self._build_result(
                status=WindowsNfsActionStatus.FAILED,
                path=spec.path,
                share_name=None,
                command=failed_command,
                message="share_name is required for Windows NFS exports",
            )
            failed.stderr = (
                "share_name is required for Windows NFS exports"
            )
            return failed

        script = _build_new_nfs_share_script(spec=spec)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsNfsActionStatus.DRY_RUN,
                path=spec.path,
                share_name=spec.share_name,
                command=command,
            )
            return dry

        if self._share_exists_with_same_path(
            name=spec.share_name, path=spec.path,
        ):
            skipped = self._build_result(
                status=WindowsNfsActionStatus.SKIPPED,
                path=spec.path,
                share_name=spec.share_name,
                command=command,
                message="share already registered with same path",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            path=spec.path,
            share_name=spec.share_name,
        )
        return rtnval

    def remove_export(self, path: str) -> WindowsNfsActionResult:
        """
            Unregister the NFS share rooted at ``path`` via
            ``Remove-NfsShare -Force``.

            Idempotent: returns ``SKIPPED`` when no share advertises
            ``path``.  Honours dry-run.

            :param path: The disk path whose share should be
                         removed.

            :returns: The :class:`WindowsNfsActionResult`.
        """
        match = self._lookup_share_by_path_via_runner(path=path)
        if match is None:
            placeholder_command = self._build_powershell_argv(
                script=_build_remove_nfs_share_script(share_name=""),
            )
            skipped = self._build_result(
                status=WindowsNfsActionStatus.SKIPPED,
                path=path,
                share_name=None,
                command=placeholder_command,
                message="no share registered for path",
            )
            return skipped

        script = _build_remove_nfs_share_script(
            share_name=match.share_name,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsNfsActionStatus.DRY_RUN,
                path=path,
                share_name=match.share_name,
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
            path=path,
            share_name=match.share_name,
        )
        return rtnval

    def reload_exports(self) -> WindowsNfsActionResult:
        """
            Restart the NFS service via
            ``Restart-Service NfsService``.

            Operational verb — never skips.  Honours dry-run.

            Note: Windows has no per-share reload cmdlet.  Per-share
            writes commit synchronously, so callers should rarely
            need to invoke this verb.

            :returns: The :class:`WindowsNfsActionResult`.
        """
        service_name = self._typed_config().service_name
        script = "Restart-Service '{}'".format(_ps_quote(value=service_name))
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsNfsActionStatus.DRY_RUN,
                path=None,
                share_name=None,
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
            path=None,
            share_name=None,
        )
        return rtnval

    def set_export_options(
        self,
        path: str,
        options: list,
    ) -> WindowsNfsActionResult:
        """
            Update the options of the share at ``path`` via
            ``Set-NfsShare``.

            Idempotent: returns ``SKIPPED`` when no share is found
            or when every requested option already matches the
            existing share.  Honours dry-run.

            :param path: The disk path whose share should be
                         updated.
            :param options: The new options list.

            :returns: The :class:`WindowsNfsActionResult`.
        """
        match = self._lookup_share_by_path_via_runner(path=path)
        if match is None:
            placeholder_command = self._build_powershell_argv(
                script=_build_set_nfs_share_script(
                    share_name="", options=list(options),
                ),
            )
            skipped = self._build_result(
                status=WindowsNfsActionStatus.SKIPPED,
                path=path,
                share_name=None,
                command=placeholder_command,
                message="no share registered for path",
            )
            return skipped

        existing_options: list = []
        if len(match.clients) > 0:
            existing_options = list(match.clients[0].options)

        if _options_match(existing=existing_options, desired=list(options)):
            placeholder_command = self._build_powershell_argv(
                script=_build_set_nfs_share_script(
                    share_name=match.share_name, options=list(options),
                ),
            )
            skipped = self._build_result(
                status=WindowsNfsActionStatus.SKIPPED,
                path=path,
                share_name=match.share_name,
                command=placeholder_command,
                message="options already at desired value",
            )
            return skipped

        script = _build_set_nfs_share_script(
            share_name=match.share_name, options=list(options),
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsNfsActionStatus.DRY_RUN,
                path=path,
                share_name=match.share_name,
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
            path=path,
            share_name=match.share_name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsNfsManagerConfig:
        """
            Return the manager's config narrowed to the typed
            subclass.

            :returns: The :class:`WindowsNfsManagerConfig`.
        """
        if isinstance(self._config, WindowsNfsManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsNfsManagerConfig(
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

    def _share_exists_with_same_path(
        self,
        *,
        name: str,
        path: str,
    ) -> bool:
        """
            Probe whether a share with name ``name`` and path
            ``path`` is already registered.

            :param name: The share name.
            :param path: The expected disk path.

            :returns: True when an existing share matches both
                      fields.
        """
        script = (
            "Get-NfsShare -Name '{}' "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(_ps_quote(value=name))
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = False
            return rtnval

        parsed = _parse_shares_json(text=stripped)
        for entry in parsed:
            if entry.share_name == name and entry.path == path:
                rtnval = True
                return rtnval
        rtnval = False
        return rtnval

    def _lookup_share_by_path_via_runner(
        self,
        *,
        path: str,
    ) -> Optional[WindowsNfsExportInfo]:
        """
            Look up the share whose ``Path`` equals ``path`` via
            ``Get-NfsShare``.

            :param path: The disk path to look up.

            :returns: The matching :class:`WindowsNfsExportInfo` or
                      ``None``.
        """
        script = (
            "Get-NfsShare | ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[WindowsNfsExportInfo] = None
            return rtnval

        shares = _parse_shares_json(text=run_result.stdout)
        rtnval = _lookup_share_by_path(shares=shares, path=path)
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsNfsActionStatus,
        path: Optional[str],
        share_name: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> WindowsNfsActionResult:
        """
            Build a result without invoking the runner.
        """
        if share_name is not None:
            detail_text = share_name
        elif path is not None:
            detail_text = path
        else:
            detail_text = None

        rtnval = WindowsNfsActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            path=path,
            share_name=share_name,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        path: Optional[str],
        share_name: Optional[str],
    ) -> WindowsNfsActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`WindowsNfsActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = WindowsNfsActionStatus.SUCCESS
        else:
            mapped_status = WindowsNfsActionStatus.FAILED

        if share_name is not None:
            detail_text = share_name
        elif path is not None:
            detail_text = path
        else:
            detail_text = None

        rtnval = WindowsNfsActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            path=path,
            share_name=share_name,
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
        quote by doubling it.  Other characters do not require
        escaping inside the single-quoted form.

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


def _build_new_nfs_share_script(*, spec: WindowsNfsExportSpec) -> str:
    """
        Render the ``New-NfsShare`` scriptblock for the spec.

        :param spec: The :class:`WindowsNfsExportSpec`.

        :returns: The scriptblock string.
    """
    name_token = _ps_quote(value=spec.share_name)
    path_token = _ps_quote(value=spec.path)
    rtnval = (
        "New-NfsShare "
        "-Name '{name}' "
        "-Path '{path}' "
        "-Authentication 'sys' "
        "-EnableUnmappedAccess $false "
        "-Permission ReadWrite "
        "-AllowRootAccess $false"
    ).format(name=name_token, path=path_token)
    return rtnval


def _build_remove_nfs_share_script(*, share_name: str) -> str:
    """
        Render the ``Remove-NfsShare`` scriptblock.

        :param share_name: The share name.

        :returns: The scriptblock string.
    """
    name_token = _ps_quote(value=share_name)
    rtnval = (
        "Remove-NfsShare -Name '{name}' -Force"
    ).format(name=name_token)
    return rtnval


def _build_set_nfs_share_script(
    *,
    share_name: str,
    options: list,
) -> str:
    """
        Render the ``Set-NfsShare`` scriptblock for the supplied
        options.

        Each option may be a bare flag (``"ReadWrite"``) or a
        ``key=value`` pair (``"Authentication=sys"``).  The
        rendered cmdlet maps recognised tokens to their canonical
        ``Set-NfsShare`` parameter:

        | Token                  | Cmdlet parameter             |
        |------------------------|------------------------------|
        | ``ReadWrite``          | ``-Permission ReadWrite``    |
        | ``ReadOnly``           | ``-Permission ReadOnly``     |
        | ``Authentication=...`` | ``-Authentication '<value>'``|
        | ``AllowRootAccess``    | ``-AllowRootAccess $true``   |
        | ``NoRootAccess``       | ``-AllowRootAccess $false``  |
        | ``UnmappedAccess``     | ``-EnableUnmappedAccess $true``|
        | ``NoUnmappedAccess``   | ``-EnableUnmappedAccess $false``|

        Unrecognised tokens are passed through as ``-<Token>`` for
        callers who supply native cmdlet parameter names.

        :param share_name: The share name.
        :param options: The options to apply.

        :returns: The scriptblock string.
    """
    name_token = _ps_quote(value=share_name)
    parts: list = []
    parts.append("Set-NfsShare")
    parts.append("-Name '{}'".format(name_token))

    for raw_option in options:
        translated = _translate_set_option(token=raw_option)
        if len(translated) > 0:
            parts.append(translated)
    rtnval = " ".join(parts)
    return rtnval


def _translate_set_option(*, token: str) -> str:
    """
        Translate a single options-list token into a
        ``Set-NfsShare`` parameter fragment.

        :param token: The raw token.

        :returns: The PowerShell parameter fragment, or an empty
                  string when the token cannot be translated.
    """
    if token is None:
        rtnval = ""
        return rtnval
    stripped = token.strip()
    if len(stripped) == 0:
        rtnval = ""
        return rtnval

    if stripped == "ReadWrite":
        rtnval = "-Permission ReadWrite"
        return rtnval
    if stripped == "ReadOnly":
        rtnval = "-Permission ReadOnly"
        return rtnval
    if stripped == "AllowRootAccess":
        rtnval = "-AllowRootAccess $true"
        return rtnval
    if stripped == "NoRootAccess":
        rtnval = "-AllowRootAccess $false"
        return rtnval
    if stripped == "UnmappedAccess":
        rtnval = "-EnableUnmappedAccess $true"
        return rtnval
    if stripped == "NoUnmappedAccess":
        rtnval = "-EnableUnmappedAccess $false"
        return rtnval

    if "=" in stripped:
        key, value = stripped.split("=", 1)
        if key == "Authentication":
            value_token = _ps_quote(value=value)
            rtnval = "-Authentication '{}'".format(value_token)
            return rtnval
        rtnval = "-{} '{}'".format(key, _ps_quote(value=value))
        return rtnval

    rtnval = "-{}".format(stripped)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — JSON parsers
# ----------------------------------------------------------------------


def _parse_shares_json(*, text: str) -> list:
    """
        Parse the JSON output of ``Get-NfsShare | ConvertTo-Json``
        into a list of :class:`WindowsNfsExportInfo`.

        ``ConvertTo-Json`` emits a single object when there is one
        share, or a list when there are multiple.  This parser
        normalises both shapes.

        :param text: The raw JSON stdout.

        :returns: A list of :class:`WindowsNfsExportInfo`.
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
        info = _build_export_info(record=record)
        if info is None:
            continue
        rtnval.append(info)
    return rtnval


def _build_export_info(
    *,
    record: dict,
) -> Optional[WindowsNfsExportInfo]:
    """
        Build a :class:`WindowsNfsExportInfo` from a decoded JSON
        record.

        :param record: The decoded JSON dict.

        :returns: The populated info, or ``None`` when essential
                  fields are missing.
    """
    name_value = record.get("Name")
    if name_value is None:
        rtnval: Optional[WindowsNfsExportInfo] = None
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

    flag_list: list = []
    permission_value = record.get("Permission")
    if isinstance(permission_value, str):
        flag_list.append(permission_value)
    elif isinstance(permission_value, list):
        for item in permission_value:
            if isinstance(item, str):
                flag_list.append(item)
    auth_value = record.get("Authentication")
    if isinstance(auth_value, str):
        flag_list.append("Authentication={}".format(auth_value))
    allow_root = record.get("AllowRootAccess")
    if allow_root is True:
        flag_list.append("AllowRootAccess")
    elif allow_root is False:
        flag_list.append("NoRootAccess")
    unmapped = record.get("EnableUnmappedAccess")
    if unmapped is True:
        flag_list.append("UnmappedAccess")
    elif unmapped is False:
        flag_list.append("NoUnmappedAccess")

    client = WindowsNfsClientSpec(host="*", options=flag_list)
    rtnval = WindowsNfsExportInfo(
        path=path,
        share_name=share_name,
        clients=[client],
        is_active=True,
    )
    return rtnval


def _lookup_share_by_path(
    *,
    shares: list,
    path: str,
) -> Optional[WindowsNfsExportInfo]:
    """
        Locate the share whose ``Path`` equals ``path``.

        :param shares: A list of :class:`WindowsNfsExportInfo`.
        :param path: The disk path to look up.

        :returns: The matching share, or ``None``.
    """
    rtnval: Optional[WindowsNfsExportInfo] = None
    for entry in shares:
        if entry.path == path:
            rtnval = entry
            return rtnval
    return rtnval


def _options_match(*, existing: list, desired: list) -> bool:
    """
        Compare two options lists for equality after sorting.

        Order is irrelevant because the Set-NfsShare cmdlet does
        not preserve a parameter ordering on the share record.

        :param existing: The current options list.
        :param desired: The requested options list.

        :returns: True when the sorted lists match.
    """
    rtnval = sorted(existing) == sorted(desired)
    return rtnval
