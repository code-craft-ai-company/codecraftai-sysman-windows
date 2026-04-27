# Implementation Plan — Windows Repository Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.repositorymanagement`
  module

- [x] 2. Define `WindowsRepositoryActionStatus` enum (SUCCESS /
  FAILED / SKIPPED / DRY_RUN)

  Implemented as `WindowsRepositoryActionStatus = ActionStatus` so
  the shared featurebase enum carries the four contract values
  without duplication.

- [x] 3. Define `WindowsRepositoryManagerConfig` dataclass
  (dry_run, elevated)

  Adds `winget_executable: str = "winget"` so callers can override
  the executable path when the runner has no usable `PATH` (e.g.
  some WinRM runspaces).

- [x] 4. Define `WindowsRepositorySpec` and `WindowsRepositoryInfo`
  dataclasses

  Spec deviation: this batch ships the `(name, url, type)` triple
  rather than the full cross-OS `RepositorySpec`.  Fields not
  expressible on winget (`display_name`, `mirror_list_url`,
  `gpg_check`, `gpg_key`, `priority`, `enabled`) are NOT carried
  on the dataclass.  Callers wanting to surface those for cross-OS
  consistency can wrap a `WindowsRepositorySpec` inside a richer
  spec at a higher layer.

- [x] 5. Define `WindowsRepositoryActionResult` dataclass with
  `name`, `url`, and `message` fields

- [x] 6. Define `WindowsRepositoryQueryError` exception

  Spec deviation: `WindowsRepositoryNotFoundError` and
  `WindowsRepositoryKeyFileNotFoundError` are NOT shipped because
  `set_priority` and `import_gpg_key` are not shipped (see items
  17 and 18).

## Parsing Helpers

- [x] 7. Implement `_parse_winget_source_list_table(stdout)` —
  locate the dash-separator row, derive column starts from the
  header row's whitespace gaps (the separator is one continuous
  dash run on real winget), and slice each subsequent data row by
  character position; return a list of dicts keyed by column
  header

  Spec deviation: column starts come from the header row's
  whitespace gaps, not from the separator dashes.  Real `winget`
  output emits a single continuous run of `-` regardless of column
  count, so the header is the only signal carrying per-column
  boundaries.  Documented in the module docstring under
  "Output-parsing fragility".

- [ ] 8. Implement `_warn_na_fields(spec)` — out of this batch's
  scope.  The `WindowsRepositorySpec` does NOT carry the N/A
  fields (`gpg_key`, `mirror_list_url`, `priority`), so there is
  nothing to warn about.  Defer to the cross-OS-spec wrapper layer
  if/when one is introduced.

## WindowsRepositoryManager — Implemented

- [x] 9. Implement `__init__` accepting runner and optional config
  (inherited from `BaseFeatureManager`)

- [x] 10. Implement `add_repository(spec)` — idempotency via
  `winget source list` matching `(name, url)`; dispatch
  `winget source add --name <spec.name> --arg <spec.url>
  --type <spec.type>`

  Spec deviation: the contract `RepositorySpec` carries
  `base_url: str | None`; this batch's `WindowsRepositorySpec`
  takes `url: str` (required) so the `base_url_required` FAILED
  branch the original tasks.md called for is not reachable.  The
  type system enforces the requirement at the boundary.

- [x] 11. Implement `remove_repository(name)` — idempotency via
  `winget source list`; dispatch `winget source remove --name
  <name>`

- [x] 12. Implement `enable_repository(name)` — no runner
  mutation; FAILED with `message="winget does not support
  enabling/disabling sources without removal"`.  Honours dry-run.

  Spec deviation: the original tasks.md called for SKIPPED-when-
  present / FAILED-when-absent semantics.  Per the prompt, this
  batch returns FAILED unconditionally with an explanatory message
  so cross-OS callers are alerted that the requested state change
  is not expressible on winget; the SKIPPED-when-present variant
  would silently mask the limitation.

- [x] 13. Implement `disable_repository(name)` — no runner
  mutation; FAILED with `message="winget does not support
  disabling sources; use remove_repository to remove"`.  Honours
  dry-run.

- [x] 14. Implement `list_repositories()` — dispatch `winget
  source list`, parse, return `list[WindowsRepositoryInfo]`

- [x] 16. Implement `refresh_repositories()` — dispatch `winget
  source update`; never skips

  Spec deviation: this batch's `refresh_repositories` takes no
  arguments; the contract's `refresh_repositories(name: str |
  None)` signature with a per-source variant is not implemented
  because winget's `source update` does not accept a per-source
  filter (the operation is global).

- [x] 19. Implement `_dispatch_write` helper — dry-run
  short-circuit, runner-result mapping, `elevated=True` only for
  writes (`source add`, `source remove`, `source update`),
  `command` capture (via the shared
  `BaseFeatureManager._dispatch`)

## WindowsRepositoryManager — Gaps (specified but not implemented)

- [ ] 15. `get_repository(name)` — out of this batch's scope.
  Callers can derive it from `list_repositories()` until the verb
  is shipped.

- [ ] 17. `set_priority(name, priority)` — out of this batch's
  scope.  winget has no priority concept; the implementation
  would amount to a typed FAILED result similar to
  `enable_repository`.

- [ ] 18. `import_gpg_key(key_source)` — out of this batch's
  scope.  winget verifies via the source's signing certificate
  rather than per-source GPG; the implementation would amount to
  a typed FAILED / SKIPPED result.

- [ ] 20. Chocolatey source management (`choco source add`) — not
  in the contract surface and not implemented; a separate manager
  may be added in the future

- [ ] 21. `winget source reset` to restore the default sources —
  out of scope; the contract does not expose a reset verb

- [ ] 22. `winget source export` / `source import` for bulk
  configuration management — out of scope

- [ ] 23. Per-user vs machine-scope distinction at the verb layer
  — coordinator responsibility; the manager only honours
  `elevated`

## Tests — Implemented

- [x] 24. `test_add_repository_invokes_winget_source_add`
- [x] 25. `test_add_repository_skipped_when_present`
- [x] 26. `test_add_repository_proceeds_when_url_differs` —
  asserts that the idempotency guard is keyed on `(name, url)`
  together so a URL change forces a re-add
- [x] 27. `test_remove_repository_invokes_winget_source_remove`
- [x] 28. `test_remove_repository_skipped_when_not_present`
- [x] 29. `test_enable_repository_returns_failed_with_message`
- [x] 30. `test_disable_repository_returns_failed_with_message`
- [x] 31. `test_list_repositories_parses_winget_source_list_text`
- [x] 32. `test_list_repositories_raises_on_nonzero_exit`
- [x] 33. `test_list_repositories_empty_when_no_data_rows`
- [x] 34. `test_refresh_repositories_invokes_winget_source_update`
- [x] 35. `test_dry_run_short_circuits_add`
- [x] 36. `test_dry_run_short_circuits_remove`
- [x] 37. `test_write_verbs_pass_elevated_true`
- [x] 38. `test_read_verbs_never_elevate`
- [x] 39. `test_failed_command_surfaces_stderr_in_result`

Total: 16 tests, all passing.

## Tests — Gaps

- [ ] 40. Tests for `get_repository` (deferred — see item 15)

- [ ] 41. Tests for `set_priority` and the
  `WindowsRepositoryNotFoundError` precondition (deferred — see
  item 17)

- [ ] 42. Tests for `import_gpg_key` and the
  `WindowsRepositoryKeyFileNotFoundError` precondition (deferred
  — see item 18)

- [ ] 43. Property-based tests (hypothesis) covering the contract
  invariants

## Follow-ups

- [ ] 44. **Cross-OS contract conformance review**: verify the
  `WindowsRepositoryActionResult` / `WindowsRepositoryInfo` /
  `WindowsRepositorySpec` field shapes against the contract at
  `automojo-sysman-common/specs/contracts/repository-management/contract.md`,
  and decide whether the contract-permitted divergences (no-op
  `enable` / `disable` returning FAILED, missing `set_priority` /
  `import_gpg_key` / `get_repository`) belong in the manager or in
  a higher cross-OS adapter.

- [ ] 45. **JSON-output migration**: when winget exposes stable
  machine-readable output for `source list`, retire the
  fixed-width text-table parser in favour of a JSON path.  The
  text parser is documented as best-effort in the module
  docstring under "Output-parsing fragility".

- [ ] 46. **Chocolatey extension plan**: scope whether
  Chocolatey sources belong in this package as an alternate
  manager (`WindowsChocolateyRepositoryManager`) or in a separate
  package.

- [ ] 47. **N/A field accounting**: decide whether to upgrade
  N/A-field warnings into a typed notice emitted through a
  future notification channel; the cross-OS spec wrapper would
  surface them at the spec layer rather than the manager layer.
