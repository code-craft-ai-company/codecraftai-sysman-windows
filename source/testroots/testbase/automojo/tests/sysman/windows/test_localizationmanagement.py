"""
    Tests for :class:`WindowsLocalizationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.localizationmanagement import (
    WindowsLocalizationActionStatus,
    WindowsLocalizationManager,
    WindowsLocalizationManagerConfig,
    WindowsLocalizationQueryError,
)


_PS_PREFIX = ("powershell.exe", "-NoProfile", "-NonInteractive", "-Command")


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.
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

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _ps_argv(*, script: str) -> tuple:
    """
        Build the canonical PowerShell argv tuple for a scriptblock so
        tests can key the mock runner.
    """
    rtnval = _PS_PREFIX + (script,)
    return rtnval


def _culture_payload() -> str:
    """
        Build a representative ``Get-Culture | ConvertTo-Json`` payload.
    """
    rtnval = (
        '{"Name":"en-US","DisplayName":"English (United States)",'
        '"LCID":1033}'
    )
    return rtnval


def _system_locale_payload() -> str:
    """
        Build a representative ``Get-WinSystemLocale | ConvertTo-Json``
        payload.
    """
    rtnval = '{"Name":"en-US","LCID":1033}'
    return rtnval


def _user_language_list_payload() -> str:
    """
        Build a representative ``Get-WinUserLanguageList | ConvertTo-Json``
        payload (multi-entry).
    """
    rtnval = (
        '['
        '{"LanguageTag":"en-US","InputMethodTips":["0409:00000409"]},'
        '{"LanguageTag":"fr-FR","InputMethodTips":["040C:0000040C"]}'
        ']'
    )
    return rtnval


def _user_language_list_single_payload() -> str:
    """
        Build a single-entry ``Get-WinUserLanguageList`` payload (the
        bare-object shape ``ConvertTo-Json`` emits for one row).
    """
    rtnval = (
        '{"LanguageTag":"en-US","InputMethodTips":["0409:00000409"]}'
    )
    return rtnval


def _timezone_payload(*, tz_id: str = "Pacific Standard Time") -> str:
    """
        Build a representative ``Get-TimeZone | ConvertTo-Json`` payload.
    """
    rtnval = (
        '{{"Id":"{}","DisplayName":"(UTC-08:00) Pacific Time (US & Canada)"}}'
    ).format(tz_id)
    return rtnval


def _timezone_list_payload() -> str:
    """
        Build a representative ``Get-TimeZone -ListAvailable | ConvertTo-Json``
        payload.
    """
    rtnval = (
        '['
        '{"Id":"UTC"},'
        '{"Id":"Pacific Standard Time"},'
        '{"Id":"Eastern Standard Time"},'
        '{"Id":"GMT Standard Time"},'
        '{"Id":"Tokyo Standard Time"}'
        ']'
    )
    return rtnval


def _culture_list_payload() -> str:
    """
        Build a representative ``[CultureInfo]::GetCultures(...)``
        payload.
    """
    rtnval = (
        '['
        '{"Name":""},'
        '{"Name":"en"},'
        '{"Name":"en-GB"},'
        '{"Name":"en-US"},'
        '{"Name":"fr-FR"}'
        ']'
    )
    return rtnval


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


def test_get_locale_parses_get_culture_output():
    """
        get_locale parses ``Get-Culture`` and ``Get-WinSystemLocale``
        and derives the country tag.
    """
    runner = _MockCommandRunner()
    runner.results[
        _ps_argv(script="Get-Culture | ConvertTo-Json -Compress -Depth 5")
    ] = RunResult(exit_code=0, stdout=_culture_payload(), stderr="")
    runner.results[
        _ps_argv(
            script=(
                "Get-WinSystemLocale | ConvertTo-Json -Compress -Depth 5"
            )
        )
    ] = RunResult(exit_code=0, stdout=_system_locale_payload(), stderr="")

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    info = manager.get_locale()

    testbase.assert_equal(info.language, "en-US")
    testbase.assert_equal(info.country, "US")
    testbase.assert_equal(info.system_default, "en-US")
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_get_keymap_parses_get_winuserlanguagelist_output():
    """
        get_keymap parses ``Get-WinUserLanguageList`` and surfaces the
        head language tag.
    """
    runner = _MockCommandRunner()
    script = (
        "Get-WinUserLanguageList "
        "| Select-Object LanguageTag, InputMethodTips "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=script)] = RunResult(
        exit_code=0, stdout=_user_language_list_payload(), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    info = manager.get_keymap()

    testbase.assert_equal(info.name, "en-US")
    testbase.assert_equal(info.layout, "en-US")
    testbase.assert_equal(info.language_list, ["en-US", "fr-FR"])
    testbase.assert_equal("0409:00000409" in info.input_methods, True)
    return


def test_get_keymap_handles_single_entry_bare_object():
    """
        get_keymap accepts the bare-object shape ``ConvertTo-Json``
        emits when there is exactly one row.
    """
    runner = _MockCommandRunner()
    script = (
        "Get-WinUserLanguageList "
        "| Select-Object LanguageTag, InputMethodTips "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=script)] = RunResult(
        exit_code=0, stdout=_user_language_list_single_payload(), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    info = manager.get_keymap()

    testbase.assert_equal(info.name, "en-US")
    testbase.assert_equal(info.language_list, ["en-US"])
    return


def test_get_timezone_parses_get_timezone_output():
    """
        get_timezone parses the JSON ``Id`` field.
    """
    runner = _MockCommandRunner()
    script = (
        "Get-TimeZone | Select-Object Id, DisplayName "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=script)] = RunResult(
        exit_code=0, stdout=_timezone_payload(), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    tz = manager.get_timezone()

    testbase.assert_equal(tz, "Pacific Standard Time")
    return


def test_list_supported_locales_returns_list():
    """
        list_supported_locales parses the ``[CultureInfo]::GetCultures``
        JSON payload.
    """
    runner = _MockCommandRunner()
    script = (
        "[System.Globalization.CultureInfo]::"
        "GetCultures([System.Globalization.CultureTypes]::AllCultures) "
        "| Select-Object Name "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=script)] = RunResult(
        exit_code=0, stdout=_culture_list_payload(), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    locales = manager.list_supported_locales()

    testbase.assert_equal("en-US" in locales, True)
    testbase.assert_equal("fr-FR" in locales, True)
    return


def test_get_supported_timezones_parses_list():
    """
        get_supported_timezones parses the ``Get-TimeZone -ListAvailable``
        payload.
    """
    runner = _MockCommandRunner()
    script = (
        "Get-TimeZone -ListAvailable | Select-Object Id "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=script)] = RunResult(
        exit_code=0, stdout=_timezone_list_payload(), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    zones = manager.get_supported_timezones()

    testbase.assert_equal("Pacific Standard Time" in zones, True)
    testbase.assert_equal("UTC" in zones, True)
    testbase.assert_equal(len(zones), 5)
    return


def test_get_supported_keymaps_returns_list():
    """
        get_supported_keymaps surfaces the union of language tags and
        input-method TIPs.
    """
    runner = _MockCommandRunner()
    script = (
        "Get-WinUserLanguageList "
        "| Select-Object LanguageTag, InputMethodTips "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=script)] = RunResult(
        exit_code=0, stdout=_user_language_list_payload(), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    keymaps = manager.get_supported_keymaps()

    testbase.assert_equal("en-US" in keymaps, True)
    testbase.assert_equal("fr-FR" in keymaps, True)
    return


# ----------------------------------------------------------------------
# Write: set_locale
# ----------------------------------------------------------------------


def test_set_locale_invokes_set_culture():
    """
        set_locale dispatches ``Set-Culture -CultureInfo '<value>'`` when
        the current culture differs.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[
        _ps_argv(script="Get-Culture | ConvertTo-Json -Compress -Depth 5")
    ] = RunResult(
        exit_code=0,
        stdout='{"Name":"fr-FR","DisplayName":"French","LCID":1036}',
        stderr="",
    )
    runner.results[
        _ps_argv(
            script=(
                "Get-WinSystemLocale | ConvertTo-Json -Compress -Depth 5"
            )
        )
    ] = RunResult(exit_code=0, stdout='{"Name":"fr-FR","LCID":1036}', stderr="")

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    result = manager.set_locale("en-US")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.SUCCESS)
    testbase.assert_equal(result.setting, "locale")
    testbase.assert_equal(result.previous_value, "fr-FR")
    set_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Set-Culture")
    ]
    testbase.assert_equal(len(set_calls), 1)
    return


def test_set_locale_skipped_when_match():
    """
        set_locale returns SKIPPED when the current culture already
        matches.
    """
    runner = _MockCommandRunner()
    runner.results[
        _ps_argv(script="Get-Culture | ConvertTo-Json -Compress -Depth 5")
    ] = RunResult(exit_code=0, stdout=_culture_payload(), stderr="")
    runner.results[
        _ps_argv(
            script=(
                "Get-WinSystemLocale | ConvertTo-Json -Compress -Depth 5"
            )
        )
    ] = RunResult(exit_code=0, stdout=_system_locale_payload(), stderr="")

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    result = manager.set_locale("en-US")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.SKIPPED)
    set_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Set-Culture")
    ]
    testbase.assert_equal(set_calls, [])
    return


def test_set_locale_dry_run_short_circuits():
    """
        set_locale with dry_run=True returns DRY_RUN without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(dry_run=True),
    )
    result = manager.set_locale("en-US")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command[-1], "Set-Culture -CultureInfo 'en-US'",
    )
    return


def test_set_locale_apply_system_wide_marks_reboot():
    """
        set_locale with apply_system_wide=True dispatches
        ``Set-WinSystemLocale`` after ``Set-Culture`` succeeds and the
        result reports ``requires_reboot=True``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[
        _ps_argv(script="Get-Culture | ConvertTo-Json -Compress -Depth 5")
    ] = RunResult(
        exit_code=0,
        stdout='{"Name":"fr-FR","DisplayName":"French","LCID":1036}',
        stderr="",
    )
    runner.results[
        _ps_argv(
            script=(
                "Get-WinSystemLocale | ConvertTo-Json -Compress -Depth 5"
            )
        )
    ] = RunResult(exit_code=0, stdout='{"Name":"fr-FR","LCID":1036}', stderr="")

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(
            apply_system_wide=True, elevated=True,
        ),
    )
    result = manager.set_locale("en-US")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.SUCCESS)
    testbase.assert_equal(result.requires_reboot, True)
    sys_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Set-WinSystemLocale")
    ]
    testbase.assert_equal(len(sys_calls), 1)
    testbase.assert_equal(sys_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Write: set_keymap
# ----------------------------------------------------------------------


def test_set_keymap_invokes_set_winuserlanguagelist():
    """
        set_keymap dispatches ``Set-WinUserLanguageList -LanguageList
        '<lang>' -Force`` when the head language differs.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    script = (
        "Get-WinUserLanguageList "
        "| Select-Object LanguageTag, InputMethodTips "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=script)] = RunResult(
        exit_code=0,
        stdout='{"LanguageTag":"fr-FR","InputMethodTips":[]}',
        stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    result = manager.set_keymap("en-US")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.SUCCESS)
    set_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Set-WinUserLanguageList")
    ]
    testbase.assert_equal(len(set_calls), 1)
    testbase.assert_equal(
        "-LanguageList 'en-US'" in set_calls[0]["args"][-1], True,
    )
    testbase.assert_equal(
        "-Force" in set_calls[0]["args"][-1], True,
    )
    return


def test_set_keymap_skipped_when_match():
    """
        set_keymap returns SKIPPED when the head language tag already
        matches.
    """
    runner = _MockCommandRunner()
    script = (
        "Get-WinUserLanguageList "
        "| Select-Object LanguageTag, InputMethodTips "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=script)] = RunResult(
        exit_code=0, stdout=_user_language_list_payload(), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    result = manager.set_keymap("en-US")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.SKIPPED)
    set_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Set-WinUserLanguageList")
    ]
    testbase.assert_equal(set_calls, [])
    return


# ----------------------------------------------------------------------
# Write: set_timezone (with IANA -> Windows mapping)
# ----------------------------------------------------------------------


def test_set_timezone_invokes_set_timezone_with_windows_id():
    """
        set_timezone dispatches ``Set-TimeZone -Id '<windows-id>'`` for
        a Windows-format input.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    get_script = (
        "Get-TimeZone | Select-Object Id, DisplayName "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=get_script)] = RunResult(
        exit_code=0, stdout=_timezone_payload(tz_id="UTC"), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(elevated=True),
    )
    result = manager.set_timezone("Pacific Standard Time")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.SUCCESS)
    set_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Set-TimeZone")
    ]
    testbase.assert_equal(len(set_calls), 1)
    testbase.assert_equal(
        set_calls[0]["args"][-1],
        "Set-TimeZone -Id 'Pacific Standard Time'",
    )
    testbase.assert_equal(set_calls[0]["elevated"], True)
    return


def test_set_timezone_resolves_iana_to_windows_id():
    """
        set_timezone resolves an IANA-style argument
        (``"America/Los_Angeles"``) to its Windows-format id
        (``"Pacific Standard Time"``) before dispatching the cmdlet.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    get_script = (
        "Get-TimeZone | Select-Object Id, DisplayName "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=get_script)] = RunResult(
        exit_code=0, stdout=_timezone_payload(tz_id="UTC"), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    result = manager.set_timezone("America/Los_Angeles")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.SUCCESS)
    set_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Set-TimeZone")
    ]
    testbase.assert_equal(
        set_calls[0]["args"][-1],
        "Set-TimeZone -Id 'Pacific Standard Time'",
    )
    return


def test_set_timezone_unknown_iana_returns_failed():
    """
        set_timezone returns FAILED with message="unknown_timezone_id"
        when the requested value is neither in the bundled IANA table
        nor a known Windows ID.
    """
    runner = _MockCommandRunner()
    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    result = manager.set_timezone("Bogus/NotAZone")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.FAILED)
    testbase.assert_equal(result.message, "unknown_timezone_id")
    # Manager must NOT dispatch anything for an unmapped tz.
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_set_timezone_skipped_when_match():
    """
        set_timezone returns SKIPPED when the parsed Windows-format ID
        already matches the resolved request.
    """
    runner = _MockCommandRunner()
    get_script = (
        "Get-TimeZone | Select-Object Id, DisplayName "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=get_script)] = RunResult(
        exit_code=0, stdout=_timezone_payload(), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    result = manager.set_timezone("America/Los_Angeles")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.SKIPPED)
    set_calls = [
        c for c in runner.calls
        if c["args"][-1].startswith("Set-TimeZone")
    ]
    testbase.assert_equal(set_calls, [])
    return


def test_set_timezone_dry_run_short_circuits():
    """
        set_timezone with dry_run=True returns DRY_RUN with the resolved
        Windows-format ID embedded in the would-have-been argv.
    """
    runner = _MockCommandRunner()
    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(dry_run=True),
    )
    result = manager.set_timezone("America/New_York")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command[-1], "Set-TimeZone -Id 'Eastern Standard Time'",
    )
    return


# ----------------------------------------------------------------------
# Cross-cutting: elevation, error surface
# ----------------------------------------------------------------------


def test_writes_elevate_per_policy():
    """
        Every write verb passes ``elevated=True`` when the config is
        configured to elevate; reads in the same flow stay unelevated.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[
        _ps_argv(script="Get-Culture | ConvertTo-Json -Compress -Depth 5")
    ] = RunResult(
        exit_code=0,
        stdout='{"Name":"fr-FR","DisplayName":"French","LCID":1036}',
        stderr="",
    )
    runner.results[
        _ps_argv(
            script=(
                "Get-WinSystemLocale | ConvertTo-Json -Compress -Depth 5"
            )
        )
    ] = RunResult(exit_code=0, stdout='{"Name":"fr-FR","LCID":1036}', stderr="")
    keymap_script = (
        "Get-WinUserLanguageList "
        "| Select-Object LanguageTag, InputMethodTips "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=keymap_script)] = RunResult(
        exit_code=0,
        stdout='{"LanguageTag":"fr-FR","InputMethodTips":[]}',
        stderr="",
    )
    get_tz_script = (
        "Get-TimeZone | Select-Object Id, DisplayName "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=get_tz_script)] = RunResult(
        exit_code=0, stdout=_timezone_payload(tz_id="UTC"), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(elevated=True),
    )
    manager.set_locale("en-US")
    manager.set_keymap("en-US")
    manager.set_timezone("America/Los_Angeles")

    for call in runner.calls:
        scriptblock = call["args"][-1]
        if scriptblock.startswith("Get-"):
            testbase.assert_equal(call["elevated"], False)
        elif scriptblock.startswith("Set-"):
            testbase.assert_equal(call["elevated"], True)
        elif scriptblock.startswith("[System.Globalization"):
            testbase.assert_equal(call["elevated"], False)
    return


def test_reads_never_elevate():
    """
        Read-only verbs never pass ``elevated=True`` even when the
        config is configured to elevate.
    """
    runner = _MockCommandRunner()
    runner.results[
        _ps_argv(script="Get-Culture | ConvertTo-Json -Compress -Depth 5")
    ] = RunResult(exit_code=0, stdout=_culture_payload(), stderr="")
    runner.results[
        _ps_argv(
            script=(
                "Get-WinSystemLocale | ConvertTo-Json -Compress -Depth 5"
            )
        )
    ] = RunResult(exit_code=0, stdout=_system_locale_payload(), stderr="")
    keymap_script = (
        "Get-WinUserLanguageList "
        "| Select-Object LanguageTag, InputMethodTips "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=keymap_script)] = RunResult(
        exit_code=0, stdout=_user_language_list_payload(), stderr="",
    )
    cl_script = (
        "[System.Globalization.CultureInfo]::"
        "GetCultures([System.Globalization.CultureTypes]::AllCultures) "
        "| Select-Object Name "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=cl_script)] = RunResult(
        exit_code=0, stdout=_culture_list_payload(), stderr="",
    )
    get_tz_script = (
        "Get-TimeZone | Select-Object Id, DisplayName "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=get_tz_script)] = RunResult(
        exit_code=0, stdout=_timezone_payload(), stderr="",
    )
    list_tz_script = (
        "Get-TimeZone -ListAvailable | Select-Object Id "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=list_tz_script)] = RunResult(
        exit_code=0, stdout=_timezone_list_payload(), stderr="",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(elevated=True),
    )
    manager.get_locale()
    manager.list_supported_locales()
    manager.get_keymap()
    manager.get_supported_keymaps()
    manager.get_timezone()
    manager.get_supported_timezones()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    get_script = (
        "Get-TimeZone | Select-Object Id, DisplayName "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    runner.results[_ps_argv(script=get_script)] = RunResult(
        exit_code=0, stdout=_timezone_payload(tz_id="UTC"), stderr="",
    )
    set_script = "Set-TimeZone -Id 'Pacific Standard Time'"
    runner.results[_ps_argv(script=set_script)] = RunResult(
        exit_code=1, stdout="",
        stderr="Set-TimeZone : Access is denied.\n",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    result = manager.set_timezone("America/Los_Angeles")

    testbase.assert_equal(result.status, WindowsLocalizationActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal("Access is denied" in result.stderr, True)
    return


def test_get_locale_raises_on_get_culture_failure():
    """
        get_locale raises :class:`WindowsLocalizationQueryError` when
        ``Get-Culture`` exits non-zero with stderr.
    """
    runner = _MockCommandRunner()
    runner.results[
        _ps_argv(script="Get-Culture | ConvertTo-Json -Compress -Depth 5")
    ] = RunResult(
        exit_code=1, stdout="",
        stderr="Get-Culture : The cmdlet name was not recognized.\n",
    )

    manager = WindowsLocalizationManager(
        runner=runner,
        config=WindowsLocalizationManagerConfig(),
    )
    raised = False
    try:
        manager.get_locale()
    except WindowsLocalizationQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return
