# Implementation Plan — Filesystem Quotas (Windows)

All tasks unchecked — this is a new feature with no existing
implementation in `codecraftai-sysman-windows`.

## Module and Models

- [ ] 1. Create `source/packages/ccai/sysman/windows/filesystemquotas.py`
  - _Requirements: All_

- [ ] 2. Define `WindowsQuotaActionStatus` (alias `ActionStatus`)

- [ ] 3. Define `WindowsFilesystemQuotasManagerConfig` dataclass
  with `quota_unit_kb: int = 1` (documentation field)
  - _Requirements: 9, 11_

- [ ] 4. Define `WindowsQuotaActionResult` (subclasses
  `FeatureActionResult`) with `mountpoint: str | None` and
  `principal: str | None`
  - _Requirements: 9_

- [ ] 5. Define `WindowsQuotaPrincipalKind` enum

- [ ] 6. Define `WindowsQuotaInfo` dataclass; document that
  inode and grace fields are always None on NTFS

- [ ] 7. Define `WindowsQuotaStatus` dataclass; document that
  `group_quotas_active` is always False

- [ ] 8. Define `WindowsQuotaQueryError(Exception)` and
  `WindowsQuotaFilesystemNotMountedError(Exception)` (latter is
  reserved for future use)

## Helpers

- [ ] 9. Implement `__init__` accepting runner + optional
  config (delegate to `BaseFeatureManager`)

- [ ] 10. Implement `_planned_command_for_enable(mountpoint)` /
  `_planned_command_for_disable(mountpoint)` /
  `_planned_command_for_set_user(...)` returning the argv used
  for the dry-run record path

## Enable / Disable

- [ ] 11. Implement `enable_quotas(mountpoint)`:
  - Dry-run guard returning DRY_RUN with `fsutil quota track`
    argv
  - Probe `get_quota_status`; SKIPPED when already enforcing
  - Dispatch `fsutil quota track <mountpoint>`
  - On failure return
  - Dispatch `fsutil quota enforce <mountpoint>`
  - _Requirements: 1, 10.1, 11_

- [ ] 12. Implement `disable_quotas(mountpoint)`:
  - Dry-run guard
  - Probe; SKIPPED when not enabled
  - Dispatch `fsutil quota disable <mountpoint>`
  - _Requirements: 2, 10.2, 11_

## Limits

- [ ] 13. Implement `set_user_quota(mountpoint, user,
  soft_bytes, hard_bytes)`:
  - Dry-run guard with planned argv
  - Probe `get_user_quota`; SKIPPED on match
  - Dispatch `fsutil quota modify <mountpoint> <hard_bytes>
    <soft_bytes> <user>`
  - _Requirements: 3, 10.3_

- [ ] 14. Implement `set_group_quota(...)`:
  - Return FAILED with `detail="ntfs_group_quota_unsupported"`
    regardless of dry_run
  - _Requirements: 4_

## Queries

- [ ] 15. Implement `get_user_quota(mountpoint, user)`:
  - Dispatch `fsutil quota query <mountpoint>` (no elevation)
  - Non-zero raises `WindowsQuotaQueryError`
  - Parse per-user blocks; return matching entry or None
  - _Requirements: 5_

- [ ] 16. Implement `get_group_quota(...)`:
  - Raise `WindowsQuotaQueryError` with
    `"ntfs_group_quota_unsupported"`
  - _Requirements: 6_

- [ ] 17. Implement `list_quotas(mountpoint)`:
  - Dispatch `fsutil quota query <mountpoint>` (no elevation)
  - Non-zero raises `WindowsQuotaQueryError`
  - Parse per-user blocks; emit USER-kind only
  - _Requirements: 7_

- [ ] 18. Implement `get_quota_status(mountpoint)`:
  - Dispatch `fsutil quota query <mountpoint>` (no elevation)
  - Non-zero raises `WindowsQuotaQueryError`
  - Parse `Logging Status` and `Enforce Limit Status`
  - Return `WindowsQuotaStatus` with
    `filesystem_type="ntfs"` and `group_quotas_active=False`
  - _Requirements: 8_

## Parsers

- [ ] 19. Implement `_parse_fsutil_quota_query(stdout,
  mountpoint)` → tuple of (status_dict, list_of_user_records)

- [ ] 20. Implement `_quota_token_to_bytes(token)` mapping
  decimal byte values and `0xff...` "no limit" sentinels to
  `int` / `None`

## Tests

- [ ] 21. Create `test_filesystemquotas.py` with
  `_MockCommandRunner`

- [ ] 22. Test `enable_quotas` invokes
  `fsutil quota track` then `fsutil quota enforce`

- [ ] 23. Test `enable_quotas` returns SKIPPED when already
  enforcing

- [ ] 24. Test `enable_quotas` returns DRY_RUN

- [ ] 25. Test `disable_quotas` invokes
  `fsutil quota disable`

- [ ] 26. Test `set_user_quota` invokes
  `fsutil quota modify` with the right argv

- [ ] 27. Test `set_user_quota` returns SKIPPED when current
  matches

- [ ] 28. Test `set_group_quota` returns FAILED with
  `detail="ntfs_group_quota_unsupported"` and no runner call

- [ ] 29. Test `get_user_quota` parses
  `fsutil quota query` output

- [ ] 30. Test `get_group_quota` raises
  `WindowsQuotaQueryError`

- [ ] 31. Test `list_quotas` parses
  `fsutil quota query` output

- [ ] 32. Test `get_quota_status` parses status lines

- [ ] 33. Test writes pass `elevated=True` when configured

- [ ] 34. Test reads never elevate

- [ ] 35. Test failed `fsutil quota track` surfaces stderr
