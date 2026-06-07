# Design

## Overview

The certificate-issuance feature is implemented as a single module,
`ccai.sysman.windows.certificateissuance`, that exposes a
`WindowsCertificateIssuanceManager` class.  This spec implements the
certificate-issuance contract defined at
`codecraftai-sysman-common/specs/contracts/certificate-issuance/contract.md`
for Windows using the PowerShell `PKI` module plus optional
`certreq.exe` for AD CS submission.

All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct filesystem access or
`subprocess` calls are made inside the module.

The feature works identically whether commands execute locally
(via `LocalCommandRunner`) or remotely over WinRM.

**See Also:**

- [Certificate Trust Management](../3-certificate-trust-management/design.md)
  — owns the *trust* side of the surface (CA anchors).  This spec
  owns the *issuance* side.
- [Command Runner Architecture](../1-command-execution/design.md)
  — the underlying execution model.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or WinRM)                     │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsCertificateIssuanceManager    │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│       WindowsCertificateIssuanceManager                     │
│  ccai.sysman.windows.certificateissuance                │
│                                                             │
│  request_certificate()    renew_certificate()               │
│  revoke_certificate()     get_certificate()                 │
│  list_certificates()      export_certificate()              │
│  import_certificate()                                       │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output, stdin) → RunResult     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsCertificateIssuanceManager`

**Location:**
`source/packages/ccai/sysman/windows/certificateissuance.py`

| Method | Signature | Description |
|---|---|---|
| `request_certificate` | `(spec) → WindowsCertIssuanceActionResult` | New-SelfSignedCertificate or certreq.exe |
| `renew_certificate` | `(alias) → WindowsCertIssuanceActionResult` | New-SelfSignedCertificate -CloneCert |
| `revoke_certificate` | `(alias, reason=None) → WindowsCertIssuanceActionResult` | Remove-Item or certreq.exe -revoke |
| `get_certificate` | `(alias) → WindowsCertificateInfo \| None` | Get-ChildItem + JSON parse |
| `list_certificates` | `() → list[WindowsCertificateInfo]` | Get-ChildItem + JSON parse |
| `export_certificate` | `(alias, path, include_private_key=False, passphrase=None) → WindowsCertIssuanceActionResult` | Export-Certificate (cert) or Export-PfxCertificate (cert+key) |
| `import_certificate` | `(path, alias, passphrase=None) → WindowsCertIssuanceActionResult` | Import-Certificate or Import-PfxCertificate |

### `WindowsCertIssuanceManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit. |
| `elevated` | `bool` | `False` | When `True`, mutating commands pass `elevated=True`. |
| `cert_store` | `str` | `"LocalMachine\\My"` | The PowerShell ``Cert:\\<store>`` location, with the ``Cert:\\`` prefix omitted. |
| `ca_config` | `str \| None` | `None` | AD CS configuration string (e.g. ``"CA01.example.com\\Issuing-CA"``); when set, ``request_certificate`` and ``revoke_certificate`` route through certreq.exe. |
| `powershell_executable` | `str` | `"powershell.exe"` | PowerShell binary. |
| `certreq_executable` | `str` | `"certreq.exe"` | certreq.exe binary. |
| `renew_threshold_days` | `int` | `30` | Skip threshold for request / renew. |

---

## Data Models

```python
class WindowsCertIssuanceActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


class WindowsRevocationReason(Enum):
    UNSPECIFIED            = "Unspecified"
    KEY_COMPROMISE         = "KeyCompromise"
    CA_COMPROMISE          = "CACompromise"
    AFFILIATION_CHANGED    = "AffiliationChanged"
    SUPERSEDED             = "Superseded"
    CESSATION_OF_OPERATION = "CessationOfOperation"


@dataclass
class WindowsCertIssuanceActionResult(FeatureActionResult):
    alias: str | None
    subject: str | None
    fingerprint: str | None


@dataclass
class WindowsCertificateInfo:
    alias: str
    subject: str
    issuer: str
    not_before: str
    not_after: str
    fingerprint: str          # Thumbprint, uppercase hex no colons
    serial_number: str
    key_algorithm: str | None
    key_length: int | None
    sans: list[str]
    path: str | None          # PSPath, when known
    is_self_signed: bool


@dataclass
class WindowsCertificateRequestSpec:
    alias: str
    common_name: str
    email: str | None = None
    sans: list[str] = []
    key_algorithm: str = "RSA"
    key_length: int = 2048
    validity_days: int = 365
    is_self_signed: bool = False
    webroot: str | None = None  # unused on Windows


@dataclass
class WindowsCertIssuanceManagerConfig(FeatureConfig):
    cert_store: str = "LocalMachine\\My"
    ca_config: str | None = None
    powershell_executable: str = "powershell.exe"
    certreq_executable: str = "certreq.exe"
    renew_threshold_days: int = 30


class WindowsCertIssuanceQueryError(Exception):
    """Raised when a read fails unexpectedly."""
```

---

## Verb / Tool Mapping (PowerShell PKI)

| Verb | Command |
|------|---------|
| `request_certificate` (self-signed) | `New-SelfSignedCertificate -Subject 'CN=<cn>' -CertStoreLocation 'Cert:\<store>' -DnsName @(<sans>) -KeyAlgorithm RSA -KeyLength <bits>` |
| `request_certificate` (AD CS) | `Set-Content` to write CSR INF -> `certreq.exe -new <inf> <reqout>` -> `certreq.exe -submit -config <ca> <reqout> <cerout>` |
| `renew_certificate` | `Get-ChildItem 'Cert:\<store>' \| Where-Object Subject -like '*CN=<cn>*' \| ForEach-Object { New-SelfSignedCertificate -CloneCert $_ -CertStoreLocation 'Cert:\<store>' }` |
| `revoke_certificate` (local) | `Get-ChildItem 'Cert:\<store>' \| Where-Object Subject -like '*<alias>*' \| Remove-Item` |
| `revoke_certificate` (AD CS) | `certreq.exe -revoke -config <ca> <serial> [-reason <r>]` |
| `get_certificate` | `Get-ChildItem 'Cert:\<store>' -Recurse \| Where-Object Thumbprint -eq <fp> -or Subject -like '*<alias>*' \| ConvertTo-Json -Compress -Depth 5` |
| `list_certificates` | `Get-ChildItem 'Cert:\<store>' \| Select-Object Thumbprint,Subject,Issuer,NotBefore,NotAfter,SerialNumber,DnsNameList,SignatureAlgorithm,PSPath \| ConvertTo-Json -Compress -Depth 5` |
| `export_certificate` (cert only) | `Export-Certificate -Cert $cert -FilePath '<path>'` |
| `export_certificate` (cert+key) | `Export-PfxCertificate -Cert $cert -FilePath '<path>' -Password (ConvertTo-SecureString -String <stdin> -AsPlainText -Force)` |
| `import_certificate` (PEM/DER) | `Import-Certificate -FilePath '<path>' -CertStoreLocation 'Cert:\<store>'` |
| `import_certificate` (PFX) | `Import-PfxCertificate -FilePath '<path>' -CertStoreLocation 'Cert:\<store>' -Password (...)` |

---

## Correctness Properties

### Property 1: Backend Routing

*For any* call to a write verb, dispatch SHALL route to AD CS via
`certreq.exe` when `config.ca_config` is non-`None` AND
`spec.is_self_signed=False`; otherwise SHALL route to PowerShell
`PKI` cmdlets.

**Validates: Requirements 1.1, 1.2, 3.1, 3.2**

### Property 2: Renew-Threshold Skip

*For any* call to `request_certificate` or `renew_certificate`
whose existing certificate has remaining validity above
`config.renew_threshold_days`, no issuance command SHALL be
dispatched.

**Validates: Requirements 1.3, 2.2, 8.1, 8.2**

### Property 3: Thumbprint-Based Idempotency for Import

*For any* call to `import_certificate` whose Thumbprint matches one
already present in the store, the runner SHALL NOT receive an
`Import-*` command and the result SHALL be `SKIPPED`.

**Validates: Requirements 7.3, 8.4**

### Property 4: Dry-Run Safety

*For any* mutating verb invoked with `dry_run=True`, the runner
SHALL NOT be invoked with a mutating command.

**Validates: Requirements 1.4, 2.4, 3.4, 6.5, 7.4, 10.1**

### Property 5: Elevation Consistency

*For any* mutating verb with `config.elevated=True`, the runner
SHALL be invoked with `elevated=True`.  *For any* read verb,
`elevated=False` SHALL be passed regardless of config.

**Validates: Requirements 1.5, 2.5, 6.6, 7.5, 10.2**

### Property 6: PFX Password Sanitisation

*For any* verb that accepts a PFX password, the password SHALL flow
on `stdin` only.  The recorded `command` field SHALL never contain
the password — the `_sanitise_script_passwords` helper redacts
literal occurrences before recording.

**Validates: Requirements 6.7, 7.6, 9.1, 9.2, 9.3, 9.4, 10.4**

### Property 7: Export Byte-Match Skip

*For any* call to `export_certificate` whose destination path
already byte-matches what would be written, the runner SHALL NOT
receive an `Export-*` command and the result SHALL be `SKIPPED`.

**Validates: Requirement 8.5**

### Property 8: PFX vs PEM Format Routing

*For any* call to `export_certificate(include_private_key=True)`,
the dispatched argv SHALL be `Export-PfxCertificate`.  *For any*
call with `include_private_key=False`, the argv SHALL be
`Export-Certificate`.

**Validates: Requirements 6.1, 6.2**

### Property 9: Failure Capture

*For any* dispatched command that exits non-zero, the result
SHALL be `FAILED` with `exit_code`, `stdout`, `stderr`, and
`command` populated.

**Validates: Requirement 10.3**

### Property 10: Reads Pure JSON Parsing

*For any* call to `get_certificate` or `list_certificates`,
parsing SHALL be pure-Python `json.loads` against the
PowerShell `ConvertTo-Json` output.

**Validates: Requirements 4.4, 5.1**

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
        stdin: str | None = None,
    ) -> RunResult: ...
```

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|-----------|-------------|
| `WindowsCertIssuanceQueryError` | A read fails unexpectedly. |

### Idempotency Guards

Before requesting / renewing, the manager looks up the existing
certificate via `Get-ChildItem ... | Where-Object Subject -like
'*<cn>*'` and parses `NotAfter` from the JSON output.

Before importing, the manager looks up the source's Thumbprint via
PowerShell's `(Get-PfxCertificate -FilePath '<path>').Thumbprint`
(or `Get-PfxData` for password-protected PFX) and compares against
`Get-ChildItem '<store>' | Where-Object Thumbprint -eq '<fp>'`.

Before exporting, the manager hashes the destination via
`Get-FileHash` and compares against the live cert's hash.

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
└── certificateissuance.py
    ├── WindowsCertIssuanceActionStatus       (alias to ActionStatus)
    ├── WindowsRevocationReason               (enum)
    ├── WindowsCertIssuanceActionResult       (dataclass)
    ├── WindowsCertificateInfo                (dataclass)
    ├── WindowsCertificateRequestSpec         (dataclass)
    ├── WindowsCertIssuanceManagerConfig      (dataclass)
    ├── WindowsCertIssuanceQueryError         (exception)
    ├── WindowsCertificateIssuanceManager     (manager class)
    └── module-level helpers
        - _ps_escape_single_quoted
        - _build_powershell_argv
        - _parse_powershell_certificates
        - _normalize_thumbprint
        - _days_until
        - _sanitise_script_passwords

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_certificateissuance.py
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` that records every `run`
  call and returns pre-seeded `RunResult` values.
- Tests verify the *arguments* passed to the runner, not system
  state.
- Each public verb has at minimum: a success-path test, an
  idempotency / skip test, a dry-run test, and a failure-surface
  test.
- A passphrase-redaction test covers the PFX export flow.
- No test requires Administrator, live PowerShell, live
  `certreq.exe`, or a live certificate store.

### Test Coverage Map

| Property | Test name |
|----------|-----------|
| 1, 8 | `test_request_certificate_invokes_powershell_with_subject` |
| 2 | `test_request_certificate_skipped_when_present_and_not_near_expiry`, `test_renew_certificate_skipped_when_not_near_expiry` |
| 3 | `test_import_certificate_skipped_when_fingerprint_present` |
| 4 | `test_request_certificate_dry_run_short_circuits` |
| 5 | `test_writes_elevate`, `test_reads_never_elevate` |
| 6 | `test_passphrase_redacted_in_recorded_command` |
| 7 | covered by `test_export_certificate_writes_to_path` skip branch |
| 8 (format routing) | `test_export_with_private_key_uses_pfx_format` |
| 9 | `test_failed_command_surfaces_stderr` |
| 10 | `test_get_certificate_parses_x509_output`, `test_list_certificates_returns_all` |

---

## Spec-vs-Contract Drift

The contract enumerates `Credential` parameters on every write
verb.  The Windows pilot exposes a separate `passphrase: str | None`
parameter on `export_certificate` and `import_certificate` for the
PFX-password use case, since that is the dominant credential use on
the Windows surface.  Threading a fully typed
`codecraftai-credentials.Credential` through the public surface is
deferred to the cross-package follow-up.

The contract's `ADCS` issuer enum is supported via the optional
`config.ca_config` knob.  When set, write verbs route through
`certreq.exe`; when unset, they route through PowerShell self-signed
issuance.

CA-side revocation reasons are accepted via the
`WindowsRevocationReason` enum and forwarded to certreq.exe's
`-reason` flag.  When the local-only path is taken, the reason is
recorded on the result `detail` field but does not change argv.
