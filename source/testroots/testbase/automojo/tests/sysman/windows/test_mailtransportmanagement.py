"""
    Tests for :class:`WindowsMailTransportManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv tuple.  No live PowerShell process is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.mailtransportmanagement import (
    WindowsMailTransportManager,
    WindowsMtaActionStatus,
    WindowsMtaConfig,
    WindowsMtaKind,
    WindowsMtaManagerConfig,
    WindowsMtaQueueStatus,
    _build_iissmtp_get_script,
    _build_iissmtp_queue_status_script,
    _build_iissmtp_set_script,
    _build_send_mailmessage_script,
    _parse_iissmtp_get_output,
    _parse_iissmtp_queue_status_output,
)


# ----------------------------------------------------------------------
# Mock runner + helpers
# ----------------------------------------------------------------------


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
        Build a failure :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


# ----------------------------------------------------------------------
# get_mta_config
# ----------------------------------------------------------------------


def test_get_mta_config_synthetic_when_mta_kind_none():
    """
        get_mta_config returns a synthetic empty config with a
        descriptive ``detail`` when ``mta_kind=NONE``.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(),
    )
    config = manager.get_mta_config()

    testbase.assert_equal(config.mta_kind, WindowsMtaKind.NONE)
    testbase.assert_equal(config.myhostname is None, True)
    testbase.assert_equal(
        "no built-in MTA" in config.detail, True,
    )
    # No PowerShell calls required when mta_kind=NONE.
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_get_mta_config_parses_iis_smtp_output():
    """
        get_mta_config parses the IIS SMTP JSON output into a
        :class:`WindowsMtaConfig` when ``mta_kind=IIS_SMTP``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout=(
            '{"FullyQualifiedDomainName":"mail.example.com",'
            '"SmartHost":"relay.example.com",'
            '"MaxMessageSize":10240000,'
            '"ServerBindings":":25:"}'
        ),
    )

    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(mta_kind=WindowsMtaKind.IIS_SMTP),
    )
    config = manager.get_mta_config()

    testbase.assert_equal(config.mta_kind, WindowsMtaKind.IIS_SMTP)
    testbase.assert_equal(config.myhostname, "mail.example.com")
    testbase.assert_equal(config.relayhost, "relay.example.com")
    testbase.assert_equal(config.message_size_limit, 10240000)
    testbase.assert_equal(config.mailbox_size_limit, 10240000)
    testbase.assert_equal(
        config.extras["ServerBindings"], ":25:",
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


# ----------------------------------------------------------------------
# set_mta_config
# ----------------------------------------------------------------------


def test_set_mta_config_failed_when_mta_kind_none():
    """
        set_mta_config returns FAILED with the stable
        ``windows_no_built_in_mta`` detail when ``mta_kind=NONE``.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(),
    )
    result = manager.set_mta_config(
        spec=WindowsMtaConfig(myhostname="mail.example.com"),
    )

    testbase.assert_equal(result.status, WindowsMtaActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "windows_no_built_in_mta")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_set_mta_config_invokes_powershell_for_iis_smtp():
    """
        set_mta_config dispatches a PowerShell pipeline that sets
        IIS SMTP properties when ``mta_kind=IIS_SMTP``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP, elevated=True,
        ),
    )
    result = manager.set_mta_config(
        spec=WindowsMtaConfig(
            myhostname="mail.example.com",
            relayhost="relay.example.com",
            message_size_limit=10240000,
        ),
    )

    testbase.assert_equal(result.status, WindowsMtaActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    call = runner.calls[0]
    testbase.assert_equal(call["args"][0], "powershell.exe")
    testbase.assert_equal(call["elevated"], True)
    script = call["args"][-1]
    testbase.assert_equal(
        "FullyQualifiedDomainName = 'mail.example.com'" in script, True,
    )
    testbase.assert_equal(
        "SmartHost = 'relay.example.com'" in script, True,
    )
    testbase.assert_equal("MaxMessageSize = 10240000" in script, True)
    testbase.assert_equal("$s.Put_()" in script, True)
    return


def test_set_mta_config_skipped_per_setting_when_match_iis_smtp():
    """
        For ``mta_kind=IIS_SMTP`` the pilot does not perform a
        per-setting probe before dispatch — the WMI surface does not
        offer cheap per-property comparisons.  This test asserts the
        documented best-effort behaviour: ``set_mta_config`` always
        dispatches when ``mta_kind=IIS_SMTP``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP, elevated=True,
        ),
    )
    result = manager.set_mta_config(
        spec=WindowsMtaConfig(myhostname="mail.example.com"),
    )

    # Best-effort: dispatches unconditionally when IIS_SMTP.
    testbase.assert_equal(result.status, WindowsMtaActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    return


def test_set_mta_config_dry_run_short_circuits():
    """
        set_mta_config returns DRY_RUN without invoking the runner
        when ``dry_run=True`` and ``mta_kind=IIS_SMTP``.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP, dry_run=True,
        ),
    )
    result = manager.set_mta_config(
        spec=WindowsMtaConfig(myhostname="mail.example.com"),
    )

    testbase.assert_equal(result.status, WindowsMtaActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# Alias verbs
# ----------------------------------------------------------------------


def test_add_alias_failed_when_mta_kind_none():
    """
        add_alias returns FAILED with ``windows_no_built_in_mta`` when
        ``mta_kind=NONE``.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(),
    )
    result = manager.add_alias(
        local="alice", remote="alice@example.com",
    )

    testbase.assert_equal(result.status, WindowsMtaActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "windows_no_built_in_mta")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_add_alias_failed_iis_smtp_unsupported():
    """
        add_alias returns FAILED with a stable ``aliases_unsupported``
        detail when ``mta_kind=IIS_SMTP`` (IIS SMTP has no native
        aliases concept).
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP,
        ),
    )
    result = manager.add_alias(
        local="alice", remote="alice@example.com",
    )

    testbase.assert_equal(result.status, WindowsMtaActionStatus.FAILED)
    testbase.assert_equal(
        result.stderr, "windows_iis_smtp_aliases_unsupported",
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_remove_alias_failed_when_mta_kind_none():
    """
        remove_alias returns FAILED with ``windows_no_built_in_mta``
        when ``mta_kind=NONE``.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(),
    )
    result = manager.remove_alias(local="alice")

    testbase.assert_equal(result.status, WindowsMtaActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "windows_no_built_in_mta")
    return


def test_list_aliases_returns_empty_list():
    """
        list_aliases returns an empty list on every Windows MTA kind.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(),
    )
    aliases = manager.list_aliases()

    testbase.assert_equal(aliases, [])
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# test_mail_delivery
# ----------------------------------------------------------------------


def test_test_mail_delivery_failed_when_mta_kind_none():
    """
        test_mail_delivery returns FAILED with
        ``windows_no_built_in_mta`` when ``mta_kind=NONE``.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(),
    )
    result = manager.test_mail_delivery(
        to="ops@example.com",
        from_addr="probe@example.com",
        subject="probe",
    )

    testbase.assert_equal(result.status, WindowsMtaActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "windows_no_built_in_mta")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_test_mail_delivery_invokes_send_mailmessage_for_iis_smtp():
    """
        test_mail_delivery dispatches a ``Send-MailMessage`` PowerShell
        pipeline when ``mta_kind=IIS_SMTP``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP, elevated=True,
        ),
    )
    result = manager.test_mail_delivery(
        to="ops@example.com",
        from_addr="probe@example.com",
        subject="probe",
    )

    testbase.assert_equal(result.status, WindowsMtaActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    call = runner.calls[0]
    script = call["args"][-1]
    testbase.assert_equal("Send-MailMessage" in script, True)
    testbase.assert_equal("-SmtpServer 'localhost'" in script, True)
    testbase.assert_equal("-To 'ops@example.com'" in script, True)
    testbase.assert_equal("-From 'probe@example.com'" in script, True)
    testbase.assert_equal("-Subject 'probe'" in script, True)
    testbase.assert_equal(call["elevated"], True)
    return


def test_test_mail_delivery_dry_run_short_circuits():
    """
        test_mail_delivery returns DRY_RUN without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP, dry_run=True,
        ),
    )
    result = manager.test_mail_delivery(
        to="t@example.com",
        from_addr="f@example.com",
        subject="s",
    )

    testbase.assert_equal(result.status, WindowsMtaActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# get_queue_status
# ----------------------------------------------------------------------


def test_get_queue_status_zero_when_mta_kind_none():
    """
        get_queue_status returns a zeroed status when
        ``mta_kind=NONE``.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(),
    )
    status = manager.get_queue_status()

    testbase.assert_equal(status.pending_count, 0)
    testbase.assert_equal(status.deferred_count, 0)
    testbase.assert_equal(status.total_size_bytes is None, True)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_get_queue_status_parses_iis_smtp_count():
    """
        get_queue_status parses the IIS SMTP queue-directory file
        count via the PowerShell helper.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(stdout='{"Count":3,"Sum":12345}')

    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP,
        ),
    )
    status = manager.get_queue_status()

    testbase.assert_equal(status.pending_count, 3)
    testbase.assert_equal(status.deferred_count, 0)
    testbase.assert_equal(status.total_size_bytes, 12345)
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


# ----------------------------------------------------------------------
# flush_queue
# ----------------------------------------------------------------------


def test_flush_queue_failed_when_mta_kind_none():
    """
        flush_queue returns FAILED with ``windows_no_built_in_mta``
        when ``mta_kind=NONE``.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(),
    )
    result = manager.flush_queue()

    testbase.assert_equal(result.status, WindowsMtaActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "windows_no_built_in_mta")
    return


def test_flush_queue_restarts_smtpsvc_for_iis_smtp():
    """
        flush_queue dispatches ``Restart-Service -Name 'SMTPSVC'``
        when ``mta_kind=IIS_SMTP``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP, elevated=True,
        ),
    )
    result = manager.flush_queue()

    testbase.assert_equal(result.status, WindowsMtaActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    script = runner.calls[0]["args"][-1]
    testbase.assert_equal("Restart-Service" in script, True)
    testbase.assert_equal("'SMTPSVC'" in script, True)
    testbase.assert_equal(runner.calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every IIS SMTP write verb passes ``elevated=True`` when the
        config requests it.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP, elevated=True,
        ),
    )
    manager.set_mta_config(
        spec=WindowsMtaConfig(myhostname="mail.example.com"),
    )
    manager.test_mail_delivery(
        to="t@example.com",
        from_addr="f@example.com",
        subject="s",
    )
    manager.flush_queue()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Reads NEVER pass ``elevated=True``, even when configured.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(
        stdout='{"FullyQualifiedDomainName":"h","SmartHost":"",'
        '"MaxMessageSize":0,"ServerBindings":":25:"}',
    )

    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP, elevated=True,
        ),
    )
    manager.get_mta_config()
    manager.list_aliases()
    runner.default_result = _ok(stdout='{"Count":0,"Sum":null}')
    manager.get_queue_status()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing IIS SMTP write returns FAILED with ``stderr``
        populated from the runner.
    """
    runner = _MockCommandRunner()
    runner.default_result = _fail(
        stderr="Restart-Service : Cannot find any service",
        exit_code=1,
    )

    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP, elevated=True,
        ),
    )
    result = manager.flush_queue()

    testbase.assert_equal(result.status, WindowsMtaActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        "Cannot find any service" in result.stderr, True,
    )
    return


def test_writes_fail_when_mta_kind_none():
    """
        Every write verb returns FAILED with
        ``windows_no_built_in_mta`` when ``mta_kind=NONE``.
    """
    runner = _MockCommandRunner()
    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(),
    )

    write_results = [
        manager.set_mta_config(spec=WindowsMtaConfig()),
        manager.add_alias(local="x", remote="y@example.com"),
        manager.remove_alias(local="x"),
        manager.test_mail_delivery(
            to="t@example.com", from_addr="f@example.com", subject="s",
        ),
        manager.flush_queue(),
    ]

    for result in write_results:
        testbase.assert_equal(result.status, WindowsMtaActionStatus.FAILED)
        testbase.assert_equal(result.stderr, "windows_no_built_in_mta")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_send_mail_works_with_iis_smtp_kind():
    """
        Dedicated regression test for the
        ``mta_kind=IIS_SMTP`` happy path of ``test_mail_delivery``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    manager = WindowsMailTransportManager(
        runner=runner,
        config=WindowsMtaManagerConfig(
            mta_kind=WindowsMtaKind.IIS_SMTP, elevated=True,
        ),
    )
    result = manager.test_mail_delivery(
        to="ops@example.com",
        from_addr="probe@example.com",
        subject="probe",
    )

    testbase.assert_equal(result.status, WindowsMtaActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    return


# ----------------------------------------------------------------------
# Module-level parser sanity tests
# ----------------------------------------------------------------------


def test_parse_iissmtp_get_output_handles_missing_keys():
    """
        The IIS SMTP parser returns a config with a descriptive
        ``detail`` when the JSON is empty.
    """
    config = _parse_iissmtp_get_output(text="")
    testbase.assert_equal(config.mta_kind, WindowsMtaKind.IIS_SMTP)
    testbase.assert_equal(config.detail is not None, True)
    return


def test_parse_iissmtp_queue_status_handles_zero_count():
    """
        The queue-status parser returns a zeroed status when ``Count``
        is zero.
    """
    status = _parse_iissmtp_queue_status_output(
        text='{"Count":0,"Sum":null}',
    )
    testbase.assert_equal(status.pending_count, 0)
    testbase.assert_equal(status.total_size_bytes is None, True)
    return


def test_iissmtp_set_script_includes_known_setters():
    """
        The set-script builder emits property-setter statements only
        for fields that are not None.
    """
    script = _build_iissmtp_set_script(
        spec=WindowsMtaConfig(
            myhostname="h.example.com",
            relayhost="r.example.com",
            message_size_limit=1024,
        ),
    )
    testbase.assert_equal(
        "FullyQualifiedDomainName = 'h.example.com'" in script, True,
    )
    testbase.assert_equal("SmartHost = 'r.example.com'" in script, True)
    testbase.assert_equal("MaxMessageSize = 1024" in script, True)
    testbase.assert_equal("$s.Put_()" in script, True)
    return


def test_iissmtp_get_script_calls_wmiobject():
    """
        The get-script builder emits a ``Get-WmiObject`` invocation
        against the IIS SMTP namespace.
    """
    script = _build_iissmtp_get_script()
    testbase.assert_equal("Get-WmiObject" in script, True)
    testbase.assert_equal("IIsSmtpServerSetting" in script, True)
    testbase.assert_equal("ConvertTo-Json" in script, True)
    return


def test_iissmtp_queue_status_script_targets_queue_dir():
    """
        The queue-status script builder embeds the queue dir.
    """
    script = _build_iissmtp_queue_status_script(queue_dir="C:\\tmp")
    testbase.assert_equal("C:\\tmp" in script, True)
    testbase.assert_equal("Get-ChildItem" in script, True)
    testbase.assert_equal("Measure-Object" in script, True)
    return


def test_send_mailmessage_script_escapes_single_quotes():
    """
        The Send-MailMessage script builder doubles single quotes
        embedded in addresses or subjects.
    """
    script = _build_send_mailmessage_script(
        smtp_server="localhost",
        to="o'brien@example.com",
        from_addr="probe@example.com",
        subject="o'probe",
    )
    testbase.assert_equal("o''brien@example.com" in script, True)
    testbase.assert_equal("o''probe" in script, True)
    return
