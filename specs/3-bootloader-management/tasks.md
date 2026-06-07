# Implementation Plan -- Bootloader Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `bootloadermanagement` module under
  `source/packages/ccai/sysman/windows/`.

- [x] 2. Alias `WindowsBootloaderActionStatus = ActionStatus`.

- [x] 3. Define `WindowsBootloaderManagerConfig` dataclass with
  `dry_run`, `elevated`, and `bcdedit_executable`
  (default `"bcdedit.exe"`).

- [x] 4. Define `WindowsBootloaderActionResult` dataclass extending
  `FeatureActionResult` with `entry_id`, `requires_reboot`, and
  `message`.

- [x] 5. Define `WindowsBootEntrySpec` dataclass holding `id`
  (`None` for auto-generated), `name`, `application`
  (default `"osloader"`), `device`, `path`, and `options`.

- [x] 6. Define `WindowsBootEntryInfo` dataclass holding `id`,
  `name`, `kernel`, `initrd`, `cmdline`, `device`, `is_default`,
  and `boot_count`.

- [x] 7. Define `WindowsBootloaderQueryError` carrying `command`,
  `exit_code`, and `stderr`.

## Module-level Helpers

- [x] 8. Implement `_normalise_guid(value)` that accepts bare,
  brace-wrapped, and aliased identifiers and returns the
  canonical brace-wrapped lower-case form.  Raises `ValueError`
  on invalid input.

- [x] 9. Implement `_is_valid_guid(value)` predicate.

- [x] 10. Implement `_split_bcdedit_blocks(text)` that splits
  ``bcdedit`` stdout on blank lines into a list of blocks.

- [x] 11. Implement `_parse_bcdedit_block(block)` that parses a
  single block into a dict (key/value pairs plus the leading
  header captured as ``_kind``).  Honour bcdedit's two-or-more-
  spaces field separator and continuation-line indentation.

- [x] 12. Implement `_parse_bcdedit_entries(text)` that calls
  block split + block parse and returns one
  `WindowsBootEntryInfo` per non-alias block.

- [x] 13. Implement `_parse_bcdedit_default(text)` that parses
  ``bcdedit /enum {bootmgr} /v`` for the ``default`` line.

- [x] 14. Implement `_parse_bcdedit_create_guid(text)` that parses
  the auto-generated GUID from ``bcdedit /create`` stdout.

## WindowsBootloaderManager Verbs

- [x] 15. Implement `list_entries()` combining
  ``bcdedit /enum osloader``, ``bcdedit /enum {fwbootmgr}`` (read-
  only merge), and ``bcdedit /enum {bootmgr} /v`` for the default
  marker.

- [x] 16. Implement `get_default_entry()` parsing
  ``bcdedit /enum {bootmgr} /v`` and resolving the GUID against
  the osloader entry list.

- [x] 17. Implement `set_default_entry(entry_id)` running
  ``bcdedit /default {<guid>}`` with idempotency probe.

- [x] 18. Implement `add_entry(spec)` running ``bcdedit /create``,
  per-option ``bcdedit /set``, and
  ``bcdedit /displayorder ... /addlast`` with idempotency probe
  and auto-generated-GUID parsing.

- [x] 19. Implement `remove_entry(entry_id)` running
  ``bcdedit /delete {<guid>}`` with idempotency probe.

- [x] 20. Implement `update_bootloader_config()` returning a
  SUCCESS no-op carrying the standard message.

- [x] 21. Implement `regenerate_config()` returning a SUCCESS
  no-op carrying the standard message.

## WindowsBootloaderManager Internal Helpers

- [x] 22. Implement `_read_default_guid()` running
  ``bcdedit /enum {bootmgr} /v``.

- [x] 23. Implement `_build_result(...)` constructing a
  `WindowsBootloaderActionResult` without dispatch.

## Tests

- [x] 24. `test_list_entries_parses_bcdedit_output` -- bcdedit
  parser.

- [x] 25. `test_get_default_entry_resolves_id` -- default-guid
  resolution.

- [x] 26. `test_get_default_entry_raises_when_unresolvable` --
  typed error.

- [x] 27. `test_set_default_entry_invokes_bcdedit` -- happy path.

- [x] 28. `test_set_default_entry_skipped_when_already_default` --
  idempotency.

- [x] 29. `test_set_default_entry_resolves_guid_format` --
  bare-GUID normalisation.

- [x] 30. `test_add_entry_invokes_bcdedit` -- multi-step happy
  path.

- [x] 31. `test_add_entry_skipped_when_present` -- idempotency.

- [x] 32. `test_add_entry_dry_run_short_circuits` -- dry-run.

- [x] 33. `test_remove_entry_invokes_bcdedit` -- happy path.

- [x] 34. `test_remove_entry_skipped_when_absent` -- idempotency.

- [x] 35. `test_update_bootloader_config_returns_noop_success` --
  no-op.

- [x] 36. `test_regenerate_config_returns_noop_success` -- no-op.

- [x] 37. `test_writes_elevate` -- elevation policy.

- [x] 38. `test_reads_never_elevate` -- elevation policy.

- [x] 39. `test_failed_command_surfaces_stderr` -- error surface.

## Follow-ups

- [ ] 40. Add a `running-kernel safety` check for `remove_entry`
  -- the cross-OS contract requires `FAILED` when removing the
  entry matches the active boot selection.  Currently the
  manager relies on the BCD store being self-consistent and does
  not consult ``GetSystemFirmwareTable`` to identify the running
  loader.

- [ ] 41. Add a `bcdedit /export` helper for backup-before-write
  workflows.  The destructive-change-safety guidance documents
  the practice but the manager does not run the export
  automatically.

- [ ] 42. Surface BCD `inherit` chains and ``options`` properties
  in `WindowsBootEntryInfo` once a richer parser is needed for
  diff-based reporting.
