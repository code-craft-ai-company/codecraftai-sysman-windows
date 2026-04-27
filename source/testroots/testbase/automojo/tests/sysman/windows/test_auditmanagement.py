"""
    Tests for :class:`WindowsAuditManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``auditpol`` / ``wevtutil`` / PowerShell process is
    invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.auditmanagement import (
    WindowsAuditActionStatus,
    WindowsAuditEvent,
    WindowsAuditEventQuery,
    WindowsAuditManager,
    WindowsAuditManagerConfig,
    WindowsAuditQueryError,
    WindowsAuditRuleInfo,
    WindowsAuditRuleSpec,
    WindowsAuditStatus,
    _build_auditpol_set,
    _build_get_winevent_script,
    _normalise_inclusion_setting,
    _parse_auditpol_csv,
    _parse_winevent_json,
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


def _seed_auditpol_csv(
    runner: _MockCommandRunner,
    *,
    rows: list,
) -> None:
    """
        Pre-seed the runner so ``auditpol /get /category:* /r``
        returns a CSV body assembled from ``rows`` (one tuple of
        ``(category, subcategory, inclusion)`` per row).
    """
    header = (
        "Machine Name,Policy Target,Category,Subcategory,"
        "Subcategory GUID,Inclusion Setting,Exclusion Setting"
    )
    body_lines = [header]
    for row in rows:
        category, subcategory, inclusion = row
        body_lines.append(
            "WIN-HOST,System,{cat},{sub},{guid},{incl},".format(
                cat=category,
                sub=subcategory,
                guid="{0CCE9210-69AE-11D9-BED3-505054503030}",
                incl=inclusion,
            )
        )
    body = "\n".join(body_lines) + "\n"
    runner.results[("auditpol", "/get", "/category:*", "/r")] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )
    return


# ----------------------------------------------------------------------
# add_rule
# ----------------------------------------------------------------------


def test_add_rule_invokes_auditpol():
    """
        add_rule dispatches the canonical ``auditpol /set`` argv.
    """
    runner = _MockCommandRunner()
    _seed_auditpol_csv(
        runner=runner,
        rows=[("Logon/Logoff", "Logon", "No Auditing")],
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(elevated=True),
    )
    spec = WindowsAuditRuleSpec(
        subcategory="Logon", success=True, failure=True,
    )
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, WindowsAuditActionStatus.SUCCESS)
    set_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["auditpol", "/set"]
    ]
    testbase.assert_equal(len(set_calls), 1)
    testbase.assert_equal(
        set_calls[0]["args"],
        [
            "auditpol", "/set",
            "/subcategory:Logon",
            "/success:enable",
            "/failure:enable",
        ],
    )
    testbase.assert_equal(set_calls[0]["elevated"], True)
    return


def test_add_rule_skipped_when_present():
    """
        add_rule returns SKIPPED when the existing inclusion setting
        already matches the desired (success, failure) tuple.
    """
    runner = _MockCommandRunner()
    _seed_auditpol_csv(
        runner=runner,
        rows=[("Logon/Logoff", "Logon", "Success and Failure")],
    )
    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(),
    )
    spec = WindowsAuditRuleSpec(
        subcategory="Logon", success=True, failure=True,
    )
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, WindowsAuditActionStatus.SKIPPED)
    set_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["auditpol", "/set"]
    ]
    testbase.assert_equal(set_calls, [])
    return


def test_add_rule_dry_run_short_circuits():
    """
        add_rule with dry_run=True returns DRY_RUN and never invokes
        the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(dry_run=True),
    )
    spec = WindowsAuditRuleSpec(subcategory="Logon")
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, WindowsAuditActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# remove_rule
# ----------------------------------------------------------------------


def test_remove_rule_invokes_auditpol():
    """
        remove_rule dispatches ``auditpol /set`` with both flags
        disabled.
    """
    runner = _MockCommandRunner()
    _seed_auditpol_csv(
        runner=runner,
        rows=[("Logon/Logoff", "Logon", "Success and Failure")],
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(),
    )
    result = manager.remove_rule(rule_id="Logon")

    testbase.assert_equal(result.status, WindowsAuditActionStatus.SUCCESS)
    set_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["auditpol", "/set"]
    ]
    testbase.assert_equal(len(set_calls), 1)
    testbase.assert_equal(
        set_calls[0]["args"],
        [
            "auditpol", "/set",
            "/subcategory:Logon",
            "/success:disable",
            "/failure:disable",
        ],
    )
    return


def test_remove_rule_skipped_when_absent():
    """
        remove_rule returns SKIPPED when the subcategory is already
        ``No Auditing``.
    """
    runner = _MockCommandRunner()
    _seed_auditpol_csv(
        runner=runner,
        rows=[("Logon/Logoff", "Logon", "No Auditing")],
    )
    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(),
    )
    result = manager.remove_rule(rule_id="Logon")

    testbase.assert_equal(result.status, WindowsAuditActionStatus.SKIPPED)
    set_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["auditpol", "/set"]
    ]
    testbase.assert_equal(set_calls, [])
    return


# ----------------------------------------------------------------------
# list_rules
# ----------------------------------------------------------------------


def test_list_rules_parses_output():
    """
        list_rules parses the CSV output of
        ``auditpol /get /category:* /r`` into one
        WindowsAuditRuleInfo per subcategory.
    """
    runner = _MockCommandRunner()
    _seed_auditpol_csv(
        runner=runner,
        rows=[
            ("Logon/Logoff", "Logon", "Success and Failure"),
            ("Object Access", "File System", "Success"),
            ("Detailed Tracking", "Process Creation", "No Auditing"),
        ],
    )

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(),
    )
    rules = manager.list_rules()

    testbase.assert_equal(len(rules), 3)
    by_name = {r.subcategory: r for r in rules}
    testbase.assert_equal(by_name["Logon"].success, True)
    testbase.assert_equal(by_name["Logon"].failure, True)
    testbase.assert_equal(by_name["File System"].success, True)
    testbase.assert_equal(by_name["File System"].failure, False)
    testbase.assert_equal(by_name["Process Creation"].success, False)
    testbase.assert_equal(by_name["Process Creation"].failure, False)
    return


# ----------------------------------------------------------------------
# enable / disable
# ----------------------------------------------------------------------


def test_enable_auditing_invokes_wevtutil():
    """
        enable_auditing dispatches ``wevtutil sl Security /e:true``.
    """
    runner = _MockCommandRunner()
    runner.results[("wevtutil", "gl", "Security")] = RunResult(
        exit_code=0,
        stdout="name: Security\nenabled: false\n",
        stderr="",
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(elevated=True),
    )
    result = manager.enable_auditing()

    testbase.assert_equal(result.status, WindowsAuditActionStatus.SUCCESS)
    sl_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["wevtutil", "sl"]
    ]
    testbase.assert_equal(len(sl_calls), 1)
    testbase.assert_equal(
        sl_calls[0]["args"],
        ["wevtutil", "sl", "Security", "/e:true"],
    )
    testbase.assert_equal(sl_calls[0]["elevated"], True)
    return


def test_enable_auditing_skipped_when_enabled():
    """
        enable_auditing returns SKIPPED when ``wevtutil gl`` reports
        the channel as already enabled.
    """
    runner = _MockCommandRunner()
    runner.results[("wevtutil", "gl", "Security")] = RunResult(
        exit_code=0,
        stdout="name: Security\nenabled: true\n",
        stderr="",
    )

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(),
    )
    result = manager.enable_auditing()

    testbase.assert_equal(result.status, WindowsAuditActionStatus.SKIPPED)
    sl_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["wevtutil", "sl"]
    ]
    testbase.assert_equal(sl_calls, [])
    return


def test_disable_auditing_requires_allow_destructive():
    """
        disable_auditing returns FAILED when allow_destructive is
        False and never invokes ``auditpol /clear``.
    """
    runner = _MockCommandRunner()
    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(),
    )
    result = manager.disable_auditing()

    testbase.assert_equal(result.status, WindowsAuditActionStatus.FAILED)
    testbase.assert_true("allow_destructive" in (result.message or ""))
    clear_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["auditpol", "/clear"]
    ]
    testbase.assert_equal(clear_calls, [])
    return


def test_disable_auditing_invokes_auditpol_when_destructive_allowed():
    """
        With ``allow_destructive=True``, disable_auditing dispatches
        ``auditpol /clear /y``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = WindowsAuditManager(
        runner=runner,
        config=WindowsAuditManagerConfig(
            allow_destructive=True, elevated=True,
        ),
    )
    result = manager.disable_auditing()

    testbase.assert_equal(result.status, WindowsAuditActionStatus.SUCCESS)
    clear_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["auditpol", "/clear"]
    ]
    testbase.assert_equal(len(clear_calls), 1)
    testbase.assert_equal(
        clear_calls[0]["args"], ["auditpol", "/clear", "/y"],
    )
    testbase.assert_equal(clear_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# get_audit_status
# ----------------------------------------------------------------------


def test_get_audit_status_parses_auditpol_output():
    """
        get_audit_status rolls the CSV into a counts dictionary and
        flips ``enabled`` when at least one subcategory is non-empty.
    """
    runner = _MockCommandRunner()
    _seed_auditpol_csv(
        runner=runner,
        rows=[
            ("Logon/Logoff", "Logon", "Success and Failure"),
            ("Object Access", "File System", "No Auditing"),
            ("Detailed Tracking", "Process Creation", "No Auditing"),
        ],
    )

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(),
    )
    status = manager.get_audit_status()

    testbase.assert_equal(isinstance(status, WindowsAuditStatus), True)
    testbase.assert_equal(status.enabled, True)
    testbase.assert_equal(status.running, True)
    testbase.assert_equal(status.rule_count, 3)
    testbase.assert_equal(
        status.subcategory_counts.get("Success and Failure"), 1,
    )
    testbase.assert_equal(
        status.subcategory_counts.get("No Auditing"), 2,
    )
    return


# ----------------------------------------------------------------------
# search_events
# ----------------------------------------------------------------------


def test_search_events_invokes_get_winevent_with_query():
    """
        search_events builds a Get-WinEvent pipeline against the
        Security channel and dispatches via PowerShell.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="[]", stderr="")

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(),
    )
    query = WindowsAuditEventQuery(event_id=4624, limit=10)
    manager.search_events(query=query)

    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"][:4],
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"],
    )
    script = last_call["args"][4]
    testbase.assert_true("Get-WinEvent" in script)
    testbase.assert_true("LogName='Security'" in script)
    testbase.assert_true("Id=4624" in script)
    testbase.assert_true("MaxEvents 10" in script)
    return


def test_search_events_parses_winevent_json():
    """
        search_events parses a JSON array of records into
        WindowsAuditEvent rows.
    """
    runner = _MockCommandRunner()
    payload = json.dumps([
        {
            "TimeCreated": "2024-04-22T10:00:00",
            "Id": 4624,
            "LogName": "Security",
            "LevelDisplayName": "Information",
            "Message": "An account was successfully logged on.",
        },
        {
            "TimeCreated": "2024-04-22T10:00:01",
            "Id": 4625,
            "LogName": "Security",
            "LevelDisplayName": "Information",
            "Message": "An account failed to log on.",
        },
    ])
    runner.default_result = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(),
    )
    events = manager.search_events(
        query=WindowsAuditEventQuery(limit=5),
    )

    testbase.assert_equal(len(events), 2)
    testbase.assert_equal(events[0].event_id, 4624)
    testbase.assert_equal(events[1].event_id, 4625)
    testbase.assert_equal(events[0].category, "Security")
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Writes pass elevated=True to the runner when the config
        opts in.
    """
    runner = _MockCommandRunner()
    _seed_auditpol_csv(
        runner=runner,
        rows=[("Logon/Logoff", "Logon", "No Auditing")],
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(elevated=True),
    )
    spec = WindowsAuditRuleSpec(
        subcategory="Logon", success=True, failure=True,
    )
    manager.add_rule(spec=spec)

    set_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["auditpol", "/set"]
    ]
    testbase.assert_equal(len(set_calls), 1)
    testbase.assert_equal(set_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Reads (list_rules / get_audit_status) never pass
        elevated=True even when the config opts in.

        Note: search_events explicitly elevates because the Windows
        Security channel requires Administrator to read.  This test
        only covers the auditpol-backed reads.
    """
    runner = _MockCommandRunner()
    _seed_auditpol_csv(
        runner=runner,
        rows=[("Logon/Logoff", "Logon", "Success")],
    )

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(elevated=True),
    )
    manager.list_rules()
    manager.get_audit_status()

    elevated_reads = [
        c for c in runner.calls
        if c["elevated"] is True
        and c["args"][:2] == ["auditpol", "/get"]
    ]
    testbase.assert_equal(elevated_reads, [])
    return


def test_failed_command_surfaces_stderr():
    """
        When auditpol exits non-zero, the result reports FAILED and
        surfaces stderr.
    """
    runner = _MockCommandRunner()
    _seed_auditpol_csv(
        runner=runner,
        rows=[("Logon/Logoff", "Logon", "No Auditing")],
    )
    runner.results[(
        "auditpol", "/set",
        "/subcategory:Logon", "/success:enable", "/failure:enable",
    )] = RunResult(exit_code=1, stdout="", stderr="boom")

    manager = WindowsAuditManager(
        runner=runner, config=WindowsAuditManagerConfig(),
    )
    spec = WindowsAuditRuleSpec(
        subcategory="Logon", success=True, failure=True,
    )
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, WindowsAuditActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "boom")
    testbase.assert_equal(result.exit_code, 1)
    return


# ----------------------------------------------------------------------
# Pure-function tests (renderers / parsers)
# ----------------------------------------------------------------------


def test_build_auditpol_set_renders_argv():
    """
        _build_auditpol_set emits the canonical argv.
    """
    spec = WindowsAuditRuleSpec(
        subcategory="Logon", success=True, failure=False,
    )
    rendered = _build_auditpol_set(spec=spec)
    testbase.assert_equal(
        rendered,
        [
            "auditpol", "/set",
            "/subcategory:Logon",
            "/success:enable",
            "/failure:disable",
        ],
    )
    return


def test_normalise_inclusion_setting():
    """
        _normalise_inclusion_setting maps every documented setting
        to the right (success, failure) tuple.
    """
    testbase.assert_equal(
        _normalise_inclusion_setting(value="No Auditing"), (False, False),
    )
    testbase.assert_equal(
        _normalise_inclusion_setting(value="Success"), (True, False),
    )
    testbase.assert_equal(
        _normalise_inclusion_setting(value="Failure"), (False, True),
    )
    testbase.assert_equal(
        _normalise_inclusion_setting(value="Success and Failure"),
        (True, True),
    )
    return


def test_parse_auditpol_csv_handles_full_file():
    """
        _parse_auditpol_csv parses every non-header row.
    """
    text = (
        "Machine Name,Policy Target,Subcategory,Subcategory GUID,"
        "Inclusion Setting,Exclusion Setting\r\n"
        "WIN-HOST,System,Logon,{GUID},Success and Failure,\r\n"
        "WIN-HOST,System,File System,{GUID},No Auditing,\r\n"
    )
    rules = _parse_auditpol_csv(text=text)
    testbase.assert_equal(len(rules), 2)
    testbase.assert_equal(rules[0].subcategory, "Logon")
    testbase.assert_equal(rules[0].success, True)
    testbase.assert_equal(rules[0].failure, True)
    testbase.assert_equal(rules[1].subcategory, "File System")
    testbase.assert_equal(rules[1].success, False)
    return


def test_build_get_winevent_script_with_raw_filter():
    """
        _build_get_winevent_script emits the verbatim raw_filter
        when supplied.
    """
    query = WindowsAuditEventQuery(raw_filter="-FilterXPath '*[System[(EventID=4624)]]'")
    script = _build_get_winevent_script(channel="Security", query=query)
    testbase.assert_true("-FilterXPath" in script)
    testbase.assert_true("EventID=4624" in script)
    return


def test_parse_winevent_json_respects_keyword_and_limit():
    """
        _parse_winevent_json filters by keyword and respects limit.
    """
    payload = json.dumps([
        {"Id": 1, "Message": "alpha"},
        {"Id": 2, "Message": "beta"},
        {"Id": 3, "Message": "alpha-gamma"},
    ])
    events = _parse_winevent_json(
        text=payload, keyword="alpha", limit=5,
    )
    testbase.assert_equal(len(events), 2)
    testbase.assert_equal(events[0].event_id, 1)
    testbase.assert_equal(events[1].event_id, 3)
    return
