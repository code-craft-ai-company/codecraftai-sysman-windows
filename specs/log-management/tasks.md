# Implementation Plan

- [ ] 1. Create the `logmanagement` module file
  - Create `source/packages/automojo/sysman/linux/windows/logmanagement.py`
  - Add module-level docstring describing purpose, the four operation categories
    (journal query, journal vacuum, log file inspection, logrotate management), and
    the coordinator-driven design
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11_

- [ ] 2. Define journal data models
  - Implement `JournalQueryParams` dataclass: `unit`, `since`, `until`, `priority`,
    `lines`, `grep`, `output_format` (all optional)
  - Implement `JournalQueryResult` dataclass: `success`, `lines`, `stderr`, `command`
  - Implement `JournalDiskUsage` dataclass: `size_human`, `size_bytes`
  - _Requirements: 1, 2_

- [ ] 3. Define action result and log file types
  - Implement `LogActionStatus` enum: `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`
  - Implement `LogActionResult` dataclass: `status`, `detail`, `exit_code`, `stdout`,
    `stderr`, `command`
  - Implement `LogFileResult` dataclass: `success`, `lines`, `stderr`, `command`
  - _Requirements: 3, 4, 5, 6, 7, 8, 9_

- [ ] 4. Define logrotate data models
  - Implement `LogrotateConfig` dataclass: `log_path`, `rotate`, `frequency`,
    `compress`, `missingok`, `notifempty`, `copytruncate`, `postrotate`
  - Implement `LogrotateDropInState` dataclass: `name`, `exists`, `content`
  - _Requirements: 8, 10_

- [ ] 5. Define `LogManagementError` exception
  - Subclass `Exception`; store `exit_code` and `stderr` attributes
  - _Requirements: 2.3_

- [ ] 6. Implement `LogManagerConfig` dataclass
  - Fields: `dry_run: bool = False`, `elevated: bool = False`
  - _Requirements: 3.4, 4.4, 7.4, 8.4, 9.4, 11_

- [ ] 7. Implement `_render_logrotate_config(config: LogrotateConfig) -> str` helper
  - Render the configuration block in logrotate syntax
  - Emit `compress`, `missingok`, `notifempty` only when `True`
  - Emit `copytruncate` only when `True`
  - Emit `postrotate` / `endscript` stanza only when `postrotate` is set
  - _Requirements: 8_

- [ ] 8. Implement `_parse_disk_usage(output: str) -> JournalDiskUsage` helper
  - Extract human-readable size with regex
  - Convert to bytes using suffix multiplier table (B/K/M/G/T)
  - Raise `LogManagementError` if output does not match expected pattern
  - _Requirements: 2_

- [ ] 9. Implement `LogManager.__init__`
  - Accept `ICommandRunner`, `IFileOperator`, and `LogManagerConfig`
  - Define class-level constant: `LOGROTATE_D = "/etc/logrotate.d"`
  - _Requirements: 1–11_

- [ ] 10. Implement `query_journal(params)`
  - Build `journalctl` args from `JournalQueryParams` fields; always append `--no-pager`
  - Run via command runner with no elevation
  - On exit 0: split stdout into lines; return `JournalQueryResult(success=True, ...)`
  - On non-zero: return `JournalQueryResult(success=False, lines=[], stderr=...)`
  - _Requirements: 1_

- [ ] 11. Implement `journal_disk_usage()`
  - Run `journalctl --disk-usage --no-pager` with no elevation
  - On exit 0: call `_parse_disk_usage(stdout)`; return `JournalDiskUsage`
  - On non-zero: raise `LogManagementError(exit_code, stderr)`
  - _Requirements: 2_

- [ ] 12. Implement `vacuum_by_size(size)` and `vacuum_by_time(period)`
  - Return `DRY_RUN` result immediately when `dry_run=True`
  - Run `journalctl --vacuum-size=<size>` / `--vacuum-time=<period>` with
    `elevated=config.elevated`
  - Map exit 0 → `SUCCESS`; non-zero → `FAILED` with exit code and stderr
  - _Requirements: 3, 4, 11_

- [ ] 13. Implement `read_log_file(path, lines)`
  - Run `tail --lines=<n> <path>` with no elevation
  - On exit 0: return `LogFileResult(success=True, lines=stdout.splitlines())`
  - On exit non-zero: return `LogFileResult(success=False, lines=[], stderr=stderr)`
  - _Requirements: 5_

- [ ] 14. Implement `search_log_file(path, pattern, *, context_lines, max_matches)`
  - Build `grep --extended-regexp [--context=] [--max-count=] <pattern> <path>`
  - Run with no elevation
  - Exit 0 → `success=True`, captured lines
  - Exit 1 → `success=True`, empty lines (no matches is not an error)
  - Exit 2 → `success=False`, stderr
  - _Requirements: 6_

- [ ] 15. Implement `rotate_log(config_path)`
  - When `dry_run=True`: run `logrotate --debug <config_path>` (no elevation); return
    `LogActionResult(status=DRY_RUN, detail=stdout)`
  - When not dry-run: run `logrotate --force <config_path>` with
    `elevated=config.elevated`
  - Map exit 0 → `SUCCESS`; non-zero → `FAILED`
  - _Requirements: 7_

- [ ] 16. Implement `query_logrotate_dropin(name)`
  - Build path `/etc/logrotate.d/<name>`
  - If file does not exist: return `LogrotateDropInState(name, exists=False, content=None)`
  - Otherwise: read file and return `LogrotateDropInState(name, exists=True, content=...)`
  - No elevation
  - _Requirements: 10_

- [ ] 17. Implement `write_logrotate_dropin(name, config)`
  - Return `DRY_RUN` immediately when `dry_run=True`
  - Call `query_logrotate_dropin(name)` to get current state
  - Render desired content via `_render_logrotate_config(config)`
  - If file exists and content matches: return `SKIPPED`
  - Write file with `elevated=config.elevated`; return `SUCCESS`
  - _Requirements: 8_

- [ ] 18. Implement `remove_logrotate_dropin(name)`
  - Return `DRY_RUN` immediately when `dry_run=True`
  - Check existence via `query_logrotate_dropin(name)`; return `SKIPPED` if not present
  - Delete file with `elevated=config.elevated`; return `SUCCESS`
  - _Requirements: 9_

- [ ] 19. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_logmanagement.py`
  - Implement `MockCommandRunner` with per-args-prefix configurable `RunResult`
  - Implement `MockFileOperator` backed by in-memory dict with call recording
  - _Requirements: 1–11_

- [ ] 20. Write tests for `_render_logrotate_config()`
  - Test: all defaults — only mandatory fields rendered
  - Test: `copytruncate=True` — `copytruncate` line present
  - Test: `postrotate` set — `postrotate`/`endscript` stanza rendered
  - Test: `compress=False` — `compress` line absent
  - _Requirements: 8_

- [ ] 21. Write tests for `_parse_disk_usage()`
  - Test: `1.2G` → `size_human="1.2G"`, `size_bytes=1288490188`
  - Test: `512M` → correct byte count
  - Test: `100K` → correct byte count
  - Test: unrecognised output → raises `LogManagementError`
  - _Requirements: 2_

- [ ] 22. Write tests for `query_journal()`
  - Test: no params — minimal command, `--no-pager` always present
  - Test: all params set — all flags present in command
  - Test: exit 0 — `success=True`, lines populated
  - Test: non-zero exit — `success=False`, stderr captured
  - _Requirements: 1_

- [ ] 23. Write tests for `journal_disk_usage()`, `vacuum_by_size()`, `vacuum_by_time()`
  - Test journal_disk_usage: success — `JournalDiskUsage` returned
  - Test journal_disk_usage: failure — raises `LogManagementError`
  - Test vacuum_by_size: success — `SUCCESS`
  - Test vacuum_by_size: failure — `FAILED`
  - Test vacuum_by_size: dry-run — `DRY_RUN`, no command
  - Test vacuum_by_size: elevated — runner called with `elevated=True`
  - Test vacuum_by_time: same cases as vacuum_by_size
  - _Requirements: 2, 3, 4_

- [ ] 24. Write tests for `read_log_file()` and `search_log_file()`
  - Test read: exit 0 — `success=True`, lines populated
  - Test read: non-zero — `success=False`
  - Test search: exit 0 — `success=True`, lines populated
  - Test search: exit 1 — `success=True`, empty lines
  - Test search: exit 2 — `success=False`
  - Test search: `context_lines` and `max_matches` — flags present in command
  - _Requirements: 5, 6_

- [ ] 25. Write tests for `rotate_log()`
  - Test normal: `logrotate --force` issued, `SUCCESS`
  - Test normal: failure — `FAILED`
  - Test dry-run: `logrotate --debug` issued, `DRY_RUN`, stdout in detail
  - Test elevated: runner called with `elevated=True`
  - _Requirements: 7_

- [ ] 26. Write tests for `write_logrotate_dropin()`, `remove_logrotate_dropin()`,
  `query_logrotate_dropin()`
  - Test write: file absent — written, `SUCCESS`
  - Test write: file exists with same content — `SKIPPED`, no write
  - Test write: file exists with different content — overwritten, `SUCCESS`
  - Test write: dry-run — `DRY_RUN`, no write
  - Test write: elevated — file operator called with `elevated=True`
  - Test remove: file exists — deleted, `SUCCESS`
  - Test remove: file absent — `SKIPPED`
  - Test remove: dry-run — `DRY_RUN`, no delete
  - Test query: file exists — `exists=True`, content returned
  - Test query: file absent — `exists=False`, `content=None`
  - _Requirements: 8, 9, 10_
