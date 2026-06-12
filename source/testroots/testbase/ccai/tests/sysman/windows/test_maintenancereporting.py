"""
    Tests for :class:`WindowsMaintenanceReporter`.

    All tests use a ``_MockCommandRunner`` to capture
    ``New-Item`` / ``Set-Content`` PowerShell calls.  No real
    filesystem is touched.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.maintenancereporting import (
    WindowsActionRecord,
    WindowsActionStatus,
    WindowsMaintenanceReporter,
    WindowsMaintenanceReporterConfig,
    WindowsReportFormat,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal :class:`ICommandRunner` stand-in.  Records every call
        and returns a configurable default result.
    """

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
        record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
        }
        self.calls.append(record)

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


def _make_reporter(
    *,
    runner: Optional[_MockCommandRunner] = None,
    config: Optional[WindowsMaintenanceReporterConfig] = None,
) -> tuple:
    """
        Build a reporter + runner pair for use in tests.
    """
    if runner is None:
        runner = _MockCommandRunner()
    if config is None:
        config = WindowsMaintenanceReporterConfig()
    reporter = WindowsMaintenanceReporter(runner=runner, config=config)
    rtnval = (reporter, runner)
    return rtnval


def test_begin_session_creates_session():
    """
        :meth:`begin_session` produces a session with the supplied id
        and an empty action list.
    """
    reporter, runner = _make_reporter()
    session = reporter.begin_session(session_id="windows-001")
    testbase.assert_equal(session.run_id, "windows-001")
    testbase.assert_true(reporter.session is not None)
    testbase.assert_equal(len(reporter.actions), 0)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_record_action_appends_to_actions():
    """
        :meth:`record_action` stores the supplied record on the
        actions list in insertion order.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="windows-002")
    reporter.record_action(WindowsActionRecord(
        feature="package-management",
        operation="install",
        status=WindowsActionStatus.SUCCESS,
    ))
    reporter.record_action(WindowsActionRecord(
        feature="service-management",
        operation="restart",
        status=WindowsActionStatus.SKIPPED,
    ))
    actions = reporter.actions
    testbase.assert_equal(len(actions), 2)
    testbase.assert_equal(actions[0].operation, "install")
    testbase.assert_equal(actions[1].status, WindowsActionStatus.SKIPPED)
    return


def test_record_query_appends_to_queries():
    """
        :meth:`record_query` stores records on the queries list.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="windows-003")
    reporter.record_query(WindowsActionRecord(
        feature="package-management",
        operation="list_installed",
        status=WindowsActionStatus.SUCCESS,
    ))
    queries = reporter.queries
    testbase.assert_equal(len(queries), 1)
    testbase.assert_equal(queries[0].operation, "list_installed")
    testbase.assert_equal(len(reporter.actions), 0)
    return


def test_record_error_captures_traceback():
    """
        :meth:`record_error` captures the type, message, traceback,
        and appends a FAILED record to the actions list.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="windows-004")
    try:
        raise ValueError("nope")
    except ValueError as exc:
        event = reporter.record_error(
            error=exc,
            context={"feature": "kernel-parameters", "operation": "apply"},
        )
    testbase.assert_equal(event["type"], "ValueError")
    testbase.assert_equal(event["message"], "nope")
    testbase.assert_true("ValueError" in event["traceback"])
    testbase.assert_equal(len(reporter.actions), 1)
    testbase.assert_equal(reporter.actions[0].status, WindowsActionStatus.FAILED)
    return


def test_end_session_sets_ended_at_and_status():
    """
        :meth:`end_session` stamps ``ended_at`` and recomputes
        ``status`` based on the recorded actions.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="windows-005")
    reporter.record_action(WindowsActionRecord(
        feature="f", operation="op", status=WindowsActionStatus.SUCCESS,
    ))
    reporter.record_action(WindowsActionRecord(
        feature="f", operation="op", status=WindowsActionStatus.FAILED,
    ))
    finalised = reporter.end_session()
    testbase.assert_true(finalised.ended_at is not None)
    testbase.assert_equal(finalised.status, WindowsActionStatus.FAILED)
    return


def test_emit_json_report_writes_file():
    """
        :meth:`emit_report` with JSON format invokes a PowerShell
        ``New-Item`` and ``Set-Content`` once each.
    """
    reporter, runner = _make_reporter()
    reporter.begin_session(session_id="windows-006")
    reporter.record_action(WindowsActionRecord(
        feature="package-management",
        operation="install",
        status=WindowsActionStatus.SUCCESS,
        detail="installed nginx",
    ))
    reporter.end_session()
    write_result = reporter.emit_report(format=WindowsReportFormat.JSON)
    testbase.assert_true(write_result.success)
    testbase.assert_true(write_result.path.endswith("\\report.json"))
    testbase.assert_equal(len(runner.calls), 2)
    mkdir_argv = runner.calls[0]["args"]
    testbase.assert_true("New-Item" in mkdir_argv[-1])
    testbase.assert_true("Directory" in mkdir_argv[-1])
    write_argv = runner.calls[1]["args"]
    testbase.assert_true("Set-Content" in write_argv[-1])
    payload = json.loads(runner.calls[1]["stdin"])
    testbase.assert_equal(payload["session"]["run_id"], "windows-006")
    testbase.assert_equal(len(payload["actions"]), 1)
    testbase.assert_equal(payload["counts"]["succeeded"], 1)
    return


def test_emit_markdown_report_writes_file():
    """
        :meth:`emit_report` with MARKDOWN format produces a file with
        the expected ``.md`` extension and ``# Windows Maintenance
        Report`` heading.
    """
    reporter, runner = _make_reporter()
    reporter.begin_session(session_id="windows-007")
    reporter.record_action(WindowsActionRecord(
        feature="firewall-management",
        operation="add_rule",
        status=WindowsActionStatus.SUCCESS,
    ))
    reporter.end_session()
    write_result = reporter.emit_report(format=WindowsReportFormat.MARKDOWN)
    testbase.assert_true(write_result.success)
    testbase.assert_true(write_result.path.endswith("\\report.md"))
    body = runner.calls[1]["stdin"]
    testbase.assert_true(body.startswith("# Windows Maintenance Report"))
    testbase.assert_true("## Summary" in body)
    testbase.assert_true("## Actions" in body)
    return


def test_emit_yaml_report_writes_file():
    """
        :meth:`emit_report` with YAML format produces a file with the
        expected ``.yaml`` extension.
    """
    reporter, runner = _make_reporter()
    reporter.begin_session(session_id="windows-008")
    reporter.end_session()
    write_result = reporter.emit_report(format=WindowsReportFormat.YAML)
    testbase.assert_true(write_result.success)
    testbase.assert_true(write_result.path.endswith("\\report.yaml"))
    body = runner.calls[1]["stdin"]
    testbase.assert_true("windows-008" in body)
    return


def test_session_counts_aggregate_correctly():
    """
        :meth:`summary_counts` tallies actions by status accurately.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="windows-009")
    statuses = [
        WindowsActionStatus.SUCCESS,
        WindowsActionStatus.SUCCESS,
        WindowsActionStatus.FAILED,
        WindowsActionStatus.SKIPPED,
        WindowsActionStatus.DRY_RUN,
    ]
    for s in statuses:
        reporter.record_action(WindowsActionRecord(
            feature="f", operation="op", status=s,
        ))
    counts = reporter.summary_counts()
    testbase.assert_equal(counts.total, 5)
    testbase.assert_equal(counts.succeeded, 2)
    testbase.assert_equal(counts.failed, 1)
    testbase.assert_equal(counts.skipped, 1)
    testbase.assert_equal(counts.dry_run, 1)
    return


def test_emit_before_end_session_raises():
    """
        Calling :meth:`emit_report` before :meth:`end_session` raises
        :class:`RuntimeError`.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="windows-010")
    raised = False
    try:
        reporter.emit_report(format=WindowsReportFormat.JSON)
    except RuntimeError:
        raised = True
    testbase.assert_true(raised)
    return


def test_emit_dry_run_skips_runner():
    """
        :meth:`emit_report` with ``config.dry_run=True`` returns a
        ``dry_run=True`` result and does not invoke the runner.
    """
    reporter, runner = _make_reporter(
        config=WindowsMaintenanceReporterConfig(dry_run=True),
    )
    reporter.begin_session(session_id="windows-011")
    reporter.end_session()
    write_result = reporter.emit_report(format=WindowsReportFormat.JSON)
    testbase.assert_true(write_result.success)
    testbase.assert_true(write_result.dry_run)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_emit_failure_when_set_content_fails():
    """
        Runner failures during ``Set-Content`` surface as
        ``WindowsReportWriteResult(success=False)`` with an error
        message.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=1,
            stdout="",
            stderr="permission denied",
        ),
    )
    reporter, _ignored = _make_reporter(runner=runner)
    reporter.begin_session(session_id="windows-012")
    reporter.end_session()
    write_result = reporter.emit_report(format=WindowsReportFormat.JSON)
    testbase.assert_false(write_result.success)
    testbase.assert_true(write_result.error is not None)
    testbase.assert_true("permission denied" in write_result.error)
    return


def test_default_session_path_uses_output_dir():
    """
        :meth:`default_session_path` joins the configured output
        directory with the session id and uses backslash separators.
    """
    reporter, _runner = _make_reporter(
        config=WindowsMaintenanceReporterConfig(
            output_dir="C:\\ProgramData\\CodeCraftAI\\Maintenance",
        ),
    )
    path = reporter.default_session_path(session_id="windows-013")
    testbase.assert_equal(
        path,
        "C:\\ProgramData\\CodeCraftAI\\Maintenance\\windows-013\\",
    )
    return


def test_default_session_path_expands_programdata_token():
    """
        ``%ProgramData%`` in the configured output dir must be
        expanded -- to the real env var when set, otherwise to the
        conventional ``C:\\ProgramData`` fallback.
    """
    reporter, _runner = _make_reporter()
    path = reporter.default_session_path(session_id="windows-014")
    testbase.assert_false("%ProgramData%" in path)
    testbase.assert_true(path.endswith("\\windows-014\\"))
    testbase.assert_true("CodeCraftAI" in path)
    testbase.assert_true("Maintenance" in path)
    return


def test_record_action_without_session_raises():
    """
        :meth:`record_action` before :meth:`begin_session` raises
        :class:`RuntimeError`.
    """
    reporter, _runner = _make_reporter()
    raised = False
    try:
        reporter.record_action(WindowsActionRecord(
            feature="f", operation="op", status=WindowsActionStatus.SUCCESS,
        ))
    except RuntimeError:
        raised = True
    testbase.assert_true(raised)
    return


def test_actions_by_status_filter():
    """
        :meth:`actions_by_status` returns only matching records in
        insertion order.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="windows-015")
    reporter.record_action(WindowsActionRecord(
        feature="f", operation="ok-1", status=WindowsActionStatus.SUCCESS,
    ))
    reporter.record_action(WindowsActionRecord(
        feature="f", operation="bad-1", status=WindowsActionStatus.FAILED,
    ))
    reporter.record_action(WindowsActionRecord(
        feature="f", operation="ok-2", status=WindowsActionStatus.SUCCESS,
    ))
    successes = reporter.actions_by_status(WindowsActionStatus.SUCCESS)
    failures = reporter.actions_by_status(WindowsActionStatus.FAILED)
    testbase.assert_equal(len(successes), 2)
    testbase.assert_equal(successes[0].operation, "ok-1")
    testbase.assert_equal(successes[1].operation, "ok-2")
    testbase.assert_equal(len(failures), 1)
    testbase.assert_equal(failures[0].operation, "bad-1")
    return


def test_actions_by_feature_filter():
    """
        :meth:`actions_by_feature` returns only records emitted by
        the supplied feature.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="windows-016")
    reporter.record_action(WindowsActionRecord(
        feature="package-management", operation="install",
        status=WindowsActionStatus.SUCCESS,
    ))
    reporter.record_action(WindowsActionRecord(
        feature="firewall-management", operation="add_rule",
        status=WindowsActionStatus.SUCCESS,
    ))
    pkg_records = reporter.actions_by_feature(feature="package-management")
    testbase.assert_equal(len(pkg_records), 1)
    testbase.assert_equal(pkg_records[0].operation, "install")
    return


def test_emit_unknown_format_returns_error_result():
    """
        :meth:`emit_report` with a non-:class:`WindowsReportFormat`
        argument returns ``success=False`` without invoking the
        runner.
    """
    reporter, runner = _make_reporter()
    reporter.begin_session(session_id="windows-017")
    reporter.end_session()
    write_result = reporter.emit_report(format="bogus", path="C:\\tmp\\x")
    testbase.assert_false(write_result.success)
    testbase.assert_true(write_result.error is not None)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_to_text_contains_key_fields():
    """
        :meth:`to_text` includes the run id, host, and a per-action
        line.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="windows-018")
    reporter.record_action(WindowsActionRecord(
        feature="package-management",
        operation="install",
        status=WindowsActionStatus.SUCCESS,
        detail="installed nginx",
    ))
    body = reporter.to_text()
    testbase.assert_true("windows-018" in body)
    testbase.assert_true("WINDOWS MAINTENANCE REPORT" in body)
    testbase.assert_true("install" in body)
    testbase.assert_true("installed nginx" in body)
    return
