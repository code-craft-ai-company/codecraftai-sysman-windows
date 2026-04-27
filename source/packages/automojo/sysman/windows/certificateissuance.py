r"""
    Windows certificate-issuance feature.

    Implements the cross-OS certificate-issuance contract for Windows
    by wrapping the PowerShell ``PKI`` module
    (``New-SelfSignedCertificate`` / ``Get-ChildItem`` against
    ``Cert:\\`` / ``Export-PfxCertificate`` / ``Export-Certificate`` /
    ``Import-PfxCertificate`` / ``Import-Certificate``) plus optional
    ``certreq.exe`` invocations for Active Directory Certificate
    Services through the injected :class:`ICommandRunner`.

    Verb / tool mapping:

    +------------------------+-------------------------------------+
    | Verb                   | PowerShell PKI command              |
    +========================+=====================================+
    | ``request_certificate``| ``New-SelfSignedCertificate``       |
    |                        | ``-Subject 'CN=<cn>'``              |
    |                        | ``-CertStoreLocation 'Cert:\\<s>'`` |
    |                        | ``-DnsName @(<sans>)``              |
    |                        | ``-KeyAlgorithm RSA``               |
    |                        | ``-KeyLength <bits>``               |
    |                        | (or ``certreq.exe`` for AD CS)      |
    +------------------------+-------------------------------------+
    | ``renew_certificate``  | ``Get-ChildItem 'Cert:\\<s>' \|``   |
    |                        | ``Where-Object Subject -like ... \|``|
    |                        | ``ForEach-Object {                  |
    |                        | New-SelfSignedCertificate           |
    |                        | -CloneCert $_                       |
    |                        | -CertStoreLocation 'Cert:\\<s>'``   |
    |                        | ``}``                               |
    +------------------------+-------------------------------------+
    | ``revoke_certificate`` | local: ``Get-ChildItem ... \|``     |
    |                        | ``Remove-Item -Force``              |
    |                        | AD CS: ``certreq.exe -revoke``      |
    |                        | ``-config <ca> <serial>``           |
    |                        | ``[-reason <r>]``                   |
    +------------------------+-------------------------------------+
    | ``get_certificate``    | ``Get-ChildItem 'Cert:\\<s>'``      |
    |                        | ``-Recurse \| Where-Object ... \|`` |
    |                        | ``Select-Object ... \|``            |
    |                        | ``ConvertTo-Json -Compress``        |
    +------------------------+-------------------------------------+
    | ``list_certificates``  | ``Get-ChildItem 'Cert:\\<s>' \|``   |
    |                        | ``Select-Object ... \|``            |
    |                        | ``ConvertTo-Json -Compress``        |
    +------------------------+-------------------------------------+
    | ``export_certificate`` | ``Export-PfxCertificate`` (with     |
    |                        | private key) or                     |
    |                        | ``Export-Certificate`` (cert only)  |
    +------------------------+-------------------------------------+
    | ``import_certificate`` | ``Import-PfxCertificate`` (PFX) or  |
    |                        | ``Import-Certificate`` (cert)       |
    +------------------------+-------------------------------------+

    PFX vs PEM handling:
        ``include_private_key=True`` on :meth:`export_certificate`
        triggers PFX format (``.pfx`` / ``.p12``) — the only Windows-
        native cert+key envelope.  Callers MUST treat the resulting
        bundle as sensitive material and supply a non-trivial
        passphrase via the ``passphrase`` parameter.  ``include_private_key=False``
        emits a ``.cer`` (DER-encoded certificate file).

    PowerShell scripting policy:
        Path components and identifiers supplied to PowerShell are
        wrapped in single-quoted literals to suppress variable
        expansion of ``$``-prefixed tokens.  Single quotes inside the
        value are escaped by doubling.

    PFX password sanitisation:
        Passphrases supplied to :meth:`export_certificate` /
        :meth:`import_certificate` flow on runner ``stdin`` only —
        they NEVER appear on argv.  The recorded ``command`` field on
        the returned result is run through
        :func:`_sanitise_script_passwords` so any literal passphrase
        token is replaced with ``"<redacted>"`` before audit logs see
        it.

    Idempotency model:
        - :meth:`request_certificate` / :meth:`renew_certificate`
          return ``SKIPPED`` when the alias's existing certificate
          has more than ``config.renew_threshold_days`` of validity.
        - :meth:`revoke_certificate` returns ``SKIPPED`` when the
          alias is absent.
        - :meth:`import_certificate` returns ``SKIPPED`` when a
          certificate with the same Thumbprint is already in the
          store.
        - :meth:`export_certificate` returns ``SKIPPED`` when the
          destination path already byte-matches what would be
          written.

    Elevation policy:
        Mutating verbs honour ``config.elevated`` (typically required
        for ``LocalMachine`` writes); reads do not elevate.
"""

import json
import os

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_DEFAULT_POWERSHELL = "powershell.exe"
_DEFAULT_CERTREQ = "certreq.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

_DEFAULT_CERT_STORE = "LocalMachine\\My"


WindowsCertIssuanceActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------


class WindowsRevocationReason(Enum):
    """
        Revocation reason codes accepted by
        :meth:`WindowsCertificateIssuanceManager.revoke_certificate`.
        The string value matches certreq.exe's ``-reason`` flag
        vocabulary.
    """
    UNSPECIFIED            = "Unspecified"
    KEY_COMPROMISE         = "KeyCompromise"
    CA_COMPROMISE          = "CACompromise"
    AFFILIATION_CHANGED    = "AffiliationChanged"
    SUPERSEDED             = "Superseded"
    CESSATION_OF_OPERATION = "CessationOfOperation"


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class WindowsCertIssuanceManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsCertificateIssuanceManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param cert_store: PowerShell store location, with the
                           ``Cert:\\`` prefix omitted (e.g.
                           ``"LocalMachine\\My"``).  Defaults to
                           ``"LocalMachine\\My"``.
        :param ca_config: AD CS configuration string (e.g.
                          ``"CA01.example.com\\Issuing-CA"``).  When
                          set, ``request_certificate`` and
                          ``revoke_certificate`` route through
                          ``certreq.exe``.
        :param powershell_executable: PowerShell binary path.
        :param certreq_executable: certreq.exe binary path.
        :param renew_threshold_days: Skip threshold (in days) for
                                     :meth:`request_certificate` and
                                     :meth:`renew_certificate`.
                                     Defaults to ``30``.
    """
    cert_store: str = _DEFAULT_CERT_STORE
    ca_config: Optional[str] = None
    powershell_executable: str = _DEFAULT_POWERSHELL
    certreq_executable: str = _DEFAULT_CERTREQ
    renew_threshold_days: int = 30


@dataclass
class WindowsCertIssuanceActionResult(FeatureActionResult):
    """
        Result of a certificate-issuance write operation.

        :param alias: Alias acted upon.
        :param subject: Certificate subject DN, when known.
        :param fingerprint: Windows Thumbprint (uppercase hex no
                            colons), when known.
    """
    alias: Optional[str] = None
    subject: Optional[str] = None
    fingerprint: Optional[str] = None


@dataclass
class WindowsCertificateInfo:
    """
        Structured metadata for a single managed certificate.

        :param alias: Caller-chosen alias.
        :param subject: Certificate subject DN.
        :param issuer: Issuer DN.
        :param not_before: Validity start (PowerShell ISO format).
        :param not_after: Validity end (PowerShell ISO format).
        :param fingerprint: Windows Thumbprint (uppercase hex, no
                            colons).
        :param serial_number: Hex serial number.
        :param key_algorithm: ``"RSA"``, ``"ECDSA"``, etc., or
                              ``None``.
        :param key_length: Key size in bits, or ``None``.
        :param sans: SANs from the DnsNameList.
        :param path: PSPath of the certificate, when known.
        :param is_self_signed: ``True`` when subject == issuer.
    """
    alias: str
    subject: str
    issuer: str
    not_before: str
    not_after: str
    fingerprint: str
    serial_number: str
    key_algorithm: Optional[str]
    key_length: Optional[int]
    sans: list = field(default_factory=list)
    path: Optional[str] = None
    is_self_signed: bool = False


@dataclass
class WindowsCertificateRequestSpec:
    """
        Caller-supplied issuance spec.

        :param alias: Caller-chosen identifier.
        :param common_name: Certificate CN.
        :param email: Reserved for ACME / certreq enrollment email.
        :param sans: Additional SANs (DnsName entries).
        :param key_algorithm: ``"RSA"`` or ``"ECDSA"``.
        :param key_length: Key size in bits.
        :param validity_days: Days of validity.
        :param is_self_signed: When ``True``, force the
                               ``New-SelfSignedCertificate`` branch
                               regardless of ``config.ca_config``.
        :param webroot: Reserved (no Windows ACME support in pilot).
    """
    alias: str
    common_name: str
    email: Optional[str] = None
    sans: list = field(default_factory=list)
    key_algorithm: str = "RSA"
    key_length: int = 2048
    validity_days: int = 365
    is_self_signed: bool = False
    webroot: Optional[str] = None


class WindowsCertIssuanceQueryError(Exception):
    """
        Raised when a certificate-issuance read fails for a reason
        other than the target certificate simply being absent.
    """

    def __init__(
        self,
        *,
        operation: str,
        message: str,
    ) -> None:
        """
            :param operation: The verb that triggered the failure.
            :param message: Human-readable detail.
        """
        self.operation = operation
        self.message = message
        err_msg_lines = ["Certificate issuance query command failed."]
        err_msg_lines.append("    OPERATION: {}".format(operation))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsCertificateIssuanceManager(BaseFeatureManager):
    """
        Windows certificate-issuance manager.  Wraps the PowerShell
        ``PKI`` module plus optional ``certreq.exe`` for AD CS
        through the injected :class:`ICommandRunner`.
    """

    result_class = WindowsCertIssuanceActionResult
    status_class = WindowsCertIssuanceActionStatus
    feature_name = "certificate-issuance"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def request_certificate(
        self,
        spec: WindowsCertificateRequestSpec,
    ) -> WindowsCertIssuanceActionResult:
        """
            Issue a new certificate keyed by ``spec.alias``.

            Routes to ``certreq.exe`` when ``config.ca_config`` is
            set and ``spec.is_self_signed`` is ``False``; otherwise
            routes to ``New-SelfSignedCertificate``.

            :param spec: The :class:`WindowsCertificateRequestSpec`.

            :returns: The :class:`WindowsCertIssuanceActionResult`.
        """
        existing_info = self._get_cert_by_alias(alias=spec.alias)
        if existing_info is not None:
            days = _days_until(not_after=existing_info.not_after)
            if days is not None and days > self._typed_config().renew_threshold_days:
                skipped = self._build_result(
                    status=WindowsCertIssuanceActionStatus.SKIPPED,
                    alias=spec.alias,
                    subject=existing_info.subject,
                    fingerprint=existing_info.fingerprint,
                    command=None,
                    detail="certificate not yet within renew threshold",
                )
                return skipped

        cfg = self._typed_config()
        if cfg.ca_config is not None and spec.is_self_signed is False:
            rtnval = self._dispatch_certreq_request(spec=spec)
            return rtnval

        rtnval = self._dispatch_powershell_request(spec=spec)
        return rtnval

    def renew_certificate(
        self,
        alias: str,
    ) -> WindowsCertIssuanceActionResult:
        """
            Renew an existing certificate via
            ``New-SelfSignedCertificate -CloneCert``.

            :param alias: The caller-chosen alias.

            :returns: The :class:`WindowsCertIssuanceActionResult`.
        """
        existing_info = self._get_cert_by_alias(alias=alias)
        if existing_info is None:
            failed = self._build_result(
                status=WindowsCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="alias not found",
            )
            return failed

        days = _days_until(not_after=existing_info.not_after)
        if days is not None and days > self._typed_config().renew_threshold_days:
            skipped = self._build_result(
                status=WindowsCertIssuanceActionStatus.SKIPPED,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=None,
                detail="certificate not yet within renew threshold",
            )
            return skipped

        cfg = self._typed_config()
        store_path = self._store_path()
        escaped_store = _ps_escape_single_quoted(value=store_path)
        escaped_alias = _ps_escape_single_quoted(value=alias)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "$cert = Get-ChildItem -Path '{store}' "
            "| Where-Object {{ $_.Subject -like '*CN={alias}*' }} "
            "| Select-Object -First 1; "
            "if ($null -eq $cert) {{ exit 2 }}; "
            "New-SelfSignedCertificate -CloneCert $cert "
            "-CertStoreLocation '{store}' | Out-Null"
        ).format(store=escaped_store, alias=escaped_alias)
        argv = _build_powershell_argv(
            powershell=cfg.powershell_executable, script=script,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=argv,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = WindowsCertIssuanceActionStatus.FAILED

        rtnval = WindowsCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            alias=alias,
            subject=existing_info.subject,
            fingerprint=existing_info.fingerprint,
        )
        return rtnval

    def revoke_certificate(
        self,
        alias: str,
        reason: Optional[WindowsRevocationReason] = None,
    ) -> WindowsCertIssuanceActionResult:
        """
            Revoke an existing certificate.

            Routes to ``certreq.exe -revoke`` when ``config.ca_config``
            is set; otherwise removes the local store entry via
            ``Remove-Item``.

            :param alias: The caller-chosen alias.
            :param reason: Optional :class:`WindowsRevocationReason`.

            :returns: The :class:`WindowsCertIssuanceActionResult`.
        """
        existing_info = self._get_cert_by_alias(alias=alias)
        if existing_info is None:
            skipped = self._build_result(
                status=WindowsCertIssuanceActionStatus.SKIPPED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="alias not present",
            )
            return skipped

        cfg = self._typed_config()
        if cfg.ca_config is not None:
            argv = [
                cfg.certreq_executable,
                "-revoke",
                "-config", cfg.ca_config,
                existing_info.serial_number,
            ]
            if reason is not None:
                argv.append("-reason")
                argv.append(reason.value)
        else:
            store_path = self._store_path()
            escaped_store = _ps_escape_single_quoted(value=store_path)
            escaped_alias = _ps_escape_single_quoted(value=alias)
            script = (
                "$ErrorActionPreference = 'Stop'; "
                "Get-ChildItem -Path '{store}' "
                "| Where-Object {{ $_.Subject -like '*CN={alias}*' }} "
                "| Remove-Item -Force"
            ).format(store=escaped_store, alias=escaped_alias)
            argv = _build_powershell_argv(
                powershell=cfg.powershell_executable, script=script,
            )

        detail_text = None
        if reason is not None:
            detail_text = "revocation_reason={}".format(reason.value)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=argv,
                detail=detail_text,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = WindowsCertIssuanceActionStatus.FAILED

        rtnval = WindowsCertIssuanceActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            alias=alias,
            subject=existing_info.subject,
            fingerprint=existing_info.fingerprint,
        )
        return rtnval

    def export_certificate(
        self,
        alias: str,
        path: str,
        include_private_key: bool = False,
        passphrase: Optional[str] = None,
    ) -> WindowsCertIssuanceActionResult:
        """
            Export the alias's certificate (and optionally key) to
            ``path``.

            With ``include_private_key=False`` (the default) the
            output is a DER-encoded ``.cer`` file produced by
            ``Export-Certificate``.  With ``include_private_key=True``
            the output is a PFX/PKCS#12 bundle produced by
            ``Export-PfxCertificate``; the caller MUST supply a
            non-trivial ``passphrase``.

            The passphrase, when supplied, flows on runner ``stdin``
            only — never on argv — and is redacted from the recorded
            ``command`` field via
            :func:`_sanitise_script_passwords`.

            :param alias: The caller-chosen alias.
            :param path: Destination filesystem path.
            :param include_private_key: When ``True``, produce a PFX
                                        bundle.  Default ``False``.
            :param passphrase: PFX export password (required for PFX).

            :returns: The :class:`WindowsCertIssuanceActionResult`.
        """
        existing_info = self._get_cert_by_alias(alias=alias)
        if existing_info is None:
            failed = self._build_result(
                status=WindowsCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="alias not found",
            )
            return failed

        cfg = self._typed_config()
        store_path = self._store_path()
        escaped_store = _ps_escape_single_quoted(value=store_path)
        escaped_alias = _ps_escape_single_quoted(value=alias)
        escaped_path = _ps_escape_single_quoted(value=path)

        if include_private_key:
            script = (
                "$ErrorActionPreference = 'Stop'; "
                "$plain = [Console]::In.ReadLine(); "
                "$pw = ConvertTo-SecureString -String $plain "
                "-AsPlainText -Force; "
                "$cert = Get-ChildItem -Path '{store}' "
                "| Where-Object {{ $_.Subject -like '*CN={alias}*' }} "
                "| Select-Object -First 1; "
                "if ($null -eq $cert) {{ exit 2 }}; "
                "Export-PfxCertificate -Cert $cert "
                "-FilePath '{path}' -Password $pw | Out-Null"
            ).format(
                store=escaped_store,
                alias=escaped_alias,
                path=escaped_path,
            )
        else:
            script = (
                "$ErrorActionPreference = 'Stop'; "
                "$cert = Get-ChildItem -Path '{store}' "
                "| Where-Object {{ $_.Subject -like '*CN={alias}*' }} "
                "| Select-Object -First 1; "
                "if ($null -eq $cert) {{ exit 2 }}; "
                "Export-Certificate -Cert $cert "
                "-FilePath '{path}' | Out-Null"
            ).format(
                store=escaped_store,
                alias=escaped_alias,
                path=escaped_path,
            )

        argv = _build_powershell_argv(
            powershell=cfg.powershell_executable, script=script,
        )

        # Idempotency: when a destination file with the same hash is
        # already in place, skip.  The hash check is delegated to a
        # small probe script.
        if self._destination_already_matches(
            alias=alias, dest_path=path,
        ):
            skipped = self._build_result(
                status=WindowsCertIssuanceActionStatus.SKIPPED,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=None,
                detail="destination already matches source",
            )
            return skipped

        sanitised = _sanitise_script_passwords(
            argv=argv, passphrase=passphrase,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=sanitised,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
            stdin=passphrase if include_private_key else None,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = WindowsCertIssuanceActionStatus.FAILED

        rtnval = WindowsCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=sanitised,
            alias=alias,
            subject=existing_info.subject,
            fingerprint=existing_info.fingerprint,
        )
        return rtnval

    def import_certificate(
        self,
        path: str,
        alias: str,
        passphrase: Optional[str] = None,
    ) -> WindowsCertIssuanceActionResult:
        """
            Import an externally-issued certificate under ``alias``.

            Dispatches ``Import-PfxCertificate`` for ``.pfx`` /
            ``.p12`` sources and ``Import-Certificate`` for everything
            else.  The PFX password (when required) flows on runner
            ``stdin`` and is redacted from the recorded ``command``
            field.

            :param path: Source file path.
            :param alias: Target alias (recorded on the result; the
                          actual alias on Windows is the cert's
                          Subject CN).
            :param passphrase: PFX password (required for PFX).

            :returns: The :class:`WindowsCertIssuanceActionResult`.
        """
        is_pfx = path.lower().endswith(".pfx") or path.lower().endswith(".p12")

        # Compute the source thumbprint via PowerShell so we can
        # apply the idempotency guard.  For PFX we use Get-PfxData
        # so the password (if any) flows on stdin.
        thumbprint = self._compute_source_thumbprint(
            path=path, is_pfx=is_pfx, passphrase=passphrase,
        )
        if thumbprint is None:
            failed = self._build_result(
                status=WindowsCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="failed to compute source thumbprint",
            )
            return failed

        if self._thumbprint_in_store(fingerprint=thumbprint):
            skipped = self._build_result(
                status=WindowsCertIssuanceActionStatus.SKIPPED,
                alias=alias,
                subject=None,
                fingerprint=thumbprint,
                command=None,
                detail="certificate with same Thumbprint already in store",
            )
            return skipped

        cfg = self._typed_config()
        store_path = self._store_path()
        escaped_store = _ps_escape_single_quoted(value=store_path)
        escaped_path = _ps_escape_single_quoted(value=path)

        if is_pfx:
            script = (
                "$ErrorActionPreference = 'Stop'; "
                "$plain = [Console]::In.ReadLine(); "
                "$pw = ConvertTo-SecureString -String $plain "
                "-AsPlainText -Force; "
                "Import-PfxCertificate -FilePath '{path}' "
                "-CertStoreLocation 'Cert:\\{store}' "
                "-Password $pw | Out-Null"
            ).format(path=escaped_path, store=escaped_store)
        else:
            script = (
                "$ErrorActionPreference = 'Stop'; "
                "Import-Certificate -FilePath '{path}' "
                "-CertStoreLocation 'Cert:\\{store}' | Out-Null"
            ).format(path=escaped_path, store=escaped_store)

        argv = _build_powershell_argv(
            powershell=cfg.powershell_executable, script=script,
        )
        sanitised = _sanitise_script_passwords(
            argv=argv, passphrase=passphrase,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=None,
                fingerprint=thumbprint,
                command=sanitised,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
            stdin=passphrase if is_pfx else None,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = WindowsCertIssuanceActionStatus.FAILED

        rtnval = WindowsCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=sanitised,
            alias=alias,
            subject=None,
            fingerprint=thumbprint,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_certificate(
        self,
        alias: str,
    ) -> Optional[WindowsCertificateInfo]:
        """
            Return :class:`WindowsCertificateInfo` for ``alias``, or
            ``None`` when no matching certificate is in the store.

            :param alias: The caller-chosen alias.

            :returns: The :class:`WindowsCertificateInfo` or ``None``.
        """
        rtnval = self._get_cert_by_alias(alias=alias)
        return rtnval

    def list_certificates(self) -> list:
        """
            Enumerate every certificate in the configured store.

            :returns: A ``list[WindowsCertificateInfo]``.
        """
        cfg = self._typed_config()
        store_path = self._store_path()
        escaped_store = _ps_escape_single_quoted(value=store_path)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Get-ChildItem -Path '{store}' "
            "| Select-Object Thumbprint,Subject,Issuer,"
            "NotBefore,NotAfter,SerialNumber,DnsNameList,"
            "SignatureAlgorithm,PSPath "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(store=escaped_store)
        argv = _build_powershell_argv(
            powershell=cfg.powershell_executable, script=script,
        )
        run_result = self._runner.run(list(argv), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = _parse_powershell_certificates(stdout=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Backend dispatch
    # ------------------------------------------------------------------

    def _dispatch_powershell_request(
        self,
        *,
        spec: WindowsCertificateRequestSpec,
    ) -> WindowsCertIssuanceActionResult:
        """
            Dispatch ``New-SelfSignedCertificate``.
        """
        cfg = self._typed_config()
        store_path = self._store_path()
        escaped_store = _ps_escape_single_quoted(value=store_path)
        escaped_cn = _ps_escape_single_quoted(value=spec.common_name)

        san_args = ""
        if len(spec.sans) > 0:
            san_quoted = []
            for san in spec.sans:
                escaped = _ps_escape_single_quoted(value=san)
                san_quoted.append("'{}'".format(escaped))
            san_args = " -DnsName @({})".format(", ".join(san_quoted))

        script = (
            "$ErrorActionPreference = 'Stop'; "
            "New-SelfSignedCertificate "
            "-Subject 'CN={cn}' "
            "-CertStoreLocation '{store}'{sans} "
            "-KeyAlgorithm {alg} "
            "-KeyLength {bits} | Out-Null"
        ).format(
            cn=escaped_cn,
            store=escaped_store,
            sans=san_args,
            alg=spec.key_algorithm,
            bits=spec.key_length,
        )
        argv = _build_powershell_argv(
            powershell=cfg.powershell_executable, script=script,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsCertIssuanceActionStatus.DRY_RUN,
                alias=spec.alias,
                subject=spec.common_name,
                fingerprint=None,
                command=argv,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = WindowsCertIssuanceActionStatus.FAILED

        rtnval = WindowsCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            alias=spec.alias,
            subject=spec.common_name,
            fingerprint=None,
        )
        return rtnval

    def _dispatch_certreq_request(
        self,
        *,
        spec: WindowsCertificateRequestSpec,
    ) -> WindowsCertIssuanceActionResult:
        """
            Dispatch ``certreq.exe -submit`` for AD CS issuance.

            For brevity the pilot composes a single submit argv
            assuming an INF file has been pre-staged via the
            CSR-only helper.  The argv is:

                certreq.exe -submit -config <ca> <reqout> <cerout>
        """
        cfg = self._typed_config()
        req_path = "{}\\{}.req".format(self._cert_dir(), spec.alias)
        cer_path = "{}\\{}.cer".format(self._cert_dir(), spec.alias)
        argv = [
            cfg.certreq_executable,
            "-submit",
            "-config", cfg.ca_config,
            req_path,
            cer_path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsCertIssuanceActionStatus.DRY_RUN,
                alias=spec.alias,
                subject=spec.common_name,
                fingerprint=None,
                command=argv,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = WindowsCertIssuanceActionStatus.FAILED

        rtnval = WindowsCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            alias=spec.alias,
            subject=spec.common_name,
            fingerprint=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsCertIssuanceManagerConfig:
        """
            Narrow the base config to the typed subclass.
        """
        rtnval = self._config
        return rtnval

    def _store_path(self) -> str:
        """
            Return the bare ``LocalMachine\\My``-style store path
            used by the manager (no ``Cert:\\`` prefix).
        """
        cfg = self._typed_config()
        rtnval = cfg.cert_store
        return rtnval

    def _cert_dir(self) -> str:
        """
            Staging directory for certreq.exe-side INF / REQ / CER
            material.  Hardcoded relative to ``%PROGRAMDATA%`` for
            the pilot.
        """
        rtnval = "C:\\ProgramData\\automojo\\certs"
        return rtnval

    def _get_cert_by_alias(
        self,
        *,
        alias: str,
    ) -> Optional[WindowsCertificateInfo]:
        """
            Look up the alias in the configured store via PowerShell
            and parse the JSON result into a
            :class:`WindowsCertificateInfo`.
        """
        cfg = self._typed_config()
        store_path = self._store_path()
        escaped_store = _ps_escape_single_quoted(value=store_path)
        escaped_alias = _ps_escape_single_quoted(value=alias)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Get-ChildItem -Path '{store}' -Recurse "
            "| Where-Object {{ $_.Subject -like '*CN={alias}*' }} "
            "| Select-Object Thumbprint,Subject,Issuer,"
            "NotBefore,NotAfter,SerialNumber,DnsNameList,"
            "SignatureAlgorithm,PSPath "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(store=escaped_store, alias=escaped_alias)
        argv = _build_powershell_argv(
            powershell=cfg.powershell_executable, script=script,
        )
        run_result = self._runner.run(list(argv), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[WindowsCertificateInfo] = None
            return rtnval

        parsed = _parse_powershell_certificates(stdout=run_result.stdout)
        if len(parsed) == 0:
            rtnval = None
            return rtnval

        # Re-key the alias on the first match so the result mirrors
        # what the caller asked for.
        first = parsed[0]
        rtnval = WindowsCertificateInfo(
            alias=alias,
            subject=first.subject,
            issuer=first.issuer,
            not_before=first.not_before,
            not_after=first.not_after,
            fingerprint=first.fingerprint,
            serial_number=first.serial_number,
            key_algorithm=first.key_algorithm,
            key_length=first.key_length,
            sans=first.sans,
            path=first.path,
            is_self_signed=first.is_self_signed,
        )
        return rtnval

    def _thumbprint_in_store(
        self,
        *,
        fingerprint: str,
    ) -> bool:
        """
            Return ``True`` when a certificate with the supplied
            Thumbprint is present in the configured store.
        """
        cfg = self._typed_config()
        store_path = self._store_path()
        escaped_store = _ps_escape_single_quoted(value=store_path)
        escaped_fp = _ps_escape_single_quoted(value=fingerprint)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "$found = Get-ChildItem -Path '{store}' "
            "| Where-Object {{ $_.Thumbprint -eq '{fp}' }}; "
            "if ($null -eq $found) {{ exit 1 }} else {{ exit 0 }}"
        ).format(store=escaped_store, fp=escaped_fp)
        argv = _build_powershell_argv(
            powershell=cfg.powershell_executable, script=script,
        )
        run_result = self._runner.run(list(argv), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _compute_source_thumbprint(
        self,
        *,
        path: str,
        is_pfx: bool,
        passphrase: Optional[str],
    ) -> Optional[str]:
        """
            Compute the Thumbprint of an on-disk source certificate
            via PowerShell.
        """
        cfg = self._typed_config()
        escaped_path = _ps_escape_single_quoted(value=path)
        if is_pfx:
            script = (
                "$ErrorActionPreference = 'Stop'; "
                "$plain = [Console]::In.ReadLine(); "
                "$pw = ConvertTo-SecureString -String $plain "
                "-AsPlainText -Force; "
                "$data = Get-PfxData -FilePath '{path}' -Password $pw; "
                "$data.EndEntityCertificates[0].Thumbprint"
            ).format(path=escaped_path)
            stdin_payload = passphrase if passphrase is not None else ""
        else:
            script = (
                "$ErrorActionPreference = 'Stop'; "
                "(Get-PfxCertificate -FilePath '{path}').Thumbprint"
            ).format(path=escaped_path)
            stdin_payload = None
        argv = _build_powershell_argv(
            powershell=cfg.powershell_executable, script=script,
        )
        run_result = self._runner.run(
            list(argv),
            elevated=False,
            stdin=stdin_payload,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        text = run_result.stdout.strip()
        if len(text) == 0:
            rtnval = None
            return rtnval
        rtnval = _normalize_thumbprint(value=text)
        return rtnval

    def _destination_already_matches(
        self,
        *,
        alias: str,
        dest_path: str,
    ) -> bool:
        """
            Return ``True`` when the destination file already has the
            same Thumbprint as the alias's cert in the store.

            The probe runs `Get-PfxCertificate` (which works for both
            cert and PFX files) on the destination and compares
            the Thumbprint to the live cert.  Returns ``False`` when
            the destination does not exist or any step fails.
        """
        cfg = self._typed_config()
        store_path = self._store_path()
        escaped_store = _ps_escape_single_quoted(value=store_path)
        escaped_alias = _ps_escape_single_quoted(value=alias)
        escaped_dest = _ps_escape_single_quoted(value=dest_path)
        script = (
            "$ErrorActionPreference = 'SilentlyContinue'; "
            "if (-not (Test-Path -Path '{dest}')) {{ exit 1 }}; "
            "$cert = Get-ChildItem -Path '{store}' "
            "| Where-Object {{ $_.Subject -like '*CN={alias}*' }} "
            "| Select-Object -First 1; "
            "if ($null -eq $cert) {{ exit 1 }}; "
            "$dest = Get-PfxCertificate -FilePath '{dest}'; "
            "if ($null -eq $dest) {{ exit 1 }}; "
            "if ($cert.Thumbprint -eq $dest.Thumbprint) "
            "{{ exit 0 }} else {{ exit 1 }}"
        ).format(
            dest=escaped_dest, store=escaped_store, alias=escaped_alias,
        )
        argv = _build_powershell_argv(
            powershell=cfg.powershell_executable, script=script,
        )
        run_result = self._runner.run(list(argv), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsCertIssuanceActionStatus,
        alias: Optional[str],
        subject: Optional[str],
        fingerprint: Optional[str],
        command: Optional[list],
        detail: Optional[str] = None,
    ) -> WindowsCertIssuanceActionResult:
        """
            Construct a :class:`WindowsCertIssuanceActionResult`
            without invoking the runner.
        """
        if command is None:
            command_copy: Optional[list] = None
        else:
            command_copy = list(command)
        rtnval = WindowsCertIssuanceActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            alias=alias,
            subject=subject,
            fingerprint=fingerprint,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PowerShell escape + argv build
# ----------------------------------------------------------------------


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.

        :param value: The raw string.

        :returns: The escaped string.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _build_powershell_argv(*, powershell: str, script: str) -> list:
    """
        Build the canonical PowerShell argv for a scriptblock.

        :param powershell: The PowerShell binary path.
        :param script: The PowerShell script text.

        :returns: The argv list.
    """
    rtnval = [
        powershell,
        _PS_NOPROFILE,
        _PS_NONINTERACTIVE,
        _PS_COMMAND,
        script,
    ]
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — JSON parsing
# ----------------------------------------------------------------------


def _parse_powershell_certificates(*, stdout: str) -> list:
    """
        Parse the JSON emitted by ``Get-ChildItem ... | Select-Object
        ... | ConvertTo-Json -Compress`` into a list of
        :class:`WindowsCertificateInfo`.

        ``ConvertTo-Json`` emits a single object when the upstream
        pipeline yields a single element and an array otherwise.
        Both cases are normalised to a list.

        :param stdout: Captured runner stdout.

        :returns: A ``list[WindowsCertificateInfo]``.
    """
    rtnval: list = []
    text = stdout.strip()
    if len(text) == 0:
        return rtnval

    try:
        parsed = json.loads(text)
    except (ValueError, Exception):
        return rtnval

    if isinstance(parsed, dict):
        records = [parsed]
    elif isinstance(parsed, list):
        records = parsed
    else:
        return rtnval

    for record in records:
        if not isinstance(record, dict):
            continue
        thumbprint = record.get("Thumbprint")
        if thumbprint is None:
            continue
        thumbprint_norm = _normalize_thumbprint(value=str(thumbprint))
        subject = str(record.get("Subject", "") or "")
        issuer = str(record.get("Issuer", "") or "")
        not_before = str(record.get("NotBefore", "") or "")
        not_after = str(record.get("NotAfter", "") or "")
        serial = str(record.get("SerialNumber", "") or "")
        ps_path_raw = record.get("PSPath", None)
        ps_path = str(ps_path_raw) if ps_path_raw is not None else None

        sans: list = []
        dns_raw = record.get("DnsNameList", None)
        if isinstance(dns_raw, list):
            for entry in dns_raw:
                if isinstance(entry, dict):
                    name_value = entry.get("Unicode", entry.get("Punycode", ""))
                    name_text = str(name_value) if name_value is not None else ""
                    if len(name_text) > 0:
                        sans.append(name_text)
                elif isinstance(entry, str):
                    if len(entry) > 0:
                        sans.append(entry)

        signature_alg_raw = record.get("SignatureAlgorithm", None)
        key_algorithm: Optional[str] = None
        if isinstance(signature_alg_raw, dict):
            friendly = signature_alg_raw.get("FriendlyName", None)
            if friendly is not None:
                if "RSA" in str(friendly).upper():
                    key_algorithm = "RSA"
                elif "ECDSA" in str(friendly).upper() or "EC" in str(friendly).upper():
                    key_algorithm = "ECDSA"
                else:
                    key_algorithm = str(friendly)

        is_self_signed = subject == issuer

        # alias defaults to the subject's CN component when the caller
        # has not supplied one.
        alias_value = ""
        for chunk in subject.split(","):
            chunk_clean = chunk.strip()
            if chunk_clean.upper().startswith("CN="):
                alias_value = chunk_clean[len("CN="):].strip()
                break
        if len(alias_value) == 0:
            alias_value = thumbprint_norm

        info = WindowsCertificateInfo(
            alias=alias_value,
            subject=subject,
            issuer=issuer,
            not_before=not_before,
            not_after=not_after,
            fingerprint=thumbprint_norm,
            serial_number=serial,
            key_algorithm=key_algorithm,
            key_length=None,
            sans=sans,
            path=ps_path,
            is_self_signed=is_self_signed,
        )
        rtnval.append(info)

    return rtnval


def _normalize_thumbprint(*, value: str) -> str:
    """
        Normalise a Thumbprint string to the Windows canonical form
        (uppercase hex, no colons).
    """
    text = value.strip()
    text = text.replace(":", "").replace(" ", "").upper()
    rtnval = text
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — date math
# ----------------------------------------------------------------------


def _days_until(*, not_after: str) -> Optional[int]:
    """
        Parse a PowerShell ISO-formatted ``NotAfter`` string and return
        integer days between now (UTC) and that time.

        Examples accepted:

        - ``"2030-01-01T00:00:00"``
        - ``"2030-01-01 00:00:00"``
        - ``"2030-01-01T00:00:00Z"``
        - ``"2030-01-01T00:00:00+00:00"``
        - the ``\\/Date(.../)`` form some older PowerShell ConvertTo-Json
          builds emit (handled by stripping the wrapper).
    """
    if len(not_after) == 0:
        rtnval: Optional[int] = None
        return rtnval

    text = not_after.strip()

    if text.startswith("\\/Date(") and text.endswith(")\\/"):
        inner = text[len("\\/Date("):-len(")\\/")]
        # The form may be "1893456000000" or "1893456000000+0000"
        plus_at = inner.find("+")
        minus_at = inner.find("-", 1)
        cut = -1
        if plus_at >= 0:
            cut = plus_at
        elif minus_at >= 0:
            cut = minus_at
        if cut >= 0:
            inner = inner[:cut]
        try:
            ms_epoch = int(inner)
            seconds = ms_epoch / 1000.0
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            delta = parsed - now
            rtnval = delta.days
            return rtnval
        except (ValueError, Exception):
            rtnval = None
            return rtnval

    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%m/%d/%Y %I:%M:%S %p",
    ]
    parsed: Optional[datetime] = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except (ValueError, Exception):
            continue

    if parsed is None:
        rtnval = None
        return rtnval

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    now = datetime.now(tz=timezone.utc)
    delta = parsed - now
    rtnval = delta.days
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — passphrase sanitisation
# ----------------------------------------------------------------------


def _sanitise_script_passwords(
    *,
    argv: list,
    passphrase: Optional[str],
) -> list:
    """
        Redact any literal occurrences of ``passphrase`` from a
        PowerShell scriptblock argv, returning a fresh list.

        This module always passes passphrases on stdin — never argv —
        so this helper is a defence-in-depth measure: in the unlikely
        case a future change embeds the passphrase in the script, the
        recorded ``command`` field will still mask it.

        :param argv: The original argv.
        :param passphrase: The secret to redact.  When ``None``, the
                           argv is returned as a fresh list with no
                           changes.

        :returns: A new ``list[str]`` with the secret replaced by
                  ``"<redacted>"`` wherever it appeared.
    """
    if passphrase is None or len(passphrase) == 0:
        rtnval = list(argv)
        return rtnval

    rtnval = []
    for token in argv:
        if isinstance(token, str) and passphrase in token:
            rtnval.append(token.replace(passphrase, "<redacted>"))
        else:
            rtnval.append(token)
    return rtnval
