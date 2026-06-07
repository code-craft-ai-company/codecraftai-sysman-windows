# Implementation Plan — Windows Encryption Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the
  `ccai.sysman.windows.encryptionmanagement` module.

- [x] 2. Define `WindowsEncryptionActionStatus` (aliased to the
  shared `featurebase.ActionStatus`).

- [x] 3. Define `WindowsEncryptionManagerConfig` dataclass with
  `powershell_executable`, `allow_destructive`, `default_cipher`
  extras on top of the base `dry_run` / `elevated`.

- [x] 4. Define `WindowsEncryptionActionResult` dataclass with
  `volume`, `device`, `requires_reboot` extras.

- [x] 5. Define `WindowsEncryptionSpec`, `WindowsEncryptionStatus`,
  `WindowsEncryptedVolumeInfo` dataclasses.

- [x] 6. Define `WindowsEncryptionQueryError` exception.

- [x] 7. Define module-level `_DESTRUCTIVE_GUARD_DETAIL`,
  `_DEFAULT_CIPHER`, `_REDACTED`, `_DEFAULT_POWERSHELL`,
  `_PS_NOPROFILE`, `_PS_NONINTERACTIVE`, `_PS_COMMAND` constants.

- [x] 8. Define the `_SECURE_STRING_PATTERN` regex used by
  `_sanitise_script_passwords` to redact embedded plaintext
  passwords.

## WindowsEncryptionManager — Implemented

- [x] 9. `__init__` accepting runner + optional config (inherited
  from `BaseFeatureManager`).

- [x] 10. `enable_encryption(volume, spec)` — dispatches a
  PowerShell scriptblock that wraps the password (when present)
  via `ConvertTo-SecureString -AsPlainText -Force` and calls
  `Enable-BitLocker -MountPoint '<vol>' -EncryptionMethod <cipher>
  -PasswordProtector -Password $pw -RecoveryPasswordProtector
  -SkipHardwareTest`; idempotent via `Get-BitLockerVolume`'s
  `ProtectionStatus` / `VolumeStatus`; sets `requires_reboot=True`.

- [x] 11. `disable_encryption(volume)` — dispatches
  `Disable-BitLocker -MountPoint '<vol>'`; destructive; gated by
  `config.allow_destructive`; idempotent via `Get-BitLockerVolume`;
  sets `requires_reboot=True` for non-skip outcomes.

- [x] 12. `get_encryption_status(volume)` — dispatches
  `Get-BitLockerVolume -MountPoint '<vol>' | ConvertTo-Json
  -Compress -Depth 5`; parses and returns a
  `WindowsEncryptionStatus`.

- [x] 13. `list_encrypted_volumes()` — dispatches
  `Get-BitLockerVolume | ConvertTo-Json -Compress -Depth 5`; emits
  one `WindowsEncryptedVolumeInfo` per encrypted volume.

- [x] 14. `rotate_key(volume, new_passphrase)` — dispatches a
  scriptblock that wraps the new password, captures the existing
  password protector's `KeyProtectorId`, calls
  `Add-BitLockerKeyProtector -PasswordProtector` first, then
  `Remove-BitLockerKeyProtector` for the old protector;
  unconditional.

- [x] 15. `backup_recovery_key(volume, path)` — dispatches a
  scriptblock that filters `(Get-BitLockerVolume).KeyProtector`
  to `KeyProtectorType -eq 'RecoveryPassword'` and writes the
  recovery password to `<path>` via `Set-Content -Encoding UTF8`;
  unconditional.

- [x] 16. `unlock_volume(volume, passphrase)` — dispatches a
  scriptblock that wraps the password and calls
  `Unlock-BitLocker -MountPoint '<vol>' -Password $pw`;
  idempotent via `Get-BitLockerVolume`'s `LockStatus` field.

- [x] 17. `_typed_config` helper that returns the config typed as
  `WindowsEncryptionManagerConfig`, synthesising defaults when the
  caller supplied a plain `FeatureConfig`.

- [x] 18. `_build_powershell_argv` — assembles the canonical
  `[powershell.exe, -NoProfile, -NonInteractive, -Command,
  <script>]` argv.

- [x] 19. `_sanitise_command_passwords` — runs
  `_sanitise_script_passwords` over the last element of an argv
  list (the scriptblock).

- [x] 20. `_build_enable_script` / `_build_rotate_script` /
  `_build_backup_script` / `_build_unlock_script` — pure-function
  scriptblock builders.

- [x] 21. `_is_encrypted` / `_is_unlocked` — internal idempotency
  probes using the same `Get-BitLockerVolume | ConvertTo-Json`
  scriptblock as the public read verbs; never elevate.

- [x] 22. `_dispatch_encryption_write` and `_build_result` helpers
  that thread `volume` / `device` / `requires_reboot` / `detail`
  through the runner call and onto the result.

## Module-level helpers — Implemented

- [x] 23. `_ps_escape_single_quoted` — escapes single quotes for
  embedding inside PowerShell single-quoted strings (PowerShell
  doubles single quotes).

- [x] 24. `_sanitise_script_passwords` — regex-driven redaction of
  `ConvertTo-SecureString -AsPlainText -Force '<pw>'` literals
  inside a scriptblock string.

- [x] 25. `_normalize_to_list` — folds the `ConvertTo-Json`
  one-or-many shape (bare object vs array) into a list.

- [x] 26. `_parse_get_bitlockervolume` — `json.loads` wrapper that
  tolerates empty stdout / malformed JSON and runs
  `_normalize_to_list` over the parse result.

- [x] 27. `_is_record_encrypted` — canonical `ProtectionStatus`-or-
  `VolumeStatus`-or-`EncryptionPercentage>=100` predicate; tolerates
  string and integer enum shapes.

- [x] 28. `_record_to_status` — builds a `WindowsEncryptionStatus`
  from a parsed `Get-BitLockerVolume` record.

- [x] 29. `_record_to_volume_info` — builds a
  `WindowsEncryptedVolumeInfo` from a parsed record.

- [x] 30. `_extract_protector_types` — pulls the `KeyProtectorType`
  list from a record's `KeyProtector[]` array; tolerates string
  and numeric shapes.

## WindowsEncryptionManager — Gaps (specified but not implemented)

- [ ] 31. `lock_volume(volume)` verb (the inverse of
  `unlock_volume`).  Required by the cross-OS contract; not
  surfaced today.

- [ ] 32. Strict-read mode for `get_encryption_status` /
  `list_encrypted_volumes` that raises
  `WindowsEncryptionQueryError` instead of returning empty / plain
  records when the underlying cmdlet exits non-zero.

- [ ] 33. `WindowsEncryptionVolumeNotFoundError` (raised by reads
  when the target volume does not exist) and
  `WindowsEncryptionCredentialError` (raised by writes when a
  credential is required but absent) — both required by the
  cross-OS contract.

- [ ] 34. AD-based recovery-password escrow via
  `Backup-BitLockerKeyProtector -ADAccountOrGroup`.  Out of scope
  per the requirements; tracked as a follow-up.

- [ ] 35. TPM protector lifecycle automation
  (`Add-BitLockerKeyProtector -TpmProtector`,
  `Remove-BitLockerKeyProtector -KeyProtectorId <tpmId>`).
  Currently the manager is silent on TPM protectors and assumes
  the TPM is already provisioned.

- [ ] 36. Used-Space-Only encryption toggle on `Enable-BitLocker`
  (`-UsedSpaceOnly` flag) — currently the cmdlet runs in
  full-volume-encryption mode regardless of how much of the volume
  is in use.

- [ ] 37. Spec-equivalence comparison in `enable_encryption`'s
  idempotency guard (today's guard checks
  encrypted-vs-not-encrypted only; a stricter guard would compare
  cipher / protector list against the spec and return SKIPPED only
  when they match).

## Tests — Implemented

- [x] 38. `test_enable_encryption_invokes_enable_bitlocker`
- [x] 39. `test_enable_encryption_skipped_when_already_encrypted`
- [x] 40. `test_enable_encryption_dry_run_short_circuits`
- [x] 41. `test_disable_encryption_requires_allow_destructive`
- [x] 42. `test_disable_encryption_invokes_disable_bitlocker_when_allowed`
- [x] 43. `test_get_encryption_status_parses_output`
- [x] 44. `test_list_encrypted_volumes_filters_correctly`
- [x] 45. `test_rotate_key_invokes_add_then_remove_bitlocker_key_protector`
- [x] 46. `test_backup_recovery_key_writes_to_path`
- [x] 47. `test_unlock_volume_passes_passphrase_via_secure_string`
- [x] 48. `test_unlock_volume_skipped_when_unlocked`
- [x] 49. `test_passphrase_redacted_in_recorded_command`
- [x] 50. `test_writes_elevate`
- [x] 51. `test_reads_never_elevate`
- [x] 52. `test_failed_command_surfaces_stderr`
- [x] 53. `test_parse_get_bitlockervolume_handles_single_object`
- [x] 54. `test_is_record_encrypted_recognises_string_and_int_protection`
- [x] 55. `test_sanitise_script_passwords_redacts_literal`

## Tests — Gaps

- [ ] 56. `disable_encryption` skipped-when-already-plain test
  (locks Requirement 2.3 — currently only the destructive-guard
  and happy-path cases are covered).

- [ ] 57. `enable_encryption` cipher-fallback test that asserts
  `spec.cipher=None` results in `-EncryptionMethod XtsAes256` being
  emitted (locks Requirement 1.2).

- [ ] 58. `enable_encryption` `recovery_key_protector=False` test
  that asserts `-RecoveryPasswordProtector` is omitted from the
  scriptblock (locks Requirement 1.3 negation).

- [ ] 59. `backup_recovery_key` no-recovery-protector test that
  asserts a FAILED result when the volume has no `RecoveryPassword`
  protector and PowerShell's `Write-Error` fires (locks Requirement
  6.4).

- [ ] 60. `rotate_key` redaction test that asserts the new
  passphrase is `<redacted>` in the recorded command field (locks
  Requirement 5.4 — currently the redaction test only covers
  `enable_encryption`).

- [ ] 61. `unlock_volume` redaction test that asserts the
  passphrase is `<redacted>` in the recorded command field (locks
  Requirement 7.3).

- [ ] 62. `get_encryption_status` cmdlet-failure test that asserts
  `is_encrypted=False`, `cipher=None` when the cmdlet exits
  non-zero (locks Requirement 3.3).

- [ ] 63. `list_encrypted_volumes` cmdlet-failure test that asserts
  `[]` rather than a raise when the cmdlet exits non-zero (locks
  Requirement 4.4).

- [ ] 64. `_record_to_status` numeric-enum test that asserts the
  parser tolerates `LockStatus=0` (numeric) as `is_unlocked=True`.

- [ ] 65. `_extract_protector_types` test that asserts numeric
  `KeyProtectorType` values are coerced to strings.

## Spec deviations from this implementation

- **Destructive `disable_encryption` requires
  `config.allow_destructive=True`.**  The default-False guard
  prevents accidental multi-hour decrypt passes.  Without the flag
  the manager returns `FAILED` carrying
  `detail="destructive_operation_requires_explicit_confirm"` and
  does NOT dispatch `Disable-BitLocker`.  The cross-OS contract
  requires the guard but is silent on the exact detail string;
  the literal is shared verbatim with the Ubuntu and macOS
  siblings.

- **Passphrase redaction in the recorded `command` field.**  The
  cross-OS contract requires sanitised argv but is silent on the
  mechanism.  BitLocker's `Enable-BitLocker -Password` and
  `Unlock-BitLocker -Password` require a `[SecureString]` value
  built in-process via `ConvertTo-SecureString -AsPlainText -Force
  '<pw>'`, which forces the plaintext into the scriptblock.  The
  manager closes the gap with `_sanitise_script_passwords`, which
  replaces every embedded password with `<redacted>` before the
  argv is captured on the result.  The contract has no field
  documenting this implementation choice.

- **`Get-BitLockerVolume` JSON-of-one-or-many handling.**
  PowerShell's `ConvertTo-Json` emits a bare object when the
  source pipeline produced exactly one item and an array
  otherwise.  The manager folds both shapes into a list via
  `_normalize_to_list` before per-record parsing.  Coordinators
  that consume the list verb get an iterable in both cases.

- **`protector_types: list[str]` field on the status record is
  Windows-specific.**  The cross-OS contract describes a
  structured `key_slots: list[EncryptionKeySlotState]` array;
  BitLocker exposes the equivalent information as a string list
  of `KeyProtectorType` values whose only enumerable identifier
  is the opaque `KeyProtectorId` GUID.  The manager surfaces the
  string list rather than the structured array.  Cross-OS
  coordinators that need slot state MUST treat
  `protector_types=[]` as "no slot info available" rather than
  "no slots".

- **Recovery-key backup writes the password to a file in-band.**
  The macOS sibling captures `fdesetup showrecoverykey`'s stdout
  into Python and tees to disk on a second runner call.  The
  Windows implementation does the filter-and-write in one
  PowerShell scriptblock so the recovery password never crosses
  the runner boundary as captured stdout.  The
  contract-required `(volume, path)` shape is preserved; the
  internal mechanism differs.

- **`backend` and `key_slots` fields are missing from the result
  / status records.**  The contract carries an
  `EncryptionBackend` enum and a `key_slots` list on the
  `EncryptionStatus` record.  The Windows implementation omits
  both because every record is implicitly `BITLOCKER` and slot
  IDs are opaque GUIDs.  Cross-OS coordinators consuming the
  contract field must default `backend` to `BITLOCKER` for
  Windows records.

- **`lock_volume` is not implemented.**  The contract's verb set
  includes `lock_volume(volume) → EncryptionActionResult`; the
  Windows surface omits it.  Coordinators that need to lock a
  data volume between unlocks must dismount the volume via the
  filesystem-management verb instead.

- **`requires_reboot=True` is a Windows-specific extension.**
  `Enable-BitLocker` and `Disable-BitLocker` against the system
  volume modify the running boot volume's encryption posture and
  require a reboot to take full effect.  The field exists on the
  result dataclass so the coordinator can schedule the reboot
  without re-querying the volume.  The contract does not carry
  this field; the Linux Ubuntu sibling sets it to `False`
  unconditionally.

## Follow-ups

- [ ] 66. **Native AD recovery-password escrow.**  Today the
  recovery-key backup writes the password to a local filesystem
  path via `Set-Content`.  The
  `Backup-BitLockerKeyProtector -ADAccountOrGroup` cmdlet escrows
  the password directly to Active Directory; a follow-up surface
  should expose this as a separate verb (e.g.
  `escrow_recovery_key_to_ad`) so coordinators in domain-joined
  environments do not have to fall back to writing the password
  to a local file.

- [ ] 67. **TPM protector lifecycle automation.**  The
  requirements explicitly scope TPM provisioning OUT
  (`Initialize-Tpm` / `Set-TpmOwnerAuth`).  The manager assumes
  the TPM is already provisioned when the host's BitLocker
  policy requires one.  A follow-up should expose
  `Add-BitLockerKeyProtector -TpmProtector` and the
  symmetrical removal verb so coordinators can manage TPM
  binding without dropping to raw PowerShell.

- [ ] 68. **Used-Space-Only encryption toggle.**  The default
  `Enable-BitLocker` invocation runs in full-volume-encryption
  mode; `-UsedSpaceOnly` is a useful toggle for fast initial
  encryption on large nearly-empty volumes.  Surface the flag on
  `WindowsEncryptionSpec` and emit it conditionally.

- [ ] 69. **`lock_volume` verb.**  Implement the contract-required
  inverse of `unlock_volume` once the project picks a stable
  cmdlet (Windows 11 / Server 2022's `Lock-BitLocker` is the
  current candidate).

- [ ] 70. **Cross-OS contract conformance review**: verify
  `WindowsEncryptionActionResult` field shapes match the cross-OS
  contract at
  `codecraftai-sysman-common/specs/contracts/encryption-management/contract.md`,
  add the missing `EncryptionBackend` field with a default of
  `BITLOCKER`, and reconcile the `key_slots` vs `protector_types`
  drift.

- [ ] 71. **codecraftai-* reuse check**: confirm that no other
  feature in `codecraftai-sysman-windows` invokes BitLocker cmdlets
  directly; all such calls should go through
  `WindowsEncryptionManager`.

- [ ] 72. **Strict-read mode**: surface
  `WindowsEncryptionQueryError` from `get_encryption_status` and
  `list_encrypted_volumes` when the underlying cmdlet exits
  non-zero, gated by a new `config.strict_reads` flag (default
  False so the existing fall-back-to-empty behaviour is
  preserved).
