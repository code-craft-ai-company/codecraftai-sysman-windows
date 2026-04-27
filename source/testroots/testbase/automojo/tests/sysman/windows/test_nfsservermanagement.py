"""
    Tests for :class:`WindowsNfsServerManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple OR by PowerShell-script prefix.  No live
    PowerShell is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.nfsservermanagement import (
    WindowsNfsActionStatus,
    WindowsNfsClientSpec,
    WindowsNfsExportInfo,
    WindowsNfsExportSpec,
    WindowsNfsExportStatus,
    WindowsNfsManagerConfig,
    WindowsNfsQueryError,
    WindowsNfsServerManager,
    _build_new_nfs_share_script,
    _build_set_nfs_share_script,
    _options_match,
    _parse_shares_json,
)


# ----------------------------------------------------------------------
# Mock runner + helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        ``run`` call and returns pre-seeded :class:`RunResult`
        values keyed by argv tuple OR by PowerShell-script prefix.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)
    script_results: dict = field(default_factory=dict)

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
        }
        self.calls.append(call_record)

        if (
            len(args) == 5
            and isinstance(args[0], str)
            and args[0].lower().startswith("powershell")
        ):
            script = args[4]
            for key_prefix, result in self.script_results.items():
                if script.startswith(key_prefix):
                    return result

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _ok(stdout: str = "", stderr: str = "") -> RunResult:
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 1,
) -> RunResult:
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


def _seed_share_lookup_by_name(
    runner: _MockCommandRunner,
    *,
    name: str,
    json_text: str,
    exit_code: int = 0,
) -> None:
    """
        Seed ``Get-NfsShare -Name '<name>'``.
    """
    prefix = "Get-NfsShare -Name '{}' ".format(name)
    runner.script_results[prefix] = RunResult(
        exit_code=exit_code,
        stdout=json_text,
        stderr="",
    )
    return


def _seed_share_listing(
    runner: _MockCommandRunner,
    *,
    json_text: str,
    exit_code: int = 0,
) -> None:
    """
        Seed ``Get-NfsShare | ConvertTo-Json...`` (no name filter).
    """
    prefix = "Get-NfsShare | ConvertTo-Json"
    runner.script_results[prefix] = RunResult(
        exit_code=exit_code,
        stdout=json_text,
        stderr="",
    )
    return


def _make_spec(
    *,
    path: str = "D:\\Shares\\Public",
    share_name: Optional[str] = "Public",
    options: Optional[list] = None,
) -> WindowsNfsExportSpec:
    if options is None:
        options_list = ["ReadWrite", "Authentication=sys"]
    else:
        options_list = list(options)
    spec = WindowsNfsExportSpec(
        path=path,
        share_name=share_name,
        clients=[WindowsNfsClientSpec(host="*", options=options_list)],
    )
    return spec


# ----------------------------------------------------------------------
# add_export
# ----------------------------------------------------------------------


def test_add_export_invokes_powershell():
    """
        add_export dispatches the canonical New-NfsShare cmdlet
        through PowerShell.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_share_lookup_by_name(
        runner, name="Public", json_text="", exit_code=1,
    )

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)
    spec = _make_spec()

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, WindowsNfsActionStatus.SUCCESS)

    add_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and call["args"][0].lower().startswith("powershell")
            and "New-NfsShare" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(add_calls), 1)
    script = add_calls[0]["args"][4]
    testbase.assert_equal("-Name 'Public'" in script, True)
    testbase.assert_equal(
        "-Path 'D:\\Shares\\Public'" in script, True,
    )
    testbase.assert_equal("-Authentication 'sys'" in script, True)
    testbase.assert_equal(
        "-EnableUnmappedAccess $false" in script, True,
    )
    testbase.assert_equal("-Permission ReadWrite" in script, True)
    testbase.assert_equal("-AllowRootAccess $false" in script, True)
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_add_export_skipped_when_present():
    """
        add_export returns SKIPPED when Get-NfsShare reports a share
        with the same name and path.
    """
    runner = _MockCommandRunner()
    _seed_share_lookup_by_name(
        runner,
        name="Public",
        json_text=(
            '{"Name":"Public","Path":"D:\\\\Shares\\\\Public",'
            '"Authentication":"sys","Permission":"ReadWrite",'
            '"AllowRootAccess":false,"EnableUnmappedAccess":false}'
        ),
    )

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)
    spec = _make_spec()

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, WindowsNfsActionStatus.SKIPPED)

    new_share_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and call["args"][0].lower().startswith("powershell")
            and "New-NfsShare" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(new_share_calls), 0)
    return


def test_add_export_dry_run():
    """
        add_export under dry-run returns DRY_RUN and does not call
        New-NfsShare.
    """
    runner = _MockCommandRunner()
    config = WindowsNfsManagerConfig(elevated=True, dry_run=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)
    spec = _make_spec()

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, WindowsNfsActionStatus.DRY_RUN)
    new_share_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and call["args"][0].lower().startswith("powershell")
            and "New-NfsShare" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(new_share_calls), 0)
    return


# ----------------------------------------------------------------------
# share_name required
# ----------------------------------------------------------------------


def test_share_name_required_for_writes():
    """
        add_export with empty share_name returns FAILED without
        invoking the runner.
    """
    runner = _MockCommandRunner()
    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)
    spec = _make_spec(share_name=None)

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, WindowsNfsActionStatus.FAILED)
    testbase.assert_equal(
        "share_name is required" in result.message, True,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_share_name_required_for_writes_empty_string():
    """
        add_export with an empty-string share_name returns FAILED.
    """
    runner = _MockCommandRunner()
    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)
    spec = _make_spec(share_name="")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, WindowsNfsActionStatus.FAILED)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# remove_export
# ----------------------------------------------------------------------


def test_remove_export_invokes_powershell():
    """
        remove_export looks up the share by path and dispatches
        Remove-NfsShare.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_share_listing(
        runner,
        json_text=(
            '{"Name":"Public","Path":"D:\\\\Shares\\\\Public",'
            '"Authentication":"sys","Permission":"ReadWrite",'
            '"AllowRootAccess":false,"EnableUnmappedAccess":false}'
        ),
    )

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)

    result = manager.remove_export(path="D:\\Shares\\Public")

    testbase.assert_equal(result.status, WindowsNfsActionStatus.SUCCESS)

    remove_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and call["args"][0].lower().startswith("powershell")
            and "Remove-NfsShare" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(remove_calls), 1)
    script = remove_calls[0]["args"][4]
    testbase.assert_equal("-Name 'Public'" in script, True)
    testbase.assert_equal("-Force" in script, True)
    testbase.assert_equal(remove_calls[0]["elevated"], True)
    return


def test_remove_export_skipped_when_absent():
    """
        remove_export returns SKIPPED when no share advertises the
        path.
    """
    runner = _MockCommandRunner()
    _seed_share_listing(runner, json_text="")

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)

    result = manager.remove_export(path="D:\\Shares\\Missing")

    testbase.assert_equal(result.status, WindowsNfsActionStatus.SKIPPED)
    remove_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and call["args"][0].lower().startswith("powershell")
            and "Remove-NfsShare" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(remove_calls), 0)
    return


# ----------------------------------------------------------------------
# list_exports
# ----------------------------------------------------------------------


def test_list_exports_parses_json_output():
    """
        list_exports parses Get-NfsShare JSON output into
        :class:`WindowsNfsExportInfo` records.
    """
    runner = _MockCommandRunner()
    _seed_share_listing(
        runner,
        json_text=(
            "[{\"Name\":\"Public\",\"Path\":\"D:\\\\Shares\\\\Public\","
            "\"Authentication\":\"sys\",\"Permission\":\"ReadWrite\","
            "\"AllowRootAccess\":false,\"EnableUnmappedAccess\":false},"
            "{\"Name\":\"Restricted\",\"Path\":\"D:\\\\Shares\\\\Restricted\","
            "\"Authentication\":\"sys\",\"Permission\":\"ReadOnly\","
            "\"AllowRootAccess\":false,\"EnableUnmappedAccess\":false}]"
        ),
    )

    config = WindowsNfsManagerConfig()
    manager = WindowsNfsServerManager(runner=runner, config=config)

    rtnval = manager.list_exports()

    testbase.assert_equal(len(rtnval), 2)
    by_name = {entry.share_name: entry for entry in rtnval}
    testbase.assert_equal(by_name["Public"].path, "D:\\Shares\\Public")
    testbase.assert_equal(by_name["Public"].is_active, True)
    testbase.assert_equal(
        by_name["Restricted"].path, "D:\\Shares\\Restricted",
    )
    return


def test_list_exports_raises_on_failure_with_stderr():
    """
        list_exports raises :class:`WindowsNfsQueryError` when
        PowerShell exits non-zero with non-empty stderr.
    """
    runner = _MockCommandRunner()
    _seed_share_listing(
        runner,
        json_text="",
        exit_code=1,
    )
    runner.script_results["Get-NfsShare | ConvertTo-Json"] = _fail(
        stderr="CommandNotFoundException", exit_code=1,
    )

    config = WindowsNfsManagerConfig()
    manager = WindowsNfsServerManager(runner=runner, config=config)

    raised: Optional[WindowsNfsQueryError] = None
    try:
        manager.list_exports()
    except WindowsNfsQueryError as exc:
        raised = exc
    testbase.assert_equal(raised is not None, True)
    return


# ----------------------------------------------------------------------
# reload_exports
# ----------------------------------------------------------------------


def test_reload_exports_invokes_powershell():
    """
        reload_exports dispatches Restart-Service via PowerShell.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)

    result = manager.reload_exports()

    testbase.assert_equal(result.status, WindowsNfsActionStatus.SUCCESS)

    restart_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and call["args"][0].lower().startswith("powershell")
            and "Restart-Service" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(restart_calls), 1)
    testbase.assert_equal(
        "NfsService" in restart_calls[0]["args"][4], True,
    )
    testbase.assert_equal(restart_calls[0]["elevated"], True)
    return


def test_reload_exports_uses_restart_service():
    """
        The reload script invokes ``Restart-Service NfsService``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)

    result = manager.reload_exports()

    testbase.assert_equal(result.status, WindowsNfsActionStatus.SUCCESS)
    script = result.command[4]
    testbase.assert_equal(script.startswith("Restart-Service"), True)
    return


# ----------------------------------------------------------------------
# get_export_status
# ----------------------------------------------------------------------


def test_get_export_status_parses_json_output():
    """
        get_export_status parses Get-NfsShare output into a status
        record.
    """
    runner = _MockCommandRunner()
    _seed_share_listing(
        runner,
        json_text=(
            "{\"Name\":\"Public\",\"Path\":\"D:\\\\Shares\\\\Public\","
            "\"Authentication\":\"sys\",\"Permission\":\"ReadWrite\","
            "\"AllowRootAccess\":false,\"EnableUnmappedAccess\":false}"
        ),
    )

    config = WindowsNfsManagerConfig()
    manager = WindowsNfsServerManager(runner=runner, config=config)

    status = manager.get_export_status(path="D:\\Shares\\Public")

    testbase.assert_equal(status.is_active, True)
    testbase.assert_equal(status.client_count, 1)
    testbase.assert_equal("ReadWrite" in status.flags, True)
    testbase.assert_equal(
        "Authentication=sys" in status.flags, True,
    )
    return


def test_get_export_status_returns_inactive_for_missing_path():
    """
        get_export_status returns inactive for a path with no
        registered share.
    """
    runner = _MockCommandRunner()
    _seed_share_listing(runner, json_text="")

    config = WindowsNfsManagerConfig()
    manager = WindowsNfsServerManager(runner=runner, config=config)

    status = manager.get_export_status(path="D:\\Missing")

    testbase.assert_equal(status.is_active, False)
    testbase.assert_equal(status.client_count, 0)
    testbase.assert_equal(status.flags, [])
    return


# ----------------------------------------------------------------------
# set_export_options
# ----------------------------------------------------------------------


def test_set_export_options_invokes_powershell():
    """
        set_export_options dispatches Set-NfsShare with the
        translated parameters.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_share_listing(
        runner,
        json_text=(
            "{\"Name\":\"Public\",\"Path\":\"D:\\\\Shares\\\\Public\","
            "\"Authentication\":\"sys\",\"Permission\":\"ReadWrite\","
            "\"AllowRootAccess\":false,\"EnableUnmappedAccess\":false}"
        ),
    )

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)

    result = manager.set_export_options(
        path="D:\\Shares\\Public",
        options=["ReadOnly", "Authentication=krb5"],
    )

    testbase.assert_equal(result.status, WindowsNfsActionStatus.SUCCESS)

    set_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and call["args"][0].lower().startswith("powershell")
            and "Set-NfsShare" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(set_calls), 1)
    script = set_calls[0]["args"][4]
    testbase.assert_equal("-Permission ReadOnly" in script, True)
    testbase.assert_equal(
        "-Authentication 'krb5'" in script, True,
    )
    return


def test_set_export_options_skipped_when_match():
    """
        set_export_options returns SKIPPED when the existing share
        already carries the requested options.
    """
    runner = _MockCommandRunner()
    _seed_share_listing(
        runner,
        json_text=(
            "{\"Name\":\"Public\",\"Path\":\"D:\\\\Shares\\\\Public\","
            "\"Authentication\":\"sys\",\"Permission\":\"ReadWrite\","
            "\"AllowRootAccess\":false,\"EnableUnmappedAccess\":false}"
        ),
    )

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)

    result = manager.set_export_options(
        path="D:\\Shares\\Public",
        options=[
            "ReadWrite",
            "Authentication=sys",
            "NoRootAccess",
            "NoUnmappedAccess",
        ],
    )

    testbase.assert_equal(result.status, WindowsNfsActionStatus.SKIPPED)
    set_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and call["args"][0].lower().startswith("powershell")
            and "Set-NfsShare" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(set_calls), 0)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs pass elevated=True when configured.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_share_lookup_by_name(
        runner, name="Public", json_text="", exit_code=1,
    )

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)
    spec = _make_spec()
    manager.add_export(spec=spec)

    add_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and "New-NfsShare" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(add_calls), 1)
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs and idempotency probes never pass elevated=True.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_share_listing(
        runner,
        json_text=(
            "{\"Name\":\"Public\",\"Path\":\"D:\\\\Shares\\\\Public\","
            "\"Authentication\":\"sys\",\"Permission\":\"ReadWrite\","
            "\"AllowRootAccess\":false,\"EnableUnmappedAccess\":false}"
        ),
    )

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)
    manager.list_exports()
    manager.get_export_status(path="D:\\Shares\\Public")

    for call in runner.calls:
        if len(call["args"]) >= 5:
            script = call["args"][4]
            if (
                script.startswith("Get-NfsShare")
                and "New-NfsShare" not in script
                and "Remove-NfsShare" not in script
                and "Set-NfsShare" not in script
            ):
                testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing New-NfsShare cmdlet produces a FAILED result with
        the runner's stderr / exit-code / command attached.
    """
    runner = _MockCommandRunner()
    _seed_share_lookup_by_name(
        runner, name="Public", json_text="", exit_code=1,
    )
    runner.script_results["New-NfsShare"] = _fail(
        stderr="The share already exists", exit_code=1,
    )

    config = WindowsNfsManagerConfig(elevated=True)
    manager = WindowsNfsServerManager(runner=runner, config=config)
    spec = _make_spec()

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, WindowsNfsActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        "The share already exists" in result.stderr, True,
    )
    return


# ----------------------------------------------------------------------
# Renderer / parser / helper unit tests
# ----------------------------------------------------------------------


def test_build_new_nfs_share_script_carries_safe_defaults():
    """
        The default New-NfsShare scriptblock carries the canonical
        safe-default cmdlet parameters.
    """
    spec = _make_spec(
        path="D:\\Shares\\Public",
        share_name="Public",
    )
    script = _build_new_nfs_share_script(spec=spec)
    testbase.assert_equal("-Authentication 'sys'" in script, True)
    testbase.assert_equal(
        "-EnableUnmappedAccess $false" in script, True,
    )
    testbase.assert_equal("-Permission ReadWrite" in script, True)
    testbase.assert_equal("-AllowRootAccess $false" in script, True)
    return


def test_build_set_nfs_share_script_translates_known_tokens():
    """
        Recognised options translate to canonical Set-NfsShare
        parameters.
    """
    script = _build_set_nfs_share_script(
        share_name="Public",
        options=[
            "ReadOnly",
            "Authentication=krb5",
            "NoRootAccess",
            "UnmappedAccess",
        ],
    )
    testbase.assert_equal("-Name 'Public'" in script, True)
    testbase.assert_equal("-Permission ReadOnly" in script, True)
    testbase.assert_equal(
        "-Authentication 'krb5'" in script, True,
    )
    testbase.assert_equal("-AllowRootAccess $false" in script, True)
    testbase.assert_equal(
        "-EnableUnmappedAccess $true" in script, True,
    )
    return


def test_parse_shares_json_handles_single_object_and_list():
    """
        ``ConvertTo-Json`` returns a single object when there is one
        share and a list when there are multiple; the parser
        normalises both.
    """
    single = (
        "{\"Name\":\"Public\",\"Path\":\"D:\\\\Shares\\\\Public\","
        "\"Authentication\":\"sys\",\"Permission\":\"ReadWrite\"}"
    )
    listed = (
        "[" + single + "," + single + "]"
    )
    parsed_single = _parse_shares_json(text=single)
    parsed_listed = _parse_shares_json(text=listed)
    testbase.assert_equal(len(parsed_single), 1)
    testbase.assert_equal(len(parsed_listed), 2)
    testbase.assert_equal(parsed_single[0].share_name, "Public")
    return


def test_options_match_is_order_insensitive():
    """
        ``_options_match`` returns True when the sorted lists match.
    """
    testbase.assert_equal(
        _options_match(
            existing=["ReadWrite", "Authentication=sys"],
            desired=["Authentication=sys", "ReadWrite"],
        ),
        True,
    )
    testbase.assert_equal(
        _options_match(
            existing=["ReadWrite"],
            desired=["ReadOnly"],
        ),
        False,
    )
    return


# ----------------------------------------------------------------------
# Result-type sanity
# ----------------------------------------------------------------------


def test_export_info_dataclass_carries_share_name_field():
    """
        The :class:`WindowsNfsExportInfo` dataclass exposes
        ``share_name`` for cross-OS parity.
    """
    info = WindowsNfsExportInfo(
        path="D:\\Shares\\Public",
        share_name="Public",
        clients=[WindowsNfsClientSpec()],
        is_active=True,
    )
    testbase.assert_equal(info.share_name, "Public")
    return


def test_export_status_dataclass_shape():
    """
        :class:`WindowsNfsExportStatus` exposes path, is_active,
        client_count, flags.
    """
    status = WindowsNfsExportStatus(
        path="D:\\Shares\\Public",
        is_active=False,
        client_count=0,
        flags=[],
    )
    testbase.assert_equal(status.path, "D:\\Shares\\Public")
    testbase.assert_equal(status.is_active, False)
    testbase.assert_equal(status.client_count, 0)
    testbase.assert_equal(status.flags, [])
    return
