"""
    Tests for :class:`WindowsPackageManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``winget`` process is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.packagemanagement import (
    WindowsPackageActionStatus,
    WindowsPackageInfo,
    WindowsPackageManager,
    WindowsPackageManagerConfig,
    WindowsPackageQueryError,
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


def _install_argv(name: str, version: Optional[str] = None) -> list:
    """
        Build the canonical ``winget install`` argv the manager
        produces for ``name`` / ``version``.
    """
    argv = [
        "winget",
        "install",
        "--exact",
        "--id",
        name,
    ]
    if version is not None:
        argv.append("--version")
        argv.append(version)
    argv.append("--silent")
    argv.append("--accept-package-agreements")
    argv.append("--accept-source-agreements")
    rtnval = argv
    return rtnval


def _uninstall_argv(name: str) -> list:
    """
        Build the canonical ``winget uninstall`` argv.
    """
    rtnval = [
        "winget",
        "uninstall",
        "--exact",
        "--id",
        name,
        "--silent",
    ]
    return rtnval


def _upgrade_argv(name: str) -> list:
    """
        Build the canonical ``winget upgrade <name>`` argv.
    """
    rtnval = [
        "winget",
        "upgrade",
        "--exact",
        "--id",
        name,
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]
    return rtnval


def _upgrade_all_argv() -> list:
    """
        Build the canonical ``winget upgrade --all`` argv.
    """
    rtnval = [
        "winget",
        "upgrade",
        "--all",
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]
    return rtnval


def _source_update_argv() -> list:
    """
        Build the canonical ``winget source update`` argv.
    """
    rtnval = ["winget", "source", "update"]
    return rtnval


def _list_one_argv(name: str) -> list:
    """
        Build the canonical ``winget list --exact --id <name>`` argv.
    """
    rtnval = ["winget", "list", "--exact", "--id", name]
    return rtnval


def _list_all_argv() -> list:
    """
        Build the canonical ``winget list`` argv.
    """
    rtnval = ["winget", "list"]
    return rtnval


def _show_argv(name: str) -> list:
    """
        Build the canonical ``winget show --exact --id <name>`` argv.
    """
    rtnval = ["winget", "show", "--exact", "--id", name]
    return rtnval


def _upgrade_probe_argv() -> list:
    """
        Build the canonical ``winget upgrade --include-unknown`` argv.
    """
    rtnval = ["winget", "upgrade", "--include-unknown"]
    return rtnval


# ----------------------------------------------------------------------
# Output fixtures.
# ----------------------------------------------------------------------


_LIST_NOT_INSTALLED = (
    "No installed package found matching input criteria.\n"
)


_LIST_NAME_WIDTH = 18
_LIST_ID_WIDTH = 28
_LIST_VERSION_WIDTH = 14
_LIST_HEADER = (
    "Name              Id                          Version       Source"
)
_LIST_SEPARATOR = (
    "------------------------------------------------------------------"
)


def _list_one_table(
    *,
    name: str,
    display_name: str,
    version: str,
    source: str = "winget",
) -> str:
    """
        Build a representative ``winget list --exact --id`` payload
        with the canonical fixed-width header / separator / one data
        row.

        Column widths match the header so each cell, when slotted in
        with left-justified padding, lines up with the corresponding
        header token's character position.
    """
    row = "{display:<{name_w}}{id_value:<{id_w}}{version:<{ver_w}}{source}".format(
        display=display_name,
        name_w=_LIST_NAME_WIDTH,
        id_value=name,
        id_w=_LIST_ID_WIDTH,
        version=version,
        ver_w=_LIST_VERSION_WIDTH,
        source=source,
    )
    rtnval = "\n".join([_LIST_HEADER, _LIST_SEPARATOR, row, ""])
    return rtnval


def _list_table_multi(rows: list) -> str:
    """
        Build a multi-row ``winget list`` payload from
        ``[(display_name, id, version, source), ...]`` tuples.
    """
    out_lines = [_LIST_HEADER, _LIST_SEPARATOR]
    for row in rows:
        display, package_id, version, source = row
        out_lines.append(
            "{display:<{name_w}}{id_value:<{id_w}}{version:<{ver_w}}{source}".format(
                display=display,
                name_w=_LIST_NAME_WIDTH,
                id_value=package_id,
                id_w=_LIST_ID_WIDTH,
                version=version,
                ver_w=_LIST_VERSION_WIDTH,
                source=source,
            )
        )
    out_lines.append("")
    rtnval = "\n".join(out_lines)
    return rtnval


def _show_text(
    *,
    name: str,
    display_name: str,
    version: str,
    publisher: str,
    description: str,
    homepage: str,
) -> str:
    """
        Build a representative ``winget show`` text payload.
    """
    out_lines = [
        "Found {} [{}]".format(display_name, name),
        "Version: {}".format(version),
        "Publisher: {}".format(publisher),
        "Description: {}".format(description),
        "Homepage: {}".format(homepage),
    ]
    rtnval = "\n".join(out_lines) + "\n"
    return rtnval


def _seed_installed(
    *,
    runner: _MockCommandRunner,
    name: str,
    display_name: str = "PowerShell",
    version: str = "7.4.0",
    source: str = "winget",
) -> None:
    """
        Seed both the ``--exact --id`` probe and the table listing
        so idempotency probes find ``name`` installed.
    """
    payload = _list_one_table(
        name=name,
        display_name=display_name,
        version=version,
        source=source,
    )
    runner.results[tuple(_list_one_argv(name=name))] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _seed_not_installed(
    *,
    runner: _MockCommandRunner,
    name: str,
) -> None:
    """
        Seed the ``--exact --id`` probe to return the canonical
        "no installed package" sentinel.
    """
    runner.results[tuple(_list_one_argv(name=name))] = RunResult(
        exit_code=0, stdout=_LIST_NOT_INSTALLED, stderr="",
    )
    return


def _seed_upgrade_available(
    *,
    runner: _MockCommandRunner,
    name: str,
    display_name: str = "PowerShell",
    installed: str = "7.4.0",
) -> None:
    """
        Seed ``winget upgrade --include-unknown`` to list ``name``
        as having an available upgrade.
    """
    payload = _list_one_table(
        name=name,
        display_name=display_name,
        version=installed,
        source="winget",
    )
    runner.results[tuple(_upgrade_probe_argv())] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _seed_no_upgrades(
    *,
    runner: _MockCommandRunner,
) -> None:
    """
        Seed ``winget upgrade --include-unknown`` to return an empty
        upgrades table (header + separator only).
    """
    payload = (
        "Name              Id                          "
        "Version       Available     Source\n"
        "----------------------------------------------"
        "----------------------------------\n"
    )
    runner.results[tuple(_upgrade_probe_argv())] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


# ----------------------------------------------------------------------
# install_package
# ----------------------------------------------------------------------


def test_install_package_invokes_winget_install():
    """
        install_package on a missing package dispatches the canonical
        ``winget install --exact --id ...`` argv with the silent /
        accept-* flags.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_not_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.install_package(name="Microsoft.PowerShell")
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SUCCESS)
    install_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "install"
    ]
    testbase.assert_equal(len(install_calls), 1)
    testbase.assert_equal(
        install_calls[0]["args"],
        _install_argv(name="Microsoft.PowerShell"),
    )
    testbase.assert_equal(result.name, "Microsoft.PowerShell")
    return


def test_install_package_skipped_when_installed():
    """
        install_package with no version returns SKIPPED when the
        package is already installed at any version.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_installed(
        runner=runner,
        name="Microsoft.PowerShell",
        version="7.4.0",
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.install_package(name="Microsoft.PowerShell")
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SKIPPED)
    install_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "install"
    ]
    testbase.assert_equal(install_calls, [])
    return


def test_install_package_with_version_passes_version_flag():
    """
        install_package with a version argument injects ``--version
        <ver>`` into the argv between ``--id <name>`` and the silent
        flags.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_not_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    manager.install_package(name="Microsoft.PowerShell", version="7.3.0")
    install_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "install"
    ]
    testbase.assert_equal(len(install_calls), 1)
    expected = _install_argv(name="Microsoft.PowerShell", version="7.3.0")
    testbase.assert_equal(install_calls[0]["args"], expected)
    return


def test_install_package_skipped_when_version_matches():
    """
        install_package with a version argument returns SKIPPED when
        the installed version exactly matches the requested version.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_installed(
        runner=runner,
        name="Microsoft.PowerShell",
        version="7.4.0",
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.install_package(
        name="Microsoft.PowerShell", version="7.4.0",
    )
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SKIPPED)
    return


def test_install_package_with_different_version_proceeds():
    """
        install_package with a version argument that differs from the
        currently-installed version dispatches the install.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_installed(
        runner=runner,
        name="Microsoft.PowerShell",
        version="7.4.0",
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.install_package(
        name="Microsoft.PowerShell", version="7.3.0",
    )
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SUCCESS)
    install_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "install"
    ]
    testbase.assert_equal(len(install_calls), 1)
    return


# ----------------------------------------------------------------------
# remove_package
# ----------------------------------------------------------------------


def test_remove_package_invokes_winget_uninstall():
    """
        remove_package on an installed package dispatches the
        canonical ``winget uninstall --exact --id ...`` argv.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.remove_package(name="Microsoft.PowerShell")
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SUCCESS)
    uninstall_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "uninstall"
    ]
    testbase.assert_equal(len(uninstall_calls), 1)
    testbase.assert_equal(
        uninstall_calls[0]["args"],
        _uninstall_argv(name="Microsoft.PowerShell"),
    )
    return


def test_remove_package_skipped_when_not_installed():
    """
        remove_package returns SKIPPED when the package is absent.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_not_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.remove_package(name="Microsoft.PowerShell")
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SKIPPED)
    uninstall_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "uninstall"
    ]
    testbase.assert_equal(uninstall_calls, [])
    return


# ----------------------------------------------------------------------
# upgrade_package / upgrade_all
# ----------------------------------------------------------------------


def test_upgrade_package_invokes_winget_upgrade():
    """
        upgrade_package on an installed package with an available
        upgrade dispatches the canonical ``winget upgrade --exact
        --id ...`` argv.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_installed(runner=runner, name="Microsoft.PowerShell")
    _seed_upgrade_available(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.upgrade_package(name="Microsoft.PowerShell")
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SUCCESS)
    upgrade_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) > 1
            and c["args"][1] == "upgrade"
            and "--all" not in c["args"]
            and "--include-unknown" not in c["args"]
        )
    ]
    testbase.assert_equal(len(upgrade_calls), 1)
    testbase.assert_equal(
        upgrade_calls[0]["args"],
        _upgrade_argv(name="Microsoft.PowerShell"),
    )
    return


def test_upgrade_package_skipped_when_no_upgrade_available():
    """
        upgrade_package returns SKIPPED when ``winget upgrade
        --include-unknown`` does not list the package as having an
        available upgrade.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_installed(runner=runner, name="Microsoft.PowerShell")
    _seed_no_upgrades(runner=runner)
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.upgrade_package(name="Microsoft.PowerShell")
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SKIPPED)
    upgrade_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) > 1
            and c["args"][1] == "upgrade"
            and "--all" not in c["args"]
            and "--include-unknown" not in c["args"]
        )
    ]
    testbase.assert_equal(upgrade_calls, [])
    return


def test_upgrade_package_skipped_when_not_installed():
    """
        upgrade_package returns SKIPPED with an explicit "not
        installed" message when the package is not installed.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_not_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.upgrade_package(name="Microsoft.PowerShell")
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SKIPPED)
    testbase.assert_equal(
        result.message,
        "package is not installed; nothing to upgrade",
    )
    return


def test_upgrade_all_invokes_winget_upgrade_all():
    """
        upgrade_all dispatches ``winget upgrade --all`` with the
        silent / accept-* flags.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.upgrade_all()
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    testbase.assert_equal(runner.calls[0]["args"], _upgrade_all_argv())
    testbase.assert_equal(result.name, None)
    return


# ----------------------------------------------------------------------
# is_installed
# ----------------------------------------------------------------------


def test_is_installed_returns_true_for_installed():
    """
        is_installed returns True when ``winget list`` exits 0 with
        a row matching the requested id.
    """
    runner = _MockCommandRunner()
    _seed_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    rtnval = manager.is_installed(name="Microsoft.PowerShell")
    testbase.assert_equal(rtnval, True)
    return


def test_is_installed_returns_false_for_not_installed():
    """
        is_installed returns False when stdout matches the canonical
        "no installed package" sentinel.
    """
    runner = _MockCommandRunner()
    _seed_not_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    rtnval = manager.is_installed(name="Microsoft.PowerShell")
    testbase.assert_equal(rtnval, False)
    return


def test_is_installed_returns_false_on_nonzero_exit():
    """
        is_installed returns False when ``winget list`` exits
        non-zero, regardless of stdout content.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_list_one_argv(name="Missing.Pkg"))] = RunResult(
        exit_code=1, stdout="", stderr="some failure",
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    rtnval = manager.is_installed(name="Missing.Pkg")
    testbase.assert_equal(rtnval, False)
    return


# ----------------------------------------------------------------------
# get_package_info / list_installed
# ----------------------------------------------------------------------


def test_get_package_info_parses_winget_show_text():
    """
        get_package_info parses the ``Key: Value`` lines from
        ``winget show`` into a :class:`WindowsPackageInfo`.
    """
    runner = _MockCommandRunner()
    payload = _show_text(
        name="Microsoft.PowerShell",
        display_name="PowerShell",
        version="7.4.0",
        publisher="Microsoft Corporation",
        description="A cross-platform shell.",
        homepage="https://example.invalid/powershell",
    )
    runner.results[tuple(_show_argv(name="Microsoft.PowerShell"))] = (
        RunResult(exit_code=0, stdout=payload, stderr="")
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    info = manager.get_package_info(name="Microsoft.PowerShell")
    testbase.assert_true(info is not None)
    testbase.assert_equal(info.id, "Microsoft.PowerShell")
    testbase.assert_equal(info.name, "PowerShell")
    testbase.assert_equal(info.version, "7.4.0")
    testbase.assert_equal(info.publisher, "Microsoft Corporation")
    testbase.assert_equal(info.description, "A cross-platform shell.")
    testbase.assert_equal(info.homepage, "https://example.invalid/powershell")
    return


def test_get_package_info_returns_none_for_missing():
    """
        get_package_info returns None when ``winget show`` reports
        the canonical "No package found" sentinel.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_show_argv(name="Nope"))] = RunResult(
        exit_code=1,
        stdout="",
        stderr="No package found matching input criteria.",
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    info = manager.get_package_info(name="Nope")
    testbase.assert_equal(info, None)
    return


def test_get_package_info_raises_on_unexpected_failure():
    """
        get_package_info raises :class:`WindowsPackageQueryError`
        when ``winget show`` exits non-zero with an unrecognised
        stderr.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_show_argv(name="Boom"))] = RunResult(
        exit_code=2,
        stdout="",
        stderr="some unrelated catastrophic failure",
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    raised = False
    try:
        manager.get_package_info(name="Boom")
    except WindowsPackageQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_list_installed_parses_winget_list_text_table():
    """
        list_installed parses the multi-row ``winget list`` text
        table into one :class:`WindowsPackageInfo` per data row,
        respecting the column-aligned slicing strategy.
    """
    runner = _MockCommandRunner()
    payload = _list_table_multi(
        rows=[
            ("PowerShell", "Microsoft.PowerShell", "7.4.0", "winget"),
            ("Visual Studio", "Microsoft.VisualStudio", "17.8.0", "winget"),
            ("VLC", "VideoLAN.VLC", "3.0.20", "winget"),
        ],
    )
    runner.results[tuple(_list_all_argv())] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    listed = manager.list_installed()
    testbase.assert_equal(len(listed), 3)

    first = listed[0]
    testbase.assert_equal(first.name, "PowerShell")
    testbase.assert_equal(first.id, "Microsoft.PowerShell")
    testbase.assert_equal(first.version, "7.4.0")
    testbase.assert_equal(first.source, "winget")

    third = listed[2]
    testbase.assert_equal(third.name, "VLC")
    testbase.assert_equal(third.id, "VideoLAN.VLC")
    testbase.assert_equal(third.version, "3.0.20")
    return


def test_list_installed_raises_on_nonzero_exit():
    """
        list_installed raises :class:`WindowsPackageQueryError` when
        the runner reports a non-zero exit code.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_list_all_argv())] = RunResult(
        exit_code=3, stdout="", stderr="catastrophic",
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    raised = False
    try:
        manager.list_installed()
    except WindowsPackageQueryError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# refresh_metadata
# ----------------------------------------------------------------------


def test_refresh_metadata_invokes_winget_source_update():
    """
        refresh_metadata dispatches ``winget source update``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.refresh_metadata()
    testbase.assert_equal(result.status, WindowsPackageActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    testbase.assert_equal(runner.calls[0]["args"], _source_update_argv())
    return


# ----------------------------------------------------------------------
# Dry-run / elevation
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_install():
    """
        Dry-run short-circuits install_package without invoking the
        runner, including the idempotency probe.
    """
    runner = _MockCommandRunner()
    manager = WindowsPackageManager(
        runner=runner,
        config=WindowsPackageManagerConfig(dry_run=True),
    )
    result = manager.install_package(name="Microsoft.PowerShell")
    testbase.assert_equal(result.status, WindowsPackageActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        _install_argv(name="Microsoft.PowerShell"),
    )
    return


def test_dry_run_short_circuits_remove():
    """
        Dry-run short-circuits remove_package without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsPackageManager(
        runner=runner,
        config=WindowsPackageManagerConfig(dry_run=True),
    )
    result = manager.remove_package(name="Microsoft.PowerShell")
    testbase.assert_equal(result.status, WindowsPackageActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_write_verbs_pass_elevated_true():
    """
        Write verbs forward ``elevated=True`` to the runner when the
        config enables elevation.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_not_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner,
        config=WindowsPackageManagerConfig(elevated=True),
    )
    manager.install_package(name="Microsoft.PowerShell")
    install_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "install"
    ]
    testbase.assert_equal(len(install_calls), 1)
    testbase.assert_equal(install_calls[0]["elevated"], True)
    return


def test_read_verbs_never_elevate():
    """
        Reads and idempotency-probe commands never pass
        ``elevated=True`` regardless of the config.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner,
        config=WindowsPackageManagerConfig(elevated=True),
    )
    # Direct read.
    manager.is_installed(name="Microsoft.PowerShell")
    # Write whose idempotency probe must also not elevate.
    manager.remove_package(name="Microsoft.PowerShell")

    list_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "list"
    ]
    for c in list_calls:
        testbase.assert_equal(c["elevated"], False)

    uninstall_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "uninstall"
    ]
    testbase.assert_equal(len(uninstall_calls), 1)
    testbase.assert_equal(uninstall_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Failure surfacing / argv flag completeness
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr_in_result():
    """
        A non-zero exit from the write maps to FAILED with
        exit_code, stdout, stderr and command preserved on the
        result.
    """
    runner = _MockCommandRunner()
    _seed_not_installed(runner=runner, name="Microsoft.PowerShell")
    write_argv = _install_argv(name="Microsoft.PowerShell")
    runner.results[tuple(write_argv)] = RunResult(
        exit_code=5,
        stdout="some progress",
        stderr="install failed: package signed by untrusted key",
    )
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    result = manager.install_package(name="Microsoft.PowerShell")
    testbase.assert_equal(result.status, WindowsPackageActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 5)
    testbase.assert_equal(
        result.stderr,
        "install failed: package signed by untrusted key",
    )
    testbase.assert_equal(result.command, write_argv)
    return


def test_silent_and_accept_flags_present_on_install():
    """
        Every install argv carries ``--silent``,
        ``--accept-package-agreements``, and
        ``--accept-source-agreements`` so ``winget`` never blocks on
        agreement prompts.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_not_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner, config=WindowsPackageManagerConfig(),
    )
    manager.install_package(name="Microsoft.PowerShell")
    install_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "install"
    ]
    testbase.assert_equal(len(install_calls), 1)
    argv = install_calls[0]["args"]
    testbase.assert_true("--silent" in argv)
    testbase.assert_true("--accept-package-agreements" in argv)
    testbase.assert_true("--accept-source-agreements" in argv)
    testbase.assert_true("--exact" in argv)
    testbase.assert_true("--id" in argv)
    return


def test_extra_install_args_appended_to_install_argv():
    """
        ``WindowsPackageManagerConfig.extra_install_args`` are
        appended to the install argv after the silent / accept-*
        flags, so callers can opt into ``--scope user`` without
        needing to subclass.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_not_installed(runner=runner, name="Microsoft.PowerShell")
    manager = WindowsPackageManager(
        runner=runner,
        config=WindowsPackageManagerConfig(
            extra_install_args=["--scope", "user"],
        ),
    )
    manager.install_package(name="Microsoft.PowerShell")
    install_calls = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][1] == "install"
    ]
    testbase.assert_equal(len(install_calls), 1)
    argv = install_calls[0]["args"]
    testbase.assert_true("--scope" in argv)
    testbase.assert_true("user" in argv)
    return
