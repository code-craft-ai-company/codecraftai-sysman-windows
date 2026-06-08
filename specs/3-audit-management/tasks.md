# Implementation Plan — Windows Audit Management

Checked items (`[x]`) are implemented and covered by tests.
Unchecked items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.windows.auditmanagement`
  module.

- [x] 2. Define `WindowsAuditActionStatus` enum (re-exported alias
  of `ccai.sysman.windows.featurebase.ActionStatus`).

- [x] 3. Define `WindowsAuditManagerConfig` dataclass (`dry_run`,
  `elevated`, `powershell_executable`, `default_log_channel`,
  `allow_destructive`).

- [x] 4. Define `WindowsAuditActionResult` dataclass (subclass of
  `FeatureActionResult`; adds `rule_id`, `rule_key`, `message`).

- [x] 5. Define `WindowsAuditRuleSpec` dataclass (`subcategory`,
  `success`, `failure`).

- [x] 6. Define `WindowsAuditRuleInfo` dataclass (`rule_id`,
  `subcategory`, `category`, `success`, `failure`, `expression`,
  `subsystem`, `source`).

- [x] 7. Define `WindowsAuditStatus` dataclass (`enabled`,
  `running`, `pid`, `failure_mode`, `rate_limit`,
  `backlog_limit`, `backlog`, `lost_events`, `locked`,
  `rule_count`, `subcategory_counts`, `raw`).

- [x] 8. Define `WindowsAuditEventQuery` and `WindowsAuditEvent`
  dataclasses.

- [x] 9. Define `WindowsAuditQueryError` and
  `WindowsAuditImmutableError` exceptions.

## WindowsAuditManager — Implemented

- [x] 10. Implement `__init__` (inherited from
  `BaseFeatureManager`) accepting `runner` and optional `config`.

- [x] 11. Implement `_build_auditpol_set` rendering the canonical
  ``auditpol /set /subcategory:<name> /success:<...>
  /failure:<...>`` argv.

- [x] 12. Implement `add_rule(spec)` with idempotency guard,
  dry-run short-circuit, and runtime dispatch.

- [x] 13. Implement `remove_rule(rule_id)` reusing
  `_build_auditpol_set` against a disabled spec.

- [x] 14. Implement `list_rules` parsing the CSV output of
  `auditpol /get /category:* /r`.

- [x] 15. Implement `enable_auditing` issuing ``wevtutil sl
  <channel> /e:true`` with idempotency guard against ``wevtutil
  gl``.

- [x] 16. Implement `disable_auditing` issuing ``auditpol /clear
  /y`` gated by ``config.allow_destructive``.

- [x] 17. Implement `get_audit_status` aggregating the CSV rows
  into the structured dataclass.

- [x] 18. Implement `search_events(query)` building the
  PowerShell ``Get-WinEvent`` pipeline and parsing the JSON
  output.

- [x] 19. Implement `_parse_auditpol_csv`,
  `_normalise_inclusion_setting`, `_split_csv_line`,
  `_build_get_winevent_script`, `_parse_winevent_json`.

- [x] 20. Implement `_safe_runtime_rules`,
  `_channel_already_enabled` for write-path resilience.

- [x] 21. Implement `_typed_config`, `_build_powershell_argv`
  helpers shared with the rest of the package.

## WindowsAuditManager — Gaps (specified but not implemented)

- [ ] 22. Standalone `reload_rules` verb (n/a on Windows but
  surfaced for cross-OS conformance).

- [ ] 23. Native ``-FilterXPath`` builder for ``search_events``
  (today the manager only emits ``-FilterHashtable``; XPath is
  available via ``raw_filter``).

- [ ] 24. ``auditpol /backup`` integration for archival snapshots.

- [ ] 25. ``Get-WinEvent`` failure classification: a missing
  channel raises a different exit code than a permission denial,
  and the manager today collapses both into ``WindowsAuditQueryError``.

## Tests — Implemented

- [x] 26. `test_add_rule_invokes_auditpol`
- [x] 27. `test_add_rule_skipped_when_present`
- [x] 28. `test_add_rule_dry_run_short_circuits`
- [x] 29. `test_remove_rule_invokes_auditpol`
- [x] 30. `test_remove_rule_skipped_when_absent`
- [x] 31. `test_list_rules_parses_output`
- [x] 32. `test_enable_auditing_invokes_wevtutil`
- [x] 33. `test_enable_auditing_skipped_when_enabled`
- [x] 34. `test_disable_auditing_requires_allow_destructive`
- [x] 35. `test_disable_auditing_invokes_auditpol_when_destructive_allowed`
- [x] 36. `test_get_audit_status_parses_auditpol_output`
- [x] 37. `test_search_events_invokes_get_winevent_with_query`
- [x] 38. `test_search_events_parses_winevent_json`
- [x] 39. `test_writes_elevate`
- [x] 40. `test_reads_never_elevate`
- [x] 41. `test_failed_command_surfaces_stderr`
- [x] 42. `test_build_auditpol_set_renders_argv`
- [x] 43. `test_normalise_inclusion_setting`
- [x] 44. `test_parse_auditpol_csv_handles_full_file`
- [x] 45. `test_build_get_winevent_script_with_raw_filter`
- [x] 46. `test_parse_winevent_json_respects_keyword_and_limit`

## Tests — Gaps

- [ ] 47. Coverage for the destructive guard's interaction with
  dry-run (today the guard wins; verify behavior is documented).

- [ ] 48. Coverage for ``Get-WinEvent`` results that include a
  ``ProviderName`` array (some Windows builds emit non-string
  values).

## Spec Deviations / Revisions Needed

- [ ] 49. **`IFileOperator` collaborator**: not applicable —
  Windows audit policy is not file-backed.

- [ ] 50. **Subcategory-based addressing**: documented divergence
  from the contract's abstract ``AuditRuleSpec.expression``
  field.  Cross-OS callers must be aware.

- [ ] 51. **Elevated reads in ``search_events``**: documented
  exception to the contract's "reads do not elevate" rule.

- [ ] 52. **`AuditImmutableError` raising**: this implementation
  exposes the type for cross-OS conformance but never raises it
  (Windows has no immutable mode).

## Follow-ups

- [ ] 53. **Cross-OS contract conformance review**: verify that
  the shipping dataclasses match the abstract names in
  `codecraftai-sysman-common/specs/contracts/audit-management/contract.md`.

- [ ] 54. **SACL coverage**: document the boundary between
  audit-management (subcategory-level policy) and acl-management
  (per-object SACLs).
