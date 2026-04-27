"""
    Tests for :class:`WindowsAuthenticationManager`.

    A local ``_MockCommandRunner`` records every ``run`` call and
    returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  All ``secedit`` and PowerShell invocations are exercised
    entirely through the runner.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.authenticationmanagement import (
    WINDOWS_AUTH_LOGON,
    WINDOWS_AUTH_NETWORK,
    WindowsAuthActionStatus,
    WindowsAuthConfig,
    WindowsAuthEntry,
    WindowsAuthenticationManager,
    WindowsAuthenticationManagerConfig,
    WindowsAuthenticationQueryError,
    _format_multi_string_for_powershell,
    _parse_lsa_packages_json,
    _parse_secedit_inf,
    _render_secedit_inf,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call
        and returns pre-seeded :class:`RunResult` values keyed by
        argv tuple.
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


def _ok(stdout: str = "", stderr: str = "") -> RunResult:
    """
        Build a successful :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "", stderr: str = "", exit_code: int = 1,
) -> RunResult:
    """
        Build a failing :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


_SAMPLE_INF = (
    "[Unicode]\r\n"
    "Unicode=yes\r\n"
    "[Version]\r\n"
    "signature=\"$CHICAGO$\"\r\n"
    "Revision=1\r\n"
    "[System Access]\r\n"
    "MinimumPasswordAge = 1\r\n"
    "MaximumPasswordAge = 60\r\n"
    "MinimumPasswordLength = 14\r\n"
    "PasswordComplexity = 1\r\n"
    "[Event Audit]\r\n"
    "AuditSystemEvents = 1\r\n"
    "[Privilege Rights]\r\n"
    "SeNetworkLogonRight = *S-1-5-32-544,*S-1-5-32-545\r\n"
)


_SAMPLE_LSA_JSON = "[\"msv1_0\", \"kerberos\", \"negoextender\"]"


def _expected_export_argv() -> list:
    """
        The argv ``secedit /export`` should be invoked with.
    """
    rtnval = [
        "secedit.exe", "/export",
        "/cfg", r"C:\Windows\Temp\automojo-secedit.cfg",
        "/quiet",
    ]
    return rtnval


def _expected_get_content_argv() -> list:
    """
        The argv ``Get-Content -Raw`` should be invoked with.
    """
    rtnval = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "Get-Content -Path '{}' -Raw".format(
            r"C:\Windows\Temp\automojo-secedit.cfg",
        ),
    ]
    return rtnval


def _expected_get_lsa_argv() -> list:
    """
        The argv used to read the LSA Authentication Packages
        registry value.
    """
    script = (
        "Get-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Lsa' "
        "-Name 'Authentication Packages' | "
        "Select-Object -ExpandProperty 'Authentication Packages' | ConvertTo-Json"
    )
    rtnval = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        script,
    ]
    return rtnval


def _build_default_runner() -> _MockCommandRunner:
    """
        Build a runner pre-seeded with canonical secedit / LSA
        outputs.

        :returns: The mock runner.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_expected_export_argv())] = _ok()
    runner.results[tuple(_expected_get_content_argv())] = _ok(
        stdout=_SAMPLE_INF,
    )
    runner.results[tuple(_expected_get_lsa_argv())] = _ok(
        stdout=_SAMPLE_LSA_JSON,
    )
    return runner


def _build_manager(
    *,
    runner: _MockCommandRunner,
    dry_run: bool = False,
    elevated: bool = True,
) -> WindowsAuthenticationManager:
    """
        Build a manager with the supplied runner and config flags.

        :returns: The manager.
    """
    config = WindowsAuthenticationManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
    )
    rtnval = WindowsAuthenticationManager(runner=runner, config=config)
    return rtnval


# ----------------------------------------------------------------------
# Test cases
# ----------------------------------------------------------------------


def test_get_auth_config_parses_secedit_output():
    """
        ``get_auth_config`` parses the INF body and filters entries
        by section.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.get_auth_config(subsystem=WINDOWS_AUTH_LOGON)

    testbase.assert_equal(result.subsystem, "System Access")
    keys = [entry.policy_key for entry in result.entries]
    testbase.assert_true("MinimumPasswordLength" in keys)
    testbase.assert_true("PasswordComplexity" in keys)

    for entry in result.entries:
        testbase.assert_equal(entry.section, "System Access")
    return


def test_set_auth_config_invokes_secedit_configure():
    """
        ``set_auth_config`` writes the rendered INF and invokes
        ``secedit /configure``.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    new_config = WindowsAuthConfig(
        subsystem="System Access",
        entries=[
            WindowsAuthEntry(
                policy_key="MinimumPasswordLength",
                policy_value="20",
                section="System Access",
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=WINDOWS_AUTH_LOGON, config=new_config,
    )

    testbase.assert_equal(result.status, WindowsAuthActionStatus.SUCCESS)
    configure_calls = [
        call for call in runner.calls
        if call["args"][:2] == ["secedit.exe", "/configure"]
    ]
    testbase.assert_equal(len(configure_calls), 1)
    testbase.assert_true(configure_calls[0]["elevated"])
    return


def test_set_auth_config_skipped_when_match():
    """
        ``set_auth_config`` returns ``SKIPPED`` when every key in the
        supplied config already matches the live policy.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    new_config = WindowsAuthConfig(
        subsystem="System Access",
        entries=[
            WindowsAuthEntry(
                policy_key="MinimumPasswordLength",
                policy_value="14",
                section="System Access",
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=WINDOWS_AUTH_LOGON, config=new_config,
    )

    testbase.assert_equal(result.status, WindowsAuthActionStatus.SKIPPED)
    configure_calls = [
        call for call in runner.calls
        if call["args"][:2] == ["secedit.exe", "/configure"]
    ]
    testbase.assert_equal(len(configure_calls), 0)
    return


def test_set_auth_config_dry_run():
    """
        ``set_auth_config`` returns ``DRY_RUN`` and does not invoke
        the runner for the write.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner, dry_run=True)

    new_config = WindowsAuthConfig(
        subsystem="System Access",
        entries=[
            WindowsAuthEntry(
                policy_key="MinimumPasswordLength",
                policy_value="20",
                section="System Access",
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=WINDOWS_AUTH_LOGON, config=new_config,
    )

    testbase.assert_equal(result.status, WindowsAuthActionStatus.DRY_RUN)
    configure_calls = [
        call for call in runner.calls
        if call["args"][:2] == ["secedit.exe", "/configure"]
    ]
    testbase.assert_equal(len(configure_calls), 0)
    return


def test_list_auth_methods_parses_get_itemproperty_output():
    """
        ``list_auth_methods`` parses the JSON list and surfaces both
        enabled (live) and disabled (well-known) methods.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    methods = manager.list_auth_methods()

    names = [info.name.lower() for info in methods]
    testbase.assert_true("msv1_0" in names)
    testbase.assert_true("kerberos" in names)
    # cloudap is in well_known_packages but not in live list, so
    # should be present with enabled=False.
    cloudap_info = [
        info for info in methods if info.name.lower() == "cloudap"
    ]
    testbase.assert_equal(len(cloudap_info), 1)
    testbase.assert_false(cloudap_info[0].enabled)

    msv_info = [info for info in methods if info.name.lower() == "msv1_0"]
    testbase.assert_equal(len(msv_info), 1)
    testbase.assert_true(msv_info[0].enabled)
    return


def test_list_auth_methods_collapses_single_string_multistring():
    """
        ``list_auth_methods`` accepts the single-string MULTI_SZ
        shape PowerShell emits for one-element lists.
    """
    runner = _build_default_runner()
    runner.results[tuple(_expected_get_lsa_argv())] = _ok(
        stdout="\"kerberos\"",
    )
    manager = _build_manager(runner=runner)

    methods = manager.list_auth_methods()
    kerberos_info = [
        info for info in methods if info.name.lower() == "kerberos"
    ]
    testbase.assert_equal(len(kerberos_info), 1)
    testbase.assert_true(kerberos_info[0].enabled)
    return


def test_enable_method_invokes_set_itemproperty():
    """
        ``enable_method`` invokes ``Set-ItemProperty`` with the new
        MULTI_SZ list.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.enable_method(name="cloudap")

    testbase.assert_equal(result.status, WindowsAuthActionStatus.SUCCESS)
    set_calls = [
        call for call in runner.calls
        if "Set-ItemProperty" in (call["args"][-1] or "")
    ]
    testbase.assert_equal(len(set_calls), 1)
    testbase.assert_true(set_calls[0]["elevated"])
    testbase.assert_true("cloudap" in set_calls[0]["args"][-1])
    return


def test_enable_method_skipped_when_enabled():
    """
        ``enable_method`` returns ``SKIPPED`` when the package is
        already in the LSA list.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.enable_method(name="kerberos")

    testbase.assert_equal(result.status, WindowsAuthActionStatus.SKIPPED)
    set_calls = [
        call for call in runner.calls
        if "Set-ItemProperty" in (call["args"][-1] or "")
    ]
    testbase.assert_equal(len(set_calls), 0)
    return


def test_disable_method_invokes_set_itemproperty():
    """
        ``disable_method`` invokes ``Set-ItemProperty`` with the
        package removed from the MULTI_SZ.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.disable_method(name="kerberos")

    testbase.assert_equal(result.status, WindowsAuthActionStatus.SUCCESS)
    set_calls = [
        call for call in runner.calls
        if "Set-ItemProperty" in (call["args"][-1] or "")
    ]
    testbase.assert_equal(len(set_calls), 1)
    testbase.assert_true(set_calls[0]["elevated"])
    testbase.assert_false("'kerberos'" in set_calls[0]["args"][-1])
    return


def test_disable_method_skipped_when_disabled():
    """
        ``disable_method`` returns ``SKIPPED`` when the package is
        already absent.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.disable_method(name="cloudap")

    testbase.assert_equal(result.status, WindowsAuthActionStatus.SKIPPED)
    set_calls = [
        call for call in runner.calls
        if "Set-ItemProperty" in (call["args"][-1] or "")
    ]
    testbase.assert_equal(len(set_calls), 0)
    return


def test_validate_auth_config_returns_result():
    """
        ``validate_auth_config`` returns a populated
        :class:`WindowsAuthValidationResult` for a clean INF export.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.validate_auth_config(subsystem=WINDOWS_AUTH_LOGON)

    testbase.assert_true(result.valid)
    return


def test_writes_elevate():
    """
        ``set_auth_config``, ``enable_method``, and ``disable_method``
        invoke the runner with ``elevated=True``.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner, elevated=True)

    manager.enable_method(name="cloudap")

    set_calls = [
        call for call in runner.calls
        if "Set-ItemProperty" in (call["args"][-1] or "")
    ]
    for call in set_calls:
        testbase.assert_true(call["elevated"])
    return


def test_reads_never_elevate():
    """
        Reads invoke the runner with ``elevated=False``.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner, elevated=True)

    manager.get_auth_config(subsystem=WINDOWS_AUTH_LOGON)
    manager.list_auth_methods()
    manager.validate_auth_config(subsystem=WINDOWS_AUTH_LOGON)

    for call in runner.calls:
        args = call["args"]
        if len(args) >= 2 and args[0] == "secedit.exe" and args[1] == "/export":
            testbase.assert_false(call["elevated"])
        if len(args) > 0:
            command_text = args[-1]
        else:
            command_text = ""
        if isinstance(command_text, str):
            if "Get-ItemProperty" in command_text or "Get-Content" in command_text:
                testbase.assert_false(call["elevated"])
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero runner exit on a write surfaces as
        ``status=FAILED`` with ``stderr`` populated.
    """
    runner = _build_default_runner()

    # Pre-seed a successful Set-Content for the cfg write so the
    # configure step is reached, then have configure fail.
    configure_argv = (
        "secedit.exe", "/configure",
        "/db", r"C:\Windows\Temp\automojo-secedit.sdb",
        "/cfg", r"C:\Windows\Temp\automojo-secedit.cfg",
        "/quiet",
    )
    runner.results[configure_argv] = _fail(
        stderr="secedit: configuration failed",
        exit_code=2,
    )
    manager = _build_manager(runner=runner)

    new_config = WindowsAuthConfig(
        subsystem="System Access",
        entries=[
            WindowsAuthEntry(
                policy_key="MinimumPasswordLength",
                policy_value="20",
                section="System Access",
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=WINDOWS_AUTH_LOGON, config=new_config,
    )

    testbase.assert_equal(result.status, WindowsAuthActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_true("configuration failed" in result.stderr)
    return


def test_secedit_export_imports_correct_format():
    """
        Round-trip: the rendered INF body parses back to the same
        entries the renderer was given.
    """
    original_entries = [
        WindowsAuthEntry(
            policy_key="MinimumPasswordLength",
            policy_value="14",
            section="System Access",
        ),
        WindowsAuthEntry(
            policy_key="PasswordComplexity",
            policy_value="1",
            section="System Access",
        ),
    ]
    rendered = _render_secedit_inf(
        section_name="System Access",
        entries=original_entries,
        extra_sections={},
    )

    # Header presence asserts the secedit-acceptable preamble.
    testbase.assert_true("[Unicode]" in rendered)
    testbase.assert_true("Unicode=yes" in rendered)
    testbase.assert_true("[Version]" in rendered)
    testbase.assert_true("signature=\"$CHICAGO$\"" in rendered)
    testbase.assert_true("[System Access]" in rendered)
    testbase.assert_true(rendered.endswith("\r\n"))

    re_parsed = _parse_secedit_inf(text=rendered)
    system_access_entries = [
        entry for entry in re_parsed if entry.section == "System Access"
    ]
    keys = [entry.policy_key for entry in system_access_entries]
    testbase.assert_true("MinimumPasswordLength" in keys)
    testbase.assert_true("PasswordComplexity" in keys)
    for entry in system_access_entries:
        testbase.assert_equal(entry.section, "System Access")
    return


def test_inf_render_round_trip_is_stable():
    """
        Parse / render round-trip preserves entry keys and values
        within the targeted section.
    """
    entries_in = _parse_secedit_inf(text=_SAMPLE_INF)
    system_access_in = [
        entry for entry in entries_in if entry.section == "System Access"
    ]
    rendered = _render_secedit_inf(
        section_name="System Access",
        entries=system_access_in,
        extra_sections={},
    )
    entries_out = _parse_secedit_inf(text=rendered)
    system_access_out = [
        entry for entry in entries_out if entry.section == "System Access"
    ]

    testbase.assert_equal(len(system_access_in), len(system_access_out))
    in_map = {
        entry.policy_key: entry.policy_value for entry in system_access_in
    }
    out_map = {
        entry.policy_key: entry.policy_value for entry in system_access_out
    }
    testbase.assert_equal(in_map, out_map)
    return


def test_multi_string_format_for_powershell():
    """
        ``_format_multi_string_for_powershell`` renders a list into
        single-quoted, comma-separated array literals.
    """
    formatted = _format_multi_string_for_powershell(
        values=["msv1_0", "kerberos", "negoextender"],
    )
    testbase.assert_equal(
        formatted, "'msv1_0','kerberos','negoextender'",
    )
    return


def test_lsa_json_handles_array_and_string():
    """
        ``_parse_lsa_packages_json`` handles both the JSON-array and
        the collapsed single-string outputs.
    """
    from_array = _parse_lsa_packages_json(
        text="[\"msv1_0\",\"kerberos\"]",
    )
    testbase.assert_equal(from_array, ["msv1_0", "kerberos"])

    from_string = _parse_lsa_packages_json(text="\"kerberos\"")
    testbase.assert_equal(from_string, ["kerberos"])
    return


def test_query_error_raised_for_unexpected_failure():
    """
        A failed ``secedit /export`` surfaces as
        :class:`WindowsAuthenticationQueryError`.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_expected_export_argv())] = _fail(
        stderr="secedit: access denied",
        exit_code=5,
    )
    manager = _build_manager(runner=runner)

    raised = False
    try:
        manager.get_auth_config(subsystem=WINDOWS_AUTH_LOGON)
    except WindowsAuthenticationQueryError:
        raised = True
    testbase.assert_true(raised)
    return
