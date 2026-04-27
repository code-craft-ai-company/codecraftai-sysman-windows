"""
    Windows ssh-server-configuration feature.

    Implements the cross-OS ssh-server-configuration contract for
    Windows.  Windows ships OpenSSH server as an optional Windows
    feature (``Add-WindowsCapability -Online -Name
    OpenSSH.Server~~~~0.0.1.0``) that, once installed, exposes the
    daemon as the ``sshd`` service and stores its configuration in
    ``%ProgramData%\\ssh\\sshd_config`` (typically
    ``C:\\ProgramData\\ssh\\sshd_config``).

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Windows tool                      |
    +=============================+===================================+
    | ``get_sshd_setting``        | PowerShell ``Get-Content -Raw`` + |
    |                             | parse                             |
    +-----------------------------+-----------------------------------+
    | ``set_sshd_setting``        | ``Get-Content`` + parse + render +|
    |                             | ``Set-Content``                   |
    +-----------------------------+-----------------------------------+
    | ``remove_sshd_setting``     | ``Get-Content`` + parse + render +|
    |                             | ``Set-Content``                   |
    +-----------------------------+-----------------------------------+
    | ``list_sshd_settings``      | ``Get-Content`` + parse           |
    +-----------------------------+-----------------------------------+
    | ``add_match_block`` /       | ``Get-Content`` + parse + render +|
    | ``remove_match_block``      | ``Set-Content``                   |
    +-----------------------------+-----------------------------------+
    | ``list_match_blocks``       | ``Get-Content`` + parse           |
    +-----------------------------+-----------------------------------+
    | ``reload_sshd``             | ``Restart-Service -Name sshd``    |
    +-----------------------------+-----------------------------------+
    | ``test_sshd_config``        | ``sshd.exe -t``                   |
    +-----------------------------+-----------------------------------+
    | ``list_authorized_keys``    | ``Get-Content``                   |
    +-----------------------------+-----------------------------------+
    | ``add_authorized_key`` /    | ``Get-Content`` + render +        |
    | ``remove_authorized_key``   | ``Set-Content``                   |
    +-----------------------------+-----------------------------------+

    File I/O policy:
        Reads use PowerShell ``Get-Content -Raw -Encoding utf8``;
        writes use PowerShell ``Set-Content`` with the rendered file
        passed via ``[Console]::In.ReadToEnd()`` (stdin).  This module
        does NOT consume an :class:`IFileOperator`.

    Reload mechanism:
        Windows OpenSSH has no `Reload-Service` verb.  ``Restart-
        Service sshd`` is the canonical mechanism — equivalent in
        spirit to a stop+start cycle.  Existing connections are NOT
        retained (Windows OpenSSH terminates each child along with
        the listener); long-running automation should account for
        the brief outage.

    Authorized-keys path — admin vs non-admin distinction:
        Windows OpenSSH treats Administrator users specially.  Per
        ``%ProgramData%\\ssh\\sshd_config`` (the stock config), the
        directives at the bottom of the file read:

            Match Group administrators
                AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys

        For non-administrator users, ``AuthorizedKeysFile`` defaults
        to ``.ssh/authorized_keys`` resolved against the user's
        profile (``%UserProfile%\\.ssh\\authorized_keys``, typically
        ``C:\\Users\\<user>\\.ssh\\authorized_keys``).

        The :class:`WindowsSshServerConfigurationManager` exposes BOTH
        paths through configuration:
        - ``authorized_keys_path_template`` (default
          ``C:\\Users\\{user}\\.ssh\\authorized_keys``) for ordinary
          users.
        - ``admin_authorized_keys_path`` (default
          ``C:\\ProgramData\\ssh\\administrators_authorized_keys``)
          for administrator-group users.

        :meth:`add_authorized_key` / :meth:`remove_authorized_key` /
        :meth:`list_authorized_keys` accept an optional
        ``is_admin: bool`` keyword.  When ``True`` the
        admin-authorized-keys path is used; otherwise the per-user
        path template is rendered with ``{user}`` substitution.
        Callers MUST decide whether the supplied user is an admin
        because Windows determines admin status from group
        membership, not from the username.

    Path-syntax conventions:
        - PowerShell single-quoted strings are used for paths to
          avoid PowerShell variable expansion of ``$``-prefixed
          tokens.
        - Backslashes inside a single-quoted PowerShell literal are
          NOT escape characters; the path ``C:\\ProgramData\\ssh\\
          sshd_config`` reads as the literal string when single-
          quoted.

    Authorized-key fingerprinting:
        Fingerprint computation is performed in pure Python using
        SHA-256 of the base64-decoded key body, formatted as
        ``SHA256:<base64-no-padding>``.

    Elevation policy:
        - Writes to ``%ProgramData%\\ssh\\sshd_config`` and to the
          admin-authorized-keys file under ``%ProgramData%`` MUST
          elevate.
        - Writes to a per-user ``%UserProfile%\\.ssh\\
          authorized_keys`` MAY elevate; the pilot honours
          ``config.elevated`` uniformly (matches prior wave-1
          batches).
        - ``Restart-Service sshd`` MUST elevate.
        - ``sshd.exe -t`` MUST elevate (it reads host-key files).
        - Reads (``Get-Content``) never elevate.
"""

import base64
import hashlib
import os

from dataclasses import dataclass, field
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

_SSHD_EXE = "sshd.exe"

_DEFAULT_SSHD_CONFIG_PATH = r"C:\ProgramData\ssh\sshd_config"
_DEFAULT_AUTHORIZED_KEYS_TEMPLATE = (
    r"C:\Users\{user}\.ssh\authorized_keys"
)
_DEFAULT_ADMIN_AUTHORIZED_KEYS_PATH = (
    r"C:\ProgramData\ssh\administrators_authorized_keys"
)
_DEFAULT_SERVICE_NAME = "sshd"

_MATCH_KEYWORD = "Match"


WindowsSshActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class WindowsSshServerConfigurationManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`WindowsSshServerConfigurationManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param sshd_config_path: Absolute path to the primary
                                 ``sshd_config`` file.  Default is
                                 ``C:\\ProgramData\\ssh\\sshd_config``.
        :param authorized_keys_path_template: Format string used to
                                              resolve a user's
                                              ``authorized_keys``
                                              path.  Must contain
                                              ``{user}``.  Default is
                                              ``C:\\Users\\{user}\\.ssh\\authorized_keys``.
        :param admin_authorized_keys_path: Path used when the target
                                           user is an Administrator-
                                           group member.  Default is
                                           ``C:\\ProgramData\\ssh\\administrators_authorized_keys``.
        :param service_name: Windows service name used by
                             :meth:`WindowsSshServerConfigurationManager.reload_sshd`.
                             Default is ``"sshd"``.
        :param powershell_executable: PowerShell executable used to
                                      run cmdlets.
    """
    sshd_config_path: str = _DEFAULT_SSHD_CONFIG_PATH
    authorized_keys_path_template: str = _DEFAULT_AUTHORIZED_KEYS_TEMPLATE
    admin_authorized_keys_path: str = _DEFAULT_ADMIN_AUTHORIZED_KEYS_PATH
    service_name: str = _DEFAULT_SERVICE_NAME
    powershell_executable: str = _DEFAULT_POWERSHELL


@dataclass
class WindowsSshActionResult(FeatureActionResult):
    """
        Result of an ssh-server-configuration write operation.

        :param keyword: The ``sshd_config`` keyword targeted, or
                        ``None``.
        :param path: The file path the action targeted, or ``None``.
        :param user: The user whose authorized-keys file was targeted,
                     or ``None``.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    keyword: Optional[str] = None
    path: Optional[str] = None
    user: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsSshSettingValue:
    """
        A single ``sshd_config`` keyword / value pair.
    """
    keyword: str
    value: str
    source_file: str


@dataclass
class WindowsSshMatchBlock:
    """
        A single ``Match <criteria>`` block.
    """
    criteria: str
    settings: dict = field(default_factory=dict)


@dataclass
class WindowsSshAuthorizedKey:
    """
        A single ``authorized_keys`` entry.
    """
    key_type: str
    key_data: str
    comment: Optional[str] = None
    fingerprint: Optional[str] = None
    options: Optional[str] = None


class WindowsSshQueryError(Exception):
    """
        Raised when an ssh-server-configuration read fails for a reason
        other than the target source simply being absent.
    """

    def __init__(
        self,
        *,
        path: str,
        message: str,
    ) -> None:
        """
            :param path: The file path whose read failed.
            :param message: Human-readable detail.
        """
        self.path = path
        self.message = message
        err_msg_lines = ["SSH query command failed."]
        err_msg_lines.append("    PATH: {}".format(path))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsSshServerConfigurationManager(BaseFeatureManager):
    """
        Windows ssh-server-configuration manager.  Reads, writes, and
        validates ``%ProgramData%\\ssh\\sshd_config`` and per-user
        ``authorized_keys`` (with the admin-vs-non-admin split) through
        the injected :class:`ICommandRunner`.
    """

    result_class = WindowsSshActionResult
    status_class = WindowsSshActionStatus
    feature_name = "ssh-server-configuration"

    # ------------------------------------------------------------------
    # Read verbs — sshd_config
    # ------------------------------------------------------------------

    def get_sshd_setting(
        self,
        keyword: str,
    ) -> Optional[WindowsSshSettingValue]:
        """
            Read a single top-level ``sshd_config`` keyword.

            :param keyword: The ``sshd_config`` keyword.

            :returns: The :class:`WindowsSshSettingValue` or ``None``.
        """
        path = self._typed_config().sshd_config_path
        content = self._read_file(path=path)
        if content is None:
            rtnval: Optional[WindowsSshSettingValue] = None
            return rtnval

        top_settings, _ = _parse_sshd_config(content=content)
        keyword_lower = keyword.lower()
        for parsed_keyword, parsed_value in top_settings.items():
            if parsed_keyword.lower() == keyword_lower:
                rtnval = WindowsSshSettingValue(
                    keyword=parsed_keyword,
                    value=parsed_value,
                    source_file=path,
                )
                return rtnval
        rtnval = None
        return rtnval

    def list_sshd_settings(self) -> dict:
        """
            Enumerate every top-level ``sshd_config`` keyword.
        """
        path = self._typed_config().sshd_config_path
        content = self._read_file(path=path)
        if content is None:
            rtnval: dict = {}
            return rtnval

        top_settings, _ = _parse_sshd_config(content=content)
        rtnval = top_settings
        return rtnval

    def list_match_blocks(self) -> list:
        """
            Enumerate every ``Match`` block.
        """
        path = self._typed_config().sshd_config_path
        content = self._read_file(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        _, match_blocks = _parse_sshd_config(content=content)
        rtnval = match_blocks
        return rtnval

    def list_authorized_keys(
        self,
        user: str,
        *,
        is_admin: bool = False,
    ) -> list:
        """
            Read and parse a user's ``authorized_keys`` file.

            :param user: The username whose key file is queried.
                         Ignored when ``is_admin=True`` because all
                         Administrator-group users share
                         ``administrators_authorized_keys``.
            :param is_admin: When ``True`` the manager reads
                             ``admin_authorized_keys_path`` instead of
                             rendering the per-user template.

            :returns: A ``list[WindowsSshAuthorizedKey]``.
        """
        path = self._authorized_keys_path(user=user, is_admin=is_admin)
        content = self._read_file(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        rtnval = _parse_authorized_keys(content=content)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — sshd_config
    # ------------------------------------------------------------------

    def set_sshd_setting(
        self,
        keyword: str,
        value: str,
    ) -> WindowsSshActionResult:
        """
            Set a top-level ``sshd_config`` keyword.

            :param keyword: The keyword.
            :param value: The desired value text.

            :returns: The :class:`WindowsSshActionResult`.
        """
        path = self._typed_config().sshd_config_path
        write_command = self._build_set_content_argv(path=path)

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        top_settings, match_blocks = _parse_sshd_config(
            content=existing_content,
        )
        new_top_settings = _set_top_setting(
            top_settings=top_settings,
            keyword=keyword,
            value=value,
        )
        new_content = _render_sshd_config(
            top_settings=new_top_settings,
            match_blocks=match_blocks,
        )

        if existing is not None and existing == new_content:
            skipped = self._build_result(
                status=WindowsSshActionStatus.SKIPPED,
                keyword=keyword,
                path=path,
                command=write_command,
                message="setting already at desired value",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSshActionStatus.DRY_RUN,
                keyword=keyword,
                path=path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=keyword,
            path=path,
            user=None,
        )
        return rtnval

    def remove_sshd_setting(
        self,
        keyword: str,
    ) -> WindowsSshActionResult:
        """
            Remove every top-level occurrence of ``keyword``.
        """
        path = self._typed_config().sshd_config_path
        write_command = self._build_set_content_argv(path=path)

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=WindowsSshActionStatus.SKIPPED,
                keyword=keyword,
                path=path,
                command=write_command,
                message="config file does not exist",
            )
            return skipped

        top_settings, match_blocks = _parse_sshd_config(content=existing)
        new_top_settings = _remove_top_setting(
            top_settings=top_settings,
            keyword=keyword,
        )
        new_content = _render_sshd_config(
            top_settings=new_top_settings,
            match_blocks=match_blocks,
        )

        if new_content == existing:
            skipped = self._build_result(
                status=WindowsSshActionStatus.SKIPPED,
                keyword=keyword,
                path=path,
                command=write_command,
                message="keyword not present at top level",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSshActionStatus.DRY_RUN,
                keyword=keyword,
                path=path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=keyword,
            path=path,
            user=None,
        )
        return rtnval

    def add_match_block(
        self,
        criteria: str,
        settings: dict,
    ) -> WindowsSshActionResult:
        """
            Add or update a ``Match <criteria>`` block.
        """
        path = self._typed_config().sshd_config_path
        write_command = self._build_set_content_argv(path=path)

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        top_settings, match_blocks = _parse_sshd_config(
            content=existing_content,
        )

        normalized_settings = dict(settings)
        new_blocks = _upsert_match_block(
            match_blocks=match_blocks,
            criteria=criteria,
            settings=normalized_settings,
        )
        new_content = _render_sshd_config(
            top_settings=top_settings,
            match_blocks=new_blocks,
        )

        if existing is not None and existing == new_content:
            skipped = self._build_result(
                status=WindowsSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="match block already present",
            )
            skipped.detail = "match:{}".format(criteria)
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.detail = "match:{}".format(criteria)
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=None,
        )
        rtnval.detail = "match:{}".format(criteria)
        return rtnval

    def remove_match_block(
        self,
        criteria: str,
    ) -> WindowsSshActionResult:
        """
            Remove a ``Match <criteria>`` block.
        """
        path = self._typed_config().sshd_config_path
        write_command = self._build_set_content_argv(path=path)

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=WindowsSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="config file does not exist",
            )
            skipped.detail = "match:{}".format(criteria)
            return skipped

        top_settings, match_blocks = _parse_sshd_config(content=existing)
        new_blocks = _remove_match_block(
            match_blocks=match_blocks,
            criteria=criteria,
        )

        if len(new_blocks) == len(match_blocks):
            skipped = self._build_result(
                status=WindowsSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="match block not present",
            )
            skipped.detail = "match:{}".format(criteria)
            return skipped

        new_content = _render_sshd_config(
            top_settings=top_settings,
            match_blocks=new_blocks,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.detail = "match:{}".format(criteria)
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=None,
        )
        rtnval.detail = "match:{}".format(criteria)
        return rtnval

    # ------------------------------------------------------------------
    # Operational verbs
    # ------------------------------------------------------------------

    def reload_sshd(self) -> WindowsSshActionResult:
        """
            Restart the sshd service via ``Restart-Service``.

            Windows OpenSSH has no signal-driven `reload`; restarting
            the service is the canonical mechanism.

            :returns: The :class:`WindowsSshActionResult`.
        """
        service = self._typed_config().service_name
        escaped_service = _ps_escape_single_quoted(value=service)
        script = "Restart-Service -Name '{}'".format(escaped_service)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSshActionStatus.DRY_RUN,
                keyword=None,
                path=None,
                command=command,
                message=service,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            keyword=None,
            path=None,
            user=None,
        )
        return rtnval

    def test_sshd_config(self) -> WindowsSshActionResult:
        """
            Validate the current ``sshd_config`` via ``sshd.exe -t``.

            ``sshd.exe`` typically lives at
            ``%ProgramFiles%\\OpenSSH\\sshd.exe`` and is on PATH after
            the OpenSSH server feature is installed.  The manager
            invokes ``sshd.exe`` by basename and lets the runner
            resolve through PATH.

            :returns: The :class:`WindowsSshActionResult`.
        """
        command = [_SSHD_EXE, "-t"]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSshActionStatus.DRY_RUN,
                keyword=None,
                path=self._typed_config().sshd_config_path,
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
            keyword=None,
            path=self._typed_config().sshd_config_path,
            user=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Authorized-key write verbs
    # ------------------------------------------------------------------

    def add_authorized_key(
        self,
        user: str,
        key: str,
        *,
        is_admin: bool = False,
    ) -> WindowsSshActionResult:
        """
            Append a key line to the appropriate ``authorized_keys``.

            When ``is_admin=True`` the key is appended to
            ``%ProgramData%\\ssh\\administrators_authorized_keys``;
            otherwise to the per-user
            ``%UserProfile%\\.ssh\\authorized_keys``.  See the module
            docstring for the rationale.
        """
        path = self._authorized_keys_path(user=user, is_admin=is_admin)
        write_command = self._build_set_content_argv(path=path)

        parsed_new = _parse_authorized_key_line(line=key)
        if parsed_new is None:
            failed = self._build_result(
                status=WindowsSshActionStatus.FAILED,
                keyword=None,
                path=path,
                command=write_command,
                message="key line failed to parse",
            )
            failed.user = user
            return failed

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        existing_keys = _parse_authorized_keys(content=existing_content)
        for existing_entry in existing_keys:
            if existing_entry.fingerprint == parsed_new.fingerprint:
                skipped = self._build_result(
                    status=WindowsSshActionStatus.SKIPPED,
                    keyword=None,
                    path=path,
                    command=write_command,
                    message="key already present",
                )
                skipped.user = user
                return skipped

        new_content = _append_authorized_key(
            content=existing_content, key_line=key,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.user = user
            return dry

        # TODO: per-user authorized_keys writes for the calling user
        # do not strictly require elevation.  Honour the config for
        # now (matches prior wave-1 batches).
        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=user,
        )
        return rtnval

    def remove_authorized_key(
        self,
        user: str,
        fingerprint: str,
        *,
        is_admin: bool = False,
    ) -> WindowsSshActionResult:
        """
            Remove the entry whose fingerprint matches.
        """
        path = self._authorized_keys_path(user=user, is_admin=is_admin)
        write_command = self._build_set_content_argv(path=path)

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=WindowsSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="authorized_keys file does not exist",
            )
            skipped.user = user
            return skipped

        new_content = _remove_authorized_key(
            content=existing, fingerprint=fingerprint,
        )

        if new_content == existing:
            skipped = self._build_result(
                status=WindowsSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="key with given fingerprint not present",
            )
            skipped.user = user
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.user = user
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=user,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsSshServerConfigurationManagerConfig:
        """
            Narrow the base config to the typed subclass.
        """
        rtnval = self._config
        return rtnval

    def _authorized_keys_path(
        self,
        *,
        user: str,
        is_admin: bool,
    ) -> str:
        """
            Render the ``authorized_keys`` path for a user, choosing
            between the per-user template and the admin path based on
            ``is_admin``.

            :param user: The username (used only for the per-user
                         template).
            :param is_admin: When ``True``, returns the configured
                             admin-authorized-keys path verbatim.
        """
        typed = self._typed_config()
        if is_admin:
            rtnval = typed.admin_authorized_keys_path
            return rtnval
        rtnval = typed.authorized_keys_path_template.format(user=user)
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a plain-text file via ``Get-Content -Raw``.

            :param path: The absolute path.

            :returns: The contents on success; ``None`` when the
                      cmdlet reports a non-zero exit code (i.e. the
                      path is missing).
        """
        escaped_path = _ps_escape_single_quoted(value=path)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Get-Content -Path '{}' -Raw -ErrorAction Stop"
        ).format(escaped_path)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical PowerShell argv for a scriptblock.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _build_set_content_argv(self, *, path: str) -> list:
        """
            Build the argv used to write a plain-text file via
            ``Set-Content``.  Content is supplied on stdin.

            :param path: The destination file path.

            :returns: The argv list.
        """
        escaped_path = _ps_escape_single_quoted(value=path)
        script = (
            "$content = [Console]::In.ReadToEnd(); "
            "Set-Content -Path '{}' -Value $content -NoNewline -Encoding utf8"
        ).format(escaped_path)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsSshActionStatus,
        keyword: Optional[str],
        path: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> WindowsSshActionResult:
        """
            Build a result without invoking the runner.
        """
        if keyword is not None:
            detail_text = keyword
        elif path is not None:
            detail_text = path
        else:
            detail_text = None

        rtnval = WindowsSshActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            keyword=keyword,
            path=path,
            user=None,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        keyword: Optional[str],
        path: Optional[str],
        user: Optional[str],
    ) -> WindowsSshActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`WindowsSshActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = WindowsSshActionStatus.SUCCESS
        else:
            mapped_status = WindowsSshActionStatus.FAILED

        if keyword is not None:
            detail_text = keyword
        elif path is not None:
            detail_text = path
        else:
            detail_text = None

        rtnval = WindowsSshActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            keyword=keyword,
            path=path,
            user=user,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PowerShell escape
# ----------------------------------------------------------------------


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.

        :param value: The raw string.

        :returns: The escaped string.
    """
    rtnval = value.replace("'", "''")
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — sshd_config parser / renderer
# ----------------------------------------------------------------------


def _parse_sshd_config(*, content: str) -> tuple:
    """
        Parse ``sshd_config`` content into a top-level settings map and
        a list of :class:`WindowsSshMatchBlock`.

        :param content: The raw file content.

        :returns: ``(top_settings, match_blocks)``.
    """
    top_settings: dict = {}
    match_blocks: list = []
    current_block: Optional[WindowsSshMatchBlock] = None

    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        tokens = stripped.split(None, 1)
        if len(tokens) < 2:
            keyword = tokens[0]
            value = ""
        else:
            keyword = tokens[0]
            value = tokens[1].strip()

        if keyword.lower() == _MATCH_KEYWORD.lower():
            current_block = WindowsSshMatchBlock(
                criteria=value,
                settings={},
            )
            match_blocks.append(current_block)
            continue

        if current_block is None:
            top_settings[keyword] = value
        else:
            current_block.settings[keyword] = value

    rtnval = (top_settings, match_blocks)
    return rtnval


def _render_sshd_config(
    *,
    top_settings: dict,
    match_blocks: list,
) -> str:
    """
        Render a deterministic ``sshd_config``.
    """
    lines: list = []
    for keyword, value in top_settings.items():
        if len(value) == 0:
            lines.append(keyword)
        else:
            lines.append("{} {}".format(keyword, value))

    for block in match_blocks:
        lines.append("Match {}".format(block.criteria))
        for keyword, value in block.settings.items():
            if len(value) == 0:
                lines.append("    {}".format(keyword))
            else:
                lines.append("    {} {}".format(keyword, value))

    body = "\n".join(lines)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _set_top_setting(
    *,
    top_settings: dict,
    keyword: str,
    value: str,
) -> dict:
    """
        Return a new top-settings dict with ``keyword`` upserted.
    """
    keyword_lower = keyword.lower()
    rtnval: dict = {}
    replaced = False
    for existing_keyword, existing_value in top_settings.items():
        if existing_keyword.lower() == keyword_lower:
            rtnval[existing_keyword] = value
            replaced = True
            continue
        rtnval[existing_keyword] = existing_value
    if not replaced:
        rtnval[keyword] = value
    return rtnval


def _remove_top_setting(
    *,
    top_settings: dict,
    keyword: str,
) -> dict:
    """
        Return a new top-settings dict with ``keyword`` removed.
    """
    keyword_lower = keyword.lower()
    rtnval: dict = {}
    for existing_keyword, existing_value in top_settings.items():
        if existing_keyword.lower() == keyword_lower:
            continue
        rtnval[existing_keyword] = existing_value
    return rtnval


def _upsert_match_block(
    *,
    match_blocks: list,
    criteria: str,
    settings: dict,
) -> list:
    """
        Return a new list of match blocks with the given criteria
        upserted.
    """
    target_norm = _collapse_whitespace(text=criteria)
    rtnval: list = []
    found = False
    for block in match_blocks:
        block_norm = _collapse_whitespace(text=block.criteria)
        if block_norm == target_norm:
            replacement = WindowsSshMatchBlock(
                criteria=block.criteria,
                settings=dict(settings),
            )
            rtnval.append(replacement)
            found = True
            continue
        rtnval.append(block)
    if not found:
        new_block = WindowsSshMatchBlock(
            criteria=criteria,
            settings=dict(settings),
        )
        rtnval.append(new_block)
    return rtnval


def _remove_match_block(
    *,
    match_blocks: list,
    criteria: str,
) -> list:
    """
        Return a new list with every block whose criteria matches
        removed (whitespace-collapsed comparison).
    """
    target_norm = _collapse_whitespace(text=criteria)
    rtnval: list = []
    for block in match_blocks:
        block_norm = _collapse_whitespace(text=block.criteria)
        if block_norm == target_norm:
            continue
        rtnval.append(block)
    return rtnval


def _collapse_whitespace(*, text: str) -> str:
    """
        Collapse runs of whitespace into a single space and strip ends.
    """
    parts = text.split()
    rtnval = " ".join(parts)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — authorized_keys parser / renderer
# ----------------------------------------------------------------------


_KNOWN_KEY_TYPES = (
    "ssh-rsa",
    "ssh-dss",
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "sk-ssh-ed25519@openssh.com",
)


def _parse_authorized_keys(*, content: str) -> list:
    """
        Parse a full ``authorized_keys`` file into entries.
    """
    rtnval: list = []
    for raw in content.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        parsed = _parse_authorized_key_line(line=line)
        if parsed is None:
            continue
        rtnval.append(parsed)
    return rtnval


def _parse_authorized_key_line(
    *,
    line: str,
) -> Optional[WindowsSshAuthorizedKey]:
    """
        Parse a single ``authorized_keys`` line.
    """
    parts = line.split()
    if len(parts) < 2:
        rtnval: Optional[WindowsSshAuthorizedKey] = None
        return rtnval

    type_index: Optional[int] = None
    for i, token in enumerate(parts):
        if token in _KNOWN_KEY_TYPES:
            type_index = i
            break

    if type_index is None:
        rtnval = None
        return rtnval

    if type_index + 1 >= len(parts):
        rtnval = None
        return rtnval

    if type_index == 0:
        options: Optional[str] = None
    else:
        options = " ".join(parts[:type_index])

    key_type = parts[type_index]
    key_data = parts[type_index + 1]

    comment_parts = parts[type_index + 2:]
    if len(comment_parts) > 0:
        comment: Optional[str] = " ".join(comment_parts)
    else:
        comment = None

    fingerprint = _compute_fingerprint(key_data=key_data)

    rtnval = WindowsSshAuthorizedKey(
        key_type=key_type,
        key_data=key_data,
        comment=comment,
        fingerprint=fingerprint,
        options=options,
    )
    return rtnval


def _compute_fingerprint(*, key_data: str) -> Optional[str]:
    """
        Compute an OpenSSH-format SHA-256 fingerprint for a key body.
    """
    try:
        decoded = base64.b64decode(key_data, validate=True)
    except (ValueError, Exception):
        rtnval: Optional[str] = None
        return rtnval

    digest = hashlib.sha256(decoded).digest()
    encoded = base64.b64encode(digest).decode("ascii").rstrip("=")
    rtnval = "SHA256:{}".format(encoded)
    return rtnval


def _append_authorized_key(*, content: str, key_line: str) -> str:
    """
        Append a key line to the file content.
    """
    trimmed = key_line.strip()
    if len(content) == 0:
        rtnval = trimmed + "\n"
        return rtnval
    if not content.endswith("\n"):
        body = content + "\n"
    else:
        body = content
    rtnval = body + trimmed + "\n"
    return rtnval


def _remove_authorized_key(
    *,
    content: str,
    fingerprint: str,
) -> str:
    """
        Return content with every line whose computed fingerprint
        matches removed.
    """
    if len(content) == 0:
        return content

    kept: list = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            kept.append(raw)
            continue
        parsed = _parse_authorized_key_line(line=stripped)
        if parsed is None:
            kept.append(raw)
            continue
        if parsed.fingerprint == fingerprint:
            continue
        kept.append(raw)

    body = "\n".join(kept)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body
