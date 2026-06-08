"""
    Windows repository-management feature.

    Implements the cross-OS repository-management contract for Windows
    by managing ``winget`` sources via ``winget source add`` /
    ``winget source remove`` / ``winget source list`` /
    ``winget source update``.  Every verb is dispatched through an
    :class:`ICommandRunner` so the manager works identically under
    local and WinRM execution.

    Elevation policy:
        Writes (``source add`` / ``source remove`` / ``source update``)
        honour ``config.elevated`` and pass it through to the runner.
        Reads (``source list``) and idempotency probes always pass
        ``elevated=False``.

    Enable / disable semantics:
        ``winget`` does NOT model "disabled-but-still-configured"
        sources.  A source is either present (and consulted) or
        removed.  :meth:`enable_repository` and
        :meth:`disable_repository` therefore return ``FAILED`` with a
        descriptive ``message`` rather than silently succeeding, so
        cross-OS callers are alerted that the requested state change
        is not expressible on Windows.  Callers wanting to stop
        consulting a source must invoke :meth:`remove_repository`.

    Output-parsing fragility:
        ``winget source list`` emits a fixed-width text table whose
        column boundaries and dash-separator row vary across
        ``winget`` versions and Windows locales (the column headers
        and the dash row are localised).  This module's parsing
        strategy mirrors the package-management table parser:

            1. Locate the dash-separator row (``-`` runs interleaved
               with whitespace).
            2. Use the column starts inferred from the dashes to
               slice each subsequent data row by character position.
            3. Skip lines that are obvious progress / footer noise.

        Callers should treat the table parser as best-effort and
        rely on :meth:`WindowsRepositoryManager.list_repositories`
        for visibility but not for byte-exact diffing.  Future
        ``winget`` versions are expected to expose stable JSON output
        for ``source list``; when that lands, the parser should be
        retired in favour of a JSON path.
"""

import os

from dataclasses import dataclass
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

_DEFAULT_SOURCE_TYPE = "Microsoft.PreIndexed.Package"


WindowsRepositoryActionStatus = ActionStatus


@dataclass
class WindowsRepositoryManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsRepositoryManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param winget_executable: Path to the ``winget`` executable
                                  used to dispatch verbs.  Defaults to
                                  ``"winget"`` (PATH-resolved).  Set to
                                  an absolute path when the runner does
                                  not have a usable ``PATH`` (e.g. some
                                  WinRM runspaces).
    """
    winget_executable: str = _DEFAULT_WINGET


@dataclass
class WindowsRepositoryActionResult(FeatureActionResult):
    """
        Result of a repository-management write operation.

        :param name: The source name targeted, or ``None`` for
                     :meth:`refresh_repositories`.
        :param url: The source ``--arg`` URL when applicable.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes (e.g. "winget does not
                        support disabling sources").
    """
    name: Optional[str] = None
    url: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsRepositorySpec:
    """
        Specification for a ``winget`` source.

        :param name: The source name (the ``--name`` argument to
                     ``winget source add``).  Used as both the
                     uniqueness key and the result echo.
        :param url: The source URL (the ``--arg`` argument to
                    ``winget source add``).
        :param type: The source type (the ``--type`` argument).
                     Defaults to
                     ``"Microsoft.PreIndexed.Package"``, which is
                     what the canonical ``winget`` community source
                     uses.  ``"Microsoft.Rest"`` is the other
                     supported value at the time of writing.
    """
    name: str
    url: str
    type: str = _DEFAULT_SOURCE_TYPE


@dataclass
class WindowsRepositoryInfo:
    """
        Summary of one ``winget`` source as enumerated by
        :meth:`WindowsRepositoryManager.list_repositories`.

        :param name: Source name.
        :param url: Source URL (the ``Argument`` column from ``winget
                    source list``).  Empty string when the column is
                    blank.
        :param type: Source type (the ``Type`` column).  Empty
                     string when the column is blank.
    """
    name: str
    url: str
    type: str


class WindowsRepositoryQueryError(Exception):
    """
        Raised when a repository read fails for a reason other than
        the target simply being absent.
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

        err_msg_lines = ["Repository query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsRepositoryManager(BaseFeatureManager):
    """
        Windows ``winget`` source manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`.

        Writes (``source add`` / ``source remove`` / ``source
        update``) honour ``config.elevated`` and pass it through to
        the runner.  Reads (``source list``) always pass
        ``elevated=False``.
    """

    result_class = WindowsRepositoryActionResult
    status_class = WindowsRepositoryActionStatus
    feature_name = "repository-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_repository(
        self,
        spec: WindowsRepositorySpec,
    ) -> WindowsRepositoryActionResult:
        """
            Add a ``winget`` source via ``winget source add``.

            Skipped when a source with the same ``name`` and ``url``
            is already present.

            :param spec: The :class:`WindowsRepositorySpec` to add.

            :returns: The :class:`WindowsRepositoryActionResult`.
        """
        command = self._build_source_add_argv(spec=spec)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsRepositoryActionStatus.DRY_RUN,
                name=spec.name,
                url=spec.url,
                command=command,
            )
            return dry

        existing = self._find_existing_source(name=spec.name)
        if existing is not None and existing.url == spec.url:
            skipped = self._build_result(
                status=WindowsRepositoryActionStatus.SKIPPED,
                name=spec.name,
                url=spec.url,
                command=command,
                message="repository already exists with matching url",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=spec.name,
            url=spec.url,
            operation="add_repository",
        )
        return rtnval

    def remove_repository(
        self,
        name: str,
    ) -> WindowsRepositoryActionResult:
        """
            Remove a ``winget`` source via ``winget source remove``.

            Skipped when no source with the requested ``name`` is
            present.

            :param name: The source name.

            :returns: The :class:`WindowsRepositoryActionResult`.
        """
        command = self._build_source_remove_argv(name=name)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsRepositoryActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        existing = self._find_existing_source(name=name)
        if existing is None:
            skipped = self._build_result(
                status=WindowsRepositoryActionStatus.SKIPPED,
                name=name,
                command=command,
                message="repository is not present",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            url=existing.url,
            operation="remove_repository",
        )
        return rtnval

    def enable_repository(
        self,
        name: str,
    ) -> WindowsRepositoryActionResult:
        """
            Enable a ``winget`` source.

            ``winget`` does not support enabling/disabling sources
            without removing them: a source is either configured
            (and always consulted) or absent.  This method therefore
            returns ``FAILED`` with a descriptive ``message`` and
            does NOT invoke the runner.  Honours dry-run.

            :param name: The source name.

            :returns: The :class:`WindowsRepositoryActionResult`.
        """
        # Synthesise a placeholder argv so the result still records
        # what the caller's intent was; the runner is never invoked.
        command = self._build_no_op_argv(action="enable", name=name)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsRepositoryActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        message = (
            "winget does not support enabling/disabling sources "
            "without removal"
        )
        rtnval = self._build_result(
            status=WindowsRepositoryActionStatus.FAILED,
            name=name,
            command=command,
            message=message,
            stderr=message,
        )
        return rtnval

    def disable_repository(
        self,
        name: str,
    ) -> WindowsRepositoryActionResult:
        """
            Disable a ``winget`` source.

            ``winget`` does not support disabling sources: the only
            way to stop consulting a source is :meth:`remove_repository`.
            This method therefore returns ``FAILED`` with a
            descriptive ``message`` and does NOT invoke the runner.
            Honours dry-run.

            :param name: The source name.

            :returns: The :class:`WindowsRepositoryActionResult`.
        """
        command = self._build_no_op_argv(action="disable", name=name)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsRepositoryActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        message = (
            "winget does not support disabling sources; "
            "use remove_repository to remove"
        )
        rtnval = self._build_result(
            status=WindowsRepositoryActionStatus.FAILED,
            name=name,
            command=command,
            message=message,
            stderr=message,
        )
        return rtnval

    def refresh_repositories(self) -> WindowsRepositoryActionResult:
        """
            Refresh ``winget`` source metadata via ``winget source
            update``.  Never skips.

            :returns: The :class:`WindowsRepositoryActionResult`.
        """
        command = self._build_source_update_argv()
        rtnval = self._dispatch_write(
            command=command,
            name=None,
            operation="refresh_repositories",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_repositories(self) -> list:
        """
            Enumerate every configured ``winget`` source via ``winget
            source list``.

            Reads never elevate.

            :returns: One :class:`WindowsRepositoryInfo` per parsed
                      row.
            :raises WindowsRepositoryQueryError: When the runner
                                                 reports a non-zero
                                                 exit code.
        """
        command = self._build_source_list_argv()
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise WindowsRepositoryQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rows = self._parse_winget_source_list_table(
            stdout=run_result.stdout,
        )

        rtnval: list = []
        for row in rows:
            info = self._row_to_info(row=row)
            if info is not None:
                rtnval.append(info)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsRepositoryManagerConfig:
        """
            Return the manager's config typed as
            :class:`WindowsRepositoryManagerConfig`.

            :returns: The active config typed as
                      :class:`WindowsRepositoryManagerConfig`.
        """
        if isinstance(self._config, WindowsRepositoryManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsRepositoryManagerConfig(
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

    def _build_source_add_argv(
        self,
        *,
        spec: WindowsRepositorySpec,
    ) -> list:
        """
            Build the full argv for ``winget source add --name ... --arg
            ... [--type ...]``.

            The ``--type`` flag is included whenever ``spec.type`` is
            non-empty, including the documented default
            ``"Microsoft.PreIndexed.Package"`` so the recorded argv
            reflects exactly what was dispatched.

            :param spec: The :class:`WindowsRepositorySpec`.

            :returns: The full argv.
        """
        argv: list = [
            self._winget_executable(),
            "source",
            "add",
            "--name",
            spec.name,
            "--arg",
            spec.url,
        ]
        if spec.type is not None and len(spec.type) > 0:
            argv.append("--type")
            argv.append(spec.type)

        rtnval = argv
        return rtnval

    def _build_source_remove_argv(self, *, name: str) -> list:
        """
            Build the full argv for ``winget source remove --name <name>``.

            :param name: The source name.

            :returns: The full argv.
        """
        rtnval = [
            self._winget_executable(),
            "source",
            "remove",
            "--name",
            name,
        ]
        return rtnval

    def _build_source_list_argv(self) -> list:
        """
            Build the full argv for ``winget source list``.

            :returns: The full argv.
        """
        rtnval = [
            self._winget_executable(),
            "source",
            "list",
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

    def _build_no_op_argv(
        self,
        *,
        action: str,
        name: str,
    ) -> list:
        """
            Build a placeholder argv recorded onto the result for
            verbs that ``winget`` does not support
            (:meth:`enable_repository` /
            :meth:`disable_repository`).

            The argv is never dispatched; it is captured so audit /
            dry-run tooling can see what the caller asked for.

            :param action: The intent (``"enable"`` / ``"disable"``).
            :param name: The source name.

            :returns: A synthetic argv.
        """
        rtnval = [
            self._winget_executable(),
            "source",
            "# {} not supported".format(action),
            "--name",
            name,
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
        url: Optional[str] = None,
        operation: str,
    ) -> WindowsRepositoryActionResult:
        """
            Dispatch a write command, honouring ``config.elevated``,
            and short-circuit on dry-run via the base
            :meth:`_dispatch`.

            :param command: The full argv.
            :param name: The source name to echo onto the result.
            :param url: The source URL to echo onto the result.
            :param operation: Reserved for future auto-reporting.

            :returns: The :class:`WindowsRepositoryActionResult`.
        """
        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation=operation,
            elevated_override=None,
            name=name,
            url=url,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsRepositoryActionStatus,
        name: Optional[str],
        command: list,
        url: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsRepositoryActionResult:
        """
            Construct a :class:`WindowsRepositoryActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param name: The source name to echo onto the result.
            :param command: The would-have-been argv, copied for capture.
            :param url: The source URL to echo onto the result.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`WindowsRepositoryActionResult`.
        """
        rtnval = WindowsRepositoryActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            url=url,
            message=message,
            stderr=stderr,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — idempotency probes
    # ------------------------------------------------------------------

    def _find_existing_source(
        self,
        *,
        name: str,
    ) -> Optional[WindowsRepositoryInfo]:
        """
            Return the :class:`WindowsRepositoryInfo` for ``name`` from
            the current ``winget source list`` output, or ``None`` when
            the source is absent.

            On read failure this helper returns ``None`` rather than
            raising, so the idempotency guard never short-circuits an
            otherwise legitimate write.  The hard-error path is owned
            by :meth:`list_repositories`.

            :param name: The source name.

            :returns: The matching :class:`WindowsRepositoryInfo`, or
                      ``None``.
        """
        command = self._build_source_list_argv()
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval: Optional[WindowsRepositoryInfo] = None
            return rtnval

        rows = self._parse_winget_source_list_table(
            stdout=run_result.stdout,
        )
        for row in rows:
            row_name = row.get("Name", "")
            if row_name == name:
                rtnval = self._row_to_info(row=row)
                return rtnval

        rtnval = None
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — output parsing
    # ------------------------------------------------------------------

    def _parse_winget_source_list_table(
        self,
        *,
        stdout: str,
    ) -> list:
        """
            Parse a fixed-width ``winget source list`` table into a
            list of column->value dicts.

            Strategy mirrors :meth:`WindowsPackageManager
            ._parse_winget_list_table`: locate the dash-separator
            row to anchor where the data rows begin, derive column
            starts from the header row's whitespace gaps (the
            separator is one continuous dash run), and slice each
            subsequent data row by character position.

            :param stdout: Raw stdout text.

            :returns: A list of dicts keyed by column header.  Empty
                      list when no dash-separator row is found.
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
            Return True when every non-whitespace character on
            ``line`` is ``-`` and at least one ``-`` is present.

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

    def _compute_columns(
        self,
        *,
        header_line: str,
    ) -> tuple:
        """
            Compute the ``(column_starts, column_names)`` pair from
            the header row's whitespace gaps.

            ``winget``'s separator row is one continuous ``-`` run,
            so the dashes cannot be used for column detection.
            Instead each non-whitespace token in the header
            (``Name``, ``Argument``, ``Type``, ...) is treated as
            the start of a new column at its first character
            position.

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
    ) -> Optional[WindowsRepositoryInfo]:
        """
            Convert a parsed table row dict into a
            :class:`WindowsRepositoryInfo`.

            :param row: One row from
                       :meth:`_parse_winget_source_list_table`.

            :returns: The :class:`WindowsRepositoryInfo`, or ``None``
                      when the row lacks a non-empty ``Name``.
        """
        name_value = row.get("Name", "")
        url_value = row.get("Argument", "")
        type_value = row.get("Type", "")

        if len(name_value) == 0:
            rtnval: Optional[WindowsRepositoryInfo] = None
            return rtnval

        rtnval = WindowsRepositoryInfo(
            name=name_value,
            url=url_value,
            type=type_value,
        )
        return rtnval
