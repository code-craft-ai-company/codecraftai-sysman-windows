"""
    Tests for :class:`WindowsBootloaderManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv.  No live ``bcdedit`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.bootloadermanagement import (
    WindowsBootEntrySpec,
    WindowsBootloaderActionStatus,
    WindowsBootloaderManager,
    WindowsBootloaderManagerConfig,
    WindowsBootloaderQueryError,
)


_BCDEDIT = "bcdedit.exe"
_DEFAULT_GUID = "{a1b2c3d4-1111-2222-3333-444455556666}"
_OTHER_GUID = "{99887766-5544-3322-1100-aabbccddeeff}"


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
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


def _ok(stdout: str = "") -> RunResult:
    """
        Build an exit-code-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=0,
        stdout=stdout,
        stderr="",
    )
    return rtnval


def _fail(stderr: str = "boom", exit_code: int = 1) -> RunResult:
    """
        Build a non-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
    )
    return rtnval


def _make_manager(
    *,
    runner: _MockCommandRunner,
    elevated: bool = False,
    dry_run: bool = False,
) -> WindowsBootloaderManager:
    """
        Construct a manager wired to ``runner``.
    """
    config = WindowsBootloaderManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        bcdedit_executable=_BCDEDIT,
    )
    rtnval = WindowsBootloaderManager(runner=runner, config=config)
    return rtnval


_OSLOADER_OUTPUT = """\
Windows Boot Loader
-------------------
identifier              {a1b2c3d4-1111-2222-3333-444455556666}
device                  partition=C:
path                    \\Windows\\system32\\winload.efi
description             Windows 11
locale                  en-US

Windows Boot Loader
-------------------
identifier              {99887766-5544-3322-1100-aabbccddeeff}
device                  partition=D:
path                    \\Windows\\system32\\winload.efi
description             Windows Server
locale                  en-US
"""

_BOOTMGR_OUTPUT = """\
Windows Boot Manager
--------------------
identifier              {bootmgr}
device                  partition=\\Device\\HarddiskVolume1
description             Windows Boot Manager
locale                  en-US
default                 {a1b2c3d4-1111-2222-3333-444455556666}
displayorder            {a1b2c3d4-1111-2222-3333-444455556666}
                        {99887766-5544-3322-1100-aabbccddeeff}
timeout                 30
"""


def _seed_enum_calls(runner: _MockCommandRunner) -> None:
    """
        Pre-seed the standard ``bcdedit /enum`` invocations used by
        ``list_entries`` and ``_read_default_guid``.
    """
    runner.results[(_BCDEDIT, "/enum", "osloader")] = _ok(
        stdout=_OSLOADER_OUTPUT,
    )
    runner.results[(_BCDEDIT, "/enum", "{fwbootmgr}")] = _ok(stdout="")
    runner.results[(_BCDEDIT, "/enum", "{bootmgr}", "/v")] = _ok(
        stdout=_BOOTMGR_OUTPUT,
    )
    return


# ----------------------------------------------------------------------
# list_entries
# ----------------------------------------------------------------------


def test_list_entries_parses_bcdedit_output():
    """
        ``list_entries`` parses ``bcdedit /enum osloader`` and marks
        the default GUID's entry as the default.
    """
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    manager = _make_manager(runner=runner)

    entries = manager.list_entries()

    testbase.assert_equal(len(entries), 2)
    testbase.assert_equal(entries[0].id, _DEFAULT_GUID)
    testbase.assert_equal(entries[0].name, "Windows 11")
    testbase.assert_equal(entries[0].is_default, True)
    testbase.assert_equal(entries[1].id, _OTHER_GUID)
    testbase.assert_equal(entries[1].is_default, False)


# ----------------------------------------------------------------------
# get_default_entry
# ----------------------------------------------------------------------


def test_get_default_entry_resolves_id():
    """
        ``get_default_entry`` resolves the default GUID via the
        ``{bootmgr}`` block and looks it up in the osloader list.
    """
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    manager = _make_manager(runner=runner)

    default_entry = manager.get_default_entry()

    testbase.assert_equal(default_entry.id, _DEFAULT_GUID)
    testbase.assert_equal(default_entry.name, "Windows 11")
    testbase.assert_equal(default_entry.is_default, True)


def test_get_default_entry_raises_when_unresolvable():
    """
        ``get_default_entry`` raises :class:`WindowsBootloaderQueryError`
        when ``{bootmgr}`` does not carry a default.
    """
    runner = _MockCommandRunner()
    runner.results[(_BCDEDIT, "/enum", "{bootmgr}", "/v")] = _ok(stdout="")
    runner.results[(_BCDEDIT, "/enum", "osloader")] = _ok(
        stdout=_OSLOADER_OUTPUT,
    )
    runner.results[(_BCDEDIT, "/enum", "{fwbootmgr}")] = _ok(stdout="")
    manager = _make_manager(runner=runner)

    raised = False
    try:
        manager.get_default_entry()
    except WindowsBootloaderQueryError:
        raised = True

    testbase.assert_equal(raised, True)


# ----------------------------------------------------------------------
# set_default_entry
# ----------------------------------------------------------------------


def test_set_default_entry_invokes_bcdedit():
    """
        ``set_default_entry`` runs ``bcdedit /default {<guid>}`` when
        the requested GUID differs from the current default.
    """
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.set_default_entry(entry_id=_OTHER_GUID)

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.SUCCESS)
    testbase.assert_equal(result.requires_reboot, True)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        [_BCDEDIT, "/default", _OTHER_GUID],
    )
    testbase.assert_equal(last_call["elevated"], True)


def test_set_default_entry_skipped_when_already_default():
    """
        ``set_default_entry`` returns SKIPPED when the requested GUID
        is already the default.
    """
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    manager = _make_manager(runner=runner)

    result = manager.set_default_entry(entry_id=_DEFAULT_GUID)

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.SKIPPED)
    default_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2 and call["args"][1] == "/default"
    ]
    testbase.assert_equal(len(default_calls), 0)


def test_set_default_entry_resolves_guid_format():
    """
        ``set_default_entry`` accepts a bare GUID and normalises it
        to the canonical brace-wrapped lower-case form before
        invoking ``bcdedit``.
    """
    bare_guid = "99887766-5544-3322-1100-AABBCCDDEEFF"
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.set_default_entry(entry_id=bare_guid)

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        [_BCDEDIT, "/default", _OTHER_GUID],
    )
    testbase.assert_equal(result.entry_id, _OTHER_GUID)


# ----------------------------------------------------------------------
# add_entry
# ----------------------------------------------------------------------


def test_add_entry_invokes_bcdedit():
    """
        ``add_entry`` runs ``bcdedit /create``, ``bcdedit /set`` per
        option, and ``bcdedit /displayorder ... /addlast``.
    """
    new_guid = "{12345678-aabb-ccdd-eeff-001122334455}"
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    create_command = (
        _BCDEDIT, "/create", new_guid, "/d", "Test Entry",
        "/application", "osloader",
    )
    runner.results[create_command] = _ok(
        stdout="The entry {} was successfully created.\n".format(new_guid),
    )
    runner.results[(
        _BCDEDIT, "/set", new_guid, "device", "partition=E:",
    )] = _ok()
    runner.results[(
        _BCDEDIT, "/set", new_guid, "path",
        "\\Windows\\system32\\winload.efi",
    )] = _ok()
    runner.results[(
        _BCDEDIT, "/displayorder", new_guid, "/addlast",
    )] = _ok()
    manager = _make_manager(runner=runner, elevated=True)

    spec = WindowsBootEntrySpec(
        id=new_guid,
        name="Test Entry",
        application="osloader",
        device="partition=E:",
        path="\\Windows\\system32\\winload.efi",
        options=None,
    )
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.SUCCESS)
    testbase.assert_equal(result.entry_id, new_guid)
    create_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2 and call["args"][1] == "/create"
    ]
    testbase.assert_equal(len(create_calls), 1)
    set_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2 and call["args"][1] == "/set"
    ]
    testbase.assert_equal(len(set_calls), 2)
    displayorder_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2 and call["args"][1] == "/displayorder"
    ]
    testbase.assert_equal(len(displayorder_calls), 1)


def test_add_entry_skipped_when_present():
    """
        ``add_entry`` returns SKIPPED when an entry with the same
        GUID and matching description already exists.
    """
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    manager = _make_manager(runner=runner, elevated=True)

    spec = WindowsBootEntrySpec(
        id=_DEFAULT_GUID,
        name="Windows 11",
        application="osloader",
        device=None,
        path=None,
        options=None,
    )
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.SKIPPED)
    create_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2 and call["args"][1] == "/create"
    ]
    testbase.assert_equal(len(create_calls), 0)


def test_add_entry_dry_run_short_circuits():
    """
        ``add_entry`` returns DRY_RUN without invoking the runner
        when ``config.dry_run`` is True.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, dry_run=True)

    spec = WindowsBootEntrySpec(
        id=None,
        name="Test",
        application="osloader",
    )
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)


# ----------------------------------------------------------------------
# remove_entry
# ----------------------------------------------------------------------


def test_remove_entry_invokes_bcdedit():
    """
        ``remove_entry`` runs ``bcdedit /delete {<guid>}`` when the
        entry is present.
    """
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    runner.results[(_BCDEDIT, "/delete", _OTHER_GUID)] = _ok()
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.remove_entry(entry_id=_OTHER_GUID)

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        [_BCDEDIT, "/delete", _OTHER_GUID],
    )


def test_remove_entry_skipped_when_absent():
    """
        ``remove_entry`` returns SKIPPED when no matching GUID is
        present in the BCD store.
    """
    missing_guid = "{deadbeef-1234-5678-9abc-defefefefefe}"
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    manager = _make_manager(runner=runner)

    result = manager.remove_entry(entry_id=missing_guid)

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.SKIPPED)
    delete_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2 and call["args"][1] == "/delete"
    ]
    testbase.assert_equal(len(delete_calls), 0)


# ----------------------------------------------------------------------
# update_bootloader_config / regenerate_config
# ----------------------------------------------------------------------


def test_update_bootloader_config_returns_noop_success():
    """
        ``update_bootloader_config`` returns SUCCESS without invoking
        ``bcdedit`` (BCD writes are immediate).
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.update_bootloader_config()

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 0)


def test_regenerate_config_returns_noop_success():
    """
        ``regenerate_config`` returns SUCCESS without invoking
        ``bcdedit``.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.regenerate_config()

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 0)


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Writes pass ``elevated=True`` to the runner when
        ``config.elevated`` is True.
    """
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    runner.results[(_BCDEDIT, "/default", _OTHER_GUID)] = _ok()
    manager = _make_manager(runner=runner, elevated=True)

    manager.set_default_entry(entry_id=_OTHER_GUID)

    set_default_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2 and call["args"][1] == "/default"
    ]
    testbase.assert_equal(len(set_default_calls), 1)
    testbase.assert_equal(set_default_calls[0]["elevated"], True)


def test_reads_never_elevate():
    """
        Reads (``bcdedit /enum ...``) never pass ``elevated=True``
        regardless of config.
    """
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    manager = _make_manager(runner=runner, elevated=True)

    manager.list_entries()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)


# ----------------------------------------------------------------------
# Failed-command surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failed ``bcdedit /default`` invocation surfaces the runner
        stderr on a FAILED result.
    """
    runner = _MockCommandRunner()
    _seed_enum_calls(runner=runner)
    runner.results[(_BCDEDIT, "/default", _OTHER_GUID)] = _fail(
        stderr="The boot configuration data store could not be opened.",
        exit_code=2,
    )
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.set_default_entry(entry_id=_OTHER_GUID)

    testbase.assert_equal(result.status, WindowsBootloaderActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(
        "could not be opened" in (result.stderr or ""),
        True,
    )
