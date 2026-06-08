"""
    Tests for :class:`WindowsCertificateTrustManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv tuple.  No live PowerShell, no live cert store, no live
    filesystem.
"""

import base64
import hashlib
import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.certificatetrustmanagement import (
    WindowsCertificateTrustManager,
    WindowsCertTrustActionStatus,
    WindowsCertTrustManagerConfig,
    WindowsCertTrustQueryError,
)


# ----------------------------------------------------------------------
# Test helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in.  Matches by argv tuple.
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

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
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


def _make_pem(*, der_bytes: bytes) -> str:
    encoded = base64.b64encode(der_bytes).decode("ascii")
    body_lines: list = []
    cursor = 0
    while cursor < len(encoded):
        body_lines.append(encoded[cursor:cursor + 64])
        cursor += 64
    rtnval_lines = ["-----BEGIN CERTIFICATE-----"]
    rtnval_lines.extend(body_lines)
    rtnval_lines.append("-----END CERTIFICATE-----")
    rtnval = "\n".join(rtnval_lines) + "\n"
    return rtnval


def _expected_fingerprint(*, der_bytes: bytes) -> str:
    """
        Compute the Windows canonical uppercase no-colon Thumbprint.
    """
    rtnval = hashlib.sha256(der_bytes).hexdigest().upper()
    return rtnval


_DER_FOO = b"DER-FOO-BYTES-1234567890"
_DER_BAR = b"DER-BAR-BYTES-ABCDEFGHIJ"

_LOCALMACHINE_ROOT = r"Cert:\LocalMachine\Root"
_CURRENTUSER_ROOT = r"Cert:\CurrentUser\Root"


def _foo_pem() -> str:
    rtnval = _make_pem(der_bytes=_DER_FOO)
    return rtnval


def _bar_pem() -> str:
    rtnval = _make_pem(der_bytes=_DER_BAR)
    return rtnval


def _foo_fp() -> str:
    rtnval = _expected_fingerprint(der_bytes=_DER_FOO)
    return rtnval


def _bar_fp() -> str:
    rtnval = _expected_fingerprint(der_bytes=_DER_BAR)
    return rtnval


def _ps_argv(*, script: str) -> tuple:
    """
        Build the canonical PowerShell argv tuple for keying the
        runner's results dict.
    """
    rtnval = (
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script,
    )
    return rtnval


def _read_source_script(*, path: str) -> str:
    """
        The exact script the manager uses to read source bytes.
    """
    rtnval = (
        "$ErrorActionPreference = 'Stop'; "
        "Get-Content -Path '{}' -Raw -ErrorAction Stop"
    ).format(path)
    return rtnval


def _probe_script(*, store: str, fingerprint: str) -> str:
    rtnval = (
        "$ErrorActionPreference = 'Stop'; "
        "$found = Get-ChildItem -Path '{store}' "
        "| Where-Object {{ $_.Thumbprint -eq '{fp}' }}; "
        "if ($null -eq $found) {{ exit 1 }} "
        "else {{ exit 0 }}"
    ).format(store=store, fp=fingerprint)
    return rtnval


def _import_script(*, file_path: str, store: str) -> str:
    rtnval = (
        "$ErrorActionPreference = 'Stop'; "
        "Import-Certificate -FilePath '{file}' "
        "-CertStoreLocation '{store}' | Out-Null"
    ).format(file=file_path, store=store)
    return rtnval


def _remove_script(*, store: str, fingerprint: str) -> str:
    rtnval = (
        "$ErrorActionPreference = 'Stop'; "
        "Get-ChildItem -Path '{store}' "
        "| Where-Object {{ $_.Thumbprint -eq '{fp}' }} "
        "| Remove-Item -Force"
    ).format(store=store, fp=fingerprint)
    return rtnval


def _list_script(*, store: str) -> str:
    rtnval = (
        "$ErrorActionPreference = 'Stop'; "
        "Get-ChildItem -Path '{store}' "
        "| Select-Object Thumbprint,Subject,Issuer,"
        "NotBefore,NotAfter,SerialNumber,PSPath,EnhancedKeyUsageList "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(store=store)
    return rtnval


def _details_script(*, store: str, fingerprint: str) -> str:
    rtnval = (
        "$ErrorActionPreference = 'Stop'; "
        "Get-ChildItem -Path '{store}' -Recurse "
        "| Where-Object {{ $_.Thumbprint -eq '{fp}' }} "
        "| Select-Object Thumbprint,Subject,Issuer,"
        "NotBefore,NotAfter,SerialNumber,PSPath,EnhancedKeyUsageList "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(store=store, fp=fingerprint)
    return rtnval


def _make_json_record(*, fingerprint: str) -> dict:
    rtnval = {
        "Thumbprint": fingerprint,
        "Subject": "CN=Foo CA, O=Foo Inc",
        "Issuer": "CN=Foo Root, O=Foo Inc",
        "NotBefore": "2024-01-01T00:00:00",
        "NotAfter": "2034-01-01T00:00:00",
        "SerialNumber": "0123456789ABCDEF",
        "PSPath": "Microsoft.PowerShell.Security\\Certificate::LocalMachine\\Root\\{}".format(fingerprint),
        "EnhancedKeyUsageList": [
            {"FriendlyName": "Server Authentication", "ObjectId": "1.3.6.1.5.5.7.3.1"},
            {"FriendlyName": "Client Authentication", "ObjectId": "1.3.6.1.5.5.7.3.2"},
        ],
    }
    return rtnval


# ----------------------------------------------------------------------
# add_trusted_cert
# ----------------------------------------------------------------------


def test_add_trusted_cert_invokes_import_certificate():
    """
        add_trusted_cert dispatches PowerShell Import-Certificate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_read_source_script(path=r"C:\tmp\foo.pem"),
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[_ps_argv(
        script=_probe_script(store=_LOCALMACHINE_ROOT, fingerprint=_foo_fp()),
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner,
        config=WindowsCertTrustManagerConfig(elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path=r"C:\tmp\foo.pem",
    )

    testbase.assert_equal(result.status, WindowsCertTrustActionStatus.SUCCESS)
    testbase.assert_equal(result.fingerprint, _foo_fp())
    import_calls = [
        c for c in runner.calls
        if "Import-Certificate" in (c["args"][-1] if len(c["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(import_calls), 1)
    testbase.assert_equal(import_calls[0]["elevated"], True)
    return


def test_add_trusted_cert_skipped_when_fingerprint_present():
    """
        add_trusted_cert returns SKIPPED when the store already
        contains a cert with the same Thumbprint.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_read_source_script(path=r"C:\tmp\foo.pem"),
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[_ps_argv(
        script=_probe_script(store=_LOCALMACHINE_ROOT, fingerprint=_foo_fp()),
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner, config=WindowsCertTrustManagerConfig(),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path=r"C:\tmp\foo.pem",
    )

    testbase.assert_equal(result.status, WindowsCertTrustActionStatus.SKIPPED)
    import_calls = [
        c for c in runner.calls
        if "Import-Certificate" in (c["args"][-1] if len(c["args"]) > 0 else "")
    ]
    testbase.assert_equal(import_calls, [])
    return


def test_add_trusted_cert_dry_run_short_circuits():
    """
        dry_run=True prevents Import-Certificate from being dispatched.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_read_source_script(path=r"C:\tmp\foo.pem"),
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[_ps_argv(
        script=_probe_script(store=_LOCALMACHINE_ROOT, fingerprint=_foo_fp()),
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner,
        config=WindowsCertTrustManagerConfig(dry_run=True, elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path=r"C:\tmp\foo.pem",
    )

    testbase.assert_equal(result.status, WindowsCertTrustActionStatus.DRY_RUN)
    import_calls = [
        c for c in runner.calls
        if "Import-Certificate" in (c["args"][-1] if len(c["args"]) > 0 else "")
    ]
    testbase.assert_equal(import_calls, [])
    return


def test_add_trusted_cert_invalid_pem_returns_failed():
    """
        Source bytes that are neither a valid PEM CERTIFICATE block nor
        decodable as DER produce FAILED with no Import-Certificate
        dispatch.
    """
    runner = _MockCommandRunner()
    runner.results[_ps_argv(
        script=_read_source_script(path=r"C:\tmp\garbage.pem"),
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner, config=WindowsCertTrustManagerConfig(elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path=r"C:\tmp\garbage.pem",
    )

    testbase.assert_equal(result.status, WindowsCertTrustActionStatus.FAILED)
    import_calls = [
        c for c in runner.calls
        if "Import-Certificate" in (c["args"][-1] if len(c["args"]) > 0 else "")
    ]
    testbase.assert_equal(import_calls, [])
    return


# ----------------------------------------------------------------------
# remove_trusted_cert
# ----------------------------------------------------------------------


def test_remove_trusted_cert_invokes_remove_item():
    """
        remove_trusted_cert dispatches a Get-ChildItem | Remove-Item
        pipeline.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_probe_script(store=_LOCALMACHINE_ROOT, fingerprint=_foo_fp()),
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner,
        config=WindowsCertTrustManagerConfig(elevated=True),
    )
    result = manager.remove_trusted_cert(
        store="system-ca", fingerprint=_foo_fp(),
    )

    testbase.assert_equal(result.status, WindowsCertTrustActionStatus.SUCCESS)
    remove_calls = [
        c for c in runner.calls
        if "Remove-Item" in (c["args"][-1] if len(c["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(remove_calls), 1)
    testbase.assert_equal(remove_calls[0]["elevated"], True)
    return


def test_remove_trusted_cert_skipped_when_absent():
    """
        remove_trusted_cert returns SKIPPED when the probe reports
        the fingerprint is absent.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=1, stdout="", stderr=""),
    )

    manager = WindowsCertificateTrustManager(
        runner=runner, config=WindowsCertTrustManagerConfig(),
    )
    result = manager.remove_trusted_cert(
        store="system-ca", fingerprint=_foo_fp(),
    )

    testbase.assert_equal(result.status, WindowsCertTrustActionStatus.SKIPPED)
    remove_calls = [
        c for c in runner.calls
        if "Remove-Item" in (c["args"][-1] if len(c["args"]) > 0 else "")
    ]
    testbase.assert_equal(remove_calls, [])
    return


# ----------------------------------------------------------------------
# list_trusted_certs / get_cert_details
# ----------------------------------------------------------------------


def test_list_trusted_certs_parses_json():
    """
        list_trusted_certs parses the ConvertTo-Json output.
    """
    records = [
        _make_json_record(fingerprint=_foo_fp()),
        _make_json_record(fingerprint=_bar_fp()),
    ]
    json_payload = json.dumps(records)

    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_list_script(store=_LOCALMACHINE_ROOT),
    )] = RunResult(exit_code=0, stdout=json_payload, stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner, config=WindowsCertTrustManagerConfig(),
    )
    info_list = manager.list_trusted_certs(store="system-ca")

    testbase.assert_equal(len(info_list), 2)
    fingerprints = sorted([info.fingerprint for info in info_list])
    expected = sorted([_foo_fp(), _bar_fp()])
    testbase.assert_equal(fingerprints, expected)
    testbase.assert_equal(
        info_list[0].purposes,
        ["Server Authentication", "Client Authentication"],
    )
    testbase.assert_equal(info_list[0].store, "system-ca")
    return


def test_list_trusted_certs_handles_single_record():
    """
        ConvertTo-Json emits a single object (not a list) when the
        upstream pipeline yields exactly one element; the manager
        normalises both shapes.
    """
    record = _make_json_record(fingerprint=_foo_fp())
    json_payload = json.dumps(record)

    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_list_script(store=_LOCALMACHINE_ROOT),
    )] = RunResult(exit_code=0, stdout=json_payload, stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner, config=WindowsCertTrustManagerConfig(),
    )
    info_list = manager.list_trusted_certs(store="system-ca")

    testbase.assert_equal(len(info_list), 1)
    testbase.assert_equal(info_list[0].fingerprint, _foo_fp())
    return


def test_get_cert_details_returns_full_info():
    """
        get_cert_details returns a populated WindowsCertTrustInfo when
        the fingerprint is found in any store.
    """
    record = _make_json_record(fingerprint=_foo_fp())
    json_payload = json.dumps(record)

    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=1, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_details_script(
            store=_LOCALMACHINE_ROOT, fingerprint=_foo_fp(),
        ),
    )] = RunResult(exit_code=0, stdout=json_payload, stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner, config=WindowsCertTrustManagerConfig(),
    )
    info = manager.get_cert_details(fingerprint=_foo_fp())

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.fingerprint, _foo_fp())
    testbase.assert_equal(info.serial_number, "0123456789ABCDEF")
    testbase.assert_equal(info.store, "system-ca")
    return


def test_get_cert_details_unknown_fingerprint_returns_none():
    """
        get_cert_details returns None when no store has the fingerprint.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=1, stdout="", stderr=""),
    )

    manager = WindowsCertificateTrustManager(
        runner=runner, config=WindowsCertTrustManagerConfig(),
    )
    info = manager.get_cert_details(fingerprint=_foo_fp())

    testbase.assert_equal(info, None)
    return


# ----------------------------------------------------------------------
# update_trust_store
# ----------------------------------------------------------------------


def test_update_trust_store_returns_success_noop():
    """
        update_trust_store is a SUCCESS no-op on Windows — runner is
        NOT invoked.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = WindowsCertificateTrustManager(
        runner=runner,
        config=WindowsCertTrustManagerConfig(elevated=True),
    )
    result = manager.update_trust_store()

    testbase.assert_equal(result.status, WindowsCertTrustActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# Store routing / elevation / errors
# ----------------------------------------------------------------------


def test_unsupported_store_returns_failed():
    """
        An unknown store name returns FAILED without dispatching.
    """
    runner = _MockCommandRunner()
    manager = WindowsCertificateTrustManager(
        runner=runner, config=WindowsCertTrustManagerConfig(),
    )
    result = manager.add_trusted_cert(
        store="bogus", path=r"C:\tmp\foo.pem",
    )
    testbase.assert_equal(result.status, WindowsCertTrustActionStatus.FAILED)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_unsupported_store_in_list_raises():
    """
        list_trusted_certs raises for an unknown store name.
    """
    runner = _MockCommandRunner()
    manager = WindowsCertificateTrustManager(
        runner=runner, config=WindowsCertTrustManagerConfig(),
    )
    raised = False
    try:
        manager.list_trusted_certs(store="bogus")
    except WindowsCertTrustQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_writes_pass_elevated_true_for_system_store():
    """
        With elevated=True, system-ca writes pass elevated=True to the
        runner.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_read_source_script(path=r"C:\tmp\foo.pem"),
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[_ps_argv(
        script=_probe_script(store=_LOCALMACHINE_ROOT, fingerprint=_foo_fp()),
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner,
        config=WindowsCertTrustManagerConfig(elevated=True),
    )
    manager.add_trusted_cert(store="system-ca", path=r"C:\tmp\foo.pem")

    import_calls = [
        c for c in runner.calls
        if "Import-Certificate" in (c["args"][-1] if len(c["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(import_calls), 1)
    testbase.assert_equal(import_calls[0]["elevated"], True)
    return


def test_user_store_writes_do_not_elevate():
    """
        Writes against user-ca pass elevated=False even when
        config.elevated=True (CurrentUser store needs no elevation).
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_read_source_script(path=r"C:\tmp\foo.pem"),
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[_ps_argv(
        script=_probe_script(store=_CURRENTUSER_ROOT, fingerprint=_foo_fp()),
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner,
        config=WindowsCertTrustManagerConfig(elevated=True),
    )
    manager.add_trusted_cert(store="user-ca", path=r"C:\tmp\foo.pem")

    import_calls = [
        c for c in runner.calls
        if "Import-Certificate" in (c["args"][-1] if len(c["args"]) > 0 else "")
    ]
    testbase.assert_equal(len(import_calls), 1)
    testbase.assert_equal(import_calls[0]["elevated"], False)
    return


def test_reads_never_elevate():
    """
        list_trusted_certs always passes elevated=False.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_list_script(store=_LOCALMACHINE_ROOT),
    )] = RunResult(exit_code=0, stdout="[]", stderr="")

    manager = WindowsCertificateTrustManager(
        runner=runner,
        config=WindowsCertTrustManagerConfig(elevated=True),
    )
    manager.list_trusted_certs(store="system-ca")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When Import-Certificate exits non-zero the result is FAILED
        with stderr / exit_code captured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[_ps_argv(
        script=_read_source_script(path=r"C:\tmp\foo.pem"),
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[_ps_argv(
        script=_probe_script(store=_LOCALMACHINE_ROOT, fingerprint=_foo_fp()),
    )] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[_ps_argv(
        script=_import_script(
            file_path=r"C:\tmp\foo.pem", store=_LOCALMACHINE_ROOT,
        ),
    )] = RunResult(
        exit_code=1, stdout="", stderr="access denied",
    )

    manager = WindowsCertificateTrustManager(
        runner=runner,
        config=WindowsCertTrustManagerConfig(elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path=r"C:\tmp\foo.pem",
    )

    testbase.assert_equal(result.status, WindowsCertTrustActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "access denied")
    return
