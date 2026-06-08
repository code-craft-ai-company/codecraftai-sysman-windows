"""
    Windows package-management feature.

    Implements the cross-OS package-management contract for Windows using
    the Windows Package Manager (``winget.exe``).  Every verb is dispatched
    through an :class:`ICommandRunner` so the manager works identically
    under local and WinRM execution.

    Elevation policy:
        Windows has no ``sudo``.  ``config.elevated=True`` is advisory:
        the manager forwards ``elevated=True`` to the runner for every
        write verb (install / remove / upgrade / refresh) and
        ``elevated=False`` for every read verb (``is_installed``,
        ``get_package_info``, ``list_installed``) and every idempotency
        probe.  Machine-scope writes on Windows 10 1809+ require an
        Administrator token; the runner is responsible for already
        carrying that token.

    Non-interactive flags:
        Every write argv includes ``--silent
        --accept-package-agreements --accept-source-agreements`` so
        ``winget`` never blocks on agreement prompts under unattended
        execution.  These are real ``winget`` CLI flags.

    Output-parsing fragility:
        ``winget`` produces text output for ``list`` / ``show`` /
        ``upgrade`` rather than a stable machine-readable form.  The
        column boundaries and dash-separator rows differ across
        ``winget`` versions and Windows locales (the column headers and
        the dash row are localised).  This module's parsing strategy is:

            1. Locate the dash-separator row (``-`` runs interleaved
               with whitespace) which appears once after the header
               row and never inside the data rows.
            2. Use the column starts inferred from the header row to
               slice each subsequent data row by character position;
               fall back to whitespace splitting when a row is shorter
               than expected.
            3. Skip lines that are obvious progress / footer noise
               (lines containing ``\\``, ``/``, ``|``, ``-`` runs
               without other characters, or that start with the
               progress-spinner glyphs).

        Callers should treat the table parsers as best-effort and rely
        on :meth:`WindowsPackageManager.is_installed` (which is
        version-agnostic) for hard idempotency decisions.  Future
        ``winget`` versions are expected to expose stable JSON output;
        when that lands, the table parsers should be retired in favour
        of a JSON path.
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


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_DEFAULT_WINGET = "winget"

# Real winget CLI flags.
_FLAG_EXACT = "--exact"
_FLAG_ID = "--id"
_FLAG_VERSION = "--version"
_FLAG_SILENT = "--silent"
_FLAG_ACCEPT_PACKAGE_AGREEMENTS = "--accept-package-agreements"
_FLAG_ACCEPT_SOURCE_AGREEMENTS = "--accept-source-agreements"
_FLAG_ALL = "--all"
_FLAG_INCLUDE_UNKNOWN = "--include-unknown"

# Sentinel substrings ``winget`` prints when a query target is missing.
# These vary by locale; the lower-cased English form is the documented
# canonical value, and the manager treats any of them as "missing".
_SENTINELS_PACKAGE_NOT_FOUND = (
    "no installed package found",
    "no package found matching input criteria",
    "no applicable installed package",
)


WindowsPackageActionStatus = ActionStatus


@dataclass
class WindowsPackageManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsPackageManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param winget_executable: Path to the ``winget`` executable used
                                  to dispatch verbs.  Defaults to
                                  ``"winget"`` (PATH-resolved).  Set to
                                  an absolute path when the runner does
                                  not have a usable ``PATH`` (e.g. some
                                  WinRM runspaces).
        :param extra_install_args: Optional list of additional argv
                                   tokens appended to every ``winget
                                   install`` / ``winget upgrade`` /
                                   ``winget uninstall`` argv.  Defaults
                                   to ``None`` (treated as no extra
                                   args).  Useful for forwarding
                                   ``--scope user``, ``--source
                                   winget``, etc.  These tokens are
                                   echoed onto the recorded result.
    """
    winget_executable: str = _DEFAULT_WINGET
    extra_install_args: Optional[list] = None


@dataclass
class WindowsPackageActionResult(FeatureActionResult):
    """
        Result of a package-management write operation.

        :param name: The package id targeted, or ``None`` for
                     ``upgrade_all`` and ``refresh_metadata``.
        :param version: The version targeted (or resolved) when known.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    version: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsPackageInfo:
    """
        Information about a package as reported by ``winget``.

        :param name: Display name as printed in the ``Name`` column of
                     ``winget list`` / ``winget show``.
        :param id: Stable package id (``Microsoft.PowerShell``, etc.)
                   as printed in the ``Id`` column of ``winget list``
                   and the ``Id`` row of ``winget show``.
        :param version: Installed version string (or the latest version
                        reported by ``winget show`` when not yet
                        installed).
        :param publisher: Publisher / vendor name as printed in
                          ``winget show``.  ``None`` when not known
                          (e.g. ``winget list`` does not surface the
                          publisher).
        :param description: Short description from ``winget show``.
                            ``None`` when not known.
        :param homepage: Upstream homepage URL from ``winget show``.
                         ``None`` when not known.
        :param source: Source channel name (``winget``, ``msstore``)
                       as printed in the ``Source`` column of
                       ``winget list``.  ``None`` when not known.
    """
    name: str
    id: str
    version: str
    publisher: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    source: Optional[str] = None


class WindowsPackageQueryError(Exception):
    """
        Raised when a package read fails for a reason other than the
        package being absent.
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
        self.command: list = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Package query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsPackageManager(BaseFeatureManager):
    """
        Windows ``winget`` package manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`.

        Writes (``install`` / ``uninstall`` / ``upgrade`` / ``source
        update``) honour ``config.elevated`` and pass it through to the
        runner.  Reads (``list`` / ``show``) always pass
        ``elevated=False``.
    """

    result_class = WindowsPackageActionResult
    status_class = WindowsPackageActionStatus
    feature_name = "package-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def install_package(
        self,
        name: str,
        version: Optional[str] = None,
    ) -> WindowsPackageActionResult:
        """
            Install a package, optionally pinned to a specific version.

            Skipped when the package is already installed AND either
            ``version`` is None or the currently-installed version
            equals the requested version.

            :param name: The package id (``--id`` argument).
            :param version: Optional exact version pin
                            (``winget install ... --version <ver>``).

            :returns: The :class:`WindowsPackageActionResult`.
        """
        command = self._build_install_argv(name=name, version=version)

        if self._config.dry_run:
            dry_result = self._build_result(
                status=WindowsPackageActionStatus.DRY_RUN,
                name=name,
                version=version,
                command=command,
            )
            return dry_result

        existing_info = self._probe_installed_info(name=name)
        if existing_info is not None:
            if version is None or existing_info.version == version:
                skipped = self._build_result(
                    status=WindowsPackageActionStatus.SKIPPED,
                    name=name,
                    version=existing_info.version,
                    command=command,
                    message="package is already installed",
                )
                return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            version=version,
            operation="install_package",
        )
        return rtnval

    def remove_package(self, name: str) -> WindowsPackageActionResult:
        """
            Remove an installed package.  Skipped when the package is
            not installed.

            :param name: The package id.

            :returns: The :class:`WindowsPackageActionResult`.
        """
        command = self._build_uninstall_argv(name=name)

        if self._config.dry_run:
            dry_result = self._build_result(
                status=WindowsPackageActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry_result

        if not self.is_installed(name=name):
            skipped = self._build_result(
                status=WindowsPackageActionStatus.SKIPPED,
                name=name,
                command=command,
                message="package is not installed",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            operation="remove_package",
        )
        return rtnval

    def upgrade_package(self, name: str) -> WindowsPackageActionResult:
        """
            Upgrade a single package to its latest available version.

            Skipped when the package is not installed (with an explicit
            "nothing to upgrade" message) and when ``winget upgrade
            --include-unknown`` does not list the package as having an
            available upgrade.

            :param name: The package id.

            :returns: The :class:`WindowsPackageActionResult`.
        """
        command = self._build_upgrade_argv(name=name)

        if self._config.dry_run:
            dry_result = self._build_result(
                status=WindowsPackageActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry_result

        if not self.is_installed(name=name):
            skipped = self._build_result(
                status=WindowsPackageActionStatus.SKIPPED,
                name=name,
                command=command,
                message="package is not installed; nothing to upgrade",
            )
            return skipped

        if not self._has_upgrade_available(name=name):
            skipped = self._build_result(
                status=WindowsPackageActionStatus.SKIPPED,
                name=name,
                command=command,
                message="package is already at the latest available version",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            operation="upgrade_package",
        )
        return rtnval

    def upgrade_all(self) -> WindowsPackageActionResult:
        """
            Upgrade every installed package to its latest available
            version.  Never skips.

            :returns: The :class:`WindowsPackageActionResult`.
        """
        command = self._build_upgrade_all_argv()
        rtnval = self._dispatch_write(
            command=command,
            name=None,
            operation="upgrade_all",
        )
        return rtnval

    def refresh_metadata(self) -> WindowsPackageActionResult:
        """
            Refresh ``winget`` source metadata via ``winget source
            update``.  Never skips.

            :returns: The :class:`WindowsPackageActionResult`.
        """
        command = self._build_source_update_argv()
        rtnval = self._dispatch_write(
            command=command,
            name=None,
            operation="refresh_metadata",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def is_installed(self, name: str) -> bool:
        """
            Return whether a package is currently installed.

            Dispatches ``winget list --exact --id <name>`` without
            elevation.  Treats exit code 0 with stdout that does NOT
            contain a "no installed package" sentinel as "installed".

            :param name: The package id.

            :returns: True when ``winget list`` exits zero with a row
                      matching the requested id; False otherwise.
        """
        command = self._build_list_one_argv(name=name)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        if self._stdout_looks_missing(stdout=run_result.stdout):
            rtnval = False
            return rtnval

        # ``winget list`` always emits a header even on hits, so we also
        # require at least one data row that mentions the requested id.
        rows = self._parse_winget_list_table(stdout=run_result.stdout)
        for row in rows:
            row_id = row.get("Id", "")
            if row_id == name:
                rtnval = True
                return rtnval

        # Some winget versions surface the id only in the ``Name`` column
        # for built-ins; fall back to "any data row present" so we do not
        # under-report installed state on those versions.
        if len(rows) > 0:
            rtnval = True
            return rtnval

        rtnval = False
        return rtnval

    def get_package_info(self, name: str) -> Optional[WindowsPackageInfo]:
        """
            Query package metadata via ``winget show --exact --id
            <name>``.

            :param name: The package id.

            :returns: A :class:`WindowsPackageInfo` when ``winget``
                      recognises the id; ``None`` when ``winget``
                      reports that no matching package was found.
            :raises WindowsPackageQueryError: When ``winget show``
                                              fails for any other
                                              reason.
        """
        command = self._build_show_argv(name=name)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            stdout_missing = self._stdout_looks_missing(
                stdout=run_result.stdout,
            )
            stderr_missing = self._stdout_looks_missing(
                stdout=run_result.stderr,
            )
            if stdout_missing or stderr_missing:
                rtnval: Optional[WindowsPackageInfo] = None
                return rtnval
            raise WindowsPackageQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        if self._stdout_looks_missing(stdout=run_result.stdout):
            rtnval = None
            return rtnval

        info = self._parse_winget_show_text(
            stdout=run_result.stdout,
            requested_id=name,
        )
        rtnval = info
        return rtnval

    def list_installed(self) -> list:
        """
            List every installed package as reported by ``winget
            list``.

            :returns: One :class:`WindowsPackageInfo` per parsed row.
            :raises WindowsPackageQueryError: When the runner reports
                                              a non-zero exit code.
        """
        command = self._build_list_all_argv()
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise WindowsPackageQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rows = self._parse_winget_list_table(stdout=run_result.stdout)
        rtnval: list = []
        for row in rows:
            info = self._row_to_info(row=row)
            if info is not None:
                rtnval.append(info)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsPackageManagerConfig:
        """
            Return the manager's config typed as
            :class:`WindowsPackageManagerConfig`.

            The base class stores it as :class:`FeatureConfig` for
            type-checker friendliness.  This helper centralises the
            ``isinstance`` cast so callers can read
            ``winget_executable`` / ``extra_install_args`` without
            losing static-type clarity.

            :returns: The active config typed as
                      :class:`WindowsPackageManagerConfig`.
        """
        if isinstance(self._config, WindowsPackageManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsPackageManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _winget_executable(self) -> str:
        """
            Return the configured ``winget`` executable path.

            :returns: The executable path / name from the config.
        """
        rtnval = self._typed_config().winget_executable
        return rtnval

    def _extra_install_args(self) -> list:
        """
            Return a defensive copy of ``extra_install_args`` from the
            config, or an empty list when None.

            :returns: A fresh ``list[str]`` of extra argv tokens.
        """
        typed_config = self._typed_config()
        if typed_config.extra_install_args is None:
            rtnval: list = []
            return rtnval
        rtnval = list(typed_config.extra_install_args)
        return rtnval

    def _build_install_argv(
        self,
        *,
        name: str,
        version: Optional[str],
    ) -> list:
        """
            Build the full argv for ``winget install``.

            :param name: The package id.
            :param version: Optional exact version pin.

            :returns: The full argv.
        """
        argv: list = [
            self._winget_executable(),
            "install",
            _FLAG_EXACT,
            _FLAG_ID,
            name,
        ]
        if version is not None:
            argv.append(_FLAG_VERSION)
            argv.append(version)
        argv.append(_FLAG_SILENT)
        argv.append(_FLAG_ACCEPT_PACKAGE_AGREEMENTS)
        argv.append(_FLAG_ACCEPT_SOURCE_AGREEMENTS)

        extras = self._extra_install_args()
        if len(extras) > 0:
            argv.extend(extras)

        rtnval = argv
        return rtnval

    def _build_uninstall_argv(self, *, name: str) -> list:
        """
            Build the full argv for ``winget uninstall``.

            :param name: The package id.

            :returns: The full argv.
        """
        argv: list = [
            self._winget_executable(),
            "uninstall",
            _FLAG_EXACT,
            _FLAG_ID,
            name,
            _FLAG_SILENT,
        ]

        extras = self._extra_install_args()
        if len(extras) > 0:
            argv.extend(extras)

        rtnval = argv
        return rtnval

    def _build_upgrade_argv(self, *, name: str) -> list:
        """
            Build the full argv for ``winget upgrade <name>``.

            :param name: The package id.

            :returns: The full argv.
        """
        argv: list = [
            self._winget_executable(),
            "upgrade",
            _FLAG_EXACT,
            _FLAG_ID,
            name,
            _FLAG_SILENT,
            _FLAG_ACCEPT_PACKAGE_AGREEMENTS,
            _FLAG_ACCEPT_SOURCE_AGREEMENTS,
        ]

        extras = self._extra_install_args()
        if len(extras) > 0:
            argv.extend(extras)

        rtnval = argv
        return rtnval

    def _build_upgrade_all_argv(self) -> list:
        """
            Build the full argv for ``winget upgrade --all``.

            :returns: The full argv.
        """
        rtnval = [
            self._winget_executable(),
            "upgrade",
            _FLAG_ALL,
            _FLAG_SILENT,
            _FLAG_ACCEPT_PACKAGE_AGREEMENTS,
            _FLAG_ACCEPT_SOURCE_AGREEMENTS,
        ]
        return rtnval

    def _build_source_update_argv(self) -> list:
        """
            Build the full argv for ``winget source update``.

            :returns: The full argv.
        """
        rtnval = [
            self._winget_executable(),
            "source",
            "update",
        ]
        return rtnval

    def _build_list_one_argv(self, *, name: str) -> list:
        """
            Build the full argv for ``winget list --exact --id <name>``.

            :param name: The package id.

            :returns: The full argv.
        """
        rtnval = [
            self._winget_executable(),
            "list",
            _FLAG_EXACT,
            _FLAG_ID,
            name,
        ]
        return rtnval

    def _build_list_all_argv(self) -> list:
        """
            Build the full argv for ``winget list``.

            :returns: The full argv.
        """
        rtnval = [
            self._winget_executable(),
            "list",
        ]
        return rtnval

    def _build_show_argv(self, *, name: str) -> list:
        """
            Build the full argv for ``winget show --exact --id <name>``.

            :param name: The package id.

            :returns: The full argv.
        """
        rtnval = [
            self._winget_executable(),
            "show",
            _FLAG_EXACT,
            _FLAG_ID,
            name,
        ]
        return rtnval

    def _build_upgrade_probe_argv(self) -> list:
        """
            Build the argv used by the upgrade-availability probe:
            ``winget upgrade --include-unknown``.

            :returns: The full argv.
        """
        rtnval = [
            self._winget_executable(),
            "upgrade",
            _FLAG_INCLUDE_UNKNOWN,
        ]
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — dispatch / result construction
    # ------------------------------------------------------------------

    def _dispatch_write(
        self,
        *,
        command: list,
        name: Optional[str],
        version: Optional[str] = None,
        operation: str,
    ) -> WindowsPackageActionResult:
        """
            Dispatch a write command, honouring ``config.elevated``,
            and short-circuit on dry-run via the base :meth:`_dispatch`.

            :param command: The full argv.
            :param name: The package id to echo onto the result.
            :param version: The version to echo onto the result.
            :param operation: Reserved for future auto-reporting.

            :returns: The :class:`WindowsPackageActionResult`.
        """
        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation=operation,
            elevated_override=None,
            name=name,
            version=version,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsPackageActionStatus,
        name: Optional[str],
        version: Optional[str] = None,
        command: list,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsPackageActionResult:
        """
            Construct a :class:`WindowsPackageActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param name: The package id to echo onto the result.
            :param version: The version to echo onto the result.
            :param command: The would-have-been argv, copied for capture.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`WindowsPackageActionResult`.
        """
        rtnval = WindowsPackageActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            version=version,
            message=message,
            stderr=stderr,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — idempotency probes
    # ------------------------------------------------------------------

    def _probe_installed_info(
        self,
        *,
        name: str,
    ) -> Optional[WindowsPackageInfo]:
        """
            Return the parsed info row for ``name`` from ``winget
            list --exact --id <name>``, or ``None`` when the package
            is not installed.

            :param name: The package id.

            :returns: The :class:`WindowsPackageInfo`, or ``None``.
        """
        command = self._build_list_one_argv(name=name)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval: Optional[WindowsPackageInfo] = None
            return rtnval

        if self._stdout_looks_missing(stdout=run_result.stdout):
            rtnval = None
            return rtnval

        rows = self._parse_winget_list_table(stdout=run_result.stdout)
        if len(rows) == 0:
            rtnval = None
            return rtnval

        # Prefer the row whose ``Id`` column matches exactly; fall back
        # to the first row when the column is absent.
        chosen_row = None
        for row in rows:
            row_id = row.get("Id", "")
            if row_id == name:
                chosen_row = row
                break
        if chosen_row is None:
            chosen_row = rows[0]

        rtnval = self._row_to_info(row=chosen_row)
        return rtnval

    def _has_upgrade_available(self, *, name: str) -> bool:
        """
            Return True when ``winget upgrade --include-unknown``
            lists ``name`` as having an available upgrade.

            Exit-code variability:
                ``winget upgrade`` returns ``0x8A150011`` (a.k.a.
                ``-2147023087``) on some versions when no upgrades are
                available.  The probe treats any non-zero exit as
                "no upgrade visible" rather than raising, because the
                hard-error path is owned by :meth:`get_package_info`.

            :param name: The package id.

            :returns: True when ``name`` appears in the upgrade table;
                      False otherwise (including on command failure).
        """
        command = self._build_upgrade_probe_argv()
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        rows = self._parse_winget_list_table(stdout=run_result.stdout)
        for row in rows:
            row_id = row.get("Id", "")
            if row_id == name:
                rtnval = True
                return rtnval

        rtnval = False
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — output parsing
    # ------------------------------------------------------------------

    def _stdout_looks_missing(self, *, stdout: str) -> bool:
        """
            Heuristically detect "package not found" output.

            Searches the lower-cased text for any of the documented
            sentinel substrings.  ``winget`` localises these strings
            but the English form is used for the canonical English
            locale and surfaces in most CI environments.

            :param stdout: The captured stdout (or stderr) text.

            :returns: True when the text matches any sentinel; False
                      otherwise.
        """
        text_lower = stdout.lower()
        for sentinel in _SENTINELS_PACKAGE_NOT_FOUND:
            if sentinel in text_lower:
                rtnval = True
                return rtnval
        rtnval = False
        return rtnval

    def _parse_winget_list_table(self, *, stdout: str) -> list:
        """
            Parse a fixed-width ``winget list`` / ``winget upgrade``
            table into a list of column->value dicts.

            Strategy:
                1. Locate the dash-separator row (a line whose
                   non-whitespace characters are all ``-``).  This
                   anchors where the data rows begin; ``winget``
                   emits one continuous run of ``-`` regardless of
                   column count.
                2. Use the previous non-empty line as the header row.
                   Compute column starts from the header's
                   whitespace gaps (each non-whitespace token starts
                   a new column).  This is the robust approach
                   because the separator is one big run with no
                   per-column boundaries.
                3. Slice every subsequent data row by those starts;
                   trim each cell.  Skip lines that look like
                   progress noise (``\\``, ``/``, ``|`` glyphs, or
                   empty strings).

            :param stdout: Raw stdout text.

            :returns: A list of dicts, each keyed by column header.
                      An empty list is returned when no
                      dash-separator row is found.
        """
        lines = stdout.splitlines()

        separator_index = -1
        for index in range(len(lines)):
            candidate = lines[index]
            if self._line_is_dash_separator(line=candidate):
                separator_index = index
                break

        if separator_index <= 0:
            rtnval: list = []
            return rtnval

        # Walk backwards to find the header line (skip blank lines).
        header_index = -1
        for index in range(separator_index - 1, -1, -1):
            candidate_line = lines[index]
            if len(candidate_line.strip()) > 0:
                header_index = index
                break

        if header_index < 0:
            rtnval = []
            return rtnval

        header_line = lines[header_index]

        column_starts, column_names = self._compute_columns(
            header_line=header_line,
        )
        if len(column_starts) == 0:
            rtnval = []
            return rtnval

        rtnval = []
        for index in range(separator_index + 1, len(lines)):
            data_line = lines[index]
            if len(data_line.strip()) == 0:
                continue
            if self._line_is_progress_noise(line=data_line):
                continue
            row = self._slice_row(
                line=data_line,
                column_starts=column_starts,
                column_names=column_names,
            )
            if row is not None:
                rtnval.append(row)
        return rtnval

    def _line_is_dash_separator(self, *, line: str) -> bool:
        """
            Return True when every non-whitespace char on ``line`` is
            ``-`` and at least one ``-`` is present.

            :param line: The line to test.

            :returns: True when the line is a column-separator row.
        """
        seen_dash = False
        for char in line:
            if char == "-":
                seen_dash = True
                continue
            if char == " ":
                continue
            if char == "\t":
                continue
            rtnval = False
            return rtnval
        rtnval = seen_dash
        return rtnval

    def _line_is_progress_noise(self, *, line: str) -> bool:
        """
            Return True when ``line`` looks like a ``winget`` progress
            spinner or a footer row.

            ``winget`` prints rotating ``\\``, ``|``, ``/``, ``-``
            spinner glyphs at the start of progress lines, and footer
            rows like ``Found 12 entries`` after the table.  This
            method matches the spinner pattern conservatively; the
            footer pattern is left to the caller to recognise via the
            absence of column-aligned data.

            :param line: The line to test.

            :returns: True for progress noise; False otherwise.
        """
        stripped = line.strip()
        if len(stripped) == 0:
            rtnval = True
            return rtnval

        # Progress spinner lines are typically a single glyph
        # potentially followed by whitespace and a percentage marker.
        if len(stripped) <= 2:
            head = stripped[0]
            if head in ("\\", "|", "/", "-"):
                rtnval = True
                return rtnval

        rtnval = False
        return rtnval

    def _compute_columns(
        self,
        *,
        header_line: str,
    ) -> tuple:
        """
            Compute the (start_position, header_name) list from the
            header row by treating each non-whitespace token as a
            column.

            ``winget``'s separator row is one continuous ``-`` run
            with no per-column gaps, so the dashes cannot be used
            for column detection.  Instead we derive column starts
            from the header text's whitespace gaps: each
            non-whitespace token (``Name``, ``Id``, ``Version``,
            ``Source``, ``Argument``, ``Type``, ``Available``)
            starts a new column at its first character position.

            :param header_line: The line above the separator.

            :returns: A 2-tuple ``(column_starts, column_names)``.
                      ``([], [])`` when ``header_line`` is empty.
        """
        column_starts: list = []
        column_names: list = []

        index = 0
        line_len = len(header_line)
        while index < line_len:
            char = header_line[index]
            if char == " " or char == "\t":
                index += 1
                continue

            # Token start.
            token_start = index
            while index < line_len:
                next_char = header_line[index]
                if next_char == " " or next_char == "\t":
                    break
                index += 1
            token_text = header_line[token_start:index]
            column_starts.append(token_start)
            column_names.append(token_text)

        rtnval = (column_starts, column_names)
        return rtnval

    def _slice_row(
        self,
        *,
        line: str,
        column_starts: list,
        column_names: list,
    ) -> Optional[dict]:
        """
            Slice ``line`` by ``column_starts`` and return a dict
            keyed by ``column_names``.

            When ``line`` is shorter than expected (a common case for
            the trailing ``Source`` column on built-in apps), missing
            cells are recorded as empty strings.

            :param line: The data row to slice.
            :param column_starts: Column-start character offsets.
            :param column_names: Column header labels in the same
                                 order as ``column_starts``.

            :returns: The parsed dict, or ``None`` when ``line`` is
                      empty after stripping.
        """
        if len(line.strip()) == 0:
            rtnval: Optional[dict] = None
            return rtnval

        row: dict = {}
        for index in range(len(column_starts)):
            start = column_starts[index]
            if index + 1 < len(column_starts):
                end = column_starts[index + 1]
            else:
                end = len(line)
            if start >= len(line):
                cell_value = ""
            else:
                slice_end = min(end, len(line))
                cell_value = line[start:slice_end].strip()
            column_label = column_names[index]
            row[column_label] = cell_value

        rtnval = row
        return rtnval

    def _row_to_info(
        self,
        *,
        row: dict,
    ) -> Optional[WindowsPackageInfo]:
        """
            Convert a parsed table row dict into a
            :class:`WindowsPackageInfo`.

            :param row: One row from :meth:`_parse_winget_list_table`.

            :returns: The :class:`WindowsPackageInfo`, or ``None``
                      when the row lacks the minimum required fields
                      (a non-empty Name or Id).
        """
        name_value = row.get("Name", "")
        id_value = row.get("Id", "")
        version_value = row.get("Version", "")
        source_value = row.get("Source", "")

        if len(name_value) == 0 and len(id_value) == 0:
            rtnval: Optional[WindowsPackageInfo] = None
            return rtnval

        # Fall back so callers always have a meaningful identifier.
        if len(id_value) == 0:
            id_final = name_value
        else:
            id_final = id_value
        if len(name_value) == 0:
            name_final = id_value
        else:
            name_final = name_value

        if len(source_value) == 0:
            source_final: Optional[str] = None
        else:
            source_final = source_value

        rtnval = WindowsPackageInfo(
            name=name_final,
            id=id_final,
            version=version_value,
            publisher=None,
            description=None,
            homepage=None,
            source=source_final,
        )
        return rtnval

    def _parse_winget_show_text(
        self,
        *,
        stdout: str,
        requested_id: str,
    ) -> Optional[WindowsPackageInfo]:
        """
            Parse the text output of ``winget show --exact --id
            <name>`` into a :class:`WindowsPackageInfo`.

            ``winget show`` emits ``Key: Value`` lines (with the
            value possibly continuing on indented lines for long
            descriptions).  The keys are localised; this parser
            recognises the canonical English forms which is what the
            documented invariants assume.

            :param stdout: Raw stdout text.
            :param requested_id: The package id the caller asked
                                 about; used as a fallback when the
                                 ``Id`` row is omitted (some winget
                                 versions print only the display
                                 name as the first line).

            :returns: The :class:`WindowsPackageInfo`, or ``None``
                      when no usable fields could be extracted.
        """
        fields: dict = {}
        first_line: Optional[str] = None

        for raw_line in stdout.splitlines():
            line = raw_line.rstrip()
            if len(line.strip()) == 0:
                continue
            if first_line is None:
                first_line = line.strip()
                # The first non-empty line for ``winget show`` is
                # typically ``Found <Name> [<Id>]`` — we record it for
                # fallback name extraction but still try to parse it
                # as a key/value line below.
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            normalised_key = key.strip()
            normalised_value = value.strip()
            if len(normalised_key) == 0:
                continue
            fields[normalised_key] = normalised_value

        version_value = fields.get("Version", "")
        publisher_value = fields.get("Publisher", "")
        description_value = fields.get("Description", "")
        homepage_value = fields.get("Homepage", "")
        id_value = fields.get("Id", "")
        name_value = fields.get("Name", "")

        if len(id_value) == 0:
            id_final = requested_id
        else:
            id_final = id_value

        if len(name_value) == 0:
            # Try to recover the display name from the leading
            # "Found <Name> [<Id>]" line that ``winget show`` prints.
            recovered = self._recover_show_name(first_line=first_line)
            if recovered is not None:
                name_final = recovered
            else:
                name_final = id_final
        else:
            name_final = name_value

        if len(publisher_value) == 0:
            publisher_final: Optional[str] = None
        else:
            publisher_final = publisher_value

        if len(description_value) == 0:
            description_final: Optional[str] = None
        else:
            description_final = description_value

        if len(homepage_value) == 0:
            homepage_final: Optional[str] = None
        else:
            homepage_final = homepage_value

        if len(version_value) == 0 and len(name_final) == 0 and len(id_final) == 0:
            rtnval: Optional[WindowsPackageInfo] = None
            return rtnval

        rtnval = WindowsPackageInfo(
            name=name_final,
            id=id_final,
            version=version_value,
            publisher=publisher_final,
            description=description_final,
            homepage=homepage_final,
            source=None,
        )
        return rtnval

    def _recover_show_name(
        self,
        *,
        first_line: Optional[str],
    ) -> Optional[str]:
        """
            Recover the display name from a ``winget show`` leading
            line of the form ``Found <Name> [<Id>]``.

            :param first_line: The first non-empty stdout line.

            :returns: The recovered name, or ``None`` when the line
                      does not match the expected pattern.
        """
        if first_line is None:
            rtnval: Optional[str] = None
            return rtnval

        # Strip the optional ``Found `` prefix.
        candidate = first_line
        if candidate.startswith("Found "):
            candidate = candidate[len("Found "):]
        # The id is enclosed in trailing brackets when present; drop it.
        bracket_index = candidate.rfind("[")
        if bracket_index > 0:
            candidate = candidate[:bracket_index].strip()

        if len(candidate) == 0:
            rtnval = None
            return rtnval

        rtnval = candidate
        return rtnval
