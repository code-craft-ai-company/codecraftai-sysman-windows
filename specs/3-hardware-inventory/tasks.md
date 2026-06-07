# Implementation Plan — Hardware Inventory (Windows)

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

This feature is read-only — no dry-run distinction, no idempotency
guards, no mutating operations.

## Module and Models

- [x] 1. Create the `hardwareinventory` module under
  `ccai.sysman.windows`.

- [x] 2. Define `WindowsHardwareInventoryActionStatus` with
  `SUCCESS`, `FAILED`, `SKIPPED` (no `DRY_RUN` per contract).

- [x] 3. Define `WindowsHardwareInventoryManagerConfig` inheriting
  from `FeatureConfig`, adding `powershell_executable`
  (default `"powershell.exe"`).  Document that `dry_run` is
  ignored and `elevated` is reserved for selective elevation.

- [x] 4. Define `WindowsHardwareInventoryQueryError` with
  `command` / `exit_code` / `stderr` and a multi-line message
  built via `os.linesep.join`.

- [x] 5. Define query-result dataclasses: `WindowsSystemInfo`,
  `WindowsCpuInfo`, `WindowsMemoryInfo`, `WindowsMemoryModule`,
  `WindowsStorageDeviceInfo`, `WindowsNetworkDeviceInfo`,
  `WindowsPciDeviceInfo`, `WindowsUsbDeviceInfo`,
  `WindowsFirmwareInfo`.

## Manager — Implemented

- [x] 6. Implement `__init__` (inherited; no overrides needed).

- [x] 7. Implement `_read` helper that dispatches a runner call with
  `elevated=False` and raises
  `WindowsHardwareInventoryQueryError` on a non-zero exit code.

- [x] 8. Implement `_build_powershell_argv` and `_typed_config`
  helpers.

- [x] 9. Implement `get_system_info` via
  `Get-ComputerInfo | ConvertTo-Json`.

- [x] 10. Implement `get_cpu_info` via
  `Get-CimInstance Win32_Processor | ConvertTo-Json`, summing
  socket counts and decoding the `Architecture` enum.

- [x] 11. Implement `get_memory_info` via
  `Get-CimInstance Win32_PhysicalMemory | ConvertTo-Json` and
  `Get-CimInstance Win32_OperatingSystem | Select-Object
  TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json`.

- [x] 12. Implement `list_storage_devices` via
  `Get-Disk | ConvertTo-Json` and `Get-Volume | ConvertTo-Json`,
  correlating by sorted `DriveLetter`.

- [x] 13. Implement `list_network_devices` via
  `Get-NetAdapter | ConvertTo-Json`.

- [x] 14. Implement `list_pci_devices` via
  `Get-CimInstance Win32_PnPEntity | Where-Object PNPClass='PCI'
  | ConvertTo-Json`.

- [x] 15. Implement `list_usb_devices` via
  `Get-CimInstance Win32_USBHub | ConvertTo-Json`.

- [x] 16. Implement `get_firmware_info` via
  `Get-CimInstance Win32_BIOS | ConvertTo-Json`, decoding UEFI
  via `BiosCharacteristics` token 43.

## Module-Level Parsers — Implemented

- [x] 17. `_coerce_json_records`, `_decode_json_object`.

- [x] 18. `_parse_get_computer_info`.

- [x] 19. `_parse_win32_processor`, `_decode_processor_arch`.

- [x] 20. `_parse_win32_physical_memory`, `_decode_memory_type`,
  `_merge_win32_operating_system`.

- [x] 21. `_parse_get_disk_and_volume`.

- [x] 22. `_parse_get_net_adapter`.

- [x] 23. `_parse_win32_pnp_entity_pci`, `_parse_pnp_device_id`.

- [x] 24. `_parse_win32_usbhub`.

- [x] 25. `_parse_win32_bios`.

## Tests — Implemented

- [x] 26. `test_get_system_info_invokes_get_computer_info_and_parses`
- [x] 27. `test_get_cpu_info_parses_get_ciminstance_output`
- [x] 28. `test_get_memory_info_aggregates_modules_and_total`
- [x] 29. `test_list_storage_devices_parses_get_disk_output`
- [x] 30. `test_list_network_devices_parses_get_netadapter_output`
- [x] 31. `test_list_pci_devices_parses_get_ciminstance_output`
- [x] 32. `test_list_usb_devices_parses_get_ciminstance_output`
- [x] 33. `test_get_firmware_info_parses_get_ciminstance_output`
- [x] 34. `test_failed_command_raises_query_error`
- [x] 35. `test_reads_never_elevate`

## Gaps (specified but not implemented)

- [ ] 36. `Confirm-SecureBootUEFI` invocation for `secure_boot`
  detection.  Currently `secure_boot` is always `None`.

- [ ] 37. `slots_total` from
  `Get-CimInstance Win32_PhysicalMemoryArray.MemoryDevices`.
  Currently only `slots_used` is populated.

- [ ] 38. CPU L1 cache size + `flags`.  ``Win32_Processor`` does
  not surface either; the proper read is via
  ``Get-CimInstance -Namespace root\\Microsoft\\Windows\\Storage``
  for cache topology and ``CPUID``-style native helpers for
  feature flags.  Currently `cache_l1` and `flags` are always
  empty / `None`.

- [ ] 39. Multi-volume disk correlation.  The pilot pairs the Nth
  disk with the Nth alphabetically-sorted DriveLetter; a disk
  with multiple volumes surfaces only the first.  The proper read
  is via `Get-Partition` + `Get-Volume` joined on `DiskNumber`,
  documented as a follow-up.

- [ ] 40. ``Win32_BIOS.ReleaseDate`` translation from the
  ``/Date(<ms>)/`` form to an ISO-8601 string.  Currently the raw
  string is surfaced verbatim.

- [ ] 41. USB device serial extraction.  ``Win32_USBHub`` does not
  carry a serial column; the proper read is via the deeper
  ``Get-PnpDeviceProperty`` -> `DEVPKEY_Device_Address` /
  `DEVPKEY_Device_HardwareIds` chain, documented as a follow-up.

- [ ] 42. `collect_snapshot` aggregator with partial-tolerance
  semantics per the cross-OS contract.

## Tests — Gaps

- [ ] 43. PowerShell-7-emitted-string ``Architecture`` test.
  PowerShell 7+ may serialise the enum as the string name rather
  than the integer; the parser already accepts both shapes but
  has no dedicated test.

- [ ] 44. Empty-stdout test (zero records) covering each
  list-emitting verb.

- [ ] 45. Multi-socket CPU aggregation test.

## Follow-ups

- [ ] 46. Profile PowerShell cold-start cost.  Each
  ``powershell.exe -Command`` invocation adds ~250-400 ms on
  Windows; the eight independent PowerShell invocations may be
  consolidated into a single scriptblock that emits a tagged
  payload to amortise the cost.

- [ ] 47. **codecraftai-* reuse check**: `codecraftai-interop` may
  expose generic PowerShell / CIM helpers that would shrink the
  parser surface here.

- [ ] 48. Spec-vs-contract drift discovered while writing this
  spec: the contract surfaces a `machine_id` field that on
  Windows lives at `HKLM:\\SOFTWARE\\Microsoft\\Cryptography
  \\MachineGuid` (registry, not WMI).  This pilot does not
  surface it; reconciliation needed in the next contract
  revision.
