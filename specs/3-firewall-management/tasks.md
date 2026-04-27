# Implementation Plan — Windows Firewall Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.firewallmanagement`
  module.  The bundled services-to-(port, protocol) table lives
  inline in the same module as `_BUNDLED_SERVICES_TABLE` rather than
  in a separate `_services_table.py` file (matching the macOS
  sibling's structure for cross-OS-symmetric reviewability).

- [x] 2. Define `WindowsFirewallActionStatus` enum (re-exported from
  `featurebase.ActionStatus`)

- [x] 3. Define `WindowsFirewallManagerConfig` dataclass (`dry_run`,
  `elevated`, `powershell_executable`, `services_table` override
  hook)

- [x] 4. Define `WindowsFirewallActionResult` dataclass (`zone`,
  `detail_name`, `message`)

- [x] 5. Define `WindowsZoneInfo` and `WindowsFirewallState`
  dataclasses, plus the `WindowsFirewallQueryError` exception

## WindowsFirewallManager — Implemented

- [x] 6. `__init__` accepts runner and optional config

- [x] 7. `add_service(service, zone)` / `remove_service(service, zone)`
  — services-to-(port, protocol) resolution from the bundled table
  with caller override, zone validation against
  `{Domain, Private, Public, Any}`, idempotent via
  `Get-NetFirewallRule -DisplayName <sentinel>`

- [x] 8. `add_port(port, protocol, zone)` /
  `remove_port(port, protocol, zone)` — protocol validation
  (`tcp` / `udp`), zone validation, idempotent with
  `DisplayName=automojo-port-<port>-<protocol>`

- [x] 9. `add_rich_rule(rule, zone)` /
  `remove_rich_rule(rule, zone)` — `-DisplayName` clause is required
  (FAILED with `message="display_name_required"` otherwise) so the
  manager has a stable identifier for idempotency and removal

- [x] 10. `set_default_zone(zone)` — validated SKIPPED no-op

  > **Spec deviation:** Windows has no first-class "default profile"
  > concept; profiles activate based on the OS-assigned
  > `NetworkCategory` of each connection.  The pilot returns
  > `SKIPPED` for valid zone names with an explanatory `message`,
  > and `FAILED` with `message="invalid_zone"` for unknown zones.

- [x] 11. `reload()` — SUCCESS no-op with explanatory `message`

  > **Spec deviation:** Windows Firewall applies rule changes
  > immediately, so no reload is required.  The runner is never
  > invoked.

- [x] 12. `query_state()` — parses `Get-NetFirewallProfile` JSON
  (`running=True` iff every profile's `Enabled` is True) and
  `Get-NetConnectionProfile` JSON (active `NetworkCategory` mapped to
  profile name for `default_zone`)

- [x] 13. `query_zone(zone)` / `query_all_zones()` —
  `Get-NetFirewallRule | ConvertTo-Json` parse, classified by
  `DisplayName` prefix (`automojo-svc-` / `automojo-port-` /
  rich-rule passthrough); `interfaces` and `sources` are always
  empty lists (Windows has no per-zone interface or source concept)

- [x] 14. PowerShell argv construction matches the locked-in shape
  (`["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
  "<script>"]`)

## WindowsFirewallManager — Gaps (specified but not implemented)

- [ ] 15. Outbound rules — every rule built by this manager is
  `-Direction Inbound`.  Outbound support requires either an opt-in
  flag on the spec or a separate `add_outbound_*` verb family.

- [ ] 16. Rule-group management (`-Group`) for bulk enable/disable.

- [ ] 17. Firewall profile configuration (default action, logging) —
  out of scope; this module manages rules, not profile policy.

- [ ] 18. Advanced address filters (`-LocalAddress`,
  `-InterfaceAlias`) beyond what `add_rich_rule` accepts verbatim.

- [ ] 19. IPsec policy integration (`New-NetIPsecRule`).

## Tests — Implemented

- [x] 20. `test_query_state_running_when_all_profiles_enabled`
- [x] 21. `test_query_state_not_running_when_one_disabled`
- [x] 22. `test_query_state_default_zone_from_int_category` (and the
  string-category variant covered in
  `test_query_state_running_when_all_profiles_enabled`)
- [x] 23. `test_query_zone_classifies_by_displayname_prefix`
- [x] 24. `test_add_service_skipped_when_rule_exists`
- [x] 25. `test_add_service_runs_new_netfirewallrule`
- [x] 26. `test_add_port_runs_new_netfirewallrule_with_localport`
- [x] 27. `test_set_default_zone_skipped_when_valid_profile`
- [x] 28. `test_set_default_zone_failed_when_invalid_profile`
- [x] 29. `test_reload_returns_success_without_invoking_runner`
- [x] 30. `test_add_service_invalid_zone_returns_failed` (also covers
  invalid zone on writes)

## Tests — Gaps (now implemented)

- [x] 31. `test_remove_service_skipped_when_absent` /
  `test_remove_service_dispatches_when_present` /
  `test_remove_rich_rule_dispatches`
- [x] 32. `test_add_rich_rule_requires_displayname`
- [x] 33. `test_query_all_zones_iterates_three_profiles`
- [x] 34. `test_add_service_dry_run_short_circuits` /
  `test_add_port_dry_run_short_circuits`
- [x] 35. `test_reads_never_elevate_even_with_elevated_config`
- [x] 36. `test_query_zone_classifies_by_displayname_prefix` exercises
  the array path; the bare-object path is exercised by
  `_normalize_to_list` callers in the log-management tests
  (`test_normalize_to_list_dict_becomes_one_element_list`)

## Follow-ups

- [ ] 37. **Cross-OS contract conformance review**: verify the
  `running=all-profiles-enabled` semantic and the
  `default_zone=NetworkCategory` mapping against the cross-OS
  contract.

- [ ] 38. **Bundled services table**: align the
  `_BUNDLED_SERVICES_TABLE` with the macOS sibling so both OSes
  accept the same service vocabulary when driven by the same
  coordinator.  The Windows pilot ships the seven-entry table
  defined in the design (ssh / http / https / smtp / dns / smb /
  rdp); the macOS sibling ships ~12 entries.

- [ ] 39. **Sibling reuse check**: confirm no other Windows feature
  manager invokes PowerShell firewall cmdlets directly; they should
  delegate here.

- [ ] 40. **Outbound-rule extension**: pick the surface for outbound
  rules — opt-in flag on the spec, or a separate verb family.
