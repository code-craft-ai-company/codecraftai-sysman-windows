# Implementation Plan — Virtualization Management

All tasks below are unchecked at start; tick them in order.

## Module and Models

- [ ] 1. Create
  `source/packages/ccai/sysman/windows/virtualizationmanagement.py`
  with the module docstring covering Hyper-V role requirements,
  idempotency guards, stop semantics, and elevation policy.
  - _Requirements: All_

- [ ] 2. Define `WindowsVirtualizationActionStatus` (alias of
  `ActionStatus`).
  - _Requirements: 11_

- [ ] 3. Define `WindowsVmState` enum (`RUNNING`, `STOPPED`, `PAUSED`,
  `SUSPENDED`, `OTHER`).
  - _Requirements: 2, 6_

- [ ] 4. Define `WindowsVirtualizationManagerConfig` adding
  `powershell_executable`, `default_switch`, `default_vhd_dir`.
  - _Requirements: 1, 4_

- [ ] 5. Define `WindowsVirtualizationActionResult` adding `vm_name`,
  `snapshot_name`, `message`.
  - _Requirements: 11_

- [ ] 6. Define `WindowsVmSpec`, `WindowsVmInfo`, `WindowsVmDiskSpec`,
  `WindowsSnapshotInfo` dataclasses.
  - _Requirements: 4, 7, 9_

- [ ] 7. Define `WindowsVirtualizationQueryError` carrying `command`,
  `exit_code`, `stderr`, optional `message`.
  - _Requirements: 2, 3, 9_

## Helpers

- [ ] 8. Implement `_typed_config()` returning a typed config.

- [ ] 9. Implement `_build_powershell_argv(script)` for the canonical
  scriptblock dispatch.

- [ ] 10. Implement `_render_create_script(spec)` rendering the
  multi-step ``New-VM ...; Set-VMProcessor ...`` scriptblock.

- [ ] 11. Implement `_read_vm_record(name, raise_on_unknown=False)`
  probing ``Get-VM -Name``.

- [ ] 12. Implement `_disk_attached(vm, path)` probing
  ``Get-VMHardDiskDrive``.

- [ ] 13. Implement `_snapshot_exists(vm, snapshot_name)` probing
  ``Get-VMSnapshot``.

- [ ] 14. Implement `_dispatch_virt_write(...)` wrapping `_dispatch`
  with `vm_name` / `snapshot_name`.

- [ ] 15. Implement `_build_result(...)` for non-runner result
  construction.

## Read Verbs

- [ ] 16. Implement `list_vms()` per Requirement 2.

- [ ] 17. Implement `get_vm_info(name)` per Requirement 3.

- [ ] 18. Implement `list_snapshots(vm)` per Requirement 9.4.

## Write Verbs

- [ ] 19. Implement `create_vm(spec)` per Requirement 4.

- [ ] 20. Implement `delete_vm(name)` per Requirement 5.

- [ ] 21. Implement `start_vm` / `stop_vm` / `pause_vm` /
  `resume_vm` per Requirement 6.

- [ ] 22. Implement `attach_disk` / `detach_disk` per Requirements 7
  and 8.

- [ ] 23. Implement `snapshot_vm(vm, snapshot_name)` and
  `restore_snapshot(vm, snapshot_name)` per Requirement 9.

## Parsers / Helpers

- [ ] 24. Implement `_parse_get_vm_json(text)` and
  `_normalise_to_list(parsed)`.

- [ ] 25. Implement `_normalise_state(state_raw)` accepting both
  string and numeric Hyper-V state payloads.

- [ ] 26. Implement `_record_to_vm_info(record, requested_name)`.

- [ ] 27. Implement `_record_to_snapshot_info(record)`.

- [ ] 28. Implement `_stderr_looks_unknown_vm(stderr)` and
  `_stderr_looks_no_snapshots(stderr)` heuristics.

## Tests

- [ ] 29. Create
  `source/testroots/testbase/ccai/tests/sysman/windows/test_virtualizationmanagement.py`.

- [ ] 30. Tests for `list_vms` / `list_snapshots` parsers and
  unknown-VM `get_vm_info`.

- [ ] 31. Tests for every write verb's command construction, dry-run,
  and idempotency SKIPPED branch.

- [ ] 32. Test asserting `force=True` -> `Stop-VM -Force -TurnOff`
  and `force=False` -> plain `Stop-VM`.

- [ ] 33. Tests for the data-loss advisory on `delete_vm`,
  `detach_disk`, and `restore_snapshot`.

- [ ] 34. Elevation tests proving writes pass `elevated=True` and
  reads pass `elevated=False`.

- [ ] 35. Failure-surfacing test (non-zero runner exit -> FAILED with
  stderr).

- [ ] 36. State-normalisation test asserting that numeric `State` (
  `2`) maps onto `WindowsVmState.RUNNING`.

## Follow-ups

- [ ] 37. **Virtual switch lifecycle** — track separately under
  `3-firewall-management` or a future `3-network-virtualization`
  feature.

- [ ] 38. **Hyper-V Replica / Live Migration** — track separately
  under a future high-availability feature.

- [ ] 39. **Storage-Spaces backing for VHDXs** — track in
  `3-storage-management`.

- [ ] 40. **WSL2 distribution management** — track in a separate
  feature; intentionally NOT covered by this contract.

- [ ] 41. **VHD lifecycle** — `New-VHD`, `Remove-VHD`,
  `Resize-VHD` are not exposed today; track in
  `3-storage-management`.
