"""
    Windows authentication-management feature.

    Implements the cross-OS authentication-management contract for
    Windows using ``secedit /export`` / ``secedit /configure`` for
    local-security-policy round-trip and PowerShell
    ``Get-ItemProperty`` / ``Set-ItemProperty`` against the LSA
    registry path under
    ``HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Lsa`` for
    authentication-package management.  Every command is dispatched
    through the injected :class:`ICommandRunner`.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Windows tool                      |
    +=============================+===================================+
    | ``get_auth_config``         | ``secedit /export /cfg <tmp.cfg>``|
    |                             | + ``Get-Content <tmp.cfg>`` +     |
    |                             | parse                             |
    +-----------------------------+-----------------------------------+
    | ``set_auth_config``         | ``Set-Content <tmp.cfg>`` +       |
    |                             | ``secedit /configure /db          |
    |                             | <tmp.sdb> /cfg <tmp.cfg>``        |
    +-----------------------------+-----------------------------------+
    | ``list_auth_methods``       | ``Get-ItemProperty                |
    |                             | 'HKLM:\\...\\Lsa' -Name           |
    |                             | 'Authentication Packages' |       |
    |                             | ConvertTo-Json``                  |
    +-----------------------------+-----------------------------------+
    | ``enable_method``           | ``Set-ItemProperty                |
    |                             | 'HKLM:\\...\\Lsa' -Name           |
    |                             | 'Authentication Packages' -Value  |
    |                             | <new MULTI_SZ>``                  |
    +-----------------------------+-----------------------------------+
    | ``disable_method``          | (same as enable; new list with    |
    |                             | name removed)                     |
    +-----------------------------+-----------------------------------+
    | ``validate_auth_config``    | ``secedit /validate <tmp.cfg>``   |
    |                             | + internal parser                 |
    +-----------------------------+-----------------------------------+

    Authentication-model divergence:
        Unlike Linux PAM, Windows authentication has no per-service
        stack composer and no concept of a stack at all.  Logon
        authentication is driven by the LSA, which loads the
        authentication-package DLLs listed in the ``Authentication
        Packages`` ``MULTI_SZ`` registry value.  Account / lockout
        / password / privilege policy is governed by the local
        security policy database, fronted by ``secedit`` (CLI) and
        ``secpol.msc`` (GUI).  This module exposes both surfaces
        through the cross-OS contract verbs.

    Elevation policy:
        Write verbs honour ``config.elevated``.  Reads
        (``get_auth_config``, ``list_auth_methods``,
        ``validate_auth_config``) never elevate.

    File I/O policy:
        ``secedit`` writes to a temp ``.cfg`` file under
        ``C:\\Windows\\Temp\\`` (overridable via
        :class:`WindowsAuthenticationManagerConfig`).  Every read /
        write goes through the runner; no direct filesystem access
        occurs in this module.
"""

import json
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

_DEFAULT_SECEDIT = "secedit.exe"

_DEFAULT_LSA_PATH = r"HKLM:\SYSTEM\CurrentControlSet\Control\Lsa"
_AUTH_PACKAGES_VALUE_NAME = "Authentication Packages"

_DEFAULT_TEMP_CFG_PATH = r"C:\Windows\Temp\codecraftai-secedit.cfg"
_DEFAULT_TEMP_SDB_PATH = r"C:\Windows\Temp\codecraftai-secedit.sdb"

_AUTH_KIND_LOGON = "LOGON"
_AUTH_KIND_NETWORK = "NETWORK"
_AUTH_KIND_INTERACTIVE = "INTERACTIVE"
_AUTH_KIND_SERVICE = "SERVICE"

_SECTION_SYSTEM_ACCESS = "System Access"
_SECTION_PRIVILEGE_RIGHTS = "Privilege Rights"
_SECTION_EVENT_AUDIT = "Event Audit"
_SECTION_REGISTRY_VALUES = "Registry Values"

_CANONICAL_SECTION_ORDER = (
    _SECTION_SYSTEM_ACCESS,
    _SECTION_EVENT_AUDIT,
    _SECTION_PRIVILEGE_RIGHTS,
    _SECTION_REGISTRY_VALUES,
)

_DEFAULT_WELL_KNOWN_PACKAGES = (
    "msv1_0",
    "kerberos",
    "negoextender",
    "cloudap",
    "schannel",
    "wdigest",
)


WindowsAuthActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Subsystem typing
# ----------------------------------------------------------------------


@dataclass
class WindowsAuthSubsystem:
    """
        Discriminated wrapper around a security-policy section.

        :param kind: One of ``LOGON`` / ``NETWORK`` /
                     ``INTERACTIVE`` / ``SERVICE`` / ``OTHER``.
        :param name: The INF section name.  When ``kind`` is
                     ``OTHER`` callers MUST supply this; otherwise
                     it is fixed by the kind.
    """
    kind: str
    name: str


WINDOWS_AUTH_LOGON = WindowsAuthSubsystem(
    kind=_AUTH_KIND_LOGON, name=_SECTION_SYSTEM_ACCESS,
)
WINDOWS_AUTH_NETWORK = WindowsAuthSubsystem(
    kind=_AUTH_KIND_NETWORK, name=_SECTION_PRIVILEGE_RIGHTS,
)
WINDOWS_AUTH_INTERACTIVE = WindowsAuthSubsystem(
    kind=_AUTH_KIND_INTERACTIVE, name=_SECTION_EVENT_AUDIT,
)
WINDOWS_AUTH_SERVICE = WindowsAuthSubsystem(
    kind=_AUTH_KIND_SERVICE, name=_SECTION_REGISTRY_VALUES,
)


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


def _default_well_known_packages() -> list:
    """
        Build a fresh copy of the default well-known LSA package
        names.

        :returns: A new list of strings.
    """
    rtnval = list(_DEFAULT_WELL_KNOWN_PACKAGES)
    return rtnval


@dataclass
class WindowsAuthenticationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsAuthenticationManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Default
                                      ``"powershell.exe"``.
        :param secedit_executable: Path to ``secedit.exe``.
                                   Default ``"secedit.exe"``.
        :param lsa_path: PSDrive form of the LSA registry path.
                         Default
                         ``HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Lsa``.
        :param temp_cfg_path: Absolute path used for the secedit
                              ``.cfg`` round-trip.  Default
                              ``C:\\Windows\\Temp\\codecraftai-secedit.cfg``.
        :param temp_sdb_path: Absolute path used for the secedit
                              ``.sdb`` working database.  Default
                              ``C:\\Windows\\Temp\\codecraftai-secedit.sdb``.
        :param well_known_packages: List of LSA authentication-
                                    package names that
                                    :meth:`WindowsAuthenticationManager.list_auth_methods`
                                    surfaces with ``enabled=False``
                                    when they are not present in
                                    the live registry list.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    secedit_executable: str = _DEFAULT_SECEDIT
    lsa_path: str = _DEFAULT_LSA_PATH
    temp_cfg_path: str = _DEFAULT_TEMP_CFG_PATH
    temp_sdb_path: str = _DEFAULT_TEMP_SDB_PATH
    well_known_packages: list = field(
        default_factory=_default_well_known_packages,
    )


@dataclass
class WindowsAuthEntry:
    """
        A single security-policy entry.

        :param policy_key: The INI key name (e.g.
                           ``MinimumPasswordLength``).
        :param policy_value: The value text (e.g. ``"14"``).
        :param section: The INF section the key belongs to (e.g.
                        ``"System Access"``).
    """
    policy_key: str
    policy_value: str
    section: str


@dataclass
class WindowsAuthConfig:
    """
        A security-policy config for one subsystem.

        :param subsystem: The INF section name (or the
                          :class:`WindowsAuthSubsystem.kind` when
                          carrying a typed enum) targeted.
        :param entries: Policy entries.
    """
    subsystem: str
    entries: list = field(default_factory=list)


@dataclass
class WindowsAuthMethodInfo:
    """
        An LSA authentication package observed in the registry
        ``Authentication Packages`` MULTI_SZ.

        :param name: The package name.
        :param enabled: True when the package is currently in the
                        live MULTI_SZ list.
        :param description: Optional human-readable description.
        :param module_path: Mirrors ``name`` for contract
                            conformance.
    """
    name: str
    enabled: bool
    description: Optional[str] = None
    module_path: Optional[str] = None


@dataclass
class WindowsAuthValidationResult:
    """
        Outcome of
        :meth:`WindowsAuthenticationManager.validate_auth_config`.

        :param valid: True when no structural errors were detected.
        :param errors: List of error strings.
        :param warnings: List of warning strings.
    """
    valid: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class WindowsAuthActionResult(FeatureActionResult):
    """
        Result of an authentication-management write operation.

        :param subsystem: The subsystem targeted, when applicable.
        :param method: The method (LSA package name) targeted, when
                       applicable.
        :param message: Optional human-readable detail.
    """
    subsystem: Optional[str] = None
    method: Optional[str] = None
    message: Optional[str] = None


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class WindowsAuthenticationQueryError(Exception):
    """
        Raised when an authentication-management read fails.
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

        err_msg_lines = ["Authentication query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsAuthenticationConfigError(Exception):
    """
        Raised when a :class:`WindowsAuthConfig` is malformed or
        incompatible with the targeted subsystem.
    """

    def __init__(
        self,
        *,
        subsystem: str,
        detail: str,
    ) -> None:
        """
            :param subsystem: The subsystem the config targets.
            :param detail: Human-readable description.
        """
        self.subsystem = subsystem
        self.detail = detail

        err_msg_lines = ["Authentication config rejected."]
        err_msg_lines.append("    SUBSYSTEM: {}".format(subsystem))
        err_msg_lines.append("    DETAIL: {}".format(detail))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsAuthenticationSubsystemNotFoundError(Exception):
    """
        Raised when an unknown subsystem (or unregistered LSA
        package name) is referenced.
    """

    def __init__(
        self,
        *,
        subsystem: str,
        detail: str,
    ) -> None:
        """
            :param subsystem: The subsystem or method name.
            :param detail: Human-readable description.
        """
        self.subsystem = subsystem
        self.detail = detail

        err_msg_lines = ["Authentication subsystem not found."]
        err_msg_lines.append("    SUBSYSTEM: {}".format(subsystem))
        err_msg_lines.append("    DETAIL: {}".format(detail))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsAuthenticationManager(BaseFeatureManager):
    """
        Windows authentication-management manager.  Reads, writes,
        and validates the local security policy database via
        ``secedit`` and the LSA authentication-package list via
        the registry through the injected
        :class:`ICommandRunner`.
    """

    result_class = WindowsAuthActionResult
    status_class = WindowsAuthActionStatus
    feature_name = "authentication-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_auth_config(self, subsystem) -> WindowsAuthConfig:
        """
            Read a security-policy section's entries.

            :param subsystem: A :class:`WindowsAuthSubsystem`,
                              one of the ``WINDOWS_AUTH_*`` module
                              constants, or a raw string with the
                              INF section name.

            :returns: The :class:`WindowsAuthConfig`.
            :raises WindowsAuthenticationQueryError:
                When the secedit export fails.
        """
        section_name = self._resolve_subsystem(subsystem=subsystem)
        all_entries = self._run_secedit_export()

        filtered: list = []
        for entry in all_entries:
            if entry.section == section_name:
                filtered.append(entry)

        rtnval = WindowsAuthConfig(
            subsystem=section_name,
            entries=filtered,
        )
        return rtnval

    def list_auth_methods(self) -> list:
        """
            Enumerate the LSA authentication packages.

            :returns: A ``list[WindowsAuthMethodInfo]``.
            :raises WindowsAuthenticationQueryError:
                When the registry read fails.
        """
        live_packages = self._run_get_lsa_packages()

        live_lower: dict = {}
        for name in live_packages:
            live_lower[name.lower()] = name

        cfg = self._typed_config()
        merged: dict = {}
        for name in live_packages:
            merged[name] = True

        for well_known in cfg.well_known_packages:
            if well_known.lower() not in live_lower:
                merged[well_known] = False

        rtnval: list = []
        for name in sorted(merged.keys(), key=lambda x: x.lower()):
            info = WindowsAuthMethodInfo(
                name=name,
                enabled=merged[name],
                description=None,
                module_path=name,
            )
            rtnval.append(info)
        return rtnval

    def validate_auth_config(self, subsystem) -> WindowsAuthValidationResult:
        """
            Validate the security-policy database.

            Runs ``secedit /validate`` against the exported
            policy and supplements with the internal INF parser.

            :param subsystem: As for :meth:`get_auth_config`.

            :returns: The :class:`WindowsAuthValidationResult`.
        """
        section_name = self._resolve_subsystem(subsystem=subsystem)

        cfg = self._typed_config()
        export_command = self._build_secedit_export_command()
        export_result = self._runner.run(
            list(export_command), elevated=False,
        )
        if export_result.exit_code != 0:
            raise WindowsAuthenticationQueryError(
                command=export_command,
                exit_code=export_result.exit_code,
                stderr=export_result.stderr or "",
            )

        read_command = self._build_get_content_argv(
            path=cfg.temp_cfg_path,
        )
        read_result = self._runner.run(
            list(read_command), elevated=False,
        )
        if read_result.exit_code != 0:
            raise WindowsAuthenticationQueryError(
                command=read_command,
                exit_code=read_result.exit_code,
                stderr=read_result.stderr or "",
            )

        validate_command = [
            cfg.secedit_executable, "/validate", cfg.temp_cfg_path,
        ]
        validate_result = self._runner.run(
            list(validate_command), elevated=False,
        )

        errors: list = []
        warnings: list = []
        if validate_result.exit_code != 0:
            errors.append(
                "secedit /validate exit_code={}".format(
                    validate_result.exit_code,
                ),
            )

        parser_errors, parser_warnings = _walk_inf_for_validation(
            text=read_result.stdout,
        )
        errors.extend(parser_errors)
        warnings.extend(parser_warnings)

        # Section-presence warning specific to the requested subsystem.
        all_entries = _parse_secedit_inf(text=read_result.stdout)
        present_sections = {entry.section for entry in all_entries}
        if section_name not in present_sections:
            warnings.append(
                "section '{}' not present in exported policy".format(
                    section_name,
                ),
            )

        is_valid = len(errors) == 0
        rtnval = WindowsAuthValidationResult(
            valid=is_valid,
            errors=errors,
            warnings=warnings,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_auth_config(
        self,
        subsystem,
        config: WindowsAuthConfig,
    ) -> WindowsAuthActionResult:
        """
            Apply the supplied security-policy entries.

            Idempotent: returns ``SKIPPED`` when every key in the
            supplied config already matches the live policy.
            Honours dry-run.

            :param subsystem: As for :meth:`get_auth_config`.
            :param config: The desired
                           :class:`WindowsAuthConfig`.

            :returns: The :class:`WindowsAuthActionResult`.
        """
        section_name = self._resolve_subsystem(subsystem=subsystem)
        cfg = self._typed_config()

        # Probe current state.
        current_entries = self._run_secedit_export()
        current_in_section: dict = {}
        for entry in current_entries:
            if entry.section == section_name:
                current_in_section[entry.policy_key] = entry.policy_value

        all_match = True
        for desired_entry in config.entries:
            if current_in_section.get(desired_entry.policy_key) != desired_entry.policy_value:
                all_match = False
                break

        configure_command = [
            cfg.secedit_executable, "/configure",
            "/db", cfg.temp_sdb_path,
            "/cfg", cfg.temp_cfg_path,
            "/quiet",
        ]

        if all_match and len(config.entries) > 0:
            skipped = self._build_result(
                status=WindowsAuthActionStatus.SKIPPED,
                subsystem=section_name,
                method=None,
                command=configure_command,
                message="all keys already at desired values",
            )
            return skipped

        rendered_inf = _render_secedit_inf(
            section_name=section_name,
            entries=config.entries,
            extra_sections=current_in_section,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAuthActionStatus.DRY_RUN,
                subsystem=section_name,
                method=None,
                command=configure_command,
            )
            return dry

        write_command = self._build_set_content_argv(
            path=cfg.temp_cfg_path, content=rendered_inf,
        )
        write_result = self._runner.run(
            list(write_command), elevated=self._config.elevated,
        )
        if write_result.exit_code != 0:
            failed = self._wrap_runner_result(
                run_result=write_result,
                command=write_command,
                subsystem=section_name,
                method=None,
            )
            return failed

        configure_result = self._runner.run(
            list(configure_command), elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=configure_result,
            command=configure_command,
            subsystem=section_name,
            method=None,
        )
        return rtnval

    def enable_method(self, name: str) -> WindowsAuthActionResult:
        """
            Add an LSA authentication package to the registry
            ``Authentication Packages`` ``MULTI_SZ`` value.

            Idempotent: returns ``SKIPPED`` when the package is
            already in the list.  Honours dry-run.

            :param name: The package name (case-insensitive
                         match against the live list).

            :returns: The :class:`WindowsAuthActionResult`.
        """
        live_packages = self._run_get_lsa_packages()
        live_lower = {pkg.lower() for pkg in live_packages}

        if name.lower() in live_lower:
            cfg = self._typed_config()
            multi_string = _format_multi_string_for_powershell(
                values=live_packages,
            )
            command = self._build_set_itemproperty_argv(
                lsa_path=cfg.lsa_path,
                multi_string=multi_string,
            )
            skipped = self._build_result(
                status=WindowsAuthActionStatus.SKIPPED,
                subsystem=None,
                method=name,
                command=command,
                message="package already enabled",
            )
            return skipped

        new_list = list(live_packages)
        new_list.append(name)

        cfg = self._typed_config()
        multi_string = _format_multi_string_for_powershell(
            values=new_list,
        )
        command = self._build_set_itemproperty_argv(
            lsa_path=cfg.lsa_path,
            multi_string=multi_string,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAuthActionStatus.DRY_RUN,
                subsystem=None,
                method=name,
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
            subsystem=None,
            method=name,
        )
        return rtnval

    def disable_method(self, name: str) -> WindowsAuthActionResult:
        """
            Remove an LSA authentication package from the registry
            ``Authentication Packages`` ``MULTI_SZ`` value.

            Idempotent: returns ``SKIPPED`` when the package is
            already absent.  Honours dry-run.

            :param name: The package name.

            :returns: The :class:`WindowsAuthActionResult`.
        """
        live_packages = self._run_get_lsa_packages()
        live_lower = {pkg.lower() for pkg in live_packages}

        if name.lower() not in live_lower:
            cfg = self._typed_config()
            multi_string = _format_multi_string_for_powershell(
                values=live_packages,
            )
            command = self._build_set_itemproperty_argv(
                lsa_path=cfg.lsa_path,
                multi_string=multi_string,
            )
            skipped = self._build_result(
                status=WindowsAuthActionStatus.SKIPPED,
                subsystem=None,
                method=name,
                command=command,
                message="package already disabled",
            )
            return skipped

        new_list: list = []
        for pkg in live_packages:
            if pkg.lower() == name.lower():
                continue
            new_list.append(pkg)

        cfg = self._typed_config()
        multi_string = _format_multi_string_for_powershell(
            values=new_list,
        )
        command = self._build_set_itemproperty_argv(
            lsa_path=cfg.lsa_path,
            multi_string=multi_string,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAuthActionStatus.DRY_RUN,
                subsystem=None,
                method=name,
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
            subsystem=None,
            method=name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsAuthenticationManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed
                      :class:`WindowsAuthenticationManagerConfig`.
        """
        if isinstance(self._config, WindowsAuthenticationManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsAuthenticationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _resolve_subsystem(self, *, subsystem) -> str:
        """
            Resolve a subsystem reference into an INF section name.

            :param subsystem: A :class:`WindowsAuthSubsystem`, a
                              raw string, or one of the
                              ``WINDOWS_AUTH_*`` module constants.

            :returns: The INF section name string.
            :raises WindowsAuthenticationSubsystemNotFoundError:
                When ``subsystem`` is None or empty.
        """
        if subsystem is None:
            raise WindowsAuthenticationSubsystemNotFoundError(
                subsystem="<None>",
                detail="subsystem cannot be None",
            )

        if isinstance(subsystem, WindowsAuthSubsystem):
            section_name = subsystem.name
        elif isinstance(subsystem, str):
            kind_upper = subsystem.upper().strip()
            if kind_upper == _AUTH_KIND_LOGON:
                section_name = _SECTION_SYSTEM_ACCESS
            elif kind_upper == _AUTH_KIND_NETWORK:
                section_name = _SECTION_PRIVILEGE_RIGHTS
            elif kind_upper == _AUTH_KIND_INTERACTIVE:
                section_name = _SECTION_EVENT_AUDIT
            elif kind_upper == _AUTH_KIND_SERVICE:
                section_name = _SECTION_REGISTRY_VALUES
            else:
                section_name = subsystem
        else:
            raise WindowsAuthenticationSubsystemNotFoundError(
                subsystem=str(subsystem),
                detail="subsystem must be WindowsAuthSubsystem or str",
            )

        if len(section_name) == 0:
            raise WindowsAuthenticationSubsystemNotFoundError(
                subsystem="<empty>",
                detail="subsystem section name is empty",
            )

        rtnval = section_name
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

    def _build_secedit_export_command(self) -> list:
        """
            Build the ``secedit /export`` argv.

            :returns: The argv list.
        """
        cfg = self._typed_config()
        rtnval = [
            cfg.secedit_executable, "/export",
            "/cfg", cfg.temp_cfg_path,
            "/quiet",
        ]
        return rtnval

    def _build_get_content_argv(self, *, path: str) -> list:
        """
            Build the PowerShell argv for ``Get-Content -Path
            <path> -Raw``.

            :param path: The absolute path to read.

            :returns: The argv list.
        """
        script = "Get-Content -Path '{}' -Raw".format(path)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_set_content_argv(
        self,
        *,
        path: str,
        content: str,
    ) -> list:
        """
            Build the PowerShell argv for ``Set-Content -Path
            <path> -Value <inline>``.

            The ``<inline>`` form embeds the content as a
            single-quoted PowerShell string with embedded single
            quotes doubled.  Newlines are preserved by emitting
            backtick-n escapes inside a double-quoted string instead
            so PowerShell interprets them.

            :param path: The absolute path to write.
            :param content: The content to write.

            :returns: The argv list.
        """
        escaped_path = path.replace("'", "''")
        # Use Set-Content with a here-string to preserve content verbatim.
        # PowerShell here-strings are robust against embedded quotes.
        here_doc = "@'\n{}\n'@".format(content)
        script = (
            "Set-Content -Path '{}' -Value {}"
        ).format(escaped_path, here_doc)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_get_itemproperty_lsa_argv(self) -> list:
        """
            Build the PowerShell argv for the LSA
            ``Authentication Packages`` registry read.

            :returns: The argv list.
        """
        cfg = self._typed_config()
        script = (
            "Get-ItemProperty -Path '{}' -Name '{}' | "
            "Select-Object -ExpandProperty '{}' | ConvertTo-Json"
        ).format(
            cfg.lsa_path,
            _AUTH_PACKAGES_VALUE_NAME,
            _AUTH_PACKAGES_VALUE_NAME,
        )
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_set_itemproperty_argv(
        self,
        *,
        lsa_path: str,
        multi_string: str,
    ) -> list:
        """
            Build the PowerShell argv for the LSA registry write.

            :param lsa_path: PSDrive form of the LSA path.
            :param multi_string: PowerShell-formatted MULTI_SZ
                                 literal (e.g. ``"'pkg1','pkg2'"``).

            :returns: The argv list.
        """
        script = (
            "Set-ItemProperty -Path '{}' -Name '{}' "
            "-Value @({}) -Type MultiString"
        ).format(
            lsa_path,
            _AUTH_PACKAGES_VALUE_NAME,
            multi_string,
        )
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _run_secedit_export(self) -> list:
        """
            Run ``secedit /export`` then ``Get-Content`` and parse
            the INF body into a flat list of
            :class:`WindowsAuthEntry`.

            :returns: A ``list[WindowsAuthEntry]``.
            :raises WindowsAuthenticationQueryError:
                When either command fails.
        """
        cfg = self._typed_config()

        export_command = self._build_secedit_export_command()
        export_result = self._runner.run(
            list(export_command), elevated=False,
        )
        if export_result.exit_code != 0:
            raise WindowsAuthenticationQueryError(
                command=export_command,
                exit_code=export_result.exit_code,
                stderr=export_result.stderr or "",
            )

        read_command = self._build_get_content_argv(
            path=cfg.temp_cfg_path,
        )
        read_result = self._runner.run(
            list(read_command), elevated=False,
        )
        if read_result.exit_code != 0:
            raise WindowsAuthenticationQueryError(
                command=read_command,
                exit_code=read_result.exit_code,
                stderr=read_result.stderr or "",
            )

        rtnval = _parse_secedit_inf(text=read_result.stdout)
        return rtnval

    def _run_get_lsa_packages(self) -> list:
        """
            Read the LSA ``Authentication Packages`` registry value
            and return it as a list of strings.

            :returns: A ``list[str]``.
            :raises WindowsAuthenticationQueryError:
                When the runner reports a non-zero exit code.
        """
        command = self._build_get_itemproperty_lsa_argv()
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsAuthenticationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )
        rtnval = _parse_lsa_packages_json(text=run_result.stdout)
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsAuthActionStatus,
        subsystem: Optional[str],
        method: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> WindowsAuthActionResult:
        """
            Build a result without invoking the runner.

            :param status: The outcome status.
            :param subsystem: The subsystem targeted, or None.
            :param method: The method targeted, or None.
            :param command: The would-have-been argv.
            :param message: Optional human-readable detail.

            :returns: The :class:`WindowsAuthActionResult`.
        """
        if subsystem is not None:
            detail_text = subsystem
        elif method is not None:
            detail_text = method
        else:
            detail_text = None

        rtnval = WindowsAuthActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            subsystem=subsystem,
            method=method,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        subsystem: Optional[str],
        method: Optional[str],
    ) -> WindowsAuthActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`WindowsAuthActionResult`.

            :param run_result: The runner's result.
            :param command: The argv that was dispatched.
            :param subsystem: The subsystem targeted, or None.
            :param method: The method targeted, or None.

            :returns: The :class:`WindowsAuthActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = WindowsAuthActionStatus.SUCCESS
        else:
            mapped_status = WindowsAuthActionStatus.FAILED

        if subsystem is not None:
            detail_text = subsystem
        elif method is not None:
            detail_text = method
        else:
            detail_text = None

        rtnval = WindowsAuthActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            subsystem=subsystem,
            method=method,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — INF parser / renderer
# ----------------------------------------------------------------------


def _parse_secedit_inf(*, text: str) -> list:
    """
        Parse a ``secedit /export`` INF body into a list of
        :class:`WindowsAuthEntry`.

        :param text: The raw INF body.

        :returns: A ``list[WindowsAuthEntry]``.
    """
    rtnval: list = []
    current_section: Optional[str] = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith(";"):
            continue
        if stripped.startswith("#"):
            continue

        if stripped.startswith("[") and stripped.endswith("]"):
            section_text = stripped[1:-1].strip()
            current_section = section_text
            continue

        if current_section is None:
            continue

        if "=" not in stripped:
            continue

        key_part, _, value_part = stripped.partition("=")
        key_text = key_part.strip()
        value_text = value_part.strip()
        if len(key_text) == 0:
            continue

        entry = WindowsAuthEntry(
            policy_key=key_text,
            policy_value=value_text,
            section=current_section,
        )
        rtnval.append(entry)
    return rtnval


def _walk_inf_for_validation(*, text: str) -> tuple:
    """
        Walk an INF body looking for structural issues.

        :param text: The raw INF body.

        :returns: ``(errors, warnings)`` where each is a
                  ``list[str]``.
    """
    errors: list = []
    warnings: list = []
    current_section: Optional[str] = None
    seen_sections: list = []

    for index, raw_line in enumerate(text.splitlines()):
        line_num = index + 1
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith(";"):
            continue
        if stripped.startswith("#"):
            continue

        if stripped.startswith("[") and stripped.endswith("]"):
            section_text = stripped[1:-1].strip()
            if section_text in seen_sections:
                warnings.append(
                    "line {}: duplicate section '{}'".format(
                        line_num, section_text,
                    ),
                )
            else:
                seen_sections.append(section_text)
            current_section = section_text
            continue

        if current_section is None:
            errors.append(
                "line {}: entry outside any section".format(line_num),
            )
            continue

        if "=" not in stripped:
            errors.append(
                "line {}: missing '=' separator".format(line_num),
            )
            continue

    rtnval = (errors, warnings)
    return rtnval


def _render_secedit_inf(
    *,
    section_name: str,
    entries: list,
    extra_sections: dict,
) -> str:
    """
        Render an INF body that ``secedit /configure`` will
        accept.

        :param section_name: The section the supplied entries
                             belong to.
        :param entries: The desired entries (each carries a
                        ``policy_key`` and ``policy_value``).
        :param extra_sections: Existing entries from the same
                               section that the caller did NOT
                               override.  Used to preserve unrelated
                               keys when applying a partial change.

        :returns: The rendered INF body terminated with CRLF.
    """
    lines: list = []
    lines.append("[Unicode]")
    lines.append("Unicode=yes")
    lines.append("[Version]")
    lines.append("signature=\"$CHICAGO$\"")
    lines.append("Revision=1")

    section_entries: dict = {}
    for key_name, value_text in extra_sections.items():
        section_entries[key_name] = value_text
    for entry in entries:
        section_entries[entry.policy_key] = entry.policy_value

    for canonical_section in _CANONICAL_SECTION_ORDER:
        if canonical_section != section_name:
            continue
        lines.append("[{}]".format(canonical_section))
        for key_name in section_entries:
            value_text = section_entries[key_name]
            lines.append("{} = {}".format(key_name, value_text))

    if section_name not in _CANONICAL_SECTION_ORDER:
        lines.append("[{}]".format(section_name))
        for key_name in section_entries:
            value_text = section_entries[key_name]
            lines.append("{} = {}".format(key_name, value_text))

    body = "\r\n".join(lines)
    if not body.endswith("\r\n"):
        body = body + "\r\n"
    return body


# ----------------------------------------------------------------------
# Module-level helpers — LSA MULTI_SZ helpers
# ----------------------------------------------------------------------


def _parse_lsa_packages_json(*, text: str) -> list:
    """
        Parse the JSON output of
        ``Get-ItemProperty | Select-Object -ExpandProperty
        'Authentication Packages' | ConvertTo-Json``.

        PowerShell collapses single-element MULTI_SZ values to a
        scalar string when serialising; the parser handles both
        shapes.

        :param text: Raw stdout.

        :returns: A ``list[str]``.
    """
    stripped = text.strip()
    if len(stripped) == 0:
        rtnval: list = []
        return rtnval

    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        rtnval = []
        return rtnval

    if isinstance(decoded, list):
        rtnval = []
        for item in decoded:
            if isinstance(item, str):
                rtnval.append(item)
            else:
                rtnval.append(str(item))
        return rtnval

    if isinstance(decoded, str):
        # Newline-separated MULTI_SZ collapses to a single string in
        # some PowerShell flows; split when needed.
        if "\n" in decoded or "\r" in decoded:
            parts: list = []
            for part in decoded.replace("\r", "\n").split("\n"):
                trimmed = part.strip()
                if len(trimmed) > 0:
                    parts.append(trimmed)
            rtnval = parts
            return rtnval
        rtnval = [decoded]
        return rtnval

    rtnval = [str(decoded)]
    return rtnval


def _format_multi_string_for_powershell(*, values: list) -> str:
    """
        Render a list of strings into PowerShell array-literal
        syntax suitable for the ``-Value @(...)`` portion of
        ``Set-ItemProperty``.

        Each value is wrapped in single quotes with embedded single
        quotes doubled.

        :param values: The values.

        :returns: A string like ``"'kerberos','msv1_0'"``.
    """
    parts: list = []
    for raw_value in values:
        text_value = str(raw_value)
        escaped = text_value.replace("'", "''")
        parts.append("'{}'".format(escaped))
    rtnval = ",".join(parts)
    return rtnval
