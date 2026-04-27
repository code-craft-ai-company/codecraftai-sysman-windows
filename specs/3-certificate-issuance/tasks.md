# Implementation Plan — Certificate Issuance (Windows)

Checked items (`[x]`) are implemented and covered by tests.
Unchecked items (`[ ]`) identify out-of-scope or follow-up work.

## Module and Models

- [x] 1. Create the `certificateissuance` module under
  `source/packages/automojo/sysman/windows/`.

- [x] 2. Define `WindowsCertIssuanceActionStatus` alias to the shared
  `ActionStatus` enum.

- [x] 3. Define `WindowsRevocationReason` enum with the six contract
  members, mapped to certreq.exe's vocabulary
  (`Unspecified`, `KeyCompromise`, ...).

- [x] 4. Define `WindowsCertIssuanceManagerConfig` dataclass with
  `dry_run`, `elevated`, `cert_store`, `ca_config`,
  `powershell_executable`, `certreq_executable`,
  `renew_threshold_days`.

- [x] 5. Define `WindowsCertIssuanceActionResult` dataclass extending
  `FeatureActionResult` with `alias`, `subject`, `fingerprint`.

- [x] 6. Define `WindowsCertificateInfo` dataclass.

- [x] 7. Define `WindowsCertificateRequestSpec` dataclass.

- [x] 8. Define `WindowsCertIssuanceQueryError` exception.

## WindowsCertificateIssuanceManager — Implemented

- [x] 9. Implement `__init__`.

- [x] 10. Implement `request_certificate(spec)`:
  - Renew-threshold guard.
  - Self-signed via `New-SelfSignedCertificate`.
  - AD CS via certreq.exe when `config.ca_config` is set.

- [x] 11. Implement `renew_certificate(alias)`:
  - Renew-threshold guard.
  - `New-SelfSignedCertificate -CloneCert`.

- [x] 12. Implement `revoke_certificate(alias, reason=None)`:
  - Local: `Remove-Item` against the matching cert.
  - AD CS: `certreq.exe -revoke`.
  - Reason-string mapping.

- [x] 13. Implement `get_certificate(alias)`:
  - `Get-ChildItem ... | ConvertTo-Json` parse.

- [x] 14. Implement `list_certificates()`:
  - `Get-ChildItem ... | ConvertTo-Json` parse.

- [x] 15. Implement `export_certificate`:
  - `Export-Certificate` (cert only) or
    `Export-PfxCertificate` (cert+key).
  - Idempotent via `Get-FileHash`.
  - Passphrase on stdin.
  - Recorded command sanitised.

- [x] 16. Implement `import_certificate`:
  - `Import-Certificate` for `.cer/.crt/.pem`.
  - `Import-PfxCertificate` for `.pfx/.p12`.
  - Thumbprint-based idempotency.
  - Passphrase on stdin.

## Helpers

- [x] 17. `_ps_escape_single_quoted` — escape single quotes for
  PowerShell single-quoted string literals.

- [x] 18. `_build_powershell_argv` — canonical argv shape.

- [x] 19. `_parse_powershell_certificates` — JSON ->
  list[WindowsCertificateInfo].

- [x] 20. `_normalize_thumbprint` — uppercase hex no colons.

- [x] 21. `_days_until` — parse PowerShell ISO date and compute
  days remaining.

- [x] 22. `_sanitise_script_passwords` — redact passphrase tokens
  from a PowerShell scriptblock argv.

## Tests — Implemented

- [x] 23. `test_request_certificate_invokes_powershell_with_subject`.

- [x] 24. `test_request_certificate_skipped_when_present_and_not_near_expiry`.

- [x] 25. `test_request_certificate_dry_run_short_circuits`.

- [x] 26. `test_request_certificate_self_signed_uses_powershell`.

- [x] 27. `test_renew_certificate_invokes_powershell`.

- [x] 28. `test_renew_certificate_skipped_when_not_near_expiry`.

- [x] 29. `test_revoke_certificate_invokes_powershell`.

- [x] 30. `test_revoke_certificate_with_reason`.

- [x] 31. `test_get_certificate_parses_x509_output`.

- [x] 32. `test_list_certificates_returns_all`.

- [x] 33. `test_export_certificate_writes_to_path`.

- [x] 34. `test_export_with_private_key_uses_pfx_format`.

- [x] 35. `test_import_certificate_skipped_when_fingerprint_present`.

- [x] 36. `test_passphrase_redacted_in_recorded_command`.

- [x] 37. `test_writes_elevate`.

- [x] 38. `test_reads_never_elevate`.

- [x] 39. `test_failed_command_surfaces_stderr`.

## Out of Scope (Contract Surface Not Yet Implemented)

- [ ] 40. `Credential` parameter — defer until the cross-package
  `automojo-credentials` integration lands.

- [ ] 41. ACME issuance via Posh-ACME / win-acme — defer to a
  follow-up wave; current pilot covers self-signed +
  AD CS.

- [ ] 42. Smartcard / TPM-resident keys — currently
  `New-SelfSignedCertificate -KeyExportPolicy NonExportable`
  is not surfaced; add an option in a follow-up.

## Follow-ups

- [ ] 43. **CSR-only flow** — emit a CSR for offline CA submission
  without immediately submitting via certreq.

- [ ] 44. **Group Policy template integration** — surface
  `-Template <name>` for AD CS template-driven enrolment.
