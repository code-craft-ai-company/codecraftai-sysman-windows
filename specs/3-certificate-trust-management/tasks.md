# Implementation Plan — Certificate Trust Management (Windows)

Checked items (`[x]`) are implemented and covered by tests.
Unchecked items (`[ ]`) identify out-of-scope or follow-up work.

## Module and Models

- [x] 1. Create the `certificatetrustmanagement` module under
  `source/packages/automojo/sysman/windows/`.

- [x] 2. Define `WindowsCertTrustActionStatus` alias to the shared
  `ActionStatus` enum.

- [x] 3. Define `WindowsCertTrustManagerConfig` dataclass:
  `dry_run`, `elevated`, `store_path_map`, `default_store`,
  `powershell_executable`.

- [x] 4. Define `WindowsCertTrustActionResult` dataclass extending
  `FeatureActionResult` with `store`, `fingerprint`, `path`.

- [x] 5. Define `WindowsCertTrustInfo` dataclass with
  `fingerprint`, `subject`, `issuer`, `not_before`, `not_after`,
  `serial_number`, `store`, `path`, `purposes`.

- [x] 6. Define `WindowsCertTrustQueryError` exception.

## WindowsCertificateTrustManager — Implemented

- [x] 7. Implement `__init__`.

- [x] 8. Implement `add_trusted_cert(store, path, purpose=None)`:
  - Resolves store -> PowerShell store path.
  - Reads source via `Get-Content -Raw` through PowerShell.
  - Detects PEM vs DER by `BEGIN CERTIFICATE` marker.
  - Computes SHA-256 fingerprint in pure Python.
  - Probes target store via `Get-ChildItem | Where-Object` for
    idempotency.
  - Honours dry-run.
  - Dispatches `Import-Certificate -FilePath '<path>'
    -CertStoreLocation '<store>' | Out-Null`.
  - Honours elevation policy (LocalMachine elevates, CurrentUser
    never).

- [x] 9. Implement `remove_trusted_cert(store, fingerprint)`:
  - Probes via `Get-ChildItem | Where-Object`.
  - Honours dry-run.
  - Dispatches `Get-ChildItem | Where-Object | Remove-Item -Force`.

- [x] 10. Implement `list_trusted_certs(store)`:
  - Raises `WindowsCertTrustQueryError` for unsupported store.
  - Dispatches `Get-ChildItem | Select-Object | ConvertTo-Json
    -Compress -Depth 5`.
  - Parses JSON into `WindowsCertTrustInfo` records.
  - Handles both array and single-object output forms.
  - Reads never elevate.

- [x] 11. Implement `get_cert_details(fingerprint)`:
  - Iterates every supported store via `-Recurse`.
  - Returns `None` when no match.
  - Reads never elevate.
  - Accepts colonised / non-colonised / prefixed input.

- [x] 12. Implement `update_trust_store()`:
  - Returns `SUCCESS` no-op (Windows applies trust changes
    immediately).
  - Honours dry-run for symmetry.

## Helpers

- [x] 13. `_ps_escape_single_quoted` — escape single quotes by
  doubling for safe interpolation into PowerShell single-quoted
  literals.

- [x] 14. `_parse_pem_blocks` — split content into CERTIFICATE
  envelopes.

- [x] 15. `_compute_fingerprint` — pure-Python SHA-256.

- [x] 16. `_fingerprint_from_source_bytes` — detect PEM vs DER.

- [x] 17. `_normalize_fingerprint` — accept various input shapes,
  return canonical Windows uppercase no-colon form.

- [x] 18. `_parse_powershell_certificates` — parse
  `ConvertTo-Json` output (array or single object) into
  `list[WindowsCertTrustInfo]`.

## Tests — Implemented

- [x] 19. `test_add_trusted_cert_invokes_import_certificate`.

- [x] 20. `test_add_trusted_cert_skipped_when_fingerprint_present`.

- [x] 21. `test_add_trusted_cert_dry_run_short_circuits`.

- [x] 22. `test_add_trusted_cert_invalid_pem_returns_failed`.

- [x] 23. `test_remove_trusted_cert_invokes_remove_item`.

- [x] 24. `test_remove_trusted_cert_skipped_when_absent`.

- [x] 25. `test_list_trusted_certs_parses_json`.

- [x] 26. `test_list_trusted_certs_handles_single_record`.

- [x] 27. `test_get_cert_details_returns_full_info`.

- [x] 28. `test_get_cert_details_unknown_fingerprint_returns_none`.

- [x] 29. `test_update_trust_store_returns_success_noop`.

- [x] 30. `test_unsupported_store_returns_failed`.

- [x] 31. `test_unsupported_store_in_list_raises`.

- [x] 32. `test_writes_pass_elevated_true_for_system_store`.

- [x] 33. `test_user_store_writes_do_not_elevate`.

- [x] 34. `test_reads_never_elevate`.

- [x] 35. `test_failed_command_surfaces_stderr`.

## Out of Scope (Contract Surface Not Yet Implemented)

- [ ] 36. `set_store_enabled` — Windows certificate stores have
  no enable/disable toggle.

- [ ] 37. `verify_certificate` — overlaps with
  `automojo-credentials`; defer per RHEL spec's task 21 follow-up.

- [ ] 38. `TrustPurpose` enum — `EnhancedKeyUsageList` is
  surfaced as a list on `WindowsCertTrustInfo.purposes` but the
  manager does not filter or scope by purpose at install time.

## Follow-ups

- [ ] 39. **SHA-1 vs SHA-256 Thumbprint bridging** — Windows
  certificate objects expose `Thumbprint` as SHA-1 by default.
  The current pilot computes SHA-256 in pure Python and uses
  that for `-eq '<fp>'` filtering, which only matches when the
  caller passes a SHA-256-equivalent value.  A follow-up should
  detect `(Get-PSDrive Cert).Provider`'s installed certificate
  hash algorithm and emit a matching `Where-Object` filter.

- [ ] 40. **`certutil -addstore` fallback** — for hosts where
  PowerShell `PKI` module is not installed, the
  `certutil -addstore <store-name> <file>` command-line tool is
  always available.  Add a fallback path keyed on a config
  flag.

- [ ] 41. **Group-Policy-managed roots** — roots distributed via
  Active Directory Group Policy are stored under
  `Cert:\LocalMachine\Root\Certificates` but cannot be removed
  via `Remove-Item` while the GPO is active.  Detect and
  surface as `FAILED` with a descriptive `detail`.

- [ ] 42. **Windows Update root cert program (`AuthRoot`)** —
  the Windows Update mechanism that fetches new public-internet
  roots on demand is independent of `Cert:\LocalMachine\Root`
  enumeration.  Decide whether the manager should expose the
  cached on-disk authroot store
  (`HKLM\SOFTWARE\Microsoft\SystemCertificates\AuthRoot\Certificates`)
  as a virtual `"system-ca-authroot"` store.
