# Implementation Plan — Windows Network Configuration

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.networkconfiguration`
  module

- [x] 2. Define `WindowsNetworkActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN) — aliases the shared `ActionStatus` enum from
  `featurebase`

- [x] 3. Define `WindowsNetworkConfigurationManagerConfig` dataclass
  (dry_run, elevated, plus the `powershell_executable` knob shared
  with the Windows pilot)

- [x] 4. Define `WindowsNetworkActionResult` dataclass with
  `interface`, `destination`, `requires_reboot`, and `message` fields.
  *Spec deviation*: the original spec called for a `detail` (dict) and
  a `diff` field; the landed shape uses the inherited `detail: str`
  from `FeatureActionResult` plus a top-level `requires_reboot: bool`
  to surface the `Rename-Computer` deferred-reboot requirement.  `diff`
  is omitted because every Windows write goes through PowerShell
  cmdlets rather than declarative file rewrites where a unified diff
  would make sense.

- [x] 5. Define `WindowsInterfaceInfo` and `WindowsRouteInfo`
  dataclasses

- [x] 6. Define `WindowsNetworkQueryError` exception

## Parsing Helpers

- [x] 7. Implement `_normalize_to_list(payload)` converting a bare
  PowerShell JSON object into a one-element list when the cmdlet
  emits a single record

- [x] 8. Implement `_parse_get_netadapter(stdout)` returning a list
  of dicts representing each adapter row

- [x] 9. Implement `_parse_get_netipaddress(stdout)` returning a
  dict keyed on `InterfaceAlias` with separate `ipv4` / `ipv6`
  buckets of `<IPAddress>/<PrefixLength>` strings

- [x] 10. Implement `_parse_get_netroute(stdout)` returning a list of
  `WindowsRouteInfo`, coercing `NextHop == "0.0.0.0"` / `"::"` to
  `next_hop=None`

- [x] 11. Implement `_parse_get_dns(stdout)` flattening every
  `ServerAddresses` array (one per address family) into a single
  ordered list

- [x] 12. Implement `_dotted_quad_to_prefix(netmask)` counting set
  bits in the dotted-quad mask, or passing through when the input
  is already an integer string.  Pure function so it can be tested
  in isolation.

- [x] 13. Implement `_build_powershell_argv(script)` returning the argv
  `["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
  script]`

## WindowsNetworkConfigurationManager — Implemented

- [x] 14. Implement `__init__` accepting runner + optional config
  (inherited from `BaseFeatureManager`)

- [x] 15. Implement `get_interfaces()` — dispatch `Get-NetAdapter |
  ConvertTo-Json` + `Get-NetIPAddress | ConvertTo-Json`, merge by
  `InterfaceAlias`

- [ ] 16. Implement `get_interface(name)` — *not in this batch*; the
  contract surface only specifies `get_interfaces`.  Reserved as a
  future thin wrapper over an `-Name`-scoped `Get-NetAdapter` call.

- [x] 17. Implement `configure_interface(name, address, netmask,
  gateway)` — convert netmask to prefix, probe current state for
  idempotency, dispatch chained `Remove-NetIPAddress` +
  `New-NetIPAddress`; omit `-DefaultGateway` when `gateway` is `None`

- [x] 18. Implement `enable_interface(name)` — idempotent via
  `Get-NetAdapter`, dispatch `Enable-NetAdapter -Name '<name>'`

- [x] 19. Implement `disable_interface(name)` — idempotent via
  `Get-NetAdapter`, dispatch `Disable-NetAdapter -Name '<name>'
  -Confirm:$false`

- [x] 20. Implement `add_route(dest, gateway)` — auto-resolve the
  default-route interface alias; FAIL with
  `message="no_default_route_interface"` when no default route exists.
  *Spec deviation*: the spec accepted an explicit `interface`
  argument and failed with `interface_required_on_windows` when
  absent.  The cross-OS contract has no per-interface argument on
  `add_route`, so the implementation auto-discovers the interface
  alias via `Get-NetRoute -DestinationPrefix 0.0.0.0/0`.

- [x] 21. Implement `remove_route(dest)` — idempotent via
  `Get-NetRoute -DestinationPrefix <dest>`; dispatch
  `Remove-NetRoute -DestinationPrefix '<dest>' -Confirm:$false`

- [ ] 22. Implement `get_routes()` — *not in this batch*; the cross-OS
  contract does not include a top-level route enumeration verb.
  `_parse_get_netroute` is exposed for tests so callers can layer it
  on top.

- [x] 23. Implement `set_dns_servers(interface, servers)` — idempotent
  via `get_dns_servers()`; dispatch
  `Set-DnsClientServerAddress -InterfaceAlias '<iface>'
  -ServerAddresses @(...)` or `-ResetServerAddresses` when empty

- [x] 24. Implement `get_dns_servers(interface)` — dispatch
  `Get-DnsClientServerAddress -InterfaceAlias '<iface>' |
  ConvertTo-Json -Compress -Depth 5`; flatten

- [ ] 25. Implement `set_search_domains(domains)` — *not in cross-OS
  contract*; reserved follow-up

- [x] 26. Implement `get_hostname()` — dispatch
  `[System.Net.Dns]::GetHostName()`

- [x] 27. Implement `set_hostname(name)` — idempotent via
  `get_hostname()` (case-insensitive); dispatch `Rename-Computer
  -NewName '<name>' -Force`; populate `requires_reboot=True` on
  SUCCESS *and* on DRY_RUN

- [x] 28. Implement `_dispatch_write` helper — runner-result mapping,
  `elevated=config.elevated` for writes, `command` capture

- [x] 29. Implement `_resolve_default_interface()` helper for
  `add_route` via `Get-NetRoute -DestinationPrefix 0.0.0.0/0`

## WindowsNetworkConfigurationManager — Gaps (specified but not implemented)

- [ ] 30. Immediate post-rename reboot (`Restart-Computer -Force`)
  — out of base contract; reserved `set_hostname_and_reboot`
  extension

- [ ] 31. `Set-NetIPInterface -NlMtuBytes` MTU management — out of
  base contract

- [ ] 32. VLAN / team / bond creation via `New-NetLbfoTeam` — out
  of base contract

- [ ] 33. Wi-Fi SSID / WPA configuration — out of base contract

- [ ] 34. 802.1x / EAP configuration — out of base contract

## Tests — Implemented

The landed test file covers 32 tests split across pure-helper
validation, manager verb behaviour, idempotency / dry-run /
elevation policy, and parser shapes (array / bare-object / mixed
PowerShell 5.1 vs 7+ payloads).  Spec test names below are mapped
to the implemented tests where the topic matches; the implemented
suite consolidates / renames where helpful.

- [x] 35. `test_get_interfaces_merges_adapter_and_ip_payloads`
  (was: `test_get_interfaces_parses_get_netadapter_json`)
- [x] 36. `test_parse_get_netadapter_handles_bare_object`
  (was: `test_get_interfaces_normalises_bare_object_into_list`)
- [x] 37. `test_merge_adapter_and_ip_joins_by_alias`
  (was: `test_get_interfaces_joins_ip_addresses_by_alias`)
- [ ] 38. `test_get_interface_returns_none_on_no_adapter_found` —
  `get_interface` not implemented (see item 16)
- [ ] 39. `test_get_interface_raises_on_unexpected_error` — same
- [x] 40. `test_dotted_quad_to_prefix_class_c` /
  `test_configure_interface_dispatches_remove_and_new`
  (was: `test_configure_interface_converts_dotted_quad_netmask`)
- [x] 41. `test_configure_interface_omits_default_gateway_when_none`
- [x] 42. `test_configure_interface_skipped_when_already_set`
  (was: `test_configure_interface_skipped_when_state_matches`)
- [x] 43. `test_configure_interface_dry_run_short_circuits`
  (was: `test_configure_interface_dry_run_captures_command`)
- [x] 44. `test_enable_interface_skipped_when_already_up`
- [x] 45. `test_disable_interface_skipped_when_already_disabled`
- [x] 46. `test_add_route_failed_when_no_default_route`
  (was: `test_add_route_requires_interface_or_returns_failed` —
  reframed as auto-discovery rather than caller-supplied interface)
- [x] 47. `test_add_route_skipped_when_matching_gateway_exists`
  (was: `test_add_route_skipped_when_matching_triple_exists`)
- [x] 48. `test_remove_route_skipped_when_absent`
- [x] 49. `test_parse_get_netroute_normalises_zero_next_hop`
  (was: `test_get_routes_normalises_zero_next_hop_to_none`)
- [x] 50. `test_set_dns_servers_uses_reset_for_empty_list`
- [x] 51. `test_set_dns_servers_skipped_when_matching`
- [x] 52. `test_parse_get_dns_handles_array_of_arrays` /
  `test_get_dns_servers_returns_flattened_list`
  (was: `test_get_dns_servers_normalises_null_string_and_array`)
- [ ] 53. `test_set_search_domains_passes_empty_array_when_empty` —
  `set_search_domains` not implemented (see item 25)
- [ ] 54. `test_set_search_domains_skipped_when_matching` — same
- [x] 55. `test_get_hostname_returns_stripped_stdout`
- [x] 56. `test_set_hostname_dispatches_rename_computer_and_marks_reboot`
  (was: `test_set_hostname_dispatches_rename_computer`)
- [x] 57. `test_set_hostname_skipped_when_matching_case_insensitive`
- [x] 58. `test_set_hostname_dry_run_marks_reboot`
  (was: `test_set_hostname_detail_contains_requires_reboot` — the
  flag lives on the result's top-level `requires_reboot` field
  rather than the inherited `detail` dict)

## Tests — Gaps

- [x] 59. Test dry-run short-circuits every mutating method —
  covered by `test_configure_interface_dry_run_short_circuits` and
  `test_set_hostname_dry_run_marks_reboot`; broader matrix coverage
  is folded into `test_writes_elevate_when_config_elevated`

- [x] 60. Test reads never pass `elevated=True` regardless of config —
  `test_reads_never_elevate_when_config_elevated`

- [x] 61. Test writes pass `elevated=True` when config enables it —
  `test_writes_elevate_when_config_elevated`

- [ ] 62. Property-based tests (hypothesis) covering Properties
  1 through 26 — deferred

- [ ] 63. Retry-aspect test proving a transient `New-NetIPAddress`
  failure followed by success is transparent to the manager —
  deferred (no retry aspect is wired into the locked-in pattern)

## Follow-ups

- [ ] 64. **Cross-OS contract conformance review**: verify the
  `WindowsNetworkActionResult` field shapes match the cross-OS
  contract at
  `automojo-sysman-common/specs/contracts/network-configuration/contract.md`.
  In particular: confirm `requires_reboot` belongs in the contract
  (cross-OS callers can otherwise miss the deferred change).

- [ ] 65. **Immediate-reboot extension plan**: scope whether a
  companion `set_hostname_and_reboot` verb belongs in this manager
  or a separate lifecycle-management feature.

- [ ] 66. **automojo-* reuse check**: confirm no other feature in
  `automojo-sysman-windows` invokes `Get-NetAdapter` /
  `New-NetIPAddress` / `Set-DnsClientServerAddress` /
  `Rename-Computer` directly; all such calls should go through
  `WindowsNetworkConfigurationManager`.
