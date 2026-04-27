# Implementation Plan — Windows Storage Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.storagemanagement` module.

- [x] 2. Define `WindowsStorageActionStatus` (aliased to the shared
  `featurebase.ActionStatus`).

- [x] 3. Define `WindowsStorageManagerConfig` dataclass with
  `powershell_executable`, `default_filesystem`,
  `default_resiliency`, and `default_subsystem` extras on top of the
  base `dry_run` / `elevated`.

- [x] 4. Define `WindowsStorageActionResult` dataclass with
  `target` / `pool` / `volume` / `message` extras.

- [x] 5. Define `WindowsDiskInfo`, `WindowsVolumeInfo`,
  `WindowsPoolSpec`, `WindowsVolumeSpec` dataclasses.

- [x] 6. Define `WindowsStorageQueryError` exception (defined; not
  yet raised — see deviation note).

## WindowsStorageManager — Implemented

- [x] 7. `__init__` accepting runner + optional config (inherited from
  `BaseFeatureManager`).

- [x] 8. `list_disks()` — `Get-Disk | ConvertTo-Json` plus
  `Get-PhysicalDisk -CanPool` to derive `is_pool_member`.

- [x] 9. `list_volumes()` — `Get-Volume | ConvertTo-Json` plus
  `Get-VirtualDisk` for pool attribution.

- [x] 10. `create_volume(spec)` — pool flow runs `New-Volume
  -StoragePoolFriendlyName ... -ResiliencySettingName ... -FileSystem
  ...`; partition flow runs `New-Partition | Format-Volume`.
  Idempotent (pool flow) via `Get-VirtualDisk` friendly-name probe.

- [x] 11. `delete_volume(volume_id)` — drive-letter flow runs
  `Get-Partition | Remove-Partition -Confirm:$false`;
  virtual-disk flow runs `Remove-VirtualDisk -FriendlyName ...
  -Confirm:$false`.  Both surface a data-loss advisory on success.

- [x] 12. `resize_volume(volume_id, size_bytes)` — `Resize-Partition
  -DriveLetter ... -Size ...`; idempotent within 1 MiB; missing
  volume returns FAILED.

- [x] 13. `create_pool(spec)` — `New-StoragePool -FriendlyName ...
  -StorageSubSystemFriendlyName 'Windows Storage*' -PhysicalDisks
  (Get-PhysicalDisk -CanPool $True | Where-Object Number -in
  @(...))`; idempotent via `Get-StoragePool`.

- [x] 14. `delete_pool(pool_name)` — `Remove-StoragePool
  -FriendlyName ... -Confirm:$false`; idempotent via
  `Get-StoragePool`; data-loss advisory on success.

- [x] 15. `add_to_pool(pool_name, disk_id)` — `Add-PhysicalDisk
  -StoragePoolFriendlyName ... -PhysicalDisks (Get-PhysicalDisk |
  Where-Object Number -eq ...)`; idempotent via
  `Get-PhysicalDisk -StoragePoolFriendlyName ...`.

- [x] 16. `remove_from_pool(pool_name, disk_id)` —
  `Remove-PhysicalDisk -StoragePoolFriendlyName ... -PhysicalDisks
  (...) -Confirm:$false`; idempotent via membership probe; data-loss
  advisory on success.

- [x] 17. `get_volume_info(volume_id)` — indexed lookup over
  `list_volumes()`; accepts both ``"X"`` and ``"X:"``.

- [x] 18. `get_disk_info(disk_id)` — indexed lookup over
  `list_disks()`.

- [x] 19. `_build_powershell_argv` — wraps every script in
  ``["powershell.exe", "-NoProfile", "-NonInteractive",
  "-Command", "<script>"]``.

- [x] 20. `_dispatch_storage_write` and `_build_result` helpers —
  thread `target` / `pool` / `volume` / `message` through
  `BaseFeatureManager._dispatch`.

## WindowsStorageManager — Gaps (specified but not implemented)

- [ ] 21. Strict-read mode for the read verbs that raises
  `WindowsStorageQueryError` instead of returning empty / `None`.

- [ ] 22. `Resize-VirtualDisk` support so `resize_volume` can target
  Storage-Spaces virtual disks by friendly name.

- [ ] 23. Wire `WindowsDiskInfo.mount_points` from
  `Get-Partition | Get-PartitionAccessPath`.

- [ ] 24. Honour `WindowsPoolSpec.resiliency` by setting
  ``-ResiliencySettingNameDefault`` on `New-StoragePool` (currently
  preserved on the result for audit logging only).

- [ ] 25. Validate that `disk_id` for `add_to_pool` /
  `remove_from_pool` is in fact a valid `Get-Disk.Number` before
  dispatch; today the check is implicit (the cmdlet fails at runtime
  with a clear stderr).

## Tests — Implemented

- [x] 26. `test_list_disks_invokes_get_disk_and_parses`
- [x] 27. `test_list_volumes_parses_output`
- [x] 28. `test_create_volume_invokes_new_volume`
- [x] 29. `test_create_volume_skipped_when_already_present`
- [x] 30. `test_create_volume_dry_run_short_circuits`
- [x] 31. `test_delete_volume_invokes_remove_partition`
- [x] 32. `test_delete_volume_skipped_when_absent`
- [x] 33. `test_resize_volume_invokes_resize_partition`
- [x] 34. `test_resize_volume_skipped_when_size_matches`
- [x] 35. `test_create_pool_invokes_new_storagepool`
- [x] 36. `test_delete_pool_invokes_remove_storagepool`
- [x] 37. `test_add_to_pool_invokes_add_physicaldisk`
- [x] 38. `test_remove_from_pool_invokes_remove_physicaldisk`
- [x] 39. `test_get_volume_info_parses_output`
- [x] 40. `test_get_disk_info_parses_output`
- [x] 41. `test_writes_elevate`
- [x] 42. `test_reads_never_elevate`
- [x] 43. `test_failed_command_surfaces_stderr`
- [x] 44. Module-level helper coverage
  (`_disk_number_token`, `_render_disk_number_list`,
  `_normalise_drive_letter`, `_looks_like_drive_letter`).

## Tests — Gaps

- [ ] 45. Partition-flow argv coverage for `create_volume` (currently
  the partition flow is implemented and reachable via the contract
  but only the pool flow has a dedicated argv-construction test).

- [ ] 46. Virtual-disk-flow `delete_volume` argv-construction test
  (the dispatch is implemented but the dedicated test seeds the
  drive-letter flow).

- [ ] 47. `create_pool` SKIPPED-when-already-exists test.

- [ ] 48. ShrinkRefused path for ReFS-backed volumes on
  `resize_volume`.

- [ ] 49. Property-based test for `_normalise_drive_letter` and
  `_looks_like_drive_letter`.

## Follow-ups

- [ ] 50. **Cross-OS contract conformance review**: verify
  `WindowsStorageActionResult` field shapes match the cross-OS
  contract at
  `automojo-sysman-common/specs/contracts/storage-management/contract.md`.

- [ ] 51. **automojo-* reuse check**: confirm that no other feature
  in `automojo-sysman-windows` invokes the Storage cmdlets directly;
  all such calls should go through `WindowsStorageManager`.

## Spec deviations from this implementation

- **`create_volume` dispatches between pool and partition flows.**
  The cross-OS contract has a single `create_volume` verb but Windows
  has two distinct cmdlet pipelines: pool-backed `New-Volume` and
  raw-partition `New-Partition + Format-Volume`.  The spec dispatches
  on whether `WindowsVolumeSpec.pool` is non-empty.  When `pool` is
  `None`, `disk_id` is required.

- **`delete_volume` dispatches on the shape of `volume_id`.**  Single-
  letter drive identifiers route to the partition flow; everything
  else routes to the virtual-disk flow (`Remove-VirtualDisk
  -FriendlyName ...`).  This mirrors how Windows operators address
  volumes in the wild.

- **`resize_volume` only supports the partition flow today.**
  Resizing a Storage-Spaces virtual disk uses `Resize-VirtualDisk`,
  which has different parameter semantics; that path is reserved for
  a follow-up batch.

- **`WindowsPoolSpec.resiliency` is preserved but not consumed by
  `New-StoragePool`.**  The pool-level resiliency knob is
  `-ResiliencySettingNameDefault` and is applied at pool creation
  time; subsequent `New-Volume` calls in the same pool then default
  to that value.  Today the field is consumed only by `New-Volume`
  via `WindowsVolumeSpec.resiliency` (or the config's
  `default_resiliency`).

- **`WindowsPoolSpec.disks` items are interpreted as disk numbers.**
  The cross-OS contract lets each OS define its disk identifier
  syntax; Windows uses the integer number reported by `Get-Disk`.
  The implementation normalises every input through
  `_disk_number_token` so callers can pass strings like ``"3"``,
  ``" 3 "``, or ``"03"``.

- **No strict-read mode.**  Read verbs return empty lists / `None`
  when their underlying cmdlet fails, mirroring the established
  Windows pattern.  The `WindowsStorageQueryError` exception is
  defined and reserved for a future strict-read variant.

- **`WindowsDiskInfo.mount_points` is always empty.**  Populated via
  `Get-Partition | Get-PartitionAccessPath` in a follow-up batch.

- **`is_pool_member` derivation uses ``CanPool=False``.**  A disk
  that cannot be repooled is, by construction, already a member of a
  pool (or is a system / boot disk that Windows refuses to pool).
  This is a pragmatic heuristic — the alternative would be a join
  against `Get-PhysicalDisk -StoragePoolFriendlyName *` which is
  brittle on older PowerShell versions.

- **PowerShell argv shape is ``-NoProfile -NonInteractive
  -Command``.**  This blocks `$PROFILE` mutations and any prompt
  PowerShell would otherwise emit during automation.

- **Resize tolerance is 1 MiB.**  NTFS allocates in 4 KiB clusters; we
  pick a coarser bound to avoid silent SKIPPED on tiny rounding
  deltas.

- **Single-quote escaping is doubling.**  Caller-supplied strings
  are interpolated into PowerShell single-quoted literals; embedded
  single quotes are escaped by being doubled (PowerShell's quoting
  rule).
