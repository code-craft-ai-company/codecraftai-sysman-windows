"""
    Tests for :class:`WindowsRemoteLogForwardingManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``wecutil.exe`` /
    ``powershell.exe`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows import remotelogforwarding as rlf
from ccai.sysman.windows.remotelogforwarding import (
    WindowsForwarderActionStatus,
    WindowsForwarderInfo,
    WindowsForwarderManagerConfig,
    WindowsForwarderQueryError,
    WindowsForwarderSpec,
    WindowsForwarderStatus,
    WindowsForwarderTestResult,
    WindowsRemoteLogForwardingManager,
    _render_subscription_xml,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        call and returns pre-seeded :class:`RunResult` values
        keyed by argv tuple.
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
            "env": env,
        }
        self.calls.append(call_record)

        # Try exact match first.
        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        # Match by argv prefix for PowerShell scriptblocks.
        if len(args) >= 1 and args[0].endswith("powershell.exe"):
            for prefix_key, prefix_value in self.results.items():
                if isinstance(prefix_key, tuple) and (
                    len(prefix_key) >= 1
                    and prefix_key[0] == "powershell"
                ):
                    rtnval = prefix_value
                    return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _seed_powershell_default(
    runner: _MockCommandRunner,
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """
        Seed the runner so that any ``powershell.exe`` invocation
        returns the supplied :class:`RunResult` when no exact
        argv match is found.
    """
    runner.results[("powershell",)] = RunResult(
        exit_code=exit_code, stdout=stdout, stderr=stderr,
    )
    return


# ----------------------------------------------------------------------
# add_forwarder
# ----------------------------------------------------------------------


def test_add_forwarder_invokes_wecutil_cs():
    """
        add_forwarder writes the XML via PowerShell and then
        registers the subscription via ``wecutil.exe cs <path>``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = WindowsRemoteLogForwardingManager(
        runner=runner,
        config=WindowsForwarderManagerConfig(elevated=True),
    )
    spec = WindowsForwarderSpec(
        id="collector1", host="collector.example.com",
        port=5985, protocol="http", query="*",
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, WindowsForwarderActionStatus.SUCCESS,
    )

    cs_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "wecutil.exe"
            and call["args"][1] == "cs"
        )
    ]
    testbase.assert_equal(len(cs_calls), 1)
    testbase.assert_equal(cs_calls[0]["args"][0], "wecutil.exe")
    testbase.assert_equal(cs_calls[0]["args"][1], "cs")
    testbase.assert_equal(
        cs_calls[0]["args"][2].endswith("collector1.xml"), True,
    )
    testbase.assert_equal(cs_calls[0]["elevated"], True)
    return


def test_add_forwarder_skipped_when_present():
    """
        add_forwarder returns SKIPPED when the on-disk XML
        matches the rendered desired XML byte-for-byte.
    """
    runner = _MockCommandRunner()
    spec = WindowsForwarderSpec(
        id="collector1", host="collector.example.com",
        port=5985, protocol="http", query="*",
    )
    desired_xml = _render_subscription_xml(spec=spec)
    _seed_powershell_default(
        runner, exit_code=0, stdout=desired_xml, stderr="",
    )

    manager = WindowsRemoteLogForwardingManager(
        runner=runner, config=WindowsForwarderManagerConfig(),
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, WindowsForwarderActionStatus.SKIPPED,
    )
    cs_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "wecutil.exe"
            and call["args"][1] == "cs"
        )
    ]
    testbase.assert_equal(len(cs_calls), 0)
    return


def test_add_forwarder_dry_run():
    """
        With ``dry_run=True``, add_forwarder records the
        intended argv but does not invoke ``wecutil`` or write
        the XML.
    """
    runner = _MockCommandRunner()
    config = WindowsForwarderManagerConfig(dry_run=True)
    manager = WindowsRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    spec = WindowsForwarderSpec(
        id="collector1", host="collector.example.com",
        port=5985, protocol="http",
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, WindowsForwarderActionStatus.DRY_RUN,
    )
    cs_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "wecutil.exe"
            and call["args"][1] == "cs"
        )
    ]
    testbase.assert_equal(len(cs_calls), 0)
    return


def test_wec_subscription_xml_generation():
    """
        The rendered subscription XML conforms to the WEF
        schema: includes the namespace declaration,
        ``SubscriptionId``, ``Enabled``, ``TransportName``, the
        QueryList block, and the ``LogFile`` element.
    """
    spec = WindowsForwarderSpec(
        id="collector1", host="collector.example.com",
        port=5986, protocol="https",
        query="*[System[(Level=1 or Level=2)]]",
        description="critical alerts",
    )
    body = _render_subscription_xml(spec=spec)

    testbase.assert_equal(
        "xmlns=\"http://schemas.microsoft.com/2006/03/windows/events/subscription\""
        in body,
        True,
    )
    testbase.assert_equal(
        "<SubscriptionId>collector1</SubscriptionId>" in body,
        True,
    )
    testbase.assert_equal(
        "<SubscriptionType>SourceInitiated</SubscriptionType>"
        in body,
        True,
    )
    testbase.assert_equal(
        "<Description>critical alerts</Description>" in body,
        True,
    )
    testbase.assert_equal(
        "<Enabled>true</Enabled>" in body, True,
    )
    testbase.assert_equal(
        "<TransportName>HTTPS</TransportName>" in body, True,
    )
    testbase.assert_equal(
        "<LogFile>ForwardedEvents</LogFile>" in body, True,
    )
    testbase.assert_equal("&lt;QueryList&gt;" in body, True)
    testbase.assert_equal(
        "<ContentFormat>RenderedText</ContentFormat>" in body,
        True,
    )
    return


# ----------------------------------------------------------------------
# remove_forwarder
# ----------------------------------------------------------------------


def test_remove_forwarder_invokes_wecutil_ds():
    """
        remove_forwarder dispatches ``wecutil.exe ds <id>`` to
        unregister the subscription.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[
        ("wecutil.exe", "gs", "collector1")
    ] = RunResult(
        exit_code=0,
        stdout="Subscription Id: collector1\nEnabled: true\n",
        stderr="",
    )
    _seed_powershell_default(
        runner,
        exit_code=0,
        stdout="<Subscription/>",
        stderr="",
    )

    manager = WindowsRemoteLogForwardingManager(
        runner=runner,
        config=WindowsForwarderManagerConfig(elevated=True),
    )
    result = manager.remove_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, WindowsForwarderActionStatus.SUCCESS,
    )

    ds_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "wecutil.exe"
            and call["args"][1] == "ds"
        )
    ]
    testbase.assert_equal(len(ds_calls), 1)
    testbase.assert_equal(
        ds_calls[0]["args"], ["wecutil.exe", "ds", "collector1"],
    )
    testbase.assert_equal(ds_calls[0]["elevated"], True)
    return


def test_remove_forwarder_skipped_when_absent():
    """
        remove_forwarder returns SKIPPED when the subscription
        is not registered AND no on-disk XML is present.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("wecutil.exe", "gs", "ghost")
    ] = RunResult(
        exit_code=1, stdout="",
        stderr="Subscription does not exist",
    )
    _seed_powershell_default(runner, exit_code=1)

    manager = WindowsRemoteLogForwardingManager(
        runner=runner, config=WindowsForwarderManagerConfig(),
    )
    result = manager.remove_forwarder(id="ghost")
    testbase.assert_equal(
        result.status, WindowsForwarderActionStatus.SKIPPED,
    )
    ds_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "wecutil.exe"
            and call["args"][1] == "ds"
        )
    ]
    testbase.assert_equal(len(ds_calls), 0)
    return


# ----------------------------------------------------------------------
# list_forwarders
# ----------------------------------------------------------------------


def test_list_forwarders_parses_wecutil_output():
    """
        list_forwarders parses the ``wecutil es`` listing,
        dispatches ``wecutil gs`` per subscription, and
        classifies enabled / disabled state.
    """
    runner = _MockCommandRunner()
    runner.results[("wecutil.exe", "es")] = RunResult(
        exit_code=0,
        stdout="collector1\ncollector2\n",
        stderr="",
    )
    runner.results[
        ("wecutil.exe", "gs", "collector1")
    ] = RunResult(
        exit_code=0,
        stdout=(
            "Subscription Id: collector1\n"
            "Enabled: true\n"
            "TransportName: HTTP\n"
            "Address: source1.example.com\n"
            "Query: *\n"
        ),
        stderr="",
    )
    runner.results[
        ("wecutil.exe", "gs", "collector2")
    ] = RunResult(
        exit_code=0,
        stdout=(
            "Subscription Id: collector2\n"
            "Enabled: false\n"
            "TransportName: HTTPS\n"
            "Address: source2.example.com\n"
            "Query: *[System[(Level=1)]]\n"
        ),
        stderr="",
    )

    manager = WindowsRemoteLogForwardingManager(
        runner=runner, config=WindowsForwarderManagerConfig(),
    )
    forwarders = manager.list_forwarders()
    testbase.assert_equal(len(forwarders), 2)

    by_id = {item.id: item for item in forwarders}
    testbase.assert_equal(
        isinstance(by_id["collector1"], WindowsForwarderInfo), True,
    )
    testbase.assert_equal(by_id["collector1"].enabled, True)
    testbase.assert_equal(by_id["collector1"].protocol, "http")
    testbase.assert_equal(by_id["collector1"].port, 5985)

    testbase.assert_equal(by_id["collector2"].enabled, False)
    testbase.assert_equal(by_id["collector2"].protocol, "https")
    testbase.assert_equal(by_id["collector2"].port, 5986)
    testbase.assert_equal(
        by_id["collector2"].selector_or_query,
        "*[System[(Level=1)]]",
    )
    return


def test_list_forwarders_raises_on_unexpected_failure():
    """
        list_forwarders raises WindowsForwarderQueryError when
        ``wecutil es`` exits non-zero with a non-empty stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("wecutil.exe", "es")] = RunResult(
        exit_code=5, stdout="",
        stderr="Access denied",
    )

    manager = WindowsRemoteLogForwardingManager(
        runner=runner, config=WindowsForwarderManagerConfig(),
    )
    raised = False
    try:
        manager.list_forwarders()
    except WindowsForwarderQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# enable_forwarder / disable_forwarder
# ----------------------------------------------------------------------


def test_enable_forwarder_invokes_wecutil_ss():
    """
        enable_forwarder dispatches
        ``wecutil.exe ss <id> /e:true``.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("wecutil.exe", "gs", "collector1")
    ] = RunResult(
        exit_code=0,
        stdout="Subscription Id: collector1\nEnabled: false\n",
        stderr="",
    )
    runner.results[
        ("wecutil.exe", "ss", "collector1", "/e:true")
    ] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = WindowsRemoteLogForwardingManager(
        runner=runner,
        config=WindowsForwarderManagerConfig(elevated=True),
    )
    result = manager.enable_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, WindowsForwarderActionStatus.SUCCESS,
    )

    ss_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "wecutil.exe"
            and call["args"][1] == "ss"
        )
    ]
    testbase.assert_equal(len(ss_calls), 1)
    testbase.assert_equal(
        ss_calls[0]["args"],
        ["wecutil.exe", "ss", "collector1", "/e:true"],
    )
    testbase.assert_equal(ss_calls[0]["elevated"], True)
    return


def test_enable_forwarder_skipped_when_enabled():
    """
        enable_forwarder returns SKIPPED when the subscription
        is already enabled.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("wecutil.exe", "gs", "collector1")
    ] = RunResult(
        exit_code=0,
        stdout="Subscription Id: collector1\nEnabled: true\n",
        stderr="",
    )

    manager = WindowsRemoteLogForwardingManager(
        runner=runner, config=WindowsForwarderManagerConfig(),
    )
    result = manager.enable_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, WindowsForwarderActionStatus.SKIPPED,
    )

    ss_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "wecutil.exe"
            and call["args"][1] == "ss"
        )
    ]
    testbase.assert_equal(len(ss_calls), 0)
    return


def test_disable_forwarder_invokes_wecutil_ss():
    """
        disable_forwarder dispatches
        ``wecutil.exe ss <id> /e:false``.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("wecutil.exe", "gs", "collector1")
    ] = RunResult(
        exit_code=0,
        stdout="Subscription Id: collector1\nEnabled: true\n",
        stderr="",
    )
    runner.results[
        ("wecutil.exe", "ss", "collector1", "/e:false")
    ] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = WindowsRemoteLogForwardingManager(
        runner=runner, config=WindowsForwarderManagerConfig(),
    )
    result = manager.disable_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, WindowsForwarderActionStatus.SUCCESS,
    )

    ss_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "wecutil.exe"
            and call["args"][1] == "ss"
        )
    ]
    testbase.assert_equal(len(ss_calls), 1)
    testbase.assert_equal(
        ss_calls[0]["args"],
        ["wecutil.exe", "ss", "collector1", "/e:false"],
    )
    return


# ----------------------------------------------------------------------
# test_forwarder
# ----------------------------------------------------------------------


def test_test_forwarder_invokes_write_eventlog():
    """
        test_forwarder dispatches a PowerShell scriptblock that
        invokes ``Write-EventLog`` against the
        ``codecraftai-test`` source on the Application channel.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = WindowsRemoteLogForwardingManager(
        runner=runner,
        config=WindowsForwarderManagerConfig(elevated=True),
    )
    result = manager.test_forwarder(id="collector1")
    testbase.assert_equal(
        isinstance(result, WindowsForwarderTestResult), True,
    )
    testbase.assert_equal(
        result.status, WindowsForwarderActionStatus.SUCCESS,
    )
    testbase.assert_equal(result.forwarder_id, "collector1")
    testbase.assert_equal(
        result.test_message_id is not None, True,
    )

    powershell_calls = [
        call for call in runner.calls
        if call["args"][0].endswith("powershell.exe")
    ]
    testbase.assert_equal(len(powershell_calls) >= 1, True)
    last_script = powershell_calls[-1]["args"][-1]
    testbase.assert_equal(
        "Write-EventLog -LogName Application" in last_script, True,
    )
    testbase.assert_equal(
        "codecraftai-test" in last_script, True,
    )
    testbase.assert_equal(powershell_calls[-1]["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_forwarder_status
# ----------------------------------------------------------------------


def test_get_forwarder_status_parses_output():
    """
        get_forwarder_status reports ``enabled=True`` /
        ``enabled=False`` based on the ``Enabled`` field of the
        ``wecutil gs`` output and ``enabled=False`` when the
        subscription is unknown.
    """
    runner_active = _MockCommandRunner()
    runner_active.results[
        ("wecutil.exe", "gs", "collector1")
    ] = RunResult(
        exit_code=0,
        stdout=(
            "Subscription Id: collector1\n"
            "Enabled: true\n"
            "LastError: 0\n"
            "TotalBytes: 12345\n"
        ),
        stderr="",
    )
    manager_active = WindowsRemoteLogForwardingManager(
        runner=runner_active,
        config=WindowsForwarderManagerConfig(),
    )
    status_active = manager_active.get_forwarder_status(
        id="collector1",
    )
    testbase.assert_equal(
        isinstance(status_active, WindowsForwarderStatus), True,
    )
    testbase.assert_equal(status_active.enabled, True)
    testbase.assert_equal(status_active.error_count, 0)
    testbase.assert_equal(status_active.bytes_sent, 12345)

    runner_absent = _MockCommandRunner()
    runner_absent.results[
        ("wecutil.exe", "gs", "ghost")
    ] = RunResult(
        exit_code=1, stdout="",
        stderr="Subscription does not exist",
    )
    manager_absent = WindowsRemoteLogForwardingManager(
        runner=runner_absent,
        config=WindowsForwarderManagerConfig(),
    )
    status_absent = manager_absent.get_forwarder_status(
        id="ghost",
    )
    testbase.assert_equal(status_absent.enabled, False)
    testbase.assert_equal(status_absent.error_count, None)
    return


# ----------------------------------------------------------------------
# Elevation, error surfacing
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        With ``elevated=True``, every write dispatch sets
        ``elevated=True`` on the runner call.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    config = WindowsForwarderManagerConfig(elevated=True)
    manager = WindowsRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    spec = WindowsForwarderSpec(
        id="collector1", host="example.com", port=5985,
    )
    manager.add_forwarder(spec=spec)
    cs_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "wecutil.exe"
            and call["args"][1] == "cs"
        )
    ]
    testbase.assert_equal(len(cs_calls), 1)
    testbase.assert_equal(cs_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Reads (``wecutil es``, ``wecutil gs``,
        ``Get-Content`` PowerShell, ``Write-EventLog``
        PowerShell) never elevate even when
        ``config.elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("wecutil.exe", "es")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[
        ("wecutil.exe", "gs", "collector1")
    ] = RunResult(
        exit_code=0,
        stdout="Subscription Id: collector1\nEnabled: true\n",
        stderr="",
    )

    config = WindowsForwarderManagerConfig(elevated=True)
    manager = WindowsRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    manager.list_forwarders()
    manager.get_forwarder_status(id="collector1")
    manager.test_forwarder(id="collector1")

    for call in runner.calls:
        argv0 = call["args"][0]
        is_read = False
        if argv0 == "wecutil.exe":
            second = call["args"][1] if len(call["args"]) > 1 else ""
            if second in ("es", "gs"):
                is_read = True
        if argv0.endswith("powershell.exe"):
            # Only test_forwarder + get-content reads here.
            is_read = True
        if is_read:
            testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When ``wecutil cs`` returns non-zero the manager
        surfaces the exit code and stderr without raising.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    # Detect any wecutil cs call and fail it.
    spec = WindowsForwarderSpec(
        id="collector1", host="example.com", port=5985,
    )
    xml_path = (
        "C:\\ProgramData\\CodeCraftAI\\WEC\\collector1.xml"
    )
    runner.results[
        ("wecutil.exe", "cs", xml_path)
    ] = RunResult(
        exit_code=2, stdout="",
        stderr="Failed to create subscription",
    )

    manager = WindowsRemoteLogForwardingManager(
        runner=runner, config=WindowsForwarderManagerConfig(),
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, WindowsForwarderActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(
        "Failed to create subscription" in (result.stderr or ""),
        True,
    )
    return


def test_module_docstring_documents_wef_vs_syslog():
    """
        The module docstring documents that WEF is
        Windows-to-Windows only and recommends third-party
        shippers for syslog forwarding.
    """
    docstring = rlf.__doc__ or ""
    lower = docstring.lower()
    testbase.assert_equal("wef" in lower, True)
    testbase.assert_equal(
        "syslog" in lower, True,
    )
    testbase.assert_equal(
        "winlogbeat" in lower or "nxlog" in lower, True,
    )
    return
