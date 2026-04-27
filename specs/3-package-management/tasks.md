# Implementation Plan — Windows Package Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.packagemanagement`
  module

- [x] 2. Define `WindowsPackageActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)

  Implemented as `WindowsPackageActionStatus = ActionStatus` so the
  shared featurebase enum carries the four contract values without
  duplication.

- [x] 3. Define `WindowsPackageManagerConfig` dataclass (dry_run,
  elevated, assume_yes)

  Spec deviation: this batch uses `extra_install_args` instead of
  `assume_yes`.  The `--silent --accept-package-agreements
  --accept-source-agreements` flags are unconditional because every
  documented winget batch-deployment guide requires them; the
  `assume_yes=False` mode (which would omit the acceptance flags) is
  not a real-world deployment knob and adding it would only encourage
  callers to ship configurations that block on agreement prompts.
  `extra_install_args` lets callers append `--scope user` etc.
  without subclassing.

- [x] 4. Define `WindowsPackageActionResult`, `WindowsPackageInfo`
  dataclasses

  Spec deviation: `WindowsPackageUpdateInfo` is NOT shipped in this
  batch because the contract verb `list_upgradable` is also not
  shipped (see item 19).  When the upgradable-listing verb lands, a
  `WindowsPackageUpdateInfo` dataclass with
  `(name, installed_version, available_version, arch)` fields will
  follow the `WindowsPackageInfo` shape.

- [x] 5. Define `WindowsPackageQueryError` exception

  Spec deviation: `WindowsPackageFileNotFoundError` is NOT shipped
  because `install_local` is not shipped (see item 20).

## Parsing Helpers

- [x] 6. Implement `_parse_winget_list_table(stdout)` that locates
  the dash-separator row, derives column starts from the header
  row's whitespace gaps (the separator is one continuous dash run
  on real winget), and slices each subsequent data row by character
  position

  Spec deviation: column starts come from the header row's
  whitespace gaps, not from the separator dashes.  Real `winget`
  output emits a single continuous run of `-` regardless of column
  count, so the header is the only signal carrying per-column
  boundaries.  Documented in the module docstring under
  "Output-parsing fragility".

- [x] 7. Implement `_parse_winget_show_text(stdout, requested_id)`
  that extracts `Version`, `Publisher`, `Description`, `Homepage`,
  `Id`, `Name` key-value lines into a `WindowsPackageInfo`

- [x] 8. `list_installed` reuses `_parse_winget_list_table` and
  filters via `_row_to_info`

- [x] 9. `_has_upgrade_available(name)` reuses
  `_parse_winget_list_table` against `winget upgrade
  --include-unknown` output and looks for a row whose `Id` column
  matches

- [x] 10. `_stdout_looks_missing(stdout)` matches the documented
  "no installed package" / "no package found matching input
  criteria" sentinel substrings (case-folded)

  Spec deviation: a dedicated `_is_no_upgrade_available(exit_code)`
  on `0x8A150011` is NOT shipped.  The upgrade-availability probe
  runs `winget upgrade --include-unknown` (a read) and treats any
  non-zero exit as "no upgrade visible" rather than special-casing
  exit codes; this keeps the manager forwards-compatible across
  winget versions whose exit-code surface evolves.

## WindowsPackageManager — Implemented

- [x] 11. Implement `__init__` accepting runner + optional config
  (inherited from `BaseFeatureManager`)

- [x] 12. Implement `install_package(name, version=None)` — probe
  via `winget list --exact --id`, dispatch
  `winget install --exact --id <name> [--version <v>] --silent
  --accept-package-agreements --accept-source-agreements`; SKIPPED
  when installed (and version matches when supplied)

- [x] 13. Implement `remove_package(name)` — probe via
  `is_installed`, dispatch `winget uninstall --exact --id <name>
  --silent`; SKIPPED when absent

- [x] 14. Implement `upgrade_package(name)` — probe via
  `is_installed` and `_has_upgrade_available`, dispatch
  `winget upgrade --exact --id <name> --silent
  --accept-package-agreements --accept-source-agreements`; SKIPPED
  when not installed (with `"package is not installed; nothing to
  upgrade"` message), or when no upgrade is available

- [x] 15. Implement `upgrade_all()` — dispatch `winget upgrade
  --all --silent --accept-package-agreements
  --accept-source-agreements`; never skips

- [x] 16. Implement `is_installed(name)` — `winget list --exact
  --id <name>`; True when exit 0 and stdout does NOT match the
  "no installed package" sentinel

- [x] 17. Implement `get_package_info(name)` — dispatch `winget
  show --exact --id <name>`, parse; map "no package found matching
  input criteria" to `None`; raise `WindowsPackageQueryError` on
  other failures

- [x] 18. Implement `list_installed()` — dispatch `winget list`,
  parse the tabular output; raise `WindowsPackageQueryError` on
  non-zero exit

- [x] 21. Implement `refresh_metadata()` — dispatch `winget source
  update`; never skips

- [x] 22. Implement `_dispatch_write` helper — dry-run
  short-circuit, runner-result mapping, `elevated=True` only for
  writes when `config.elevated=True`, `command` capture (via the
  shared `BaseFeatureManager._dispatch`)

## WindowsPackageManager — Gaps (specified but not implemented)

- [ ] 19. `list_upgradable()` — out of this batch's scope; the
  underlying `_has_upgrade_available` probe demonstrates the
  parsing path so a future batch can graduate it to a public read
  verb

- [ ] 20. `install_local(filepath)` — out of this batch's scope;
  requires a `WindowsPackageFileNotFoundError` precondition path

- [ ] 23. Chocolatey backend (`choco install`) — out of base
  contract; reserved for a future
  `WindowsChocolateyPackageManager` companion

- [ ] 24. `msiexec /i` fallback for `.msi` files that winget
  rejects — out of base contract

- [ ] 25. `winget pin` / `winget pin remove` for version-hold
  semantics — out of base contract

- [ ] 26. Microsoft Store-only install path (`--source msstore`)
  — reserved as future extension

## Tests — Implemented

- [x] 27. `test_install_package_invokes_winget_install`
- [x] 28. `test_install_package_with_version_passes_version_flag`
- [x] 29. `test_install_package_skipped_when_installed`
- [x] 30. `test_install_package_skipped_when_version_matches`
- [x] 31. `test_install_package_with_different_version_proceeds`
- [x] 32. `test_remove_package_invokes_winget_uninstall`
- [x] 33. `test_remove_package_skipped_when_not_installed`
- [x] 34. `test_upgrade_package_invokes_winget_upgrade`
- [x] 35. `test_upgrade_package_skipped_when_not_installed` —
  asserts the explicit "nothing to upgrade" message
- [x] 36. `test_upgrade_package_skipped_when_no_upgrade_available`
- [x] 37. `test_upgrade_all_invokes_winget_upgrade_all`
- [x] 38. `test_is_installed_returns_true_for_installed`
- [x] 39. `test_is_installed_returns_false_for_not_installed`
- [x] 40. `test_is_installed_returns_false_on_nonzero_exit`
- [x] 41. `test_get_package_info_parses_winget_show_text`
- [x] 42. `test_get_package_info_returns_none_for_missing`
- [x] 43. `test_get_package_info_raises_on_unexpected_failure`
- [x] 44. `test_list_installed_parses_winget_list_text_table`
- [x] 45. `test_list_installed_raises_on_nonzero_exit`
- [x] 46. `test_refresh_metadata_invokes_winget_source_update`
- [x] 47. `test_dry_run_short_circuits_install`
- [x] 48. `test_dry_run_short_circuits_remove`
- [x] 49. `test_write_verbs_pass_elevated_true`
- [x] 50. `test_read_verbs_never_elevate`
- [x] 51. `test_failed_command_surfaces_stderr_in_result`
- [x] 52. `test_silent_and_accept_flags_present_on_install`
- [x] 53. `test_extra_install_args_appended_to_install_argv`

Total: 27 tests, all passing.

## Tests — Gaps

- [ ] 54. Tests for the `list_upgradable` verb (deferred — see
  item 19)

- [ ] 55. Tests for `install_local` and the
  `WindowsPackageFileNotFoundError` precondition (deferred — see
  item 20)

- [ ] 56. Property-based tests (hypothesis) covering the contract
  invariants

## Follow-ups

- [ ] 57. **Cross-OS contract conformance review**: verify the
  `WindowsPackageActionResult` / `WindowsPackageInfo` field shapes
  match the cross-OS contract at
  `automojo-sysman-common/specs/contracts/package-management/contract.md`.
  The contract currently names `package_name` on the result; this
  batch uses `name` to match the Ubuntu / macOS sibling
  implementations.

- [ ] 58. **JSON-output migration**: when winget exposes stable
  machine-readable output for `list` / `show` / `upgrade`, retire
  the fixed-width text-table parser and the
  `_stdout_looks_missing` sentinel matcher in favour of a JSON
  path.  The text parser is documented as best-effort in the
  module docstring under "Output-parsing fragility".

- [ ] 59. **Chocolatey extension plan**: scope whether a
  `WindowsChocolateyPackageManager` companion belongs in this
  package or a separate package, and how the selector chooses
  between winget and choco.

- [ ] 60. **Exit-code catalogue**: build a dedicated module of
  well-known winget exit codes (e.g. `0x8A150011` "no applicable
  upgrade", `0x8A150014` "install cancelled") to drive
  `RetryAspect` configuration across features.

- [ ] 61. **automojo-* reuse check**: confirm no other feature in
  `automojo-sysman-windows` invokes `winget` directly; all such
  calls should go through `WindowsPackageManager`.
