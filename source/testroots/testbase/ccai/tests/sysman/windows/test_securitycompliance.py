"""
    Tests for :class:`WindowsSecurityComplianceManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.securitycompliance import (
    WindowsBaselineInfo,
    WindowsComplianceFinding,
    WindowsSecurityComplianceActionStatus,
    WindowsSecurityComplianceManager,
    WindowsSecurityComplianceManagerConfig,
    WindowsSecurityComplianceQueryError,
    _count_findings,
    _evaluate_defender,
    _evaluate_firewall,
    _evaluate_secedit,
    _evaluate_uac,
    _parse_secedit_inf,
    _severity_from_rule_id,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in.
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


# ----------------------------------------------------------------------
# Pure-function tests
# ----------------------------------------------------------------------


def test_parse_secedit_inf_simple():
    """
        ``_parse_secedit_inf`` decodes a small INI snippet into nested
        dicts.
    """
    text = (
        "[System Access]\r\n"
        "MinimumPasswordLength = 14\r\n"
        "LockoutBadCount = 5\r\n"
        "[Event Audit]\r\n"
        "AuditLogonEvents = 3\r\n"
    )
    rtnval = _parse_secedit_inf(text=text)
    testbase.assert_equal(
        rtnval["System Access"]["MinimumPasswordLength"], "14",
    )
    testbase.assert_equal(
        rtnval["System Access"]["LockoutBadCount"], "5",
    )
    testbase.assert_equal(
        rtnval["Event Audit"]["AuditLogonEvents"], "3",
    )
    return


def test_parse_secedit_inf_strips_quoted_values():
    """
        Values wrapped in double quotes are unwrapped.
    """
    text = (
        "[System Access]\r\n"
        'NewAdministratorName = "Administrator"\r\n'
    )
    rtnval = _parse_secedit_inf(text=text)
    testbase.assert_equal(
        rtnval["System Access"]["NewAdministratorName"], "Administrator",
    )
    return


def test_parse_secedit_inf_empty():
    """
        An empty string yields an empty dict.
    """
    rtnval = _parse_secedit_inf(text="")
    testbase.assert_equal(rtnval, {})
    return


def test_severity_from_rule_id_uac_is_high():
    """
        UAC rules are tagged ``high`` severity.
    """
    rtnval = _severity_from_rule_id(rule_id="uac.enable_lua")
    testbase.assert_equal(rtnval, "high")
    return


def test_severity_from_rule_id_password_is_medium():
    """
        Password rules are tagged ``medium`` severity.
    """
    rtnval = _severity_from_rule_id(
        rule_id="password.minimum_length_ge_14",
    )
    testbase.assert_equal(rtnval, "medium")
    return


def test_evaluate_uac_pass():
    """
        EnableLUA=1 passes.
    """
    rtnval = _evaluate_uac(lua_value="1")
    testbase.assert_equal(rtnval.status, "pass")
    return


def test_evaluate_uac_fail():
    """
        EnableLUA=0 fails.
    """
    rtnval = _evaluate_uac(lua_value="0")
    testbase.assert_equal(rtnval.status, "fail")
    return


def test_evaluate_uac_unknown():
    """
        A missing UAC value yields ``unknown``.
    """
    rtnval = _evaluate_uac(lua_value=None)
    testbase.assert_equal(rtnval.status, "unknown")
    return


def test_evaluate_defender_pass():
    """
        ``RealTimeProtectionEnabled=True`` passes.
    """
    rtnval = _evaluate_defender(
        defender_status={"RealTimeProtectionEnabled": True},
    )
    testbase.assert_equal(rtnval.status, "pass")
    return


def test_evaluate_defender_fail():
    """
        ``RealTimeProtectionEnabled=False`` fails.
    """
    rtnval = _evaluate_defender(
        defender_status={"RealTimeProtectionEnabled": False},
    )
    testbase.assert_equal(rtnval.status, "fail")
    return


def test_evaluate_firewall_pass_per_profile():
    """
        Each profile yields one finding; a True ``Enabled`` value
        passes.
    """
    profiles = [
        {"Name": "Domain", "Enabled": True},
        {"Name": "Private", "Enabled": True},
        {"Name": "Public", "Enabled": False},
    ]
    rtnval = _evaluate_firewall(firewall_profiles=profiles)
    testbase.assert_equal(len(rtnval), 3)
    statuses = {f.rule_id: f.status for f in rtnval}
    testbase.assert_equal(statuses["firewall.domain.enabled"], "pass")
    testbase.assert_equal(statuses["firewall.private.enabled"], "pass")
    testbase.assert_equal(statuses["firewall.public.enabled"], "fail")
    return


def test_evaluate_secedit_password_pass():
    """
        A 14-character minimum password length passes.
    """
    inf_dict = {
        "System Access": {
            "MinimumPasswordLength": "14",
            "LockoutBadCount": "5",
        },
        "Event Audit": {"AuditLogonEvents": "3"},
    }
    findings = _evaluate_secedit(inf_dict=inf_dict)
    statuses = {f.rule_id: f.status for f in findings}
    testbase.assert_equal(
        statuses["password.minimum_length_ge_14"], "pass",
    )
    testbase.assert_equal(
        statuses["password.lockout_threshold_in_range"], "pass",
    )
    testbase.assert_equal(
        statuses["audit.logon_events_success_and_failure"], "pass",
    )
    return


def test_evaluate_secedit_password_fail():
    """
        A short minimum password length fails.
    """
    inf_dict = {
        "System Access": {"MinimumPasswordLength": "8"},
    }
    findings = _evaluate_secedit(inf_dict=inf_dict)
    statuses = {f.rule_id: f.status for f in findings}
    testbase.assert_equal(
        statuses["password.minimum_length_ge_14"], "fail",
    )
    return


def test_count_findings_groups_by_status():
    """
        ``_count_findings`` groups findings by status.
    """
    findings = [
        WindowsComplianceFinding(
            rule_id="r1", severity="high", title="t", status="pass",
        ),
        WindowsComplianceFinding(
            rule_id="r2", severity="high", title="t", status="fail",
        ),
        WindowsComplianceFinding(
            rule_id="r3", severity="high", title="t", status="pass",
        ),
        WindowsComplianceFinding(
            rule_id="r4", severity="high", title="t", status="unknown",
        ),
    ]
    rtnval = _count_findings(findings=findings)
    testbase.assert_equal(rtnval["pass"], 2)
    testbase.assert_equal(rtnval["fail"], 1)
    testbase.assert_equal(rtnval["unknown"], 1)
    testbase.assert_equal(rtnval["warning"], 0)
    return


# ----------------------------------------------------------------------
# list_profiles
# ----------------------------------------------------------------------


def test_list_profiles_returns_two_bundled_profiles():
    """
        ``list_profiles`` returns the two bundled pilot profiles.
    """
    runner = _MockCommandRunner()
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(),
    )
    profiles = manager.list_profiles()
    testbase.assert_equal(len(profiles), 2)
    ids = {p.id for p in profiles}
    testbase.assert_equal(
        "cis_windows_baseline" in ids, True,
    )
    testbase.assert_equal(
        "microsoft_security_baseline_w11" in ids, True,
    )
    return


# ----------------------------------------------------------------------
# run_compliance_profile
# ----------------------------------------------------------------------


def test_run_compliance_profile_unknown_profile_raises():
    """
        An unknown profile id raises
        :class:`WindowsSecurityComplianceQueryError` before any
        runner call.
    """
    runner = _MockCommandRunner()
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(),
    )
    raised = False
    try:
        manager.run_compliance_profile(profile_name="bogus")
    except WindowsSecurityComplianceQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    testbase.assert_equal(runner.calls, [])
    return


def test_run_compliance_profile_builds_report_with_findings():
    """
        ``run_compliance_profile`` aggregates the four primitives into
        a :class:`WindowsComplianceReport`.
    """
    inf_text = (
        "[System Access]\r\n"
        "MinimumPasswordLength = 14\r\n"
        "LockoutBadCount = 5\r\n"
        "[Event Audit]\r\n"
        "AuditLogonEvents = 3\r\n"
    )
    runner = _MockCommandRunner()

    def stdout_for(args):
        joined = " ".join(args)
        if "secedit /export" in joined:
            return RunResult(
                exit_code=0, stdout=inf_text, stderr="",
            )
        if "Get-MpComputerStatus" in joined:
            return RunResult(
                exit_code=0,
                stdout=json.dumps({"RealTimeProtectionEnabled": True}),
                stderr="",
            )
        if "Get-NetFirewallProfile" in joined:
            return RunResult(
                exit_code=0,
                stdout=json.dumps([
                    {"Name": "Domain", "Enabled": True},
                    {"Name": "Private", "Enabled": True},
                    {"Name": "Public", "Enabled": True},
                ]),
                stderr="",
            )
        if "EnableLUA" in joined:
            return RunResult(exit_code=0, stdout="1\r\n", stderr="")
        return RunResult(exit_code=0, stdout="", stderr="")

    # Patch the runner.run to use the stdout_for resolver.
    def patched_run(args, **kwargs):
        runner.calls.append({"args": list(args), "elevated": kwargs.get("elevated", False)})
        return stdout_for(args)

    runner.run = patched_run

    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(),
    )
    report = manager.run_compliance_profile(
        profile_name="cis_windows_baseline",
    )
    testbase.assert_equal(report.profile, "cis_windows_baseline")
    testbase.assert_equal(report.counts["fail"], 0)
    testbase.assert_equal(report.counts["pass"] > 0, True)
    return


def test_run_compliance_profile_reads_dont_elevate():
    """
        Reads dispatched by ``run_compliance_profile`` do not elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="{}", stderr=""),
    )
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(elevated=True),
    )
    manager.run_compliance_profile(profile_name="cis_windows_baseline")
    elevated_calls = [c for c in runner.calls if c["elevated"]]
    testbase.assert_equal(elevated_calls, [])
    return


# ----------------------------------------------------------------------
# remediate
# ----------------------------------------------------------------------


def test_remediate_returns_failed_with_explanation():
    """
        ``remediate`` returns FAILED with
        ``message="remediation_not_supported"``.
    """
    runner = _MockCommandRunner()
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(),
    )
    result = manager.remediate(finding_id="uac.enable_lua")
    testbase.assert_equal(
        result.status, WindowsSecurityComplianceActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "remediation_not_supported")
    testbase.assert_equal(runner.calls, [])
    return


def test_remediate_dry_run_short_circuits():
    """
        Dry-run remediate yields DRY_RUN with no runner call.
    """
    runner = _MockCommandRunner()
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(dry_run=True),
    )
    result = manager.remediate(finding_id="uac.enable_lua")
    testbase.assert_equal(
        result.status, WindowsSecurityComplianceActionStatus.DRY_RUN,
    )
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# Baseline persistence
# ----------------------------------------------------------------------


def test_get_baseline_returns_none_when_file_empty():
    """
        ``get_baseline`` returns None when the script reports an empty
        payload.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(),
    )
    rtnval = manager.get_baseline()
    testbase.assert_equal(rtnval, None)
    return


def test_get_baseline_parses_json():
    """
        ``get_baseline`` parses the persisted JSON.
    """
    payload = json.dumps({
        "profile": "cis_windows_baseline",
        "applied_at": "2026-04-24T01:00:00Z",
    })
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout=payload, stderr=""),
    )
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(),
    )
    rtnval = manager.get_baseline()
    testbase.assert_equal(rtnval.profile, "cis_windows_baseline")
    testbase.assert_equal(rtnval.applied_at, "2026-04-24T01:00:00Z")
    return


def test_get_baseline_corrupt_json_raises():
    """
        Corrupt baseline JSON raises
        :class:`WindowsSecurityComplianceQueryError`.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=0, stdout="{not json", stderr="",
        ),
    )
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(),
    )
    raised = False
    try:
        manager.get_baseline()
    except WindowsSecurityComplianceQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_set_baseline_dispatches_set_content():
    """
        ``set_baseline`` dispatches a ``Set-Content`` PowerShell call
        and SUCCEEDs when the runner reports zero exit.
    """
    seq = []

    def patched_run(args, **kwargs):
        seq.append({"args": list(args), "elevated": kwargs.get("elevated", False)})
        joined = " ".join(args)
        if "Test-Path" in joined and "Set-Content" not in joined:
            return RunResult(exit_code=0, stdout="", stderr="")
        return RunResult(exit_code=0, stdout="", stderr="")

    runner = _MockCommandRunner()
    runner.run = patched_run
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(),
    )
    spec = WindowsBaselineInfo(
        profile="cis_windows_baseline",
        applied_at="2026-04-24T01:00:00Z",
    )
    result = manager.set_baseline(spec=spec)
    testbase.assert_equal(
        result.status, WindowsSecurityComplianceActionStatus.SUCCESS,
    )
    write_calls = [
        c for c in seq if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls) >= 1, True)
    return


def test_set_baseline_unknown_profile_returns_failed():
    """
        ``set_baseline`` rejects unknown profile IDs with FAILED.
    """
    runner = _MockCommandRunner()
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(),
    )
    spec = WindowsBaselineInfo(
        profile="bogus", applied_at="2026-04-24T01:00:00Z",
    )
    result = manager.set_baseline(spec=spec)
    testbase.assert_equal(
        result.status, WindowsSecurityComplianceActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "unknown_profile")
    testbase.assert_equal(runner.calls, [])
    return


def test_set_baseline_dry_run_short_circuits():
    """
        Dry-run set_baseline yields DRY_RUN with no runner call.
    """
    runner = _MockCommandRunner()
    manager = WindowsSecurityComplianceManager(
        runner=runner,
        config=WindowsSecurityComplianceManagerConfig(dry_run=True),
    )
    spec = WindowsBaselineInfo(
        profile="cis_windows_baseline",
        applied_at="2026-04-24T01:00:00Z",
    )
    result = manager.set_baseline(spec=spec)
    testbase.assert_equal(
        result.status, WindowsSecurityComplianceActionStatus.DRY_RUN,
    )
    testbase.assert_equal(runner.calls, [])
    return
