# Implementation Plan — Windows Service Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked items (`[ ]`)
identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.servicemanagement` module

- [x] 2. Define `WindowsServiceActionStatus` and `WindowsServiceScope` enums

- [x] 3. Define `WindowsServiceManagerConfig` dataclass (dry_run, elevated,
  powershell_executable, target_machine)

- [x] 4. Define `WindowsServiceActionResult`, `WindowsServiceStatus`,
  `WindowsServiceInfo` dataclasses and `WindowsServiceQueryError` exception

- [x] 5. Implement the `Status`/`StartType` integer→enum-string mapping tables

## WindowsServiceManager — Implemented

- [x] 6. Implement `__init__` accepting a runner and a config

- [x] 7. Implement `start_service` / `stop_service` with idempotency guards based
  on `state` (Running / Stopped)

- [x] 8. Implement `restart_service` as unconditional via `Restart-Service`

- [ ] 9. Implement `reload_service` aliased to restart with the active-state
  precondition check  *(Pilot deviates: returns FAILED per the task spec because
  Windows services have no native reload signal.  See Follow-ups.)*

- [x] 10. Implement `enable_service` / `disable_service` via `Set-Service
  -StartupType` with idempotency guards

- [x] 11. Implement `mask_service` / `unmask_service` as aliases to
  `disable_service` / `enable_service` with docstrings that document the alias

- [x] 12. Implement `get_service_status` wrapping `Get-Service | Select-Object |
  ConvertTo-Json`, parsing the JSON via `json.loads` and handling the
  `NoServiceFoundForGivenName` case by returning a synthetic not-found status

- [x] 13. Implement `list_services` with single-object/array normalisation after
  `ConvertTo-Json`

- [x] 14. Implement `reload_daemon` as a no-op returning `SUCCESS` with a
  descriptive `message`

- [x] 15. Implement USER-scope rejection for every verb (write → FAILED result
  without runner call; read → `WindowsServiceQueryError`)

- [x] 16. Implement the `_build_powershell_argv(script)` helper that wraps text
  in `["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "<script>"]`

## WindowsServiceManager — Gaps (specified but not implemented)

- [ ] 17. Per-user services (`_<sessionid>` suffix) management — explicitly out of
  scope for the base contract; documented as a future opt-in extension

- [ ] 18. Service recovery actions (`sc failure`) — explicitly out of scope per
  contract

- [ ] 19. Service dependency graph traversal (`Get-Service -DependentServices`)

- [ ] 20. `pid` population via WMI (`Get-CimInstance Win32_Service`) — today the
  status query does not populate `pid`

- [ ] 21. Event Log extraction for service-specific events — belongs to
  `log-management`

## Tests — Implemented

- [x] 22. `test_start_service_invokes_start_service`
- [x] 23. `test_start_service_skipped_when_running`
- [x] 24. `test_stop_service_invokes_stop_service`
- [x] 25. `test_stop_service_skipped_when_stopped`
- [x] 26. `test_restart_service_unconditional`
- [ ] 27. `test_reload_service_aliased_to_restart_with_message`  *(Superseded —
  reload_service returns FAILED in this pilot; covered by
  `test_reload_service_returns_failed_with_message`.)*
- [x] 28. `test_reload_service_returns_failed_with_message`
- [x] 29. `test_enable_service_invokes_set_service_automatic`
- [x] 30. `test_enable_service_skipped_when_automatic`
- [x] 31. `test_disable_service_invokes_set_service_disabled`
- [x] 32. `test_mask_service_invokes_set_service_disabled`
- [x] 33. `test_unmask_service_invokes_set_service_automatic`
- [x] 34. `test_get_service_status_parses_json`
- [x] 35. `test_get_service_status_decodes_status_int`
- [x] 36. `test_get_service_status_decodes_start_type_int`
- [x] 37. `test_get_service_status_returns_not_found_for_unknown_service`
- [x] 38. `test_list_services_parses_json_array`
- [x] 39. `test_list_services_parses_single_object_payload`
- [x] 40. `test_reload_daemon_returns_success_noop`
- [x] 41. `test_user_scope_returns_failed_with_message`
- [x] 42. `test_user_scope_read_raises_query_error`
- [x] 43. `test_write_with_elevated_config_passes_elevated_true`
- [x] 44. `test_read_never_elevates`
- [x] 45. `test_failed_command_returns_failed_result_with_stderr`
- [x] 46. `test_powershell_argv_construction_includes_noprofile`
- [x] 47. `test_start_service_dry_run_short_circuits`

## Tests — Gaps

- [ ] 48. Test that `list_services` raises `WindowsServiceQueryError` when
  `Get-Service` returns a non-zero exit with a non-empty stderr

- [ ] 49. Test that `get_service_status` raises `WindowsServiceQueryError` when
  `Get-Service` fails with an unrecognised error ID (i.e., not
  `NoServiceFoundForGivenName`)

- [ ] 50. Test that aspect composition on the runner is transparent

- [ ] 51. Test that ConvertTo-Json outputs containing Unicode display names
  round-trip correctly through the parser

- [ ] 52. Test dry-run short-circuit for every remaining write verb (stop,
  restart, reload, enable, disable, mask, unmask, reload_daemon).  The pilot
  covers `start_service` only.

## Follow-ups

- [ ] 53. **`reload_service` semantics review**: the pilot returns FAILED with a
  descriptive message because Windows services have no native reload signal.
  The original design document proposed aliasing to `restart_service` with a
  precondition.  Decide whether the cross-OS contract should be tightened to
  treat reload-on-Windows as FAILED (current pilot) or an alias (original
  design), and align the contract's Windows row accordingly.

- [ ] 54. **Cross-OS contract conformance review**: verify that
  `WindowsServiceActionResult` / `WindowsServiceStatus` / `WindowsServiceInfo`
  shapes match the contract, and that the mask/unmask alias semantics and USER
  scope rejection are documented in the contract's Windows row.

- [ ] 55. **PowerShell 7 compatibility**: validate that `ConvertTo-Json -Compress`
  emits identical shapes in Windows PowerShell 5.1 and PowerShell 7+, and pin the
  default `powershell_executable` accordingly for remote automation.

- [ ] 56. **Per-user service enablement (opt-in)**: a future opt-in extension
  could implement the `_<sessionid>` suffix conventions and the
  `Set-Service`/`Get-Service` permutations needed to control them.  Today's pilot
  rejects USER scope with a typed FAILED result.
