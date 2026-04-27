"""
    Tests for :class:`WindowsRepositoryManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``winget`` process is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.repositorymanagement import (
    WindowsRepositoryActionStatus,
    WindowsRepositoryInfo,
    WindowsRepositoryManager,
    WindowsRepositoryManagerConfig,
    WindowsRepositoryQueryError,
    WindowsRepositorySpec,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call
        and returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
            "env": env,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


# ----------------------------------------------------------------------
# Argv builders mirroring the manager.
# ----------------------------------------------------------------------


def _source_add_argv(
    *,
    name: str,
    url: str,
    type_value: str = "Microsoft.PreIndexed.Package",
) -> list:
    """
        Build the canonical ``winget source add`` argv.
    """
    rtnval = [
        "winget",
        "source",
        "add",
        "--name",
        name,
        "--arg",
        url,
        "--type",
        type_value,
    ]
    return rtnval


def _source_remove_argv(*, name: str) -> list:
    """
        Build the canonical ``winget source remove`` argv.
    """
    rtnval = [
        "winget",
        "source",
        "remove",
        "--name",
        name,
    ]
    return rtnval


def _source_list_argv() -> list:
    """
        Build the canonical ``winget source list`` argv.
    """
    rtnval = ["winget", "source", "list"]
    return rtnval


def _source_update_argv() -> list:
    """
        Build the canonical ``winget source update`` argv.
    """
    rtnval = ["winget", "source", "update"]
    return rtnval


# ----------------------------------------------------------------------
# Output fixtures.
# ----------------------------------------------------------------------


_SOURCE_NAME_WIDTH = 12
_SOURCE_URL_WIDTH = 56
_SOURCE_LIST_HEADER = (
    "Name        Argument                                                Type"
)
_SOURCE_LIST_SEPARATOR = (
    "----------------------------------------------------------------------------"
)


def _source_list_table(rows: list) -> str:
    """
        Build a representative ``winget source list`` payload from
        ``[(name, url, type), ...]`` tuples.

        Column widths match the header so each cell aligns to the
        corresponding header token's character position.
    """
    out_lines = [_SOURCE_LIST_HEADER, _SOURCE_LIST_SEPARATOR]
    for row in rows:
        name, url, type_value = row
        out_lines.append(
            "{name:<{name_w}}{url:<{url_w}}{type_value}".format(
                name=name,
                name_w=_SOURCE_NAME_WIDTH,
                url=url,
                url_w=_SOURCE_URL_WIDTH,
                type_value=type_value,
            )
        )
    out_lines.append("")
    rtnval = "\n".join(out_lines)
    return rtnval


def _seed_source_list(
    *,
    runner: _MockCommandRunner,
    rows: list,
) -> None:
    """
        Seed ``winget source list`` to return the given rows.
    """
    payload = _source_list_table(rows=rows)
    runner.results[tuple(_source_list_argv())] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


# ----------------------------------------------------------------------
# add_repository
# ----------------------------------------------------------------------


def test_add_repository_invokes_winget_source_add():
    """
        add_repository on a missing source dispatches the canonical
        ``winget source add --name ... --arg ... --type ...`` argv.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_source_list(runner=runner, rows=[])

    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    spec = WindowsRepositorySpec(
        name="contoso",
        url="https://example.invalid/contoso",
    )
    result = manager.add_repository(spec=spec)
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.SUCCESS,
    )
    add_calls = [
        c for c in runner.calls
        if len(c["args"]) > 2 and c["args"][2] == "add"
    ]
    testbase.assert_equal(len(add_calls), 1)
    expected = _source_add_argv(
        name="contoso",
        url="https://example.invalid/contoso",
    )
    testbase.assert_equal(add_calls[0]["args"], expected)
    testbase.assert_equal(result.name, "contoso")
    testbase.assert_equal(result.url, "https://example.invalid/contoso")
    return


def test_add_repository_skipped_when_present():
    """
        add_repository returns SKIPPED when a source with the same
        name and url is already configured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_source_list(
        runner=runner,
        rows=[
            ("contoso", "https://example.invalid/contoso",
             "Microsoft.PreIndexed.Package"),
        ],
    )
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    spec = WindowsRepositorySpec(
        name="contoso",
        url="https://example.invalid/contoso",
    )
    result = manager.add_repository(spec=spec)
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.SKIPPED,
    )
    add_calls = [
        c for c in runner.calls
        if len(c["args"]) > 2 and c["args"][2] == "add"
    ]
    testbase.assert_equal(add_calls, [])
    return


def test_add_repository_proceeds_when_url_differs():
    """
        add_repository proceeds (does not skip) when an existing
        source with the same name has a different ``url``.  The
        idempotency guard is keyed on (name, url) together so a
        URL change forces a re-add.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_source_list(
        runner=runner,
        rows=[
            ("contoso", "https://example.invalid/old-url",
             "Microsoft.PreIndexed.Package"),
        ],
    )
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    spec = WindowsRepositorySpec(
        name="contoso",
        url="https://example.invalid/new-url",
    )
    result = manager.add_repository(spec=spec)
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.SUCCESS,
    )
    add_calls = [
        c for c in runner.calls
        if len(c["args"]) > 2 and c["args"][2] == "add"
    ]
    testbase.assert_equal(len(add_calls), 1)
    return


# ----------------------------------------------------------------------
# remove_repository
# ----------------------------------------------------------------------


def test_remove_repository_invokes_winget_source_remove():
    """
        remove_repository on a present source dispatches the
        canonical ``winget source remove --name <name>`` argv.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_source_list(
        runner=runner,
        rows=[
            ("contoso", "https://example.invalid/contoso",
             "Microsoft.PreIndexed.Package"),
        ],
    )
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    result = manager.remove_repository(name="contoso")
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.SUCCESS,
    )
    remove_calls = [
        c for c in runner.calls
        if len(c["args"]) > 2 and c["args"][2] == "remove"
    ]
    testbase.assert_equal(len(remove_calls), 1)
    expected = _source_remove_argv(name="contoso")
    testbase.assert_equal(remove_calls[0]["args"], expected)
    return


def test_remove_repository_skipped_when_not_present():
    """
        remove_repository returns SKIPPED when no source with the
        requested name is present.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_source_list(runner=runner, rows=[])
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    result = manager.remove_repository(name="contoso")
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.SKIPPED,
    )
    remove_calls = [
        c for c in runner.calls
        if len(c["args"]) > 2 and c["args"][2] == "remove"
    ]
    testbase.assert_equal(remove_calls, [])
    return


# ----------------------------------------------------------------------
# enable_repository / disable_repository
# ----------------------------------------------------------------------


def test_enable_repository_returns_failed_with_message():
    """
        enable_repository returns FAILED with a descriptive
        ``message`` and never invokes the runner because ``winget``
        does not support enabling sources without removal.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    result = manager.enable_repository(name="contoso")
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.FAILED,
    )
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.message,
        "winget does not support enabling/disabling sources "
        "without removal",
    )
    return


def test_disable_repository_returns_failed_with_message():
    """
        disable_repository returns FAILED with a descriptive
        ``message`` and never invokes the runner because ``winget``
        does not support disabling sources.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    result = manager.disable_repository(name="contoso")
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.FAILED,
    )
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.message,
        "winget does not support disabling sources; "
        "use remove_repository to remove",
    )
    return


# ----------------------------------------------------------------------
# list_repositories
# ----------------------------------------------------------------------


def test_list_repositories_parses_winget_source_list_text():
    """
        list_repositories parses the multi-row ``winget source list``
        text table into one :class:`WindowsRepositoryInfo` per data
        row using the column-aligned slicing strategy.
    """
    runner = _MockCommandRunner()
    _seed_source_list(
        runner=runner,
        rows=[
            ("winget", "https://cdn.winget.microsoft.com/cache",
             "Microsoft.PreIndexed.Package"),
            ("msstore", "https://storeedgefd.dsx.mp.microsoft.com/v9.0",
             "Microsoft.Rest"),
        ],
    )
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    listed = manager.list_repositories()
    testbase.assert_equal(len(listed), 2)

    first = listed[0]
    testbase.assert_equal(first.name, "winget")
    testbase.assert_equal(
        first.url, "https://cdn.winget.microsoft.com/cache",
    )
    testbase.assert_equal(first.type, "Microsoft.PreIndexed.Package")

    second = listed[1]
    testbase.assert_equal(second.name, "msstore")
    testbase.assert_equal(
        second.url, "https://storeedgefd.dsx.mp.microsoft.com/v9.0",
    )
    testbase.assert_equal(second.type, "Microsoft.Rest")
    return


def test_list_repositories_raises_on_nonzero_exit():
    """
        list_repositories raises :class:`WindowsRepositoryQueryError`
        when the runner reports a non-zero exit code.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_source_list_argv())] = RunResult(
        exit_code=3, stdout="", stderr="catastrophic",
    )
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    raised = False
    try:
        manager.list_repositories()
    except WindowsRepositoryQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_list_repositories_empty_when_no_data_rows():
    """
        list_repositories returns an empty list when ``winget
        source list`` emits a header / separator pair with no data
        rows (a freshly-reset machine).
    """
    runner = _MockCommandRunner()
    _seed_source_list(runner=runner, rows=[])
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    listed = manager.list_repositories()
    testbase.assert_equal(listed, [])
    return


# ----------------------------------------------------------------------
# refresh_repositories
# ----------------------------------------------------------------------


def test_refresh_repositories_invokes_winget_source_update():
    """
        refresh_repositories dispatches ``winget source update``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    result = manager.refresh_repositories()
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.SUCCESS,
    )
    testbase.assert_equal(len(runner.calls), 1)
    testbase.assert_equal(runner.calls[0]["args"], _source_update_argv())
    testbase.assert_equal(result.name, None)
    return


# ----------------------------------------------------------------------
# Dry-run / elevation
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_add():
    """
        Dry-run short-circuits add_repository without invoking the
        runner, including the idempotency probe.
    """
    runner = _MockCommandRunner()
    manager = WindowsRepositoryManager(
        runner=runner,
        config=WindowsRepositoryManagerConfig(dry_run=True),
    )
    spec = WindowsRepositorySpec(
        name="contoso",
        url="https://example.invalid/contoso",
    )
    result = manager.add_repository(spec=spec)
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    expected = _source_add_argv(
        name="contoso",
        url="https://example.invalid/contoso",
    )
    testbase.assert_equal(result.command, expected)
    return


def test_dry_run_short_circuits_remove():
    """
        Dry-run short-circuits remove_repository without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsRepositoryManager(
        runner=runner,
        config=WindowsRepositoryManagerConfig(dry_run=True),
    )
    result = manager.remove_repository(name="contoso")
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_write_verbs_pass_elevated_true():
    """
        Write verbs forward ``elevated=True`` to the runner when
        the config enables elevation.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_source_list(runner=runner, rows=[])
    manager = WindowsRepositoryManager(
        runner=runner,
        config=WindowsRepositoryManagerConfig(elevated=True),
    )
    spec = WindowsRepositorySpec(
        name="contoso",
        url="https://example.invalid/contoso",
    )
    manager.add_repository(spec=spec)

    add_calls = [
        c for c in runner.calls
        if len(c["args"]) > 2 and c["args"][2] == "add"
    ]
    testbase.assert_equal(len(add_calls), 1)
    testbase.assert_equal(add_calls[0]["elevated"], True)

    list_calls = [
        c for c in runner.calls
        if len(c["args"]) > 2 and c["args"][2] == "list"
    ]
    for c in list_calls:
        testbase.assert_equal(c["elevated"], False)
    return


def test_read_verbs_never_elevate():
    """
        Reads (``source list``) never pass ``elevated=True`` even
        when the config enables elevation.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_source_list(runner=runner, rows=[])
    manager = WindowsRepositoryManager(
        runner=runner,
        config=WindowsRepositoryManagerConfig(elevated=True),
    )
    manager.list_repositories()
    list_calls = [
        c for c in runner.calls
        if len(c["args"]) > 2 and c["args"][2] == "list"
    ]
    testbase.assert_equal(len(list_calls), 1)
    testbase.assert_equal(list_calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr_in_result():
    """
        A non-zero exit from the write maps to FAILED with
        exit_code, stdout, stderr and command preserved on the
        result.
    """
    runner = _MockCommandRunner()
    _seed_source_list(runner=runner, rows=[])
    write_argv = _source_add_argv(
        name="contoso",
        url="https://example.invalid/contoso",
    )
    runner.results[tuple(write_argv)] = RunResult(
        exit_code=4,
        stdout="",
        stderr="source add failed: name conflict",
    )
    manager = WindowsRepositoryManager(
        runner=runner, config=WindowsRepositoryManagerConfig(),
    )
    spec = WindowsRepositorySpec(
        name="contoso",
        url="https://example.invalid/contoso",
    )
    result = manager.add_repository(spec=spec)
    testbase.assert_equal(
        result.status, WindowsRepositoryActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 4)
    testbase.assert_equal(
        result.stderr,
        "source add failed: name conflict",
    )
    testbase.assert_equal(result.command, write_argv)
    return
