# Implementation Plan — Windows Identity Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work tracked for follow-up batches.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.identitymanagement`
  module.

- [x] 2. Define `WindowsIdentityActionStatus` (SUCCESS / FAILED /
  SKIPPED / DRY_RUN).
  *Aliased to the shared `featurebase.ActionStatus`.*

- [x] 3. Define `WindowsIdentityManagerConfig` dataclass (dry_run,
  elevated, powershell_executable, requires_rsat,
  fallback_to_local).

- [x] 4. Define `WindowsIdentityActionResult` dataclass with
  `realm`, `username`, and `message` fields layered on top of
  `FeatureActionResult`.

- [x] 5. Define `WindowsRealmKind` enum with `ACTIVE_DIRECTORY`,
  `WORKGROUP`, `UNKNOWN`.

- [x] 6. Define `WindowsRealmJoinSpec` dataclass with `realm`,
  `admin_user`, `admin_password`, `computer_name`, `ou_path`,
  `domain_controller`.

- [x] 7. Define `WindowsRealmInfo`, `WindowsRealmStatus`,
  `WindowsUserResolveResult`, `WindowsGroupResolveResult`
  dataclasses.

- [x] 8. Define `WindowsIdentityQueryError` exception.

- [ ] 9. Define `WindowsIdentityCredentialError` exception.
  *Deferred — credentials are required fields on
  `WindowsRealmJoinSpec` and on the `leave_realm` argument list.*

- [ ] 10. Define `WindowsIdentityRealmNotFoundError` exception.
  *Deferred — `leave_realm` and `resolve_*` use SKIPPED /
  found=False semantics for missing realms / principals.*

## WindowsIdentityManager — Implemented

- [x] 11. Implement `__init__` accepting runner + optional config.
  *Inherited from `BaseFeatureManager.__init__`.*

- [x] 12. Implement `join_realm(spec)` — `Add-Computer -DomainName
  <r> -Credential $cred ...`.  Password embedded as a SecureString
  inside the scriptblock; recorded argv has the password redacted.
  Idempotent via `Win32_ComputerSystem`.

- [x] 13. Implement `leave_realm(admin_user, admin_password,
  realm=None)` — `Remove-Computer -UnjoinDomainCredential $cred
  ...`.  Skips when not joined or joined to a different realm.

- [x] 14. Implement `get_realm_status()` —
  `Win32_ComputerSystem | ConvertTo-Json` parsed.

- [x] 15. Implement `list_joined_realms()` — single-domain
  reality, returns a list of zero or one.

- [x] 16. Implement `sync_users()` — `klist purge -li 0x3e7`.

- [x] 17. Implement `resolve_user(username)` — `Get-ADUser` with
  `Get-LocalUser` fallback when RSAT is unavailable.  Detects
  realm via `DistinguishedName` `DC=` components.  Falls back to
  `realm=None` for local accounts.

- [x] 18. Implement `resolve_group(groupname)` — `Get-ADGroup`
  with `Get-LocalGroup` + `Get-LocalGroupMember` fallback.

- [x] 19. Implement `_sanitise_script_passwords` — regex-based
  redaction of every `ConvertTo-SecureString -AsPlainText -Force
  '<pw>'` literal inside the recorded scriptblock.

- [x] 20. Implement `_build_powershell_argv` — canonical
  ``["powershell.exe", "-NoProfile", "-NonInteractive",
  "-Command", <script>]`` shape.

- [x] 21. Implement `_extract_realm_from_dn` — stitches the `DC=`
  components of a DistinguishedName into a dotted realm name.

- [x] 22. Implement `_strip_dn_to_cn` — reduces a DN to its
  leading CN component for membership rendering.

- [x] 23. Implement `_looks_like_rsat_missing` — substring scan
  for the canonical PowerShell error markers when RSAT is not
  loaded.

## Tests — Implemented

- [x] 24. `_MockCommandRunner` with substring-matching helper for
  scriptblock recording.

- [x] 25. `test_join_realm_invokes_add_computer` — verifies
  scriptblock fragments + `elevated=True`.

- [x] 26. `test_join_realm_skipped_when_joined`.

- [x] 27. `test_join_realm_dry_run`.

- [x] 28. `test_leave_realm_invokes_remove_computer`.

- [x] 29. `test_leave_realm_skipped_when_not_joined`.

- [x] 30. `test_get_realm_status_parses_wmi_output`.

- [x] 31. `test_get_realm_status_returns_workgroup_when_not_joined`.

- [x] 32. `test_list_joined_realms_returns_list`.

- [x] 33. `test_sync_users_invokes_klist_purge`.

- [x] 34. `test_resolve_user_parses_get_aduser_output` — AD record
  with realm extraction from DN.

- [x] 35. `test_resolve_user_falls_back_to_local` — RSAT-missing
  → `Get-LocalUser` → `realm=None` result.

- [x] 36. `test_resolve_user_returns_not_found_when_unknown`.

- [x] 37. `test_resolve_group_parses_get_adgroup_output`.

- [x] 38. `test_password_redacted_in_recorded_command` — every
  embedded `ConvertTo-SecureString` literal in the recorded
  scriptblock has its body replaced with `<redacted>`.

- [x] 39. `test_password_redacted_for_leave_realm`.

- [x] 40. `test_writes_elevate`.

- [x] 41. `test_reads_never_elevate`.

- [x] 42. `test_failed_command_surfaces_stderr`.

- [x] 43. `test_get_realm_status_raises_on_query_failure` —
  `WindowsIdentityQueryError` on WMI non-zero exit.

## Tracked Follow-Ups

- [ ] 44. `discover_realm(domain)` via
  `Resolve-DnsName -Type SRV _ldap._tcp.<domain>`.

- [ ] 45. `extra_options` on `WindowsRealmJoinSpec` for
  `Add-Computer`-specific extras (`-Options
  AccountCreate,JoinReadOnly,Win9XUpgrade`,
  `-LocalCredential`, `-UnsecuredJoin`).

- [ ] 46. `IdentityCredentialError` integration once the
  cross-OS credential abstraction lands.

- [ ] 47. RSAT availability probe surfaced as
  `WindowsIdentityToolNotAvailableError` when
  `requires_rsat=True` and RSAT is missing.

- [ ] 48. Per-user Kerberos ticket lifecycle (`klist purge` for
  arbitrary LUIDs, `kinit` analogue via
  `cmdkey` / `Add-Type` invocations).

- [ ] 49. ``RemoveComputer -WorkgroupName`` follow-up to land in a
  declared workgroup rather than the default `WORKGROUP` after
  unjoin.

- [ ] 50. Cache the `Win32_ComputerSystem` JSON across the
  `get_realm_status` / `_is_realm_joined` calls inside a single
  `join_realm` invocation.

- [ ] 51. `pwsh.exe` (PowerShell 7+) coverage in the test rig
  with explicit `powershell_executable="pwsh.exe"`.
