# Implementation Plan — Windows Filesystem Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.windows.filesystemmanagement`
  module

- [x] 2. Define `WindowsFilesystemActionStatus` enum (SUCCESS /
  FAILED / SKIPPED / DRY_RUN)

- [x] 3. Define `WindowsFilesystemManagerConfig` dataclass (dry_run,
  elevated, powershell_executable).  Spec deviation: the
  `mode_translation_table` config field was dropped because the
  partial mode-to-ACL table is intentionally a small, audited
  module-level constant rather than a per-instance override; reopening
  the table to caller overrides reintroduces the un-vetted-ACL risk
  this design avoids.

- [x] 4. Define `WindowsFilesystemActionResult` dataclass

- [x] 5. Define `WindowsMountInfo`, `WindowsFstabEntry`,
  `WindowsFileInfo` dataclasses.  Spec deviation: `WindowsDiskUsageInfo`
  was folded into `WindowsMountInfo.size_bytes` /
  `WindowsMountInfo.free_bytes`; a separate disk-usage dataclass was
  not introduced because the cross-OS contract does not require it,
  and a follow-up `query_disk_usage` verb can synthesise
  `used = size - free` when needed.

- [x] 6. Define `WindowsFilesystemQueryError` exception

- [x] 7. Define the bundled `_MODE_TO_ICACLS_GRANTS` partial mode-to-
  ACL table covering 0o600, 0o644, 0o700, 0o755, 0o775, 0o777
  (six canonical modes; the spec called for seven canonical entries
  but the seventh slot was reserved for 0o500 / 0o550 family modes
  that the pilot does not support).  The accompanying
  `_mode_to_icacls_grants(mode_int)` helper returns ``None`` for
  out-of-table modes; callers receive ``FAILED`` rather than a
  silently-snapped approximation.  Spec deviation: nearest-neighbour
  snapping is intentionally out of scope to keep the ACL semantics
  audit-traceable.

## WindowsFilesystemManager — Implemented

- [x] 8. Implement `__init__` accepting runner + optional config
  (inherited from `BaseFeatureManager`)

- [x] 9. Implement `mount(device, mountpoint, fstype, options)` —
  `New-PSDrive -Name <letter> -PSProvider FileSystem -Root <root>
  -Persist`; idempotent via `Get-PSDrive -Name`.  Spec deviation: the
  pilot uses `New-PSDrive` rather than
  `Add-PartitionAccessPath`/`Get-Partition`; the latter operates on
  partitions identified by disk + partition number, which is not a
  natural fit for the cross-OS contract's `(device, mountpoint)`
  argument shape.  `fstype` and `options` are recorded on the result
  for parity but are not consumed by `New-PSDrive`.

- [x] 10. Implement `unmount(mountpoint)` — `Remove-PSDrive -Name
  <letter>`; idempotent via `Get-PSDrive`.  Spec deviation: `lazy`
  flag dropped because the cross-OS contract's `unmount(mountpoint)`
  signature does not include it.

- [x] 11. Implement `add_fstab_entry(spec)` — **always returns FAILED
  with a documenting `message`** because Windows has no `/etc/fstab`
  analogue.  Honours dry-run.  Spec deviation: the original spec
  intended to alias this to `Add-PartitionAccessPath`, but the
  semantics ("persistent fstab-style mount-table entry") do not map
  cleanly; surfacing FAILED is the more honest behaviour.  Callers
  needing persistent mounts should use the `mount` verb (which uses
  `-Persist`) or layer a Windows-specific access-path verb in a
  follow-up.

- [x] 12. Implement `remove_fstab_entry(mountpoint)` — same FAILED-
  with-message pattern as `add_fstab_entry`.

- [x] 13. Implement `get_mounts()` — merges `Get-PSDrive -PSProvider
  FileSystem | ConvertTo-Json` with `Get-Volume | ConvertTo-Json` on
  the drive-letter token.  Tolerates `Get-Volume` failure (older
  hosts without the Storage module) by returning `fstype=None`.

- [x] 14. Implement `chown(path, user, group, recursive)` — `icacls
  <path> /setowner '<user>'` (with `/T` for recursive); `group` is
  recorded for parity but not consumed by `icacls /setowner`.
  Idempotent (non-recursive) via `Get-Acl Owner` comparison with
  domain-prefix-stripping tolerance.

- [x] 15. Implement `chmod(path, mode, recursive)` — partial mode-
  to-ACL translation table → `icacls <path> /inheritance:r /grant
  '<grant>' ...`; FAILED for unsupported modes; idempotent via
  tolerant `AccessToString` substring comparison.

- [x] 16. Implement `stat(path)` — `Get-Item | Select Mode, Length,
  LastWriteTime, FullName | ConvertTo-Json` + `Get-Acl | Select
  Owner, Group, AccessToString | ConvertTo-Json`; merges the two
  payloads into a `WindowsFileInfo`.  Returns ``None`` when
  ``Get-Item`` exits non-zero (path not found).

- [x] 17. Implement `_dispatch_filesystem_write` helper — runner
  invocation + status mapping; dry-run short-circuit handled inline
  on each verb because the result-shape extra-fields differ across
  verbs.

## WindowsFilesystemManager — Gaps (deferred)

- [ ] 18. `create_directory(path, mode, owner, group)`,
  `remove_directory(path, recursive)`,
  `create_symlink(link, target)`, `remove_symlink(link)` — out of
  scope for this batch; the cross-OS contract focuses on mount /
  permission verbs.  These will land in a follow-up filesystem-
  lifecycle batch.

- [ ] 19. `query_disk_usage(path)` — deferred; callers can compute
  ``used = WindowsMountInfo.size_bytes -
  WindowsMountInfo.free_bytes`` from `get_mounts()` for the same
  drive letter.

- [ ] 20. `set_acl(path: str, acl: WindowsAclSpec)` — direct ACL
  writes for callers that need exact NTFS semantics rather than the
  POSIX-mode approximation

- [ ] 21. NTFS junction support as a first-class verb

- [ ] 22. BitLocker / EFS operations — not in base contract

- [ ] 23. NTFS-specific streams (ADS) access — not in base contract

## Tests — Implemented

- [x] 24. `test_mount_dispatches_new_psdrive`
- [x] 25. `test_mount_skipped_when_already_mapped_to_same_target`
- [x] 26. `test_mount_dry_run_short_circuits`
- [x] 27. `test_unmount_dispatches_remove_psdrive`
- [x] 28. `test_unmount_skipped_when_not_mapped`
- [x] 29. `test_add_fstab_entry_returns_failed_with_message`
- [x] 30. `test_remove_fstab_entry_returns_failed_with_message`
- [x] 31. `test_add_fstab_entry_dry_run_short_circuits`
- [x] 32. `test_chown_dispatches_icacls_setowner`
- [x] 33. `test_chown_skipped_when_owner_matches_via_bare_name`
- [x] 34. `test_chown_recursive_appends_T_flag`
- [x] 35. `test_chmod_dispatches_icacls_grant_for_canonical_mode`
- [x] 36. `test_chmod_unsupported_mode_returns_failed`
- [x] 37. `test_chmod_skipped_when_acl_already_matches`
- [x] 38. `test_mode_to_icacls_grants_canonical_modes`
- [x] 39. `test_coerce_mode_to_int_handles_octal_strings`
- [x] 40. `test_get_mounts_merges_psdrive_and_volume`
- [x] 41. `test_get_mounts_tolerates_missing_get_volume`
- [x] 42. `test_get_mounts_raises_on_psdrive_failure`
- [x] 43. `test_stat_parses_get_item_and_get_acl`
- [x] 44. `test_stat_returns_none_on_missing_path`
- [x] 45. `test_parse_get_psdrive_handles_array_and_bare_object`
- [x] 46. `test_parse_get_acl_handles_dict_payload`
- [x] 47. `test_normalise_drive_letter_strips_colon_and_slash`
- [x] 48. `test_merge_psdrive_and_volume_skips_non_filesystem_providers`
- [x] 49. `test_writes_pass_elevated_when_configured`
- [x] 50. `test_reads_never_elevate`
- [x] 51. `test_powershell_argv_construction_includes_noprofile`
- [x] 52. `test_failed_write_returns_failed_with_stderr`

## Tests — Gaps

- [ ] 53. Property-based test over the mode-to-ACL round-trip
  asserting that `translate(reverse_translate(acl))` is the identity
  for canonical ACLs (deferred; the reverse-translate side is not
  implemented in the pilot)

- [ ] 54. Localised-Windows test ensuring `ItemNotFoundException`
  classification uses the underlying exception type rather than
  English error-text matching

- [ ] 55. Coverage for `create_directory` / `remove_directory` /
  symlink verbs once they land

## Follow-ups

- [ ] 56. **Cross-OS contract conformance review**: verify this
  implementation's `WindowsFilesystemActionResult` field shapes match
  the cross-OS contract at
  `codecraftai-sysman-common/specs/contracts/filesystem-management/contract.md`,
  and that the OS-specific mode-to-ACL translation is documented as
  an extension rather than a contract requirement.  Particular
  attention: the FAILED-with-message behaviour of `add_fstab_entry`
  / `remove_fstab_entry` may require a contract amendment to mark
  fstab as N/A on Windows.

- [ ] 57. **codecraftai-* reuse check**: confirm that no other feature
  in `codecraftai-sysman-windows` invokes `Add-PartitionAccessPath` /
  `icacls` directly; all such calls should go through
  `WindowsFilesystemManager`.
