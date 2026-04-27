"""
    Tests for :class:`WindowsFirewallManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.firewallmanagement import (
    WindowsFirewallActionResult,
    WindowsFirewallActionStatus,
    WindowsFirewallManager,
    WindowsFirewallManagerConfig,
    WindowsFirewallQueryError,
    WindowsFirewallState,
    WindowsZoneInfo,
    _classify_rule,
    _extract_display_name,
    _port_display_name,
    _profile_field_to_zones,
    _service_display_name,
    _validate_protocol,
    _validate_zone,
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


def _ps_argv(script: str) -> list:
    """
        Build the canonical PowerShell argv used by the manager.
    """
    rtnval = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]
    return rtnval


# ----------------------------------------------------------------------
# Pure-function tests
# ----------------------------------------------------------------------


def test_validate_zone_accepts_known():
    """
        ``Domain``, ``Private``, ``Public``, ``Any`` are accepted.
    """
    for token in ("Domain", "Private", "Public", "Any"):
        rtnval = _validate_zone(zone=token)
        testbase.assert_equal(rtnval, True)
    return


def test_validate_zone_rejects_unknown():
    """
        Unknown zones are rejected.
    """
    rtnval = _validate_zone(zone="Trusted")
    testbase.assert_equal(rtnval, False)
    return


def test_validate_protocol_accepts_known():
    """
        ``tcp`` and ``udp`` are accepted (case-insensitive).
    """
    rtnval = _validate_protocol(protocol="TCP")
    testbase.assert_equal(rtnval, True)
    rtnval = _validate_protocol(protocol="udp")
    testbase.assert_equal(rtnval, True)
    return


def test_validate_protocol_rejects_unknown():
    """
        Unknown protocols are rejected.
    """
    rtnval = _validate_protocol(protocol="icmp")
    testbase.assert_equal(rtnval, False)
    return


def test_service_display_name_format():
    """
        Service display names follow the ``automojo-svc-<name>``
        prefix.
    """
    rtnval = _service_display_name(service="ssh")
    testbase.assert_equal(rtnval, "automojo-svc-ssh")
    return


def test_port_display_name_format():
    """
        Port display names follow the
        ``automojo-port-<port>-<protocol>`` prefix.
    """
    rtnval = _port_display_name(port=8080, protocol="tcp")
    testbase.assert_equal(rtnval, "automojo-port-8080-tcp")
    return


def test_extract_display_name_single_quoted():
    """
        ``-DisplayName 'foo'`` is parsed.
    """
    rtnval = _extract_display_name(
        rule="-DisplayName 'block-bad' -Action Block",
    )
    testbase.assert_equal(rtnval, "block-bad")
    return


def test_extract_display_name_double_quoted():
    """
        ``-DisplayName "foo"`` is parsed.
    """
    rtnval = _extract_display_name(
        rule='-DisplayName "block-bad" -Action Block',
    )
    testbase.assert_equal(rtnval, "block-bad")
    return


def test_extract_display_name_missing():
    """
        A rule without ``-DisplayName`` returns None.
    """
    rtnval = _extract_display_name(
        rule="-Action Block -Protocol TCP",
    )
    testbase.assert_equal(rtnval, None)
    return


def test_profile_field_to_zones_string_csv():
    """
        ``"Public, Private"`` decodes to a set with both names.
    """
    rtnval = _profile_field_to_zones(raw_value="Public, Private")
    testbase.assert_equal(rtnval, {"Public", "Private"})
    return


def test_profile_field_to_zones_int_bitmask():
    """
        Bitmask 5 (=Domain | Public) decodes to the matching set.
    """
    rtnval = _profile_field_to_zones(raw_value=5)
    testbase.assert_equal(rtnval, {"Domain", "Public"})
    return


def test_profile_field_to_zones_any_bitmask():
    """
        The ``Any`` bitmask 0x7FFFFFFF decodes to ``{"Any"}``.
    """
    rtnval = _profile_field_to_zones(raw_value=0x7FFFFFFF)
    testbase.assert_equal(rtnval, {"Any"})
    return


def test_classify_rule_service():
    """
        ``automojo-svc-ssh`` is classified as a service.
    """
    services: list = []
    ports: list = []
    rich_rules: list = []
    _classify_rule(
        display_name="automojo-svc-ssh",
        services=services, ports=ports, rich_rules=rich_rules,
    )
    testbase.assert_equal(services, ["ssh"])
    testbase.assert_equal(ports, [])
    testbase.assert_equal(rich_rules, [])
    return


def test_classify_rule_port():
    """
        ``automojo-port-8080-tcp`` is classified as a port.
    """
    services: list = []
    ports: list = []
    rich_rules: list = []
    _classify_rule(
        display_name="automojo-port-8080-tcp",
        services=services, ports=ports, rich_rules=rich_rules,
    )
    testbase.assert_equal(ports, ["8080-tcp"])
    return


def test_classify_rule_rich():
    """
        Display names without a sentinel prefix are rich rules.
    """
    services: list = []
    ports: list = []
    rich_rules: list = []
    _classify_rule(
        display_name="my-custom-rule",
        services=services, ports=ports, rich_rules=rich_rules,
    )
    testbase.assert_equal(rich_rules, ["my-custom-rule"])
    return


# ----------------------------------------------------------------------
# add_service / remove_service
# ----------------------------------------------------------------------


def test_add_service_runs_new_netfirewallrule():
    """
        ``add_service("ssh", "Public")`` dispatches a
        ``New-NetFirewallRule`` call with the deterministic display
        name and the correct port.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.add_service(service="ssh", zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.SUCCESS,
    )
    write_call = runner.calls[-1]
    script = write_call["args"][-1]
    testbase.assert_equal(
        "New-NetFirewallRule -DisplayName 'automojo-svc-ssh'" in script,
        True,
    )
    testbase.assert_equal("-LocalPort 22" in script, True)
    testbase.assert_equal("-Profile Public" in script, True)
    return


def test_add_service_skipped_when_rule_exists():
    """
        When the probe reports a matching display name, ``add_service``
        returns SKIPPED.
    """
    payload = json.dumps([{"DisplayName": "automojo-svc-ssh"}])
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout=payload, stderr=""),
    )
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.add_service(service="ssh", zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.SKIPPED,
    )
    new_calls = [
        c for c in runner.calls
        if "New-NetFirewallRule" in c["args"][-1]
    ]
    testbase.assert_equal(new_calls, [])
    return


def test_add_service_unknown_service_returns_failed():
    """
        Unknown services return FAILED with
        ``message="unknown_service"``.
    """
    runner = _MockCommandRunner()
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.add_service(service="bogus", zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "unknown_service")
    testbase.assert_equal(runner.calls, [])
    return


def test_add_service_invalid_zone_returns_failed():
    """
        Invalid zones return FAILED with ``message="invalid_zone"``.
    """
    runner = _MockCommandRunner()
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.add_service(service="ssh", zone="Trusted")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "invalid_zone")
    testbase.assert_equal(runner.calls, [])
    return


def test_add_service_uses_override_services_table():
    """
        A non-None ``services_table`` on the config replaces the
        bundled mapping.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    config = WindowsFirewallManagerConfig(
        services_table={"custom": (9999, "udp")},
    )
    manager = WindowsFirewallManager(runner=runner, config=config)
    result = manager.add_service(service="custom", zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.SUCCESS,
    )
    script = runner.calls[-1]["args"][-1]
    testbase.assert_equal("-LocalPort 9999" in script, True)
    testbase.assert_equal("-Protocol UDP" in script, True)
    return


def test_remove_service_skipped_when_absent():
    """
        ``remove_service`` returns SKIPPED when the probe reports no
        matching rule.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.remove_service(service="ssh", zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.SKIPPED,
    )
    return


def test_remove_service_dispatches_when_present():
    """
        ``remove_service`` dispatches ``Remove-NetFirewallRule`` when
        the probe reports a matching rule.
    """
    payload = json.dumps([{"DisplayName": "automojo-svc-ssh"}])

    def patched_run(args, **kwargs):
        runner.calls.append({"args": list(args), "elevated": kwargs.get("elevated", False)})
        joined = args[-1]
        if "Get-NetFirewallRule" in joined:
            return RunResult(exit_code=0, stdout=payload, stderr="")
        return RunResult(exit_code=0, stdout="", stderr="")

    runner = _MockCommandRunner()
    runner.run = patched_run
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.remove_service(service="ssh", zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.SUCCESS,
    )
    write_call = runner.calls[-1]
    testbase.assert_equal(
        "Remove-NetFirewallRule -DisplayName 'automojo-svc-ssh'"
        in write_call["args"][-1],
        True,
    )
    return


# ----------------------------------------------------------------------
# add_port
# ----------------------------------------------------------------------


def test_add_port_runs_new_netfirewallrule_with_localport():
    """
        ``add_port(8080, "tcp", "Private")`` dispatches a
        ``New-NetFirewallRule`` with ``LocalPort 8080`` and the
        deterministic display name.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.add_port(
        port=8080, protocol="tcp", zone="Private",
    )
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.SUCCESS,
    )
    script = runner.calls[-1]["args"][-1]
    testbase.assert_equal(
        "automojo-port-8080-tcp" in script, True,
    )
    testbase.assert_equal("-LocalPort 8080" in script, True)
    testbase.assert_equal("-Profile Private" in script, True)
    return


def test_add_port_invalid_protocol_returns_failed():
    """
        Unknown protocols return FAILED with
        ``message="invalid_protocol"``.
    """
    runner = _MockCommandRunner()
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.add_port(
        port=8080, protocol="icmp", zone="Public",
    )
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "invalid_protocol")
    return


# ----------------------------------------------------------------------
# add_rich_rule / remove_rich_rule
# ----------------------------------------------------------------------


def test_add_rich_rule_dispatches_with_caller_displayname():
    """
        ``add_rich_rule`` dispatches ``New-NetFirewallRule`` with the
        caller's parameter string.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    rule = (
        "-DisplayName 'block-bad' -Direction Inbound -Action Block "
        "-Protocol TCP -LocalPort 8080"
    )
    result = manager.add_rich_rule(rule=rule, zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.SUCCESS,
    )
    script = runner.calls[-1]["args"][-1]
    testbase.assert_equal(script.startswith("New-NetFirewallRule "), True)
    testbase.assert_equal("-DisplayName 'block-bad'" in script, True)
    testbase.assert_equal("-Profile Public" in script, True)
    return


def test_add_rich_rule_requires_displayname():
    """
        A rule without ``-DisplayName`` yields FAILED.
    """
    runner = _MockCommandRunner()
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    rule = "-Direction Inbound -Action Block -Protocol TCP -LocalPort 80"
    result = manager.add_rich_rule(rule=rule, zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "display_name_required")
    testbase.assert_equal(runner.calls, [])
    return


def test_remove_rich_rule_dispatches():
    """
        ``remove_rich_rule`` parses the display name and dispatches
        ``Remove-NetFirewallRule``.
    """
    payload = json.dumps([{"DisplayName": "block-bad"}])

    def patched_run(args, **kwargs):
        runner.calls.append({"args": list(args), "elevated": kwargs.get("elevated", False)})
        if "Get-NetFirewallRule" in args[-1]:
            return RunResult(exit_code=0, stdout=payload, stderr="")
        return RunResult(exit_code=0, stdout="", stderr="")

    runner = _MockCommandRunner()
    runner.run = patched_run
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    rule = "-DisplayName 'block-bad' -Action Block"
    result = manager.remove_rich_rule(rule=rule, zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.SUCCESS,
    )
    last = runner.calls[-1]["args"][-1]
    testbase.assert_equal(
        "Remove-NetFirewallRule -DisplayName 'block-bad'" in last, True,
    )
    return


# ----------------------------------------------------------------------
# set_default_zone / reload
# ----------------------------------------------------------------------


def test_set_default_zone_skipped_when_valid_profile():
    """
        Valid zone names yield SKIPPED with no runner call.
    """
    runner = _MockCommandRunner()
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.set_default_zone(zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.SKIPPED,
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_set_default_zone_failed_when_invalid_profile():
    """
        Invalid zone names yield FAILED.
    """
    runner = _MockCommandRunner()
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.set_default_zone(zone="Trusted")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "invalid_zone")
    testbase.assert_equal(runner.calls, [])
    return


def test_reload_returns_success_without_invoking_runner():
    """
        ``reload`` returns SUCCESS without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    result = manager.reload()
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.SUCCESS,
    )
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# query_zone / query_state
# ----------------------------------------------------------------------


def test_query_zone_classifies_by_displayname_prefix():
    """
        ``query_zone`` classifies rules into services / ports / rich
        rules based on display-name prefix.
    """
    payload = json.dumps([
        {"DisplayName": "automojo-svc-ssh", "Profile": "Public"},
        {"DisplayName": "automojo-port-8080-tcp", "Profile": "Public"},
        {"DisplayName": "block-bad", "Profile": "Public"},
        {"DisplayName": "ignored-private", "Profile": "Private"},
    ])
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout=payload, stderr=""),
    )
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    info = manager.query_zone(zone="Public")
    testbase.assert_equal(info.services, ["ssh"])
    testbase.assert_equal(info.ports, ["8080-tcp"])
    testbase.assert_equal(info.rich_rules, ["block-bad"])
    testbase.assert_equal(info.interfaces, [])
    testbase.assert_equal(info.sources, [])
    return


def test_query_zone_invalid_zone_raises():
    """
        Invalid zone names raise :class:`WindowsFirewallQueryError`.
    """
    runner = _MockCommandRunner()
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    raised = False
    try:
        manager.query_zone(zone="Trusted")
    except WindowsFirewallQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_query_all_zones_iterates_three_profiles():
    """
        ``query_all_zones`` iterates Domain / Private / Public and
        returns one info per profile.
    """
    payload = json.dumps([])
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout=payload, stderr=""),
    )
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    rtnval = manager.query_all_zones()
    testbase.assert_equal(len(rtnval), 3)
    zones = [info.zone for info in rtnval]
    testbase.assert_equal(zones, ["Domain", "Private", "Public"])
    return


def test_query_state_running_when_all_profiles_enabled():
    """
        ``query_state`` reports ``running=True`` when every profile's
        ``Enabled`` field is True.
    """
    profile_payload = json.dumps([
        {"Name": "Domain", "Enabled": True},
        {"Name": "Private", "Enabled": True},
        {"Name": "Public", "Enabled": True},
    ])
    connection_payload = json.dumps([
        {"NetworkCategory": "Private"},
    ])

    def patched_run(args, **kwargs):
        runner.calls.append({"args": list(args), "elevated": kwargs.get("elevated", False)})
        joined = args[-1]
        if "Get-NetFirewallProfile" in joined:
            return RunResult(
                exit_code=0, stdout=profile_payload, stderr="",
            )
        if "Get-NetConnectionProfile" in joined:
            return RunResult(
                exit_code=0, stdout=connection_payload, stderr="",
            )
        return RunResult(exit_code=0, stdout="", stderr="")

    runner = _MockCommandRunner()
    runner.run = patched_run
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    state = manager.query_state()
    testbase.assert_equal(state.running, True)
    testbase.assert_equal(state.default_zone, "Private")
    return


def test_query_state_not_running_when_one_disabled():
    """
        ``query_state`` reports ``running=False`` when any profile is
        disabled.
    """
    profile_payload = json.dumps([
        {"Name": "Domain", "Enabled": True},
        {"Name": "Private", "Enabled": False},
        {"Name": "Public", "Enabled": True},
    ])

    def patched_run(args, **kwargs):
        runner.calls.append({"args": list(args), "elevated": kwargs.get("elevated", False)})
        joined = args[-1]
        if "Get-NetFirewallProfile" in joined:
            return RunResult(
                exit_code=0, stdout=profile_payload, stderr="",
            )
        return RunResult(exit_code=0, stdout="", stderr="")

    runner = _MockCommandRunner()
    runner.run = patched_run
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    state = manager.query_state()
    testbase.assert_equal(state.running, False)
    return


def test_query_state_default_zone_from_int_category():
    """
        Integer ``NetworkCategory`` (e.g. 1=Private) is mapped to a
        profile name.
    """
    profile_payload = json.dumps([
        {"Name": "Domain", "Enabled": True},
        {"Name": "Private", "Enabled": True},
        {"Name": "Public", "Enabled": True},
    ])
    connection_payload = json.dumps([{"NetworkCategory": 1}])

    def patched_run(args, **kwargs):
        runner.calls.append({"args": list(args), "elevated": kwargs.get("elevated", False)})
        joined = args[-1]
        if "Get-NetFirewallProfile" in joined:
            return RunResult(exit_code=0, stdout=profile_payload, stderr="")
        if "Get-NetConnectionProfile" in joined:
            return RunResult(exit_code=0, stdout=connection_payload, stderr="")
        return RunResult(exit_code=0, stdout="", stderr="")

    runner = _MockCommandRunner()
    runner.run = patched_run
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(),
    )
    state = manager.query_state()
    testbase.assert_equal(state.default_zone, "Private")
    return


# ----------------------------------------------------------------------
# Dry-run + elevation matrices
# ----------------------------------------------------------------------


def test_add_service_dry_run_short_circuits():
    """
        Dry-run add_service returns DRY_RUN with no runner call.
    """
    runner = _MockCommandRunner()
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(dry_run=True),
    )
    result = manager.add_service(service="ssh", zone="Public")
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.DRY_RUN,
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_add_port_dry_run_short_circuits():
    """
        Dry-run add_port returns DRY_RUN with no runner call.
    """
    runner = _MockCommandRunner()
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(dry_run=True),
    )
    result = manager.add_port(
        port=8080, protocol="tcp", zone="Private",
    )
    testbase.assert_equal(
        result.status, WindowsFirewallActionStatus.DRY_RUN,
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_reads_never_elevate_even_with_elevated_config():
    """
        Reads do not elevate even when ``config.elevated=True``.
    """
    payload = json.dumps([])
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout=payload, stderr=""),
    )
    manager = WindowsFirewallManager(
        runner=runner,
        config=WindowsFirewallManagerConfig(elevated=True),
    )
    manager.query_zone(zone="Public")
    elevated_calls = [c for c in runner.calls if c["elevated"]]
    testbase.assert_equal(elevated_calls, [])
    return
