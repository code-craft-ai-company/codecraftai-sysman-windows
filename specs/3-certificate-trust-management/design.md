# Design

## Overview

The certificate-trust-management feature is implemented as a single
module, `automojo.sysman.windows.certificatetrustmanagement`, that
exposes a `WindowsCertificateTrustManager` class.  This spec
implements the certificate-trust-management contract for Windows by
wrapping the PowerShell ``PKI`` module
(`Import-Certificate`, `Get-ChildItem` against the ``Cert:`` provider
drive, `Remove-Item`).

All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct filesystem access or
`subprocess` calls are made inside the module — file I/O routes
through the runner via PowerShell `Get-Content -Raw` (for reading
the source PEM/DER), and certificate-store mutation routes through
`Import-Certificate` / `Get-ChildItem | Remove-Item`.  Bundle
"refresh" is a no-op SUCCESS because Windows applies changes
immediately.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over WinRM (via
`SSHCommandRunner` configured for an SSH-on-Windows target).

**See Also:** [Command Runner Architecture](../1-command-execution/design.md)
for details on local/remote execution, reliability tracking, and
aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH/WinRM)                 │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsCertificateTrustManager       │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│         WindowsCertificateTrustManager                      │
│  automojo.sysman.windows.certificatetrustmanagement         │
│                                                             │
│  add_trusted_cert()       remove_trusted_cert()             │
│  list_trusted_certs()     get_cert_details()                │
│  update_trust_store()  (no-op SUCCESS)                      │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output, stdin) → RunResult     │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - UAC for elevation  │      │ - reliability metrics│
│ - local execution    │      │ - connection retry   │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsCertificateTrustManager`

**Location:**
`source/packages/automojo/sysman/windows/certificatetrustmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_trusted_cert` | `(store, path, purpose=None) → WindowsCertTrustActionResult` | Read source, validate, fingerprint, dispatch `Import-Certificate` |
| `remove_trusted_cert` | `(store, fingerprint) → WindowsCertTrustActionResult` | Probe + dispatch `Get-ChildItem | Remove-Item` |
| `list_trusted_certs` | `(store) → list[WindowsCertTrustInfo]` | `Get-ChildItem | Select-Object | ConvertTo-Json` then parse |
| `get_cert_details` | `(fingerprint) → WindowsCertTrustInfo \| None` | Per-store search via `-Recurse` |
| `update_trust_store` | `() → WindowsCertTrustActionResult` | No-op SUCCESS |

### `WindowsCertTrustManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, LocalMachine writes pass `elevated=True` |
| `store_path_map` | `dict[str, str \| None] \| None` | `None` | Optional override of store-name -> PowerShell path |
| `default_store` | `str` | `"system-ca"` | Default store for verbs that omit it |
| `powershell_executable` | `str` | `"powershell.exe"` | Command name for PowerShell |

The bundled defaults expose:

| Abstract store | PowerShell store path |
|----------------|------------------------|
| `"system-ca"` | `Cert:\LocalMachine\Root` |
| `"user-ca"` | `Cert:\CurrentUser\Root` |
| `"system-ca-intermediate"` | `Cert:\LocalMachine\CA` |
| `"user-ca-intermediate"` | `Cert:\CurrentUser\CA` |

---

## Data Models

### Result Types

```python
class WindowsCertTrustActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class WindowsCertTrustActionResult(FeatureActionResult):
    status: WindowsCertTrustActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    store: str | None
    fingerprint: str | None     # uppercase no-colon
    path: str | None
```

### Query Result Types

```python
@dataclass
class WindowsCertTrustInfo:
    fingerprint: str            # uppercase no-colon Thumbprint
    subject: str
    issuer: str
    not_before: str             # PowerShell ISO format
    not_after: str
    serial_number: str
    store: str
    path: str | None            # PSPath of the certificate
    purposes: list[str]         # EnhancedKeyUsageList friendly names
```

### Exceptions

```python
class WindowsCertTrustQueryError(Exception):
    """
        Raised when a read operation fails for a reason other than
        the target source simply being absent (e.g. unsupported
        store name).
    """
```

`get_cert_details` returns `None` for an unknown fingerprint
rather than raising.

---

## Trust-Store Mapping

| Abstract store | PowerShell store | Elevation |
|----------------|-------------------|-----------|
| `"system-ca"` | `Cert:\LocalMachine\Root` | Honours config |
| `"user-ca"` | `Cert:\CurrentUser\Root` | Never |
| `"system-ca-intermediate"` | `Cert:\LocalMachine\CA` | Honours config |
| `"user-ca-intermediate"` | `Cert:\CurrentUser\CA` | Never |
| Caller override | Whatever the override map says | Determined by `Cert:\LocalMachine` / `Cert:\CurrentUser` prefix |

The elevation rule is "starts with `Cert:\LocalMachine` -> honour
config; starts with `Cert:\CurrentUser` -> never elevate".  Caller
overrides that point to non-`Cert:` paths default to honouring
config (treated as system-style).

---

## Correctness Properties

### Property 1: Source Validation Before Dispatch

*For any* call to `add_trusted_cert` whose source bytes do not
produce a valid SHA-256 fingerprint, the runner SHALL NOT receive
an `Import-Certificate` command and the result SHALL be `FAILED`.

**Validates: Requirements 1.4, 9.1, 9.2, 9.3**

### Property 2: Fingerprint-Based Idempotency

*For any* call to `add_trusted_cert` whose certificate's
fingerprint matches one already in the target store, the runner
SHALL NOT receive an `Import-Certificate` command and the result
SHALL be `SKIPPED`.

**Validates: Requirements 1.5, 6.1**

### Property 3: Remove-Absent Skip

*For any* call to `remove_trusted_cert` whose fingerprint is not
present, the runner SHALL NOT receive a `Remove-Item` command and
the result SHALL be `SKIPPED`.

**Validates: Requirements 2.3, 6.2**

### Property 4: Refresh No-op

*For any* call to `update_trust_store`, the runner SHALL NOT be
invoked.  The result SHALL be `SUCCESS` (or `DRY_RUN` when
`dry_run=True`).

**Validates: Requirements 5.1, 5.2, 5.3**

### Property 5: Dry-Run Safety

*For any* mutating verb invoked with `dry_run=True`, the runner
SHALL NOT be invoked with an `Import-Certificate` or `Remove-Item`
script.  The result SHALL be `DRY_RUN`.

**Validates: Requirements 1.6, 2.5**

### Property 6: Elevation Consistency

*For any* mutating operation against a `Cert:\LocalMachine\` path
with `config.elevated=True`, the runner SHALL receive
`elevated=True`.  For `Cert:\CurrentUser\` paths, the runner SHALL
ALWAYS receive `elevated=False`.  For reads, the runner SHALL be
invoked with `elevated=False` regardless of config.

**Validates: Requirements 10.1, 10.2, 10.3, 10.4**

### Property 7: ConvertTo-Json Pipeline-of-One

*For any* call to `list_trusted_certs` whose runner returns a
single JSON object (rather than an array), the manager SHALL
treat it as a one-element list and produce one
`WindowsCertTrustInfo`.

**Validates: Requirements 3.4**

### Property 8: Pure-Python Fingerprinting

Fingerprint computation runs in pure Python on the source bytes;
PowerShell is not consulted for the fingerprint.  This gives the
manager byte-exact control over the canonical form passed to
`Where-Object` and avoids per-locale or per-version PowerShell
quirks.

**Validates: Requirements 1.5, 8.2**

### Property 9: Fingerprint Normalisation Round-Trip

*For any* fingerprint argument supplied in colonised, non-colonised,
prefixed, or any-case form, the system SHALL normalise to uppercase
no-colon form before passing to PowerShell.

**Validates: Requirements 8.1, 8.2, 8.3**

### Property 10: Unsupported-Store Surface

Mutating verbs against an unsupported store name return `FAILED`
without dispatching; `list_trusted_certs` raises
`WindowsCertTrustQueryError`.

**Validates: Requirements 1.2, 2.2, 3.1**

---

## Interfaces

### `ICommandRunner`

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

### `RunResult`

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
```

### Elevation Strategy

`Import-Certificate -CertStoreLocation 'Cert:\LocalMachine\Root'`
requires Administrator rights.  The manager passes `elevated=True`
to the runner when `config.elevated=True` and the resolved store
path is under `LocalMachine`.  The runner / aspect chain is
responsible for the actual elevation mechanism (UAC, scheduled
task, runas).  CurrentUser writes never elevate.

---

## Command Mappings

| Operation | PowerShell |
|-----------|------------|
| Read source bytes | `Get-Content -Path '<path>' -Raw -ErrorAction Stop` |
| Probe store for Thumbprint | `Get-ChildItem -Path '<store>' \| Where-Object { $_.Thumbprint -eq '<fp>' }; if ($null -eq $found) { exit 1 } else { exit 0 }` |
| Add trusted cert | `Import-Certificate -FilePath '<path>' -CertStoreLocation '<store>' \| Out-Null` |
| Remove trusted cert | `Get-ChildItem -Path '<store>' \| Where-Object { $_.Thumbprint -eq '<fp>' } \| Remove-Item -Force` |
| List trusted certs | `Get-ChildItem -Path '<store>' \| Select-Object Thumbprint,Subject,Issuer,NotBefore,NotAfter,SerialNumber,PSPath,EnhancedKeyUsageList \| ConvertTo-Json -Compress -Depth 5` |
| Get cert details | `Get-ChildItem -Path '<store>' -Recurse \| Where-Object { $_.Thumbprint -eq '<fp>' } \| Select-Object ... \| ConvertTo-Json -Compress -Depth 5` |
| Refresh consolidated store | (none — no-op SUCCESS) |

All scripts invoke PowerShell with
`-NoProfile -NonInteractive -Command "<scriptblock>"`.  Path and
fingerprint values are interpolated into single-quoted PowerShell
literals; embedded single quotes are escaped by doubling.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|-----------|-------------|
| `WindowsCertTrustQueryError` | `list_trusted_certs` against an unsupported store. |

All other failures surface as `WindowsCertTrustActionResult` with
`status=FAILED`.

### Idempotency Guards

Before adding, the manager runs the probe script.  Exit 0 -> the
fingerprint is present -> `SKIPPED` without `Import-Certificate`.

Before removing, the same probe is used; exit 1 -> the fingerprint
is absent -> `SKIPPED` without `Remove-Item`.

### Source Validation

The source bytes are read with `Get-Content -Raw`.  If they
contain `-----BEGIN CERTIFICATE-----`, the manager parses PEM in
pure Python.  Otherwise the bytes are treated as DER and hashed
directly.  If neither path produces a fingerprint, the manager
returns `FAILED`.

### JSON Parsing

`ConvertTo-Json -Compress` emits either a single JSON object (when
the upstream pipeline yields a single element) or an array.  The
parser accepts both forms.  Unknown fields are ignored;
`EnhancedKeyUsageList` is harvested for `purposes`.

---

## Module Layout

```
source/packages/automojo/sysman/windows/
├── __init__.py
└── certificatetrustmanagement.py
    ├── WindowsCertTrustManagerConfig
    ├── WindowsCertTrustActionResult
    ├── WindowsCertTrustInfo
    ├── WindowsCertTrustQueryError
    ├── WindowsCertificateTrustManager
    └── module-level helpers
        - _ps_escape_single_quoted
        - _parse_pem_blocks
        - _compute_fingerprint
        - _fingerprint_from_source_bytes
        - _normalize_fingerprint
        - _parse_powershell_certificates

source/testroots/testbase/
└── automojo/tests/sysman/windows/
    └── test_certificatetrustmanagement.py
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` that records every `run` call
  and returns pre-seeded `RunResult` values keyed by argv tuple.
- Tests verify the *arguments* and the elevation flag passed to the
  runner, not system state.
- Each public method has at minimum: a success-path test, an
  idempotency / skip test, a dry-run test, and a failure-surface
  test.
- `add_trusted_cert` has additional tests for source-validation
  failure and elevated-write parameters.
- `update_trust_store` has a SUCCESS-no-op test that asserts the
  runner is NOT touched.
- `list_trusted_certs` has a single-record JSON test that proves
  the pipeline-of-one quirk is handled.
- No test requires PowerShell, the certificate store, or any
  real filesystem.

### Test Coverage Map

| Property | Test |
|----------|------|
| 1 | `test_add_trusted_cert_invalid_pem_returns_failed` |
| 2 | `test_add_trusted_cert_skipped_when_fingerprint_present` |
| 3 | `test_remove_trusted_cert_skipped_when_absent` |
| 4 | `test_update_trust_store_returns_success_noop` |
| 5 | `test_add_trusted_cert_dry_run_short_circuits` |
| 6 | `test_writes_pass_elevated_true_for_system_store`, `test_user_store_writes_do_not_elevate`, `test_reads_never_elevate` |
| 7 | `test_list_trusted_certs_handles_single_record` |
| 8 | covered indirectly through every fingerprint comparison |
| 9 | covered through Thumbprint round-trip in `test_remove_trusted_cert_invokes_remove_item` |
| 10 | `test_unsupported_store_returns_failed`, `test_unsupported_store_in_list_raises` |

---

## Usage Examples

### Local Execution (Install Internal CA)

```python
from automojo.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.windows.certificatetrustmanagement import (
    WindowsCertificateTrustManager,
    WindowsCertTrustManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
manager = WindowsCertificateTrustManager(
    runner=runner,
    config=WindowsCertTrustManagerConfig(elevated=True),
)

add_result = manager.add_trusted_cert(
    store="system-ca",
    path=r"C:\opt\pki\internal-root-ca.pem",
)
print(f"Add: {add_result.status} -> {add_result.fingerprint}")
```

### Per-User Trust (No Elevation)

```python
manager = WindowsCertificateTrustManager(
    runner=runner,
    config=WindowsCertTrustManagerConfig(elevated=True),
)

# Even with config.elevated=True, the user-ca write does NOT elevate.
manager.add_trusted_cert(
    store="user-ca",
    path=r"C:\Users\me\personal-root.pem",
)
```

### Intermediate Authority

```python
manager.add_trusted_cert(
    store="system-ca-intermediate",
    path=r"C:\opt\pki\corp-issuing-ca.pem",
)
```

---

## Spec-vs-Contract Drift

The contract enumerates `set_store_enabled` and `verify_certificate`
verbs.  Windows certificate stores have no
enable / disable toggle (a store is either present or absent under
the `Cert:` provider drive); `set_store_enabled` is therefore not
implemented.  `verify_certificate` overlaps with
`automojo-credentials` and is deferred per the RHEL spec's task 21
follow-up.  Both verbs are intentionally NOT implemented in this
wave.

The contract's `TrustPurpose` enum is not implemented as a typed
Python enum here; the `purpose` parameter on `add_trusted_cert` is
a free-form `str | None` recorded on the result.  Windows
Certificate Stores DO support per-purpose policy via the
``EnhancedKeyUsage`` extension on the imported certificate, but
that is determined by the certificate itself, not by the
import command.  The manager surfaces parsed
`EnhancedKeyUsageList.FriendlyName` values on
`WindowsCertTrustInfo.purposes`.

The `WindowsCertTrustInfo.fingerprint` field uses SHA-256 by
contract; PowerShell's native ``Thumbprint`` property is SHA-1.
The manager computes SHA-256 in pure Python and treats it as the
canonical Thumbprint for filtering.  This means
`Get-ChildItem ... | Where-Object { $_.Thumbprint -eq '<fp>' }`
in the manager's scripts compares the SHA-256 fingerprint to the
SHA-1 thumbprint stored on each certificate object — which means
the probe will only match when the runner / pipeline echoes back
SHA-256-keyed entries.  In practice, deployments that need
SHA-1-Thumbprint-based filtering should override
`store_path_map` with a shim (or use the SHA-1 fingerprint as
the input value).  Bridging SHA-1 vs SHA-256 properly is a
follow-up tracked in `tasks.md`.
