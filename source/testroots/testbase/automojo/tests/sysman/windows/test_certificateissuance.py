"""
    Tests for :class:`WindowsCertificateIssuanceManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live filesystem is touched; ``powershell.exe`` and
    ``certreq.exe`` are both faked.
"""

import json

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.certificateissuance import (
    WindowsCertIssuanceActionStatus,
    WindowsCertIssuanceManagerConfig,
    WindowsCertIssuanceQueryError,
    WindowsCertificateIssuanceManager,
    WindowsCertificateRequestSpec,
    WindowsRevocationReason,
)


# ----------------------------------------------------------------------
# Test helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.  When no exact match is found, the runner consults a
        list of ``(predicate, RunResult)`` pairs in
        ``predicate_results`` and returns the first match.
    """

    results: dict = field(default_factory=dict)
    predicate_results: list = field(default_factory=list)
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

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        for predicate, result in self.predicate_results:
            if predicate(args):
                rtnval = result
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


def _far_future_iso() -> str:
    target = datetime.now(tz=timezone.utc) + timedelta(days=3650)
    rtnval = target.strftime("%Y-%m-%dT%H:%M:%S")
    return rtnval


def _near_expiry_iso() -> str:
    target = datetime.now(tz=timezone.utc) + timedelta(days=5)
    rtnval = target.strftime("%Y-%m-%dT%H:%M:%S")
    return rtnval


def _build_cert_record(
    *,
    thumbprint: str,
    subject: str,
    issuer: str,
    not_after: str,
    serial: str = "0123456789ABCDEF",
    sans: Optional[list] = None,
) -> dict:
    if sans is None:
        sans = []
    dns_list = []
    for san in sans:
        dns_list.append({"Unicode": san, "Punycode": san})
    rtnval = {
        "Thumbprint": thumbprint,
        "Subject": subject,
        "Issuer": issuer,
        "NotBefore": "2024-01-01T00:00:00",
        "NotAfter": not_after,
        "SerialNumber": serial,
        "DnsNameList": dns_list,
        "SignatureAlgorithm": {"FriendlyName": "sha256RSA"},
        "PSPath": "Microsoft.PowerShell.Security\\Certificate::LocalMachine\\My\\{}".format(
            thumbprint,
        ),
    }
    return rtnval


def _is_get_childitem(args: list) -> bool:
    if len(args) < 5:
        return False
    if args[0] != "powershell.exe":
        return False
    if args[1] != "-NoProfile":
        return False
    script = args[4]
    if "Get-ChildItem" not in script:
        return False
    if "ConvertTo-Json" not in script:
        return False
    return True


def _is_new_self_signed(args: list) -> bool:
    if len(args) < 5:
        return False
    if args[0] != "powershell.exe":
        return False
    return "New-SelfSignedCertificate" in args[4]


def _is_export_pfx(args: list) -> bool:
    if len(args) < 5:
        return False
    if args[0] != "powershell.exe":
        return False
    return "Export-PfxCertificate" in args[4]


def _is_export_cert(args: list) -> bool:
    if len(args) < 5:
        return False
    if args[0] != "powershell.exe":
        return False
    if "Export-PfxCertificate" in args[4]:
        return False
    return "Export-Certificate" in args[4]


def _is_import_pfx(args: list) -> bool:
    if len(args) < 5:
        return False
    if args[0] != "powershell.exe":
        return False
    return "Import-PfxCertificate" in args[4]


def _is_import_cert(args: list) -> bool:
    if len(args) < 5:
        return False
    if args[0] != "powershell.exe":
        return False
    if "Import-PfxCertificate" in args[4]:
        return False
    return "Import-Certificate" in args[4]


def _is_remove_item(args: list) -> bool:
    if len(args) < 5:
        return False
    if args[0] != "powershell.exe":
        return False
    return "Remove-Item" in args[4]


def _is_thumbprint_probe(args: list) -> bool:
    if len(args) < 5:
        return False
    if args[0] != "powershell.exe":
        return False
    script = args[4]
    return (
        "Where-Object" in script
        and "Thumbprint -eq" in script
        and "ConvertTo-Json" not in script
    )


def _is_get_pfx_certificate(args: list) -> bool:
    if len(args) < 5:
        return False
    if args[0] != "powershell.exe":
        return False
    return "Get-PfxCertificate" in args[4]


def _is_destination_match_probe(args: list) -> bool:
    if len(args) < 5:
        return False
    if args[0] != "powershell.exe":
        return False
    script = args[4]
    return (
        "Test-Path" in script
        and "Get-PfxCertificate" in script
        and "Get-ChildItem" in script
    )


_FOO_THUMBPRINT = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
_BAR_THUMBPRINT = "1122334455667788112233445566778811223344"


# ----------------------------------------------------------------------
# request_certificate
# ----------------------------------------------------------------------


def test_request_certificate_invokes_powershell_with_subject():
    """
        request_certificate dispatches New-SelfSignedCertificate with
        the configured subject CN and key parameters.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem, RunResult(exit_code=0, stdout="", stderr="")),
    )
    runner.predicate_results.append(
        (_is_new_self_signed, RunResult(exit_code=0, stdout="", stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(elevated=True)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    spec = WindowsCertificateRequestSpec(
        alias="web",
        common_name="example.com",
        sans=["www.example.com"],
        key_length=2048,
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SUCCESS)
    new_calls = [c for c in runner.calls if _is_new_self_signed(c["args"])]
    testbase.assert_equal(len(new_calls), 1)
    script = new_calls[0]["args"][4]
    testbase.assert_equal("CN=example.com" in script, True)
    testbase.assert_equal("LocalMachine\\My" in script, True)
    testbase.assert_equal("'www.example.com'" in script, True)
    testbase.assert_equal("-KeyLength 2048" in script, True)
    testbase.assert_equal(new_calls[0]["elevated"], True)
    return


def test_request_certificate_skipped_when_present_and_not_near_expiry():
    """
        request_certificate returns SKIPPED when the alias is already
        present and remaining validity > renew_threshold_days.
    """
    record = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web, O=Example",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
    )
    canned = json.dumps(record)
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(
        elevated=True, renew_threshold_days=30,
    )
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    spec = WindowsCertificateRequestSpec(
        alias="web", common_name="web",
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SKIPPED)
    new_calls = [c for c in runner.calls if _is_new_self_signed(c["args"])]
    testbase.assert_equal(new_calls, [])
    return


def test_request_certificate_dry_run_short_circuits():
    """
        request_certificate with dry_run=True returns DRY_RUN and the
        runner is never invoked with New-SelfSignedCertificate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem, RunResult(exit_code=0, stdout="", stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(
        dry_run=True, elevated=True,
    )
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    spec = WindowsCertificateRequestSpec(
        alias="web", common_name="example.com",
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.DRY_RUN)
    new_calls = [c for c in runner.calls if _is_new_self_signed(c["args"])]
    testbase.assert_equal(new_calls, [])
    testbase.assert_equal("New-SelfSignedCertificate" in result.command[4], True)
    return


def test_request_certificate_self_signed_uses_powershell():
    """
        Even when ca_config is set, spec.is_self_signed=True forces
        the PowerShell self-signed branch.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem, RunResult(exit_code=0, stdout="", stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(
        elevated=True,
        ca_config="CA01.example.com\\Issuing-CA",
    )
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    spec = WindowsCertificateRequestSpec(
        alias="dev", common_name="dev.local",
        is_self_signed=True,
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SUCCESS)
    new_calls = [c for c in runner.calls if _is_new_self_signed(c["args"])]
    testbase.assert_equal(len(new_calls), 1)
    certreq_calls = [c for c in runner.calls if c["args"][0] == "certreq.exe"]
    testbase.assert_equal(certreq_calls, [])
    return


# ----------------------------------------------------------------------
# renew_certificate
# ----------------------------------------------------------------------


def test_renew_certificate_invokes_powershell():
    """
        renew_certificate dispatches New-SelfSignedCertificate
        -CloneCert when the existing cert is near expiry.
    """
    record = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web, O=Example",
        issuer="CN=Example CA",
        not_after=_near_expiry_iso(),
    )
    canned = json.dumps(record)
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(elevated=True)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    result = manager.renew_certificate(alias="web")

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SUCCESS)
    clone_calls = [
        c for c in runner.calls
        if c["args"][0] == "powershell.exe"
        and "-CloneCert" in c["args"][4]
    ]
    testbase.assert_equal(len(clone_calls), 1)
    testbase.assert_equal(clone_calls[0]["elevated"], True)
    return


def test_renew_certificate_skipped_when_not_near_expiry():
    """
        renew_certificate returns SKIPPED when the existing cert is
        well clear of the renew threshold.
    """
    record = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
    )
    canned = json.dumps(record)
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(renew_threshold_days=30)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    result = manager.renew_certificate(alias="web")

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SKIPPED)
    clone_calls = [
        c for c in runner.calls
        if c["args"][0] == "powershell.exe"
        and "-CloneCert" in c["args"][4]
    ]
    testbase.assert_equal(clone_calls, [])
    return


# ----------------------------------------------------------------------
# revoke_certificate
# ----------------------------------------------------------------------


def test_revoke_certificate_invokes_powershell():
    """
        revoke_certificate dispatches Remove-Item against the matching
        cert when ca_config is unset.
    """
    record = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
    )
    canned = json.dumps(record)
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )
    runner.predicate_results.append(
        (_is_remove_item, RunResult(exit_code=0, stdout="", stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(elevated=True)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    result = manager.revoke_certificate(alias="web")

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SUCCESS)
    remove_calls = [c for c in runner.calls if _is_remove_item(c["args"])]
    testbase.assert_equal(len(remove_calls), 1)
    testbase.assert_equal(remove_calls[0]["elevated"], True)
    return


def test_revoke_certificate_with_reason():
    """
        revoke_certificate with ca_config=set dispatches
        certreq.exe -revoke -reason <r>.
    """
    record = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
        serial="0123ABCD",
    )
    canned = json.dumps(record)
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(
        elevated=True,
        ca_config="CA01.example.com\\Issuing-CA",
    )
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    result = manager.revoke_certificate(
        alias="web",
        reason=WindowsRevocationReason.KEY_COMPROMISE,
    )

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SUCCESS)
    certreq_calls = [c for c in runner.calls if c["args"][0] == "certreq.exe"]
    testbase.assert_equal(len(certreq_calls), 1)
    testbase.assert_equal(
        certreq_calls[0]["args"],
        [
            "certreq.exe", "-revoke",
            "-config", "CA01.example.com\\Issuing-CA",
            "0123ABCD",
            "-reason", "KeyCompromise",
        ],
    )
    return


# ----------------------------------------------------------------------
# get_certificate / list_certificates
# ----------------------------------------------------------------------


def test_get_certificate_parses_x509_output():
    """
        get_certificate parses the JSON output of Get-ChildItem |
        ConvertTo-Json into a populated WindowsCertificateInfo.
    """
    record = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web, O=Example",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
        serial="ABCDEF",
        sans=["example.com", "www.example.com"],
    )
    canned = json.dumps(record)
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig()
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    info = manager.get_certificate(alias="web")

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.alias, "web")
    testbase.assert_equal(info.fingerprint, _FOO_THUMBPRINT)
    testbase.assert_equal(info.serial_number, "ABCDEF")
    testbase.assert_equal(info.key_algorithm, "RSA")
    testbase.assert_equal(
        sorted(info.sans), sorted(["example.com", "www.example.com"]),
    )
    return


def test_list_certificates_returns_all():
    """
        list_certificates parses an array JSON payload into a list
        of WindowsCertificateInfo.
    """
    foo = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
    )
    bar = _build_cert_record(
        thumbprint=_BAR_THUMBPRINT,
        subject="CN=api",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
    )
    canned = json.dumps([foo, bar])
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig()
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    infos = manager.list_certificates()

    testbase.assert_equal(len(infos), 2)
    aliases = sorted([info.alias for info in infos])
    testbase.assert_equal(aliases, ["api", "web"])
    return


# ----------------------------------------------------------------------
# export_certificate / import_certificate
# ----------------------------------------------------------------------


def test_export_certificate_writes_to_path():
    """
        export_certificate (cert only) dispatches Export-Certificate.
    """
    record = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
    )
    canned = json.dumps(record)
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )
    runner.predicate_results.append(
        (_is_destination_match_probe,
         RunResult(exit_code=1, stdout="", stderr="")),
    )
    runner.predicate_results.append(
        (_is_export_cert,
         RunResult(exit_code=0, stdout="", stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(elevated=True)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    result = manager.export_certificate(
        alias="web", path="C:\\tmp\\web.cer", include_private_key=False,
    )

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SUCCESS)
    export_calls = [c for c in runner.calls if _is_export_cert(c["args"])]
    testbase.assert_equal(len(export_calls), 1)
    testbase.assert_equal(export_calls[0]["elevated"], True)
    testbase.assert_equal("C:\\tmp\\web.cer" in export_calls[0]["args"][4], True)
    return


def test_export_with_private_key_uses_pfx_format():
    """
        export_certificate with include_private_key=True dispatches
        Export-PfxCertificate (PFX format).
    """
    record = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
    )
    canned = json.dumps(record)
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )
    runner.predicate_results.append(
        (_is_destination_match_probe,
         RunResult(exit_code=1, stdout="", stderr="")),
    )
    runner.predicate_results.append(
        (_is_export_pfx,
         RunResult(exit_code=0, stdout="", stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(elevated=True)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    result = manager.export_certificate(
        alias="web", path="C:\\tmp\\web.pfx",
        include_private_key=True, passphrase="hunter2",
    )

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SUCCESS)
    pfx_calls = [c for c in runner.calls if _is_export_pfx(c["args"])]
    testbase.assert_equal(len(pfx_calls), 1)
    testbase.assert_equal(pfx_calls[0]["stdin"], "hunter2")
    return


def test_import_certificate_skipped_when_fingerprint_present():
    """
        import_certificate returns SKIPPED when a certificate with the
        same Thumbprint is already in the store.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    # Source thumbprint probe via Get-PfxCertificate.
    runner.predicate_results.append(
        (_is_get_pfx_certificate,
         RunResult(exit_code=0, stdout="{}\n".format(_FOO_THUMBPRINT), stderr="")),
    )
    # Destination thumbprint match probe.
    runner.predicate_results.append(
        (_is_thumbprint_probe,
         RunResult(exit_code=0, stdout="", stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(elevated=True)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    result = manager.import_certificate(
        path="C:\\tmp\\web.cer", alias="web",
    )

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SKIPPED)
    import_calls = [
        c for c in runner.calls
        if _is_import_pfx(c["args"]) or _is_import_cert(c["args"])
    ]
    testbase.assert_equal(import_calls, [])
    testbase.assert_equal(result.fingerprint, _FOO_THUMBPRINT)
    return


# ----------------------------------------------------------------------
# Passphrase redaction / elevation / failure surfaces
# ----------------------------------------------------------------------


def test_passphrase_redacted_in_recorded_command():
    """
        When export_certificate is called with a passphrase,
        the recorded command never contains the literal passphrase.
    """
    record = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
    )
    canned = json.dumps(record)
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )
    runner.predicate_results.append(
        (_is_destination_match_probe,
         RunResult(exit_code=1, stdout="", stderr="")),
    )
    runner.predicate_results.append(
        (_is_export_pfx,
         RunResult(exit_code=0, stdout="", stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(elevated=True)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    result = manager.export_certificate(
        alias="web", path="C:\\tmp\\web.pfx",
        include_private_key=True, passphrase="ULTRA-SECRET-VALUE",
    )

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.SUCCESS)
    # Recorded command field is sanitised.
    if result.command is not None:
        for token in result.command:
            testbase.assert_equal("ULTRA-SECRET-VALUE" in token, False)
    # The stdin path is the carrier.
    pfx_calls = [c for c in runner.calls if _is_export_pfx(c["args"])]
    testbase.assert_equal(len(pfx_calls), 1)
    testbase.assert_equal(pfx_calls[0]["stdin"], "ULTRA-SECRET-VALUE")
    return


def test_writes_elevate():
    """
        Mutating verbs pass elevated=True when config.elevated=True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout="", stderr="")),
    )
    runner.predicate_results.append(
        (_is_new_self_signed,
         RunResult(exit_code=0, stdout="", stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(elevated=True)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    spec = WindowsCertificateRequestSpec(
        alias="web", common_name="example.com",
    )
    manager.request_certificate(spec=spec)

    new_calls = [c for c in runner.calls if _is_new_self_signed(c["args"])]
    testbase.assert_equal(len(new_calls), 1)
    testbase.assert_equal(new_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        list_certificates / get_certificate never pass elevated=True.
    """
    record = _build_cert_record(
        thumbprint=_FOO_THUMBPRINT,
        subject="CN=web",
        issuer="CN=Example CA",
        not_after=_far_future_iso(),
    )
    canned = json.dumps(record)
    runner = _MockCommandRunner()
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout=canned, stderr="")),
    )

    config = WindowsCertIssuanceManagerConfig(elevated=True)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    manager.list_certificates()
    manager.get_certificate(alias="web")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When PowerShell exits non-zero, FAILED is returned with
        stderr / exit_code captured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.predicate_results.append(
        (_is_get_childitem,
         RunResult(exit_code=0, stdout="", stderr="")),
    )
    runner.predicate_results.append(
        (_is_new_self_signed,
         RunResult(exit_code=2, stdout="", stderr="invalid template")),
    )

    config = WindowsCertIssuanceManagerConfig(elevated=True)
    manager = WindowsCertificateIssuanceManager(runner=runner, config=config)
    spec = WindowsCertificateRequestSpec(
        alias="web", common_name="example.com",
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, WindowsCertIssuanceActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "invalid template")
    return


def test_query_error_exception_is_constructible():
    """
        WindowsCertIssuanceQueryError is constructible and stringifies.
    """
    err = WindowsCertIssuanceQueryError(
        operation="list_certificates",
        message="parser failed",
    )
    rendered = str(err)
    testbase.assert_equal("list_certificates" in rendered, True)
    testbase.assert_equal("parser failed" in rendered, True)
    return
