# Requirements

## Introduction

The certificate-issuance feature provides the
`automojo-sysman-windows` package with the ability to *issue*,
*renew*, *revoke*, *export*, *import*, *get*, and *list* X.509
certificates on Windows hosts.  This spec implements the
certificate-issuance contract defined at
`automojo-sysman-common/specs/contracts/certificate-issuance/contract.md`
for Windows.

A maintenance coordinator drives all operations by calling into this
feature's public interfaces.  All system commands (PowerShell `PKI`
module cmdlets — `New-SelfSignedCertificate`, `Get-ChildItem`
against ``Cert:\\``, `Export-PfxCertificate`, `Export-Certificate`,
`Import-PfxCertificate`, `Import-Certificate`, `Remove-Item` — plus
optional `certreq.exe` invocations for AD CS) are executed
exclusively through the `automojo-interfaces` command-runner
abstraction.  No `subprocess`, no direct `open`, and no direct
`pathlib` calls are made inside this module.

The Windows pilot supports two issuance pathways:

- **Self-signed via PowerShell** — `New-SelfSignedCertificate`
  against the configured certificate store
  (e.g. `Cert:\\LocalMachine\\My`).  This is the default and the
  pathway with the broadest test coverage.
- **AD CS via certreq.exe** — `certreq.exe -submit -config
  <ca> <req> <cer>` to submit a CSR to an Active Directory
  Certificate Services CA when the caller supplies
  `config.ca_config`.

PFX export passwords flow on runner ``stdin`` and are redacted from
the recorded `command` field via the
`_sanitise_script_passwords` pattern.

This feature complements — and does not duplicate — the trust-store
surface owned by `3-certificate-trust-management` (installing /
removing CA anchors).

---

## Requirements

### Requirement 1: Request a Certificate

**User Story:** As a maintenance coordinator, I want to request a
new X.509 certificate keyed by a caller-chosen alias, so that the
host can serve TLS with a configured issuer.

#### Acceptance Criteria

1. WHEN the coordinator calls `request_certificate(spec)` AND
   `config.ca_config` is `None` (or `spec.is_self_signed=True`) THE
   SYSTEM SHALL invoke
   `New-SelfSignedCertificate -Subject 'CN=<cn>'
   -CertStoreLocation '<store>' -DnsName @(<sans>) -KeyAlgorithm
   <algorithm> -KeyLength <bits>` via PowerShell.
2. WHEN `config.ca_config` is supplied AND
   `spec.is_self_signed=False` THE SYSTEM SHALL build a
   ``certreq.exe`` invocation: write a temporary CSR INF file via
   ``Set-Content``, then run ``certreq.exe -new <inf> <reqout>``
   followed by ``certreq.exe -submit -config <ca> <reqout>
   <cerout>``.
3. WHEN the alias's existing certificate (probed via the SAN match
   on the configured store) has remaining validity greater than
   `config.renew_threshold_days` THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
6. THE SYSTEM SHALL never place PFX passwords on argv; passphrase
   bytes SHALL flow on `stdin`.

---

### Requirement 2: Renew a Certificate

**User Story:** As a maintenance coordinator, I want to renew an
existing certificate by alias.

#### Acceptance Criteria

1. WHEN the coordinator calls `renew_certificate(alias)` THE SYSTEM
   SHALL look up the existing certificate via
   `Get-ChildItem '<store>' \| Where-Object Subject -like
   '*CN=<alias>*'` and, when found, dispatch
   `New-SelfSignedCertificate -CloneCert $cert
   -CertStoreLocation '<store>'`.
2. WHEN the existing certificate's remaining validity is greater
   than `config.renew_threshold_days` THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the alias is not known THE SYSTEM SHALL return
   `status=FAILED` with `detail="alias not found"`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.

---

### Requirement 3: Revoke a Certificate

**User Story:** As a maintenance coordinator, I want to revoke an
existing certificate by alias with an optional reason code.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `revoke_certificate(alias, reason=None)` AND `config.ca_config`
   is `None` THE SYSTEM SHALL remove the local certificate via
   `Get-ChildItem '<store>' \| Where-Object ... \|
   Remove-Item`.
2. WHEN `config.ca_config` is supplied THE SYSTEM SHALL invoke
   `certreq.exe -revoke -config <ca> <serial> [-reason <r>]`
   for CA-side revocation.
3. WHEN the alias is not present THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 4: Get a Certificate

**User Story:** As a maintenance coordinator, I want to fetch
structured metadata for a single certificate by alias.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_certificate(alias)` THE SYSTEM
   SHALL invoke `Get-ChildItem '<store>' -Recurse \|
   Where-Object Subject -like '*<alias>*' \|
   Select-Object Thumbprint,Subject,Issuer,NotBefore,NotAfter,
   SerialNumber,DnsNameList,SignatureAlgorithm,
   PublicKey \| ConvertTo-Json -Compress -Depth 5`.
2. WHEN no certificate is registered THE SYSTEM SHALL return
   `None`.
3. WHEN parsing fails THE SYSTEM SHALL raise
   `WindowsCertIssuanceQueryError`.
4. WHEN the call succeeds THE SYSTEM SHALL return a
   :class:`WindowsCertificateInfo`.
5. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 5: List Certificates

**User Story:** As a maintenance coordinator, I want to enumerate
every certificate in the configured store.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_certificates()` THE SYSTEM SHALL
   invoke `Get-ChildItem '<store>' \| Select-Object
   Thumbprint,Subject,Issuer,NotBefore,NotAfter,SerialNumber,...
   \| ConvertTo-Json -Compress -Depth 5`.
2. WHEN parsing fails THE SYSTEM SHALL return an empty list (a
   single bad record SHALL NOT abort the list).
3. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 6: Export a Certificate

**User Story:** As a maintenance coordinator, I want to export a
certificate (and optionally its key) to a filesystem path.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `export_certificate(alias, path, include_private_key=False)` AND
   `include_private_key` is `False` THE SYSTEM SHALL invoke
   `Export-Certificate -Cert $cert -FilePath '<path>'` via
   PowerShell.
2. WHEN `include_private_key` is `True` THE SYSTEM SHALL invoke
   `Export-PfxCertificate -Cert $cert -FilePath '<path>' -Password
   $pw` where `$pw` is read from runner stdin.
3. WHEN the alias is unknown THE SYSTEM SHALL return
   `status=FAILED`.
4. WHEN the destination path already byte-matches what would be
   written (computed via `Get-FileHash`) THE SYSTEM SHALL return
   `status=SKIPPED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True`.
7. THE SYSTEM SHALL never include PFX passwords in the recorded
   `command` field; passwords SHALL be redacted via
   `_sanitise_script_passwords`.

---

### Requirement 7: Import a Certificate

**User Story:** As a maintenance coordinator, I want to import an
externally-issued certificate under a chosen alias.

#### Acceptance Criteria

1. WHEN the coordinator calls `import_certificate(path, alias)` AND
   the source is a `.pfx` / `.p12` file THE SYSTEM SHALL invoke
   `Import-PfxCertificate -FilePath '<path>' -CertStoreLocation
   '<store>' -Password $pw`.
2. WHEN the source is a `.cer` / `.crt` / `.pem` file THE SYSTEM
   SHALL invoke `Import-Certificate -FilePath '<path>'
   -CertStoreLocation '<store>'`.
3. WHEN a certificate with the same Thumbprint is already in the
   store THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True`.
6. THE SYSTEM SHALL never include the PFX import password on argv.

---

### Requirement 8: Idempotency

**User Story:** As a maintenance coordinator, I want repeated
calls with identical posture to converge to the same result
without extraneous reissuance.

#### Acceptance Criteria

1. `request_certificate` SHALL return `SKIPPED` when the alias's
   current certificate has remaining validity above
   `config.renew_threshold_days`.
2. `renew_certificate` SHALL return `SKIPPED` under the same
   threshold.
3. `revoke_certificate` SHALL return `SKIPPED` when the alias is
   absent.
4. `import_certificate` SHALL return `SKIPPED` when a certificate
   with the same Thumbprint is already in the store.
5. `export_certificate` SHALL return `SKIPPED` when the destination
   path already byte-matches the source.

---

### Requirement 9: PFX Password Sanitisation

**User Story:** As a maintenance coordinator, I want PFX passwords
to never leak into log or audit records.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept PFX import / export passwords via the
   `passphrase: str | None` parameter on `export_certificate`,
   `import_certificate`.
2. THE SYSTEM SHALL forward the passphrase on runner `stdin` and
   construct the PowerShell scriptblock so it consumes the
   passphrase via ``[Console]::In.ReadLine()`` or equivalent.
3. THE SYSTEM SHALL run the recorded `command` argv through
   `_sanitise_script_passwords` before populating the result, so
   any literal passphrase token is replaced with
   `"<redacted>"`.
4. THE SYSTEM SHALL NEVER place the passphrase value on argv.

---

### Requirement 10: Elevation, Dry-Run, Error Reporting

**User Story:** Standard cross-feature semantics.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch any
   mutating command.
2. WHEN `config.elevated=True` THE SYSTEM SHALL dispatch mutating
   commands elevated.  Reads SHALL NOT elevate.
3. WHEN any dispatched command fails THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, and
   `command` populated.
4. THE SYSTEM SHALL NEVER include passphrase / credential bytes in
   the recorded `command` field.

---

### Requirement 11: Configuration Surface

**User Story:** As a maintenance coordinator, I want every
Windows-specific knob (cert store, CA config, PowerShell
executable, renew threshold) to live on a single config dataclass.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose
   `WindowsCertIssuanceManagerConfig(FeatureConfig)` with fields:
   `cert_store: str = "LocalMachine\\My"`,
   `ca_config: str | None = None`,
   `powershell_executable: str = "powershell.exe"`,
   `certreq_executable: str = "certreq.exe"`,
   `renew_threshold_days: int = 30`.
2. THE SYSTEM SHALL inherit `dry_run` and `elevated` from the base
   `FeatureConfig`.
