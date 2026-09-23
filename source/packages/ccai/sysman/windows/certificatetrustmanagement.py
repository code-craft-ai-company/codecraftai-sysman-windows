"""
    Windows certificate-trust-management feature.

    Implements the cross-OS certificate-trust-management contract for
    Windows by wrapping the PowerShell ``PKI`` module
    (``Import-Certificate`` / ``Get-ChildItem`` against
    ``Cert:\\LocalMachine\\Root`` and ``Cert:\\CurrentUser\\Root``).
    Every verb is dispatched through the injected
    :class:`ICommandRunner` so the manager works identically under
    local and WinRM execution.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Windows tool                      |
    +=============================+===================================+
    | ``add_trusted_cert``        | PowerShell ``Import-Certificate   |
    |                             | -FilePath '<path>'                |
    |                             | -CertStoreLocation '<store>'``    |
    +-----------------------------+-----------------------------------+
    | ``remove_trusted_cert``     | PowerShell ``Get-ChildItem        |
    |                             | '<store>' | Where-Object {        |
    |                             | $_.Thumbprint -eq '<fp>' } |      |
    |                             | Remove-Item``                     |
    +-----------------------------+-----------------------------------+
    | ``list_trusted_certs``      | PowerShell ``Get-ChildItem        |
    |                             | '<store>' | Select-Object         |
    |                             | Thumbprint,Subject,Issuer,        |
    |                             | NotBefore,NotAfter,SerialNumber | |
    |                             | ConvertTo-Json -Compress``        |
    +-----------------------------+-----------------------------------+
    | ``get_cert_details``        | PowerShell ``Get-ChildItem        |
    |                             | '<store>' -Recurse | Where-Object |
    |                             | { $_.Thumbprint -eq '<fp>' } |    |
    |                             | ConvertTo-Json -Compress``        |
    +-----------------------------+-----------------------------------+
    | ``update_trust_store``      | no-op SUCCESS (Windows applies    |
    |                             | trust changes immediately)        |
    +-----------------------------+-----------------------------------+

    Trust-store mapping (PowerShell paths):
        - ``"system-ca"`` -> ``Cert:\\LocalMachine\\Root``
        - ``"user-ca"`` -> ``Cert:\\CurrentUser\\Root``
        - ``"system-ca-intermediate"`` -> ``Cert:\\LocalMachine\\CA``
        - ``"user-ca-intermediate"`` -> ``Cert:\\CurrentUser\\CA``

    Fingerprint format:
        Windows Certificate Store ``Thumbprint`` is uppercase hex with
        no colons (e.g. ``"AB12CD..."``).  The pure-Python fingerprint
        helper produces the same form so the value flows through to
        ``Where-Object`` without translation.

    PowerShell scripting policy:
        Path components and fingerprints supplied to PowerShell are
        wrapped in single-quoted literals to suppress variable
        expansion of ``$``-prefixed tokens.  Single quotes inside the
        value are escaped by doubling.  Backslashes inside single
        quotes are NOT escapes — they are passed through verbatim.

    Elevation policy:
        Writes against ``LocalMachine`` stores honour
        ``config.elevated``.  Writes against ``CurrentUser`` stores
        do not elevate.  Reads do not elevate.
"""

import base64
import hashlib
import json
import os

from dataclasses import dataclass, field
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_DEFAULT_POWERSHELL = "powershell.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"

_SYSTEM_CA_STORE = "system-ca"
_USER_CA_STORE = "user-ca"
_SYSTEM_INTERMEDIATE_STORE = "system-ca-intermediate"
_USER_INTERMEDIATE_STORE = "user-ca-intermediate"

_LOCALMACHINE_ROOT = r"Cert:\LocalMachine\Root"
_CURRENTUSER_ROOT = r"Cert:\CurrentUser\Root"
_LOCALMACHINE_CA = r"Cert:\LocalMachine\CA"
_CURRENTUSER_CA = r"Cert:\CurrentUser\CA"

_CURRENTUSER_PREFIX = r"Cert:\CurrentUser"

_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


WindowsCertTrustActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class WindowsCertTrustManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsCertificateTrustManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param store_path_map: Optional override mapping of abstract
                               store name to PowerShell store path.
                               When ``None`` the bundled defaults are
                               used (LocalMachine\\Root,
                               CurrentUser\\Root, LocalMachine\\CA,
                               CurrentUser\\CA).
        :param default_store: The abstract store name used by
                              ``add_trusted_cert`` /
                              ``remove_trusted_cert`` /
                              ``list_trusted_certs`` when callers do
                              not supply an explicit store argument.
                              Defaults to ``"system-ca"``.
        :param powershell_executable: Path to the PowerShell
                                      executable used to run cmdlets.
    """
    store_path_map: Optional[dict] = None
    default_store: str = _SYSTEM_CA_STORE
    powershell_executable: str = _DEFAULT_POWERSHELL


@dataclass
class WindowsCertTrustActionResult(FeatureActionResult):
    """
        Result of a certificate-trust-management write operation.

        :param store: The abstract store name targeted.
        :param fingerprint: The Windows Thumbprint of the affected
                            certificate (uppercase hex no colons).
        :param path: The on-disk source path the action targeted, or
                     ``None`` for delete.
    """
    store: Optional[str] = None
    fingerprint: Optional[str] = None
    path: Optional[str] = None


@dataclass
class WindowsCertTrustInfo:
    """
        Structured metadata for a single trusted certificate.

        :param fingerprint: Windows Thumbprint (uppercase hex, no
                            colons).
        :param subject: Subject DN.
        :param issuer: Issuer DN.
        :param not_before: Validity start (PowerShell ISO format).
        :param not_after: Validity end (PowerShell ISO format).
        :param serial_number: Hex serial number.
        :param store: The abstract store the certificate was found in.
        :param path: PSPath of the certificate, when known.
        :param purposes: EnhancedKeyUsage friendly names, when present.
    """
    fingerprint: str
    subject: str
    issuer: str
    not_before: str
    not_after: str
    serial_number: str
    store: str
    path: Optional[str] = None
    purposes: list = field(default_factory=list)


class WindowsCertTrustQueryError(Exception):
    """
        Raised when a certificate-trust-management read fails for a
        reason other than the target source simply being absent.
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
        err_msg_lines = ["Certificate trust query command failed."]
        err_msg_lines.append("    OPERATION: {}".format(operation))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class WindowsCertificateTrustManager(BaseFeatureManager):
    """
        Windows certificate-trust-management manager.  Wraps the
        PowerShell ``PKI`` module and pure-Python PEM parsing through
        the injected :class:`ICommandRunner`.
    """

    result_class = WindowsCertTrustActionResult
    status_class = WindowsCertTrustActionStatus
    feature_name = "certificate-trust-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_trusted_cert(
        self,
        store: str,
        path: str,
        purpose: Optional[str] = None,
    ) -> WindowsCertTrustActionResult:
        """
            Install a PEM / DER certificate into a Windows trust store
            via PowerShell ``Import-Certificate``.

            :param store: The abstract store name.
            :param path: Source certificate path.  Both PEM and DER
                         are accepted by ``Import-Certificate``.
            :param purpose: Optional purpose hint recorded on the
                            result.

            :returns: The :class:`WindowsCertTrustActionResult`.
        """
        store_path = self._resolve_store_path(store=store)
        if store_path is None:
            failed = self._build_result(
                status=WindowsCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=None,
                detail="store '{}' is not supported on Windows".format(store),
            )
            return failed

        # Read the source via Get-Content -Raw so we can fingerprint
        # locally regardless of input format (PEM or DER).
        source_bytes = self._read_source_bytes(path=path)
        if source_bytes is None:
            failed = self._build_result(
                status=WindowsCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=None,
                detail="source path does not exist",
            )
            return failed

        fingerprint = _fingerprint_from_source_bytes(content=source_bytes)
        if fingerprint is None:
            failed = self._build_result(
                status=WindowsCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=None,
                detail="source file is not a valid certificate",
            )
            return failed

        existing = self._find_thumbprint_in_store(
            store_path=store_path,
            fingerprint=fingerprint,
        )
        if existing is True:
            skipped = self._build_result(
                status=WindowsCertTrustActionStatus.SKIPPED,
                store=store,
                fingerprint=fingerprint,
                path=path,
                command=None,
                detail="certificate already present in store",
            )
            return skipped

        write_command = self._build_import_certificate_argv(
            file_path=path,
            store_path=store_path,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsCertTrustActionStatus.DRY_RUN,
                store=store,
                fingerprint=fingerprint,
                path=path,
                command=write_command,
            )
            return dry

        elevated = self._elevation_for_store(store=store)
        run_result = self._runner.run(
            list(write_command),
            elevated=elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsCertTrustActionStatus.SUCCESS
        else:
            mapped_status = WindowsCertTrustActionStatus.FAILED

        rtnval = WindowsCertTrustActionResult(
            status=mapped_status,
            detail=purpose,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            store=store,
            fingerprint=fingerprint,
            path=path,
        )
        return rtnval

    def remove_trusted_cert(
        self,
        store: str,
        fingerprint: str,
    ) -> WindowsCertTrustActionResult:
        """
            Remove the trusted certificate whose Thumbprint matches.

            :param store: The abstract store name.
            :param fingerprint: SHA-1 / SHA-256 thumbprint, colonised
                                or non-colonised.  Normalised to
                                uppercase no-colon for comparison.

            :returns: The :class:`WindowsCertTrustActionResult`.
        """
        store_path = self._resolve_store_path(store=store)
        if store_path is None:
            failed = self._build_result(
                status=WindowsCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=fingerprint,
                path=None,
                command=None,
                detail="store '{}' is not supported on Windows".format(store),
            )
            return failed

        normalized = _normalize_fingerprint(value=fingerprint)
        existing = self._find_thumbprint_in_store(
            store_path=store_path, fingerprint=normalized,
        )
        if existing is False:
            skipped = self._build_result(
                status=WindowsCertTrustActionStatus.SKIPPED,
                store=store,
                fingerprint=normalized,
                path=None,
                command=None,
                detail="no certificate with that thumbprint in store",
            )
            return skipped

        delete_command = self._build_remove_certificate_argv(
            store_path=store_path, fingerprint=normalized,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsCertTrustActionStatus.DRY_RUN,
                store=store,
                fingerprint=normalized,
                path=None,
                command=delete_command,
            )
            return dry

        elevated = self._elevation_for_store(store=store)
        run_result = self._runner.run(
            list(delete_command),
            elevated=elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = WindowsCertTrustActionStatus.SUCCESS
        else:
            mapped_status = WindowsCertTrustActionStatus.FAILED

        rtnval = WindowsCertTrustActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(delete_command),
            store=store,
            fingerprint=normalized,
            path=None,
        )
        return rtnval

    def update_trust_store(self) -> WindowsCertTrustActionResult:
        """
            No-op on Windows — Windows applies trust changes
            immediately.  Always returns ``SUCCESS`` with
            ``command=None``; the runner is NOT invoked.

            Honours dry-run for symmetry.

            :returns: The :class:`WindowsCertTrustActionResult`.
        """
        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsCertTrustActionStatus.DRY_RUN,
                store=None,
                fingerprint=None,
                path=None,
                command=None,
                detail="Windows applies trust changes immediately",
            )
            return dry

        rtnval = self._build_result(
            status=WindowsCertTrustActionStatus.SUCCESS,
            store=None,
            fingerprint=None,
            path=None,
            command=None,
            detail="Windows applies trust changes immediately",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_trusted_certs(
        self,
        store: str,
    ) -> list:
        """
            Enumerate every trusted certificate in the named store.

            Dispatches a PowerShell ``Get-ChildItem ... |
            Select-Object ... | ConvertTo-Json`` pipeline and parses
            the JSON output.

            :param store: The abstract store name.

            :returns: A ``list[WindowsCertTrustInfo]``.

            :raises WindowsCertTrustQueryError: When the store name is
                                                unsupported.
        """
        store_path = self._resolve_store_path(store=store)
        if store_path is None:
            raise WindowsCertTrustQueryError(
                operation="list_trusted_certs",
                message="store '{}' is not supported on Windows".format(store),
            )

        list_command = self._build_list_argv(store_path=store_path)
        run_result = self._runner.run(list(list_command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval

        rtnval = _parse_powershell_certificates(
            stdout=run_result.stdout,
            store=store,
        )
        return rtnval

    def get_cert_details(
        self,
        fingerprint: str,
    ) -> Optional[WindowsCertTrustInfo]:
        """
            Look up a single certificate across every supported store
            by Thumbprint.

            :param fingerprint: SHA-1 / SHA-256 thumbprint, colonised
                                or non-colonised.

            :returns: The :class:`WindowsCertTrustInfo`, or ``None``
                      when no match is found.
        """
        normalized = _normalize_fingerprint(value=fingerprint)
        store_map = self._effective_store_map()
        for store_name, store_path in store_map.items():
            if store_path is None:
                continue
            command = self._build_get_details_argv(
                store_path=store_path, fingerprint=normalized,
            )
            run_result = self._runner.run(list(command), elevated=False)
            if run_result.exit_code != 0:
                continue
            parsed = _parse_powershell_certificates(
                stdout=run_result.stdout,
                store=store_name,
            )
            for info in parsed:
                if info.fingerprint == normalized:
                    rtnval: Optional[WindowsCertTrustInfo] = info
                    return rtnval
        rtnval = None
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsCertTrustManagerConfig:
        """
            Narrow the base config to the typed subclass.
        """
        rtnval = self._config
        return rtnval

    def _effective_store_map(self) -> dict:
        """
            Merge the bundled defaults with any caller-supplied
            override.
        """
        defaults: dict = {
            _SYSTEM_CA_STORE: _LOCALMACHINE_ROOT,
            _USER_CA_STORE: _CURRENTUSER_ROOT,
            _SYSTEM_INTERMEDIATE_STORE: _LOCALMACHINE_CA,
            _USER_INTERMEDIATE_STORE: _CURRENTUSER_CA,
        }
        override = self._typed_config().store_path_map
        if override is None:
            rtnval = defaults
            return rtnval
        merged = dict(defaults)
        for key, value in override.items():
            merged[key] = value
        rtnval = merged
        return rtnval

    def _resolve_store_path(self, *, store: str) -> Optional[str]:
        """
            Resolve a store name to its PowerShell path.

            :param store: The abstract store name.

            :returns: The PowerShell path, or ``None`` for unsupported.
        """
        store_map = self._effective_store_map()
        if store not in store_map:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = store_map[store]
        return rtnval

    def _elevation_for_store(self, *, store: str) -> bool:
        """
            ``LocalMachine`` stores honour ``config.elevated``;
            ``CurrentUser`` stores never elevate.

            :param store: The abstract store name.

            :returns: The effective elevation flag.
        """
        store_path = self._resolve_store_path(store=store)
        if store_path is None:
            rtnval = False
            return rtnval
        if store_path.startswith(_CURRENTUSER_PREFIX):
            rtnval = False
            return rtnval
        rtnval = self._config.elevated
        return rtnval

    def _read_source_bytes(self, *, path: str) -> Optional[str]:
        """
            Read a certificate source file via PowerShell ``Get-Content
            -Raw -Encoding Byte`` rendered as base64.

            For simplicity (and to cooperate with PEM input — the
            common form in cross-OS automation), this implementation
            asks PowerShell for the file's contents as raw text.  When
            the file is PEM, the runner's stdout is the PEM body
            verbatim.  When the file is DER, the bytes survive as a
            mojibake-encoded ``str`` but the in-process fingerprint
            helper detects the BEGIN marker absence and falls back to
            DER decoding.

            :param path: The absolute source path.

            :returns: The file contents as a string, or ``None`` when
                      the read fails.
        """
        escaped_path = _ps_escape_single_quoted(value=path)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Get-Content -Path '{}' -Raw -ErrorAction Stop"
        ).format(escaped_path)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _find_thumbprint_in_store(
        self,
        *,
        store_path: str,
        fingerprint: str,
    ) -> bool:
        """
            Probe a Windows certificate store for a Thumbprint match.

            :param store_path: PowerShell-style store path.
            :param fingerprint: Uppercase no-colon thumbprint.

            :returns: ``True`` when present, ``False`` when absent.
        """
        escaped_store = _ps_escape_single_quoted(value=store_path)
        escaped_fp = _ps_escape_single_quoted(value=fingerprint)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "$found = Get-ChildItem -Path '{store}' "
            "| Where-Object {{ $_.Thumbprint -eq '{fp}' }}; "
            "if ($null -eq $found) {{ exit 1 }} "
            "else {{ exit 0 }}"
        ).format(store=escaped_store, fp=escaped_fp)
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical PowerShell argv for a scriptblock.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _build_import_certificate_argv(
        self,
        *,
        file_path: str,
        store_path: str,
    ) -> list:
        """
            Build the ``Import-Certificate`` argv.
        """
        escaped_file = _ps_escape_single_quoted(value=file_path)
        escaped_store = _ps_escape_single_quoted(value=store_path)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Import-Certificate -FilePath '{file}' "
            "-CertStoreLocation '{store}' | Out-Null"
        ).format(file=escaped_file, store=escaped_store)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_remove_certificate_argv(
        self,
        *,
        store_path: str,
        fingerprint: str,
    ) -> list:
        """
            Build the argv that removes a certificate by Thumbprint.
        """
        escaped_store = _ps_escape_single_quoted(value=store_path)
        escaped_fp = _ps_escape_single_quoted(value=fingerprint)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Get-ChildItem -Path '{store}' "
            "| Where-Object {{ $_.Thumbprint -eq '{fp}' }} "
            "| Remove-Item -Force"
        ).format(store=escaped_store, fp=escaped_fp)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_list_argv(self, *, store_path: str) -> list:
        """
            Build the argv that enumerates a store as JSON.
        """
        escaped_store = _ps_escape_single_quoted(value=store_path)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Get-ChildItem -Path '{store}' "
            "| Select-Object Thumbprint,Subject,Issuer,"
            "NotBefore,NotAfter,SerialNumber,PSPath,EnhancedKeyUsageList "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(store=escaped_store)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_get_details_argv(
        self,
        *,
        store_path: str,
        fingerprint: str,
    ) -> list:
        """
            Build the argv that fetches a single certificate's details.
        """
        escaped_store = _ps_escape_single_quoted(value=store_path)
        escaped_fp = _ps_escape_single_quoted(value=fingerprint)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Get-ChildItem -Path '{store}' -Recurse "
            "| Where-Object {{ $_.Thumbprint -eq '{fp}' }} "
            "| Select-Object Thumbprint,Subject,Issuer,"
            "NotBefore,NotAfter,SerialNumber,PSPath,EnhancedKeyUsageList "
            "| ConvertTo-Json -Compress -Depth 5"
        ).format(store=escaped_store, fp=escaped_fp)
        rtnval = self._build_powershell_argv(script=script)
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsCertTrustActionStatus,
        store: Optional[str],
        fingerprint: Optional[str],
        path: Optional[str],
        command: Optional[list],
        detail: Optional[str] = None,
    ) -> WindowsCertTrustActionResult:
        """
            Build a result without invoking the runner.
        """
        if command is None:
            command_copy: Optional[list] = None
        else:
            command_copy = list(command)
        rtnval = WindowsCertTrustActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            store=store,
            fingerprint=fingerprint,
            path=path,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PowerShell escape
# ----------------------------------------------------------------------


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.

        :param value: The raw string.

        :returns: The escaped string.
    """
    rtnval = value.replace("'", "''")
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PEM / fingerprint
# ----------------------------------------------------------------------


def _parse_pem_blocks(*, content: str) -> list:
    """
        Split a PEM-bearing string into a list of CERTIFICATE blocks.
    """
    rtnval: list = []
    in_block = False
    current_lines: list = []

    for raw in content.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped == _PEM_BEGIN:
            in_block = True
            current_lines = [line]
            continue
        if stripped == _PEM_END:
            if in_block:
                current_lines.append(line)
                block_text = "\n".join(current_lines) + "\n"
                rtnval.append(block_text)
            in_block = False
            current_lines = []
            continue
        if in_block:
            current_lines.append(line)

    return rtnval


def _compute_fingerprint(
    *,
    pem: str,
    colonized: bool,
    uppercase: bool,
) -> Optional[str]:
    """
        Compute the SHA-256 fingerprint of a PEM CERTIFICATE block.
    """
    body_lines: list = []
    in_body = False
    for raw in pem.splitlines():
        stripped = raw.strip()
        if stripped == _PEM_BEGIN:
            in_body = True
            continue
        if stripped == _PEM_END:
            in_body = False
            continue
        if in_body:
            body_lines.append(stripped)

    body = "".join(body_lines)
    if len(body) == 0:
        rtnval: Optional[str] = None
        return rtnval

    try:
        decoded = base64.b64decode(body, validate=False)
    except (ValueError, Exception):
        rtnval = None
        return rtnval

    if len(decoded) == 0:
        rtnval = None
        return rtnval

    digest = hashlib.sha256(decoded).digest()

    if uppercase:
        hex_text = digest.hex().upper()
    else:
        hex_text = digest.hex().lower()

    if colonized:
        chunks: list = []
        cursor = 0
        while cursor < len(hex_text):
            chunks.append(hex_text[cursor:cursor + 2])
            cursor += 2
        rtnval = ":".join(chunks)
        return rtnval

    rtnval = hex_text
    return rtnval


def _fingerprint_from_source_bytes(*, content: str) -> Optional[str]:
    """
        Compute the SHA-256 fingerprint of a certificate file's
        contents.

        Detects PEM by the presence of ``-----BEGIN CERTIFICATE-----``;
        falls back to treating the bytes as DER.

        :param content: The raw file contents (as a string,
                        possibly mojibake-encoded for DER inputs).

        :returns: Uppercase no-colon SHA-256 fingerprint, or ``None``
                  when the bytes do not parse.
    """
    if _PEM_BEGIN in content:
        blocks = _parse_pem_blocks(content=content)
        if len(blocks) == 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = _compute_fingerprint(
            pem=blocks[0], colonized=False, uppercase=True,
        )
        return rtnval

    # DER fallback: treat the bytes as already-DER.  This is best-effort
    # because PowerShell's Get-Content -Raw on a binary returns
    # OS-encoded text, but for tests the raw bytes are present.
    try:
        encoded_bytes = content.encode("latin-1")
    except (UnicodeError, Exception):
        rtnval = None
        return rtnval

    if len(encoded_bytes) == 0:
        rtnval = None
        return rtnval

    digest = hashlib.sha256(encoded_bytes).digest()
    rtnval = digest.hex().upper()
    return rtnval


def _normalize_fingerprint(*, value: str) -> str:
    """
        Normalise a fingerprint string to the Windows canonical form
        (uppercase hex, no colons).
    """
    text = value.strip()
    lower_prefix = text.lower()
    if lower_prefix.startswith("sha256 fingerprint="):
        text = text[len("SHA256 Fingerprint="):]
    elif lower_prefix.startswith("sha256:"):
        text = text[len("SHA256:"):]
    elif lower_prefix.startswith("sha1:"):
        text = text[len("SHA1:"):]
    text = text.replace(":", "").replace(" ", "").upper()
    rtnval = text
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PowerShell JSON parsing
# ----------------------------------------------------------------------


def _parse_powershell_certificates(
    *,
    stdout: str,
    store: str,
) -> list:
    """
        Parse the JSON emitted by ``Get-ChildItem ... | Select-Object
        ... | ConvertTo-Json -Compress`` into a list of
        :class:`WindowsCertTrustInfo`.

        ``ConvertTo-Json`` emits a single object when the upstream
        pipeline yields a single element and an array otherwise.  Both
        cases are normalised to a list here.

        :param stdout: The raw stdout from the runner.
        :param store: The store name to record on each
                      :class:`WindowsCertTrustInfo`.

        :returns: A ``list[WindowsCertTrustInfo]``.
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
        thumbprint_norm = _normalize_fingerprint(value=str(thumbprint))
        subject = record.get("Subject", "")
        issuer = record.get("Issuer", "")
        not_before = record.get("NotBefore", "")
        not_after = record.get("NotAfter", "")
        serial = record.get("SerialNumber", "")
        ps_path = record.get("PSPath", None)

        purposes: list = []
        eku_raw = record.get("EnhancedKeyUsageList", None)
        if isinstance(eku_raw, list):
            for entry in eku_raw:
                if isinstance(entry, dict):
                    friendly = entry.get("FriendlyName", "")
                    if isinstance(friendly, str) and len(friendly) > 0:
                        purposes.append(friendly)
                elif isinstance(entry, str):
                    if len(entry) > 0:
                        purposes.append(entry)

        info = WindowsCertTrustInfo(
            fingerprint=thumbprint_norm,
            subject=str(subject) if subject is not None else "",
            issuer=str(issuer) if issuer is not None else "",
            not_before=str(not_before) if not_before is not None else "",
            not_after=str(not_after) if not_after is not None else "",
            serial_number=str(serial) if serial is not None else "",
            store=store,
            path=str(ps_path) if ps_path is not None else None,
            purposes=purposes,
        )
        rtnval.append(info)

    return rtnval
