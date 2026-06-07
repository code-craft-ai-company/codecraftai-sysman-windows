"""
    Tests for :class:`WindowsDnsServerManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult`
    values keyed by argv tuple.  No live PowerShell is
    invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.dnsservermanagement import (
    WindowsDnsActionStatus,
    WindowsDnsManagerConfig,
    WindowsDnsQueryError,
    WindowsDnsRecordInfo,
    WindowsDnsRecordSpec,
    WindowsDnsServerManager,
    WindowsDnsZoneInfo,
    WindowsDnsZoneSpec,
    WindowsDnsZoneStatus,
    _build_add_record_command,
    _build_add_zone_script,
    _parse_records_json,
    _parse_zones_json,
    _parse_zone_status_json,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        ``run`` call and returns pre-seeded :class:`RunResult`
        values keyed by argv tuple.
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
            "env": env,
        }
        self.calls.append(call_record)

        # PowerShell argv is ["powershell.exe", "-NoProfile",
        # "-NonInteractive", "-Command", "<script>"].  Match
        # the script payload against pre-seeded mappings.
        if len(args) == 5 and args[0].lower().startswith("powershell"):
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


def _seed_zone_exists(
    runner: _MockCommandRunner,
    *,
    zone: str,
    exists: bool,
) -> None:
    """
        Seed the ``Get-DnsServerZone -Name '<zone>'`` probe.
    """
    prefix = "Get-DnsServerZone -Name '{}' ".format(zone)
    if exists:
        result = RunResult(
            exit_code=0,
            stdout="{{\"Name\":\"{}\",\"ZoneType\":\"Primary\"}}".format(
                zone,
            ),
            stderr="",
        )
    else:
        result = RunResult(
            exit_code=1,
            stdout="",
            stderr="ObjectNotFound",
        )
    runner.script_results[prefix] = result
    return


def _seed_records(
    runner: _MockCommandRunner,
    *,
    zone: str,
    json_text: str,
) -> None:
    prefix = "Get-DnsServerResourceRecord -ZoneName '{}'".format(zone)
    runner.script_results[prefix] = RunResult(
        exit_code=0, stdout=json_text, stderr="",
    )
    return


# ----------------------------------------------------------------------
# add_zone
# ----------------------------------------------------------------------


def test_add_zone_invokes_powershell():
    """
        add_zone dispatches ``Add-DnsServerPrimaryZone -Name
        '<zone>' -ZoneFile '<zone>.dns'`` through PowerShell.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    _seed_zone_exists(runner, zone="example.com", exists=False)

    config = WindowsDnsManagerConfig(elevated=True)
    manager = WindowsDnsServerManager(runner=runner, config=config)
    spec = WindowsDnsZoneSpec(name="example.com", zone_type="primary")
    result = manager.add_zone(name="example.com", spec=spec)
    testbase.assert_equal(
        result.status, WindowsDnsActionStatus.SUCCESS,
    )

    add_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and call["args"][0].lower().startswith("powershell")
            and "Add-DnsServerPrimaryZone" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(add_calls), 1)
    script = add_calls[0]["args"][4]
    testbase.assert_equal("-Name 'example.com'" in script, True)
    testbase.assert_equal("-ZoneFile 'example.com.dns'" in script, True)
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_add_zone_skipped_when_present():
    """
        add_zone returns SKIPPED when the zone is already
        registered.
    """
    runner = _MockCommandRunner()
    _seed_zone_exists(runner, zone="example.com", exists=True)

    manager = WindowsDnsServerManager(
        runner=runner, config=WindowsDnsManagerConfig(),
    )
    spec = WindowsDnsZoneSpec(name="example.com")
    result = manager.add_zone(name="example.com", spec=spec)
    testbase.assert_equal(
        result.status, WindowsDnsActionStatus.SKIPPED,
    )
    add_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and "Add-DnsServerPrimaryZone" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(add_calls), 0)
    return


def test_add_zone_dry_run():
    """
        With ``dry_run=True`` add_zone records the intended
        PowerShell argv but does not invoke any cmdlet.
    """
    runner = _MockCommandRunner()
    config = WindowsDnsManagerConfig(dry_run=True)
    manager = WindowsDnsServerManager(runner=runner, config=config)
    spec = WindowsDnsZoneSpec(name="example.com")
    result = manager.add_zone(name="example.com", spec=spec)
    testbase.assert_equal(
        result.status, WindowsDnsActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# remove_zone
# ----------------------------------------------------------------------


def test_remove_zone_invokes_powershell():
    """
        remove_zone dispatches ``Remove-DnsServerZone -Name
        '<zone>' -Force``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    _seed_zone_exists(runner, zone="example.com", exists=True)

    config = WindowsDnsManagerConfig(elevated=True)
    manager = WindowsDnsServerManager(runner=runner, config=config)
    result = manager.remove_zone(name="example.com")
    testbase.assert_equal(
        result.status, WindowsDnsActionStatus.SUCCESS,
    )

    remove_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and "Remove-DnsServerZone" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(remove_calls), 1)
    script = remove_calls[0]["args"][4]
    testbase.assert_equal("-Name 'example.com'" in script, True)
    testbase.assert_equal("-Force" in script, True)
    testbase.assert_equal(remove_calls[0]["elevated"], True)
    return


def test_remove_zone_skipped_when_absent():
    """
        remove_zone returns SKIPPED when the zone is absent.
    """
    runner = _MockCommandRunner()
    _seed_zone_exists(runner, zone="ghost.com", exists=False)

    manager = WindowsDnsServerManager(
        runner=runner, config=WindowsDnsManagerConfig(),
    )
    result = manager.remove_zone(name="ghost.com")
    testbase.assert_equal(
        result.status, WindowsDnsActionStatus.SKIPPED,
    )
    remove_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and "Remove-DnsServerZone" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(remove_calls), 0)
    return


# ----------------------------------------------------------------------
# list_zones
# ----------------------------------------------------------------------


def test_list_zones_parses_get_dns_server_zone_output():
    """
        list_zones parses a JSON array from
        ``Get-DnsServerZone | ConvertTo-Json``.
    """
    runner = _MockCommandRunner()
    runner.script_results["Get-DnsServerZone "] = RunResult(
        exit_code=0,
        stdout=(
            "["
            "{\"Name\":\"example.com\","
            "\"ZoneType\":\"Primary\","
            "\"ZoneFile\":\"example.com.dns\","
            "\"DynamicUpdate\":\"Secure\","
            "\"IsDsIntegrated\":false},"
            "{\"Name\":\"corp.local\","
            "\"ZoneType\":\"Primary\","
            "\"ZoneFile\":null,"
            "\"DynamicUpdate\":\"Secure\","
            "\"IsDsIntegrated\":true}"
            "]"
        ),
        stderr="",
    )

    manager = WindowsDnsServerManager(
        runner=runner, config=WindowsDnsManagerConfig(),
    )
    rows = manager.list_zones()
    testbase.assert_equal(len(rows), 2)
    by_name = {row.name: row for row in rows}
    testbase.assert_equal(
        isinstance(by_name["example.com"], WindowsDnsZoneInfo), True,
    )
    testbase.assert_equal(
        by_name["example.com"].zone_file, "example.com.dns",
    )
    testbase.assert_equal(by_name["corp.local"].zone_file, None)
    return


# ----------------------------------------------------------------------
# add_record
# ----------------------------------------------------------------------


def test_add_record_invokes_powershell():
    """
        add_record dispatches the ``A`` cmdlet with the
        zone / name / IPv4 parameters.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    _seed_records(runner, zone="example.com", json_text="[]")

    config = WindowsDnsManagerConfig(elevated=True)
    manager = WindowsDnsServerManager(runner=runner, config=config)
    record = WindowsDnsRecordSpec(
        name="www", record_type="A", value="203.0.113.5", ttl=300,
    )
    result = manager.add_record(zone="example.com", record=record)
    testbase.assert_equal(
        result.status, WindowsDnsActionStatus.SUCCESS,
    )

    add_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and "Add-DnsServerResourceRecordA" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(add_calls), 1)
    script = add_calls[0]["args"][4]
    testbase.assert_equal("-ZoneName 'example.com'" in script, True)
    testbase.assert_equal("-Name 'www'" in script, True)
    testbase.assert_equal("-IPv4Address '203.0.113.5'" in script, True)
    testbase.assert_equal("New-TimeSpan -Seconds 300" in script, True)
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_add_record_skipped_when_present():
    """
        add_record returns SKIPPED when an identical record
        is already present.
    """
    runner = _MockCommandRunner()
    _seed_records(
        runner,
        zone="example.com",
        json_text=(
            "["
            "{\"HostName\":\"www\",\"RecordType\":\"A\","
            "\"RecordData\":{\"IPv4Address\":\"203.0.113.5\"}}"
            "]"
        ),
    )

    manager = WindowsDnsServerManager(
        runner=runner, config=WindowsDnsManagerConfig(),
    )
    record = WindowsDnsRecordSpec(
        name="www", record_type="A", value="203.0.113.5",
    )
    result = manager.add_record(zone="example.com", record=record)
    testbase.assert_equal(
        result.status, WindowsDnsActionStatus.SKIPPED,
    )
    add_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and "Add-DnsServerResourceRecordA" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(add_calls), 0)
    return


def test_record_type_dispatch_to_correct_cmdlet():
    """
        add_record selects the ``Add-DnsServerResourceRecord*``
        cmdlet variant from the spec's ``record_type``.
    """
    cases = [
        ("A", "Add-DnsServerResourceRecordA", "-IPv4Address"),
        ("AAAA", "Add-DnsServerResourceRecordAAAA", "-IPv6Address"),
        (
            "CNAME",
            "Add-DnsServerResourceRecordCName",
            "-HostNameAlias",
        ),
        (
            "MX",
            "Add-DnsServerResourceRecordMX",
            "-MailExchange",
        ),
        (
            "TXT",
            "Add-DnsServerResourceRecord -Txt",
            "-DescriptiveText",
        ),
        (
            "SRV",
            "Add-DnsServerResourceRecord -Srv",
            "-DomainName",
        ),
        (
            "PTR",
            "Add-DnsServerResourceRecordPtr",
            "-PtrDomainName",
        ),
        (
            "NS",
            "Add-DnsServerResourceRecord -NS",
            "-NameServer",
        ),
    ]
    for rtype, expected_cmdlet, expected_param in cases:
        record = WindowsDnsRecordSpec(
            name="x",
            record_type=rtype,
            value="value0",
            priority=10,
        )
        script = _build_add_record_command(
            zone="example.com", record=record,
        )
        testbase.assert_equal(expected_cmdlet in script, True)
        testbase.assert_equal(expected_param in script, True)
    return


# ----------------------------------------------------------------------
# remove_record
# ----------------------------------------------------------------------


def test_remove_record_invokes_powershell():
    """
        remove_record dispatches
        ``Remove-DnsServerResourceRecord -ZoneName ... -Name
        ... -RRType ... -Force``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    _seed_records(
        runner,
        zone="example.com",
        json_text=(
            "["
            "{\"HostName\":\"www\",\"RecordType\":\"A\","
            "\"RecordData\":{\"IPv4Address\":\"203.0.113.5\"}}"
            "]"
        ),
    )
    record = WindowsDnsRecordSpec(
        name="www", record_type="A", value="203.0.113.5",
    )

    config = WindowsDnsManagerConfig(elevated=True)
    manager = WindowsDnsServerManager(runner=runner, config=config)
    result = manager.remove_record(zone="example.com", record=record)
    testbase.assert_equal(
        result.status, WindowsDnsActionStatus.SUCCESS,
    )

    remove_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and "Remove-DnsServerResourceRecord" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(remove_calls), 1)
    script = remove_calls[0]["args"][4]
    testbase.assert_equal("-ZoneName 'example.com'" in script, True)
    testbase.assert_equal("-Name 'www'" in script, True)
    testbase.assert_equal("-RRType 'A'" in script, True)
    testbase.assert_equal("-Force" in script, True)
    return


# ----------------------------------------------------------------------
# list_records
# ----------------------------------------------------------------------


def test_list_records_parses_get_resource_record_output():
    """
        list_records parses A, AAAA, CNAME, and TXT records
        from the JSON output.
    """
    runner = _MockCommandRunner()
    json_text = (
        "["
        "{\"HostName\":\"www\",\"RecordType\":\"A\","
        "\"RecordData\":{\"IPv4Address\":\"203.0.113.5\"},"
        "\"TimeToLive\":{\"TotalSeconds\":300}},"
        "{\"HostName\":\"ipv6\",\"RecordType\":\"AAAA\","
        "\"RecordData\":{\"IPv6Address\":\"2001:db8::1\"}},"
        "{\"HostName\":\"alias\",\"RecordType\":\"CNAME\","
        "\"RecordData\":{\"HostNameAlias\":\"www.example.com.\"}},"
        "{\"HostName\":\"@\",\"RecordType\":\"TXT\","
        "\"RecordData\":{\"DescriptiveText\":\"v=spf1 -all\"}}"
        "]"
    )
    _seed_records(runner, zone="example.com", json_text=json_text)

    manager = WindowsDnsServerManager(
        runner=runner, config=WindowsDnsManagerConfig(),
    )
    records = manager.list_records(zone="example.com")
    by_type: dict = {}
    for record in records:
        by_type.setdefault(record.record_type, []).append(record)

    testbase.assert_equal("A" in by_type, True)
    testbase.assert_equal(by_type["A"][0].name, "www")
    testbase.assert_equal(by_type["A"][0].value, "203.0.113.5")
    testbase.assert_equal(by_type["A"][0].ttl, 300)
    testbase.assert_equal("AAAA" in by_type, True)
    testbase.assert_equal(by_type["AAAA"][0].value, "2001:db8::1")
    testbase.assert_equal("CNAME" in by_type, True)
    testbase.assert_equal("TXT" in by_type, True)
    return


# ----------------------------------------------------------------------
# reload_zone / get_zone_status
# ----------------------------------------------------------------------


def test_reload_zone_invokes_powershell():
    """
        reload_zone dispatches ``Sync-DnsServerZone -Name
        '<zone>'`` unconditionally.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    config = WindowsDnsManagerConfig(elevated=True)
    manager = WindowsDnsServerManager(runner=runner, config=config)
    result = manager.reload_zone(name="example.com")
    testbase.assert_equal(
        result.status, WindowsDnsActionStatus.SUCCESS,
    )
    sync_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and "Sync-DnsServerZone" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(sync_calls), 1)
    testbase.assert_equal(sync_calls[0]["elevated"], True)
    return


def test_get_zone_status_parses_get_dns_server_zone_output():
    """
        get_zone_status parses the JSON output of
        ``Get-DnsServerZone -Name '<name>' | ConvertTo-Json``.
    """
    runner = _MockCommandRunner()
    json_text = (
        "{"
        "\"Name\":\"example.com\","
        "\"ZoneType\":\"Primary\","
        "\"SerialNumber\":2024042401,"
        "\"LastZoneUpdate\":\"2024-04-24T12:00:00\","
        "\"MasterServers\":[]"
        "}"
    )
    runner.script_results[
        "Get-DnsServerZone -Name 'example.com' "
    ] = RunResult(exit_code=0, stdout=json_text, stderr="")

    manager = WindowsDnsServerManager(
        runner=runner, config=WindowsDnsManagerConfig(),
    )
    status = manager.get_zone_status(name="example.com")
    testbase.assert_equal(
        isinstance(status, WindowsDnsZoneStatus), True,
    )
    testbase.assert_equal(status.is_loaded, True)
    testbase.assert_equal(status.serial, 2024042401)
    testbase.assert_equal(
        "2024-04-24" in (status.last_loaded or ""), True,
    )
    return


# ----------------------------------------------------------------------
# Elevation / failure
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        With ``elevated=True``, every write dispatch sets
        ``elevated=True`` on the runner call.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    _seed_zone_exists(runner, zone="example.com", exists=False)

    config = WindowsDnsManagerConfig(elevated=True)
    manager = WindowsDnsServerManager(runner=runner, config=config)
    spec = WindowsDnsZoneSpec(name="example.com")
    manager.add_zone(name="example.com", spec=spec)

    add_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 5
            and "Add-DnsServerPrimaryZone" in call["args"][4]
        )
    ]
    testbase.assert_equal(len(add_calls), 1)
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Reads (``Get-DnsServerZone``,
        ``Get-DnsServerResourceRecord``) never elevate.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0,
        stdout="[]",
        stderr="",
    )
    config = WindowsDnsManagerConfig(elevated=True)
    manager = WindowsDnsServerManager(runner=runner, config=config)

    manager.list_zones()
    manager.list_records(zone="example.com")
    manager.get_zone_status(name="example.com")

    for call in runner.calls:
        if len(call["args"]) >= 5:
            script = call["args"][4]
            if (
                script.startswith("Get-DnsServerZone")
                or script.startswith("Get-DnsServerResourceRecord")
            ):
                testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When PowerShell returns non-zero, the manager
        surfaces the exit code and stderr.
    """
    runner = _MockCommandRunner()
    _seed_zone_exists(runner, zone="example.com", exists=False)
    runner.default_result = RunResult(
        exit_code=2,
        stdout="",
        stderr=(
            "Add-DnsServerPrimaryZone : The DnsServer module "
            "is not loaded."
        ),
    )

    manager = WindowsDnsServerManager(
        runner=runner, config=WindowsDnsManagerConfig(),
    )
    spec = WindowsDnsZoneSpec(name="example.com")
    result = manager.add_zone(name="example.com", spec=spec)
    testbase.assert_equal(
        result.status, WindowsDnsActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(
        "DnsServer module" in (result.stderr or ""), True,
    )
    return


def test_list_zones_raises_on_unexpected_failure():
    """
        list_zones raises WindowsDnsQueryError when
        PowerShell exits non-zero with non-empty stderr.
    """
    runner = _MockCommandRunner()
    runner.script_results["Get-DnsServerZone "] = RunResult(
        exit_code=1,
        stdout="",
        stderr="DnsServer module not available",
    )

    manager = WindowsDnsServerManager(
        runner=runner, config=WindowsDnsManagerConfig(),
    )
    raised = False
    try:
        manager.list_zones()
    except WindowsDnsQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_add_zone_supports_replication_scope():
    """
        When ``replication_scope`` is set the renderer emits
        ``-ReplicationScope`` rather than ``-ZoneFile``.
    """
    spec = WindowsDnsZoneSpec(
        name="corp.local",
        zone_type="primary",
        replication_scope="Domain",
    )
    script = _build_add_zone_script(name="corp.local", spec=spec)
    testbase.assert_equal("-ReplicationScope 'Domain'" in script, True)
    testbase.assert_equal("-ZoneFile" in script, False)
    return


def test_add_zone_supports_dynamic_update_secure():
    """
        When ``allow_update`` is True the renderer emits
        ``-DynamicUpdate Secure``.
    """
    spec = WindowsDnsZoneSpec(
        name="example.com",
        zone_type="primary",
        allow_update=True,
    )
    script = _build_add_zone_script(name="example.com", spec=spec)
    testbase.assert_equal("-DynamicUpdate Secure" in script, True)
    return


def test_parse_records_json_handles_dict_root():
    """
        ConvertTo-Json emits a single object (not an array)
        when the pipeline produces exactly one row; the
        parser handles both shapes.
    """
    text = (
        "{\"HostName\":\"www\",\"RecordType\":\"A\","
        "\"RecordData\":{\"IPv4Address\":\"203.0.113.5\"}}"
    )
    rows = _parse_records_json(text=text)
    testbase.assert_equal(len(rows), 1)
    testbase.assert_equal(
        isinstance(rows[0], WindowsDnsRecordInfo), True,
    )
    testbase.assert_equal(rows[0].value, "203.0.113.5")
    return


def test_parse_zones_json_handles_empty_input():
    """
        Empty input parses to an empty list without raising.
    """
    rows = _parse_zones_json(text="")
    testbase.assert_equal(len(rows), 0)
    return


def test_parse_zone_status_json_unloaded_when_empty():
    """
        Empty input parses to is_loaded=False.
    """
    parsed = _parse_zone_status_json(text="")
    testbase.assert_equal(parsed["is_loaded"], False)
    return
