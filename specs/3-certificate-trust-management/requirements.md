# Requirements

## Introduction

The certificate-trust-management feature provides the
`automojo-sysman-windows` package with the ability to manage the
host's certificate trust posture on Windows by wrapping the
PowerShell ``PKI`` module: installing and removing trust anchors,
listing the current trust set, retrieving structured metadata for an
individual certificate by Thumbprint, and (no-op) "refreshing" the
consolidated trust store.  This spec implements the
certificate-trust-management contract defined at
`automojo-sysman-common/specs/contracts/certificate-trust-management/contract.md`
for Windows.

A maintenance coordinator drives all operations by calling into this
feature's public interfaces.  The feature does not decide *what*
trust posture the host enforces — it provides the capability to
*apply* posture reliably, idempotently, and with full auditability.
Every system command (`powershell.exe`-hosted PKI cmdlets, plus
plain `Get-Content` for source reads) is executed exclusively
through the `automojo-interfaces` command-runner abstraction.  No
`subprocess`, no direct `open`, and no direct `pathlib` calls are
made inside this module.

The Windows certificate stores live under the ``Cert:`` PowerShell
provider drive: ``Cert:\LocalMachine\Root`` and
``Cert:\CurrentUser\Root`` are the principal trust roots, with
``Cert:\LocalMachine\CA`` and ``Cert:\CurrentUser\CA`` holding
intermediate authorities.  Trust changes made via
`Import-Certificate` and `Remove-Item` are picked up immediately by
`Schannel` and CryptoAPI; there is no equivalent of
`update-ca-certificates` to invoke.

---

## Requirements

### Requirement 1: Add a Trusted Certificate

**User Story:** As a maintenance coordinator, I want to install a
PEM- or DER-encoded certificate as a trust anchor in a named store,
so that the host trusts certificates issued under that CA.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `add_trusted_cert(store, path, purpose=None)` THE SYSTEM SHALL
   resolve the abstract `store` name to a PowerShell store path via
   the configured `store_path_map`.
2. WHEN the resolved store path is `None` THE SYSTEM SHALL return
   `WindowsCertTrustActionResult` with `status=FAILED` and a
   `detail` describing the unsupported store, and SHALL NOT invoke
   the runner.
3. WHEN the source `path` cannot be read (PowerShell `Get-Content`
   returns non-zero) THE SYSTEM SHALL return `status=FAILED` with
   `detail="source path does not exist"`.
4. WHEN the source bytes do not produce a valid SHA-256
   fingerprint (neither PEM nor DER) THE SYSTEM SHALL return
   `status=FAILED` and SHALL NOT invoke `Import-Certificate`.
5. WHEN the SHA-256 fingerprint of the supplied certificate matches
   one already present in the target store (probed via
   `Get-ChildItem '<store>' | Where-Object { $_.Thumbprint -eq
   '<fp>' }`) THE SYSTEM SHALL return `status=SKIPPED` with
   `fingerprint` populated.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with
   the would-have-been PowerShell argv recorded on `command` and
   SHALL NOT invoke the runner.
7. WHEN `config.elevated=True` and the resolved store path is under
   `Cert:\LocalMachine\` THE SYSTEM SHALL pass `elevated=True` for
   the `Import-Certificate` invocation.
8. WHEN the resolved store path is under `Cert:\CurrentUser\` THE
   SYSTEM SHALL pass `elevated=False` regardless of
   `config.elevated`.
9. WHEN the write succeeds THE SYSTEM SHALL return `status=SUCCESS`
   with `fingerprint`, `path`, `command`, `exit_code`, `stdout`,
   `stderr` populated.

---

### Requirement 2: Remove a Trusted Certificate

**User Story:** As a maintenance coordinator, I want to remove a
trust anchor by its Thumbprint, so that revocation is unambiguous.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `remove_trusted_cert(store, fingerprint)` THE SYSTEM SHALL
   resolve the abstract `store` to a PowerShell store path.
2. WHEN the store name is unsupported THE SYSTEM SHALL return
   `status=FAILED` and SHALL NOT invoke the runner.
3. WHEN no certificate with the requested Thumbprint is present
   THE SYSTEM SHALL return `status=SKIPPED` and SHALL NOT invoke
   `Remove-Item`.
4. WHEN a matching certificate is present THE SYSTEM SHALL run
   `Get-ChildItem '<store>' | Where-Object { $_.Thumbprint -eq
   '<fp>' } | Remove-Item -Force`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. THE SYSTEM SHALL apply the same elevation policy as Requirement
   1 (LocalMachine elevates, CurrentUser does not).

---

### Requirement 3: List Trusted Certificates

**User Story:** As a maintenance coordinator, I want to enumerate
the trusted certificates in a named store.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_trusted_certs(store)` and the
   store is unsupported THE SYSTEM SHALL raise
   `WindowsCertTrustQueryError`.
2. WHEN the store is supported THE SYSTEM SHALL invoke
   `Get-ChildItem '<store>' | Select-Object
   Thumbprint,Subject,Issuer,NotBefore,NotAfter,SerialNumber,
   PSPath,EnhancedKeyUsageList | ConvertTo-Json -Compress -Depth 5`
   and parse the resulting JSON.
3. WHEN the runner returns a non-zero exit THE SYSTEM SHALL return
   an empty list.
4. WHEN `ConvertTo-Json` emits a single object (PowerShell's
   pipeline-of-one quirk) THE SYSTEM SHALL accept it as a
   one-element list.
5. THE SYSTEM SHALL return a `list[WindowsCertTrustInfo]` with each
   entry's `store` set to the supplied store name and
   `fingerprint` normalised to uppercase no-colon hex.
6. WHEN `list_trusted_certs` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True` to the runner.

---

### Requirement 4: Get Certificate Details

**User Story:** As a maintenance coordinator, I want to retrieve
structured metadata for a single certificate by its Thumbprint.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_cert_details(fingerprint)` THE
   SYSTEM SHALL search every supported store via
   `Get-ChildItem '<store>' -Recurse | Where-Object { ... } |
   Select-Object ... | ConvertTo-Json -Compress`.
2. WHEN no store returns a match THE SYSTEM SHALL return `None`.
3. WHEN a match is found THE SYSTEM SHALL return a populated
   `WindowsCertTrustInfo`.
4. THE SYSTEM SHALL accept the fingerprint argument in colonised /
   non-colonised / prefixed form, case-insensitive.
5. WHEN `get_cert_details` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 5: Update the Trust Store (No-op)

**User Story:** As a maintenance coordinator, I want a uniform
`update_trust_store` verb regardless of OS.

#### Acceptance Criteria

1. WHEN the coordinator calls `update_trust_store()` THE SYSTEM
   SHALL return `status=SUCCESS` with `command=None` and SHALL NOT
   invoke the runner.  (Windows applies trust changes immediately.)
2. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
3. THE SYSTEM SHALL never return `FAILED` or `SKIPPED` from
   `update_trust_store` on Windows.

---

### Requirement 6: Idempotency Across Add / Remove

#### Acceptance Criteria

1. WHEN `add_trusted_cert` is called with a fingerprint already
   present THE SYSTEM SHALL return `status=SKIPPED` and SHALL NOT
   invoke `Import-Certificate`.
2. WHEN `remove_trusted_cert` is called with a fingerprint not
   present THE SYSTEM SHALL return `status=SKIPPED` and SHALL NOT
   invoke `Remove-Item`.

---

### Requirement 7: Trust-Store Mapping

#### Acceptance Criteria

1. THE SYSTEM SHALL default `"system-ca"` to
   `Cert:\LocalMachine\Root`.
2. THE SYSTEM SHALL default `"user-ca"` to
   `Cert:\CurrentUser\Root`.
3. THE SYSTEM SHALL default `"system-ca-intermediate"` to
   `Cert:\LocalMachine\CA`.
4. THE SYSTEM SHALL default `"user-ca-intermediate"` to
   `Cert:\CurrentUser\CA`.
5. THE SYSTEM SHALL accept a caller-supplied `store_path_map`
   override that adds or replaces store names.

---

### Requirement 8: Fingerprint Normalisation

#### Acceptance Criteria

1. THE SYSTEM SHALL accept fingerprints in colonised /
   non-colonised / prefixed forms, any case.
2. THE SYSTEM SHALL normalise input to uppercase no-colon hex
   (the Windows Thumbprint canonical form) before invoking
   PowerShell.
3. THE SYSTEM SHALL emit fingerprints on result objects in
   uppercase no-colon hex.

---

### Requirement 9: Source Validation

#### Acceptance Criteria

1. WHEN the source bytes start with `-----BEGIN CERTIFICATE-----`
   THE SYSTEM SHALL parse them as PEM and fingerprint the first
   block.
2. WHEN the source bytes do not start with the PEM marker THE
   SYSTEM SHALL treat them as DER and fingerprint the raw bytes.
3. WHEN the source bytes are empty or do not produce a fingerprint
   THE SYSTEM SHALL return `status=FAILED` and SHALL NOT invoke
   `Import-Certificate`.

---

### Requirement 10: Elevation Policy

#### Acceptance Criteria

1. WHEN the resolved store path begins with `Cert:\LocalMachine\`
   and `config.elevated=True` THE SYSTEM SHALL pass `elevated=True`
   for write verbs.
2. WHEN the resolved store path begins with `Cert:\CurrentUser\`
   THE SYSTEM SHALL never elevate.
3. WHEN performing reads THE SYSTEM SHALL pass `elevated=False`
   regardless of config.
4. WHEN `config.elevated=False` THE SYSTEM SHALL pass
   `elevated=False` on every write path.
