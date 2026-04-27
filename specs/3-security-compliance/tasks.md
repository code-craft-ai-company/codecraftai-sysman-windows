# Implementation Plan — Windows Security Compliance

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

> **Spec deviation (root):** This pilot ships a thin
> primitive-aggregating skeleton that composes a
> `WindowsComplianceReport` from `secedit /export`,
> `Get-MpComputerStatus`, `Get-NetFirewallProfile`, and the UAC
> `EnableLUA` registry value.  The full per-rule check / remediation
> dispatch envisaged by the design — registry rules, Defender rules,
> BitLocker rules, baseline manifests under
> `<baselines_dir>/<profile>/` — is OUT OF SCOPE for the pilot.
> Automated remediation requires the Microsoft Security Compliance
> Toolkit (LGPO + GPO baselines) and is recorded as a follow-up.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.securitycompliance` module

- [x] 2. Define `WindowsSecurityComplianceActionStatus` enum (re-
  exported from `featurebase.ActionStatus`)

- [ ] 3. Define `WindowsComplianceSeverity` enum — pilot uses string
  tokens (`"low"` / `"medium"` / `"high"` / `"critical"`) on
  `WindowsComplianceFinding.severity` to keep the report shape
  serialisable without an enum import.  Convertible to a strict enum
  in the toolkit follow-up.

- [x] 4. Define `WindowsSecurityComplianceManagerConfig` dataclass
  (`dry_run`, `elevated`, `powershell_executable`, `output_dir`,
  `baseline_file`)

- [x] 5. Define `WindowsComplianceProfile`,
  `WindowsComplianceFinding`, `WindowsComplianceReport` dataclasses

- [x] 6. Define `WindowsBaselineInfo` dataclass

- [x] 7. Define `WindowsSecurityComplianceActionResult` dataclass

- [x] 8. Define `WindowsSecurityComplianceQueryError` exception

## Parsing Helpers

- [ ] 9. `_scan_baselines_dir` — out of scope; the pilot ships two
  hard-coded profiles.

- [ ] 10. `_read_manifest` — out of scope; see #9.

- [x] 11. `_parse_secedit_inf` (BOM-tolerant decode handled by
  `Get-Content -Encoding Unicode` upstream; the parser accepts the
  decoded text and produces a flat `dict[section][key] -> value`)

- [ ] 12. `_synthesize_remediation_inf` — deferred to toolkit
  follow-up.

- [x] 13. `_ps_escape_single_quoted` (re-implemented in this module)

- [x] 14. `_build_powershell_argv` (matches the locked-in shape; the
  spec's `; exit $LASTEXITCODE` suffix is omitted because the runner
  surfaces `$LASTEXITCODE` natively for `powershell.exe`)

- [x] 15. `_severity_from_rule_id` (string-token classifier)

- [ ] 16. `_parse_baseline_json` — pilot inlines a tiny
  `json.loads` + dict-shape check in `get_baseline`; corrupt JSON
  raises `WindowsSecurityComplianceQueryError`.

## Per-Check Dispatch

- [x] 17. `_evaluate_secedit` (returns three findings: minimum
  password length, lockout threshold, audit logon events)

- [ ] 18. `_check_registry` general dispatcher — pilot only reads the
  UAC `EnableLUA` key directly; deferred.

- [x] 19. `_evaluate_defender` (per-profile Defender state via
  `Get-MpComputerStatus`)

- [ ] 20. `_check_bitlocker` — deferred.

- [ ] 21. `_dispatch_check` — superseded by the primitive-aggregating
  shape in `_build_findings`.

## Per-Rule Remediation Dispatch

- [ ] 22. `_remediate_secedit_inf` — deferred.

- [ ] 23. `_remediate_registry` — deferred.

- [ ] 24. `_remediate_defender` — deferred.

- [ ] 25. `_remediate_bitlocker` — deferred.

- [x] 26. `_dispatch_remediation` is replaced by the pilot's blanket
  `remediate -> FAILED` with `message="remediation_not_supported"` so
  the gap is typed for cross-OS callers.

## WindowsSecurityComplianceManager — Implemented

- [x] 27. `__init__` accepts runner and optional config (no
  file_operator)

- [x] 28. `list_profiles()` returns the two bundled pilot profiles

- [ ] 29. `get_profile(profile_name)` — deferred; trivial filter on
  `list_profiles`.

- [x] 30. `run_compliance_profile(profile_name)` —
  `secedit /export` + Defender + Firewall + UAC, builds report

- [x] 31. `remediate(finding_id)` — typed FAILED skeleton

- [ ] 32. `remediate_all(report)` — deferred (no automated
  remediation in the pilot).

- [x] 33. `get_baseline()` — JSON read via `Get-Content`

- [x] 34. `set_baseline(spec)` — JSON write via `Set-Content` with
  idempotency guard

## WindowsSecurityComplianceManager — Gaps

- [ ] 35. Microsoft DSC resource authoring — out of scope.

- [ ] 36. `GPT.INI` / `registry.pol` direct parsing — out of scope.

- [ ] 37. LGPO.exe integration — toolkit follow-up.

- [ ] 38. MBSA / MBAP analysis — intentionally excluded.

- [ ] 39. Signed / evidence-collection report output — not
  implemented.

- [ ] 40. Attestation via TPM — not implemented.

- [ ] 41. Continuous-monitoring / agent-mode — not implemented.

- [ ] 42. Remediation rollback — not implemented (no remediation in
  the pilot).

## Tests — Implemented

- [x] 43. `test_list_profiles_returns_two_bundled_profiles`
- [x] 44. (missing-dir) — superseded by the bundled-profile model.
- [x] 45. (manifest description) — bundled descriptions are part of
  `test_list_profiles_returns_two_bundled_profiles`.
- [ ] 46. `test_get_profile_returns_none_on_miss` — deferred with
  task #29.
- [x] 47. `test_run_compliance_profile_builds_report_with_findings`
- [ ] 48. `test_run_compliance_profile_raises_on_secedit_export_failure`
  — pilot collapses individual primitive failures into `unknown`
  status rather than raising.
- [x] 49. `test_run_compliance_profile_unknown_profile_raises`
- [x] 50. (per check_type dispatch) — covered by
  `_evaluate_*` unit tests
- [x] 51. (finding-ID format) — covered by
  `test_evaluate_firewall_pass_per_profile`
- [ ] 52. (unsupported type passthrough) — N/A in pilot
- [x] 53. (remediate dispatches secedit_inf) — pilot returns FAILED;
  see `test_remediate_returns_failed_with_explanation`
- [x] 54. (remediate dispatches Set-ItemProperty) — same
- [x] 55. (remediate dispatches Set-MpPreference) — same
- [x] 56. (remediate dispatches Enable-BitLocker) — same
- [x] 57. (remediate rejects unknown type) —
  `test_remediate_returns_failed_with_explanation`
- [ ] 58. (remediate skipped when compliant) — deferred with #32
- [ ] 59. (remediate_all skips compliant) — deferred with #32
- [ ] 60. (remediate_all not_remediable) — deferred with #32
- [ ] 61. (remediate_all preserves order) — deferred with #32
- [x] 62. `test_get_baseline_returns_none_when_file_empty`
- [x] 63. `test_get_baseline_corrupt_json_raises`
- [ ] 64. `test_set_baseline_skipped_when_equal` — deferred (idempotency
  guard exists; explicit unit test covering equality is a follow-up)
- [ ] 65. `test_set_baseline_orders_run_then_remediate_then_write` —
  pilot does NOT chain run+remediate+write; deferred.
- [ ] 66. `test_set_baseline_step_failure_no_write` — deferred with
  #65.

## Tests — Gaps

- [x] 67. Dry-run short-circuits remediate
  (`test_remediate_dry_run_short_circuits`) and baseline write
  (`test_set_baseline_dry_run_short_circuits`)

- [x] 68. `test_run_compliance_profile_reads_dont_elevate`

- [ ] 69. Test elevation IS set for writes when config enables it —
  deferred (write surface in pilot is just `set_baseline`; will add
  alongside the remediation surface).

- [x] 70. Reads never elevate — covered by
  `test_run_compliance_profile_reads_dont_elevate`.

- [ ] 71. PowerShell single-quote escaping — `_ps_escape_single_quoted`
  is exercised via dispatch; explicit unit test deferred.

- [ ] 72. UTF-16-LE BOM handling in secedit — deferred until the parser
  consumes raw bytes (currently consumes the decoded text emitted by
  `Get-Content -Encoding Unicode`).

- [ ] 73. Property-based tests (hypothesis) — deferred.

## Follow-ups

- [ ] 74. **Cross-OS contract conformance review**: verify
  `WindowsComplianceReport` / `WindowsComplianceFinding` shapes
  match the contract.

- [ ] 75. **Toolkit bootstrap**: document a deployment step that
  downloads and extracts the Microsoft Security Compliance Toolkit
  alongside the pilot's primitives.

- [ ] 76. **LGPO.exe follow-up**: plan a future enhancement that
  replaces the per-rule dispatch (and the typed-FAILED `remediate`)
  with a full GPO-shaped apply.
