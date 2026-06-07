# Implementation Plan — Windows VPN Client Management

## Module and Types

- [x] 1. Create the `ccai.sysman.windows.vpnclientmanagement` module.

- [x] 2. Re-export `WindowsVPNActionStatus`, `WindowsVPNActionResult`,
  `WindowsVPNClientStatus`, `WindowsVPNClientQueryError` from the shared
  base under Windows-prefixed names.

- [x] 3. Define `WindowsVPNClientManagerConfig` extending
  `VPNClientManagerConfig` with `tailscale_service_name`,
  `twingate_service_name`, `twingate_config_path`, `msi_cache_dir`,
  `tailscale_product_name`, `twingate_product_name` fields.

## WindowsVPNClientManager — Implementation

- [x] 4. Implement `__init__(*, runner, config=None)` calling the base.

- [x] 5. Implement `_get_installed_version(product_name, aspects)` using
  `Get-Package -Name <name>` parsed from JSON; return `None` when absent.

- [x] 6. Implement `_get_product_code(product_name, aspects)` using the
  Uninstall registry hive or `Get-Package` for the MSI ProductCode used by
  `msiexec /x`.

- [x] 7. Implement `_ensure_cache_dir(aspects)` —
  `New-Item -ItemType Directory -Force -Path <msi_cache_dir>`.

- [x] 8. Implement `install_tailscale(*, version, channel, aspects)` —
  idempotency guard via `_get_installed_version`, cache-dir prep, MSI
  download, `msiexec /i … /quiet /norestart` elevated.

- [x] 9. Implement `install_twingate_connector(*, version, aspects)` —
  parallel shape.

- [x] 10. Implement `configure(*, provider, settings, aspects)` —
  Twingate writes config via `Set-Content -Encoding UTF8`; Tailscale
  returns `SKIPPED`.

- [x] 11. Implement `bring_up(*, provider, authkey, hostname,
  advertise_tags, advertise_routes, exit_node, aspects)` —
  `Set-Service` + `Start-Service` then `tailscale up …` for Tailscale;
  `Set-Service` + `Start-Service` only for Twingate.

- [x] 12. Implement `bring_down(*, provider, aspects)` —
  `tailscale down` + `Stop-Service` for Tailscale; `Stop-Service` for
  Twingate.

- [x] 13. Implement `status(*, provider, aspects)`.

- [x] 14. Implement `uninstall(*, provider, purge, aspects)` — look up
  ProductCode, `msiexec /x <ProductCode> /quiet /norestart`, optional
  `Remove-Item -Recurse -Force` of the state directory.

## Parsers

- [x] 15. Implement `_parse_tailscale_status_json(payload)` returning
  `WindowsVPNClientStatus`. Total over all backend states.

- [x] 16. Implement `_parse_get_service_json(payload)` returning
  `WindowsVPNClientStatus(self_address=None, peer_addresses={})`.

## Tests — Examples

- [x] 17. `test_install_tailscale_downloads_then_runs_msiexec`.
- [x] 18. `test_install_tailscale_skipped_when_present`.
- [x] 19. `test_install_tailscale_failed_on_msiexec_error`.
- [x] 20. `test_install_twingate_connector_downloads_then_runs_msiexec`.
- [x] 21. `test_install_twingate_connector_skipped_when_present`.
- [x] 22. `test_configure_tailscale_returns_skipped`.
- [x] 23. `test_configure_twingate_writes_via_set_content`.
- [x] 24. `test_configure_twingate_skipped_when_content_matches`.
- [x] 25. `test_bring_up_tailscale_sets_service_then_runs_up`.
- [x] 26. `test_bring_up_tailscale_skipped_when_running_with_matching_state`.
- [x] 27. `test_bring_up_twingate_starts_service`.
- [x] 28. `test_bring_up_returns_elevation_blocked_when_uac_denies`.
- [x] 29. `test_bring_down_tailscale_stops_service`.
- [x] 30. `test_bring_down_twingate_stops_service`.
- [x] 31. `test_status_tailscale_parses_running_json`.
- [x] 32. `test_status_tailscale_returns_installed_false_when_missing`.
- [x] 33. `test_status_twingate_parses_get_service_json`.
- [x] 34. `test_uninstall_tailscale_runs_msiexec_x_with_product_code`.
- [x] 35. `test_uninstall_purge_removes_state_directory`.
- [x] 36. `test_dry_run_short_circuits_every_write_verb`.
- [x] 37. `test_writes_pass_elevated_when_configured`.
- [x] 38. `test_reads_never_elevate`.

## Tests — Property (hypothesis)

- [x] 39. `test_property_install_tailscale_idempotent` — generated
  `(version, channel)`.
- [x] 40. `test_property_uninstall_idempotent`.
- [x] 41. `test_property_bring_up_idempotent_on_matching_state`.
- [x] 42. `test_property_dry_run_never_invokes_runner`.

## Documentation

- [x] 43. Sphinx-style docstrings on every public method.

- [x] 44. Module-level docstring linking to the contract and the base spec.

## Follow-ups

- [x] 45. Cross-OS contract conformance check.
  Confirmed: the four per-OS `*VPNClientManager` classes
  (`WindowsVPNClientManager`, `MacosVPNClientManager`, `RedHatVPNClientManager`,
  `UbuntuVPNClientManager`) each implement the identical seven-verb surface —
  `install_tailscale`, `install_twingate_connector`, `configure`, `bring_up`,
  `bring_down`, `status`, `uninstall` — and all subclass the shared
  `VPNClientManagementBase` from `codecraftai-sysman-common`.

- [ ] 46. Confirm the `elevation_blocked` token is identical in shape to
  the value used by `3-firewall-management` and the planned
  `3-service-management` feature on Windows.
  DISCREPANCY (not flipped): no sibling Windows feature currently defines an
  `elevation_blocked` token. `3-firewall-management` deliberately defaults to no
  elevation and lets the command runner surface a raw access-denied error
  (firewall design.md req 4); its `detail` field carries only service/port/zone
  strings. `3-service-management` does not reference the token either. The VPN
  feature's stable `"elevation_blocked"` / `"elevation_blocked: <base_detail>"`
  detail token is therefore currently the sole definition of the convention —
  there is nothing to confirm shape-parity against. Recommend either (a) promoting
  the token to a shared constant in `codecraftai-sysman-common` so future features
  adopt it, or (b) re-scoping this task once `3-service-management` lands.

- [x] 47. Windows Firewall integration follow-up — confirm that installing
  Tailscale's MSI also installs the necessary Firewall rules; surface a
  follow-up to coordinate with `3-firewall-management` if not.
  Confirmed: the Tailscale Windows MSI (`tailscale-setup-*.msi`) bundles its own
  WireGuard interface and registers the required Windows Firewall rules as part of
  the `msiexec /i` install — the VPN feature neither needs nor should separately
  invoke `3-firewall-management`. design.md already documents (line ~184) that
  callers may inspect the action result and coordinate with `3-firewall-management`
  / `3-service-management` if a site policy strips inbound rules; no code change
  required.
