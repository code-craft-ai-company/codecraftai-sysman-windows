# Implementation Plan

- [ ] 1. Create the `servicemanagement` module file
  - Create `source/packages/automojo/sysman/linux/windows/servicemanagement.py`
  - Add module-level docstring describing purpose and coordinator-driven design
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12_

- [ ] 2. Define status enum and result dataclasses
  - Implement `ServiceActionStatus` enum with values: `SUCCESS`, `FAILED`, `SKIPPED`,
    `DRY_RUN`
  - Implement `ServiceActionResult` dataclass with fields: `status`, `service_name`,
    `exit_code`, `stdout`, `stderr`, `command`
  - Implement `ServiceInfo` dataclass with fields: `service_name`, `active_state`,
    `enabled_state`
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10_

- [ ] 3. Define typed exceptions
  - Implement `ServiceQueryError` with `exit_code` and `stderr` attributes
  - _Requirements: 9, 11_

- [ ] 4. Implement `ServiceManagerConfig` dataclass
  - Add `dry_run: bool = False` field
  - Add `elevated: bool = False` field
  - _Requirements: 1.5, 2.5, 3.4, 4.5, 5.5, 6.5, 7.5, 8.5, 10.4, 12_

- [ ] 5. Implement `ServiceManager.__init__` and `query()`
  - Accept `ICommandRunner` and `ServiceManagerConfig` as constructor arguments
  - Implement `query(name)` by running `systemctl is-active` and `systemctl is-enabled`
  - Treat any stdout string as the state value regardless of exit code for these two commands
  - Raise `ServiceQueryError` only on unexpected runner errors (e.g. runner itself fails)
  - Populate and return a `ServiceInfo` record
  - _Requirements: 9_

- [ ] 6. Implement shared private helper `_run_write_command()`
  - Accept service name, command args, and a `skip_if` callable over `ServiceInfo`
  - Call `query()` first and return `SKIPPED` if `skip_if` condition is met
  - Return `DRY_RUN` without calling runner when `dry_run=True`
  - Call runner with `elevated` flag and map result to `ServiceActionResult`
  - _Requirements: 1, 2, 5, 6, 7, 8, 12_

- [ ] 7. Implement `start()`
  - Use helper: skip when `active_state == "active"`
  - Build `systemctl start <name>` command
  - _Requirements: 1_

- [ ] 8. Implement `stop()`
  - Use helper: skip when `active_state == "inactive"`
  - Build `systemctl stop <name>` command
  - _Requirements: 2_

- [ ] 9. Implement `restart()`
  - No skip condition — always execute
  - Return `DRY_RUN` without calling runner when `dry_run=True`
  - Build `systemctl restart <name>` command; pass `elevated` flag
  - _Requirements: 3_

- [ ] 10. Implement `reload()`
  - Call `query()` first; return `FAILED` immediately if `active_state != "active"`
  - Return `DRY_RUN` without calling runner when `dry_run=True`
  - Build `systemctl reload <name>` command; pass `elevated` flag
  - _Requirements: 4_

- [ ] 11. Implement `enable()`
  - Use helper: skip when `enabled_state == "enabled"`
  - Build `systemctl enable <name>` command
  - _Requirements: 5_

- [ ] 12. Implement `disable()`
  - Use helper: skip when `enabled_state == "disabled"`
  - Build `systemctl disable <name>` command
  - _Requirements: 6_

- [ ] 13. Implement `mask()`
  - Use helper: skip when `enabled_state == "masked"`
  - Build `systemctl mask <name>` command
  - _Requirements: 7_

- [ ] 14. Implement `unmask()`
  - Use helper: skip when `enabled_state != "masked"`
  - Build `systemctl unmask <name>` command
  - _Requirements: 8_

- [ ] 15. Implement `daemon_reload()`
  - No skip condition and no service name argument
  - Return `DRY_RUN` without calling runner when `dry_run=True`
  - Build `systemctl daemon-reload` command; pass `elevated` flag
  - Return `ServiceActionResult` with `service_name=""` (not applicable)
  - _Requirements: 10_

- [ ] 16. Implement `get_journal()`
  - Accept `name` and optional `lines: int = 50`
  - Build `journalctl -u <name> --no-pager -n <lines>` command
  - Do NOT pass `elevated` flag (read-only operation)
  - Return captured stdout as string on success
  - Raise `ServiceQueryError` on non-zero exit code
  - _Requirements: 11_

- [ ] 17. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_servicemanagement.py`
  - Implement `MockCommandRunner` that returns configurable `RunResult` per command args
  - Helper factory `make_is_active_result(state)` and `make_is_enabled_result(state)` for
    setting up query responses
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12_

- [ ] 18. Write tests for `query()`
  - Test: active + enabled service — returns correct `ServiceInfo`
  - Test: inactive + disabled service — returns correct `ServiceInfo`
  - Test: masked service — `enabled_state` is `"masked"`
  - Test: unit not found — both states return `"not-found"`
  - _Requirements: 9_

- [ ] 19. Write tests for `start()` and `stop()`
  - Test start: service inactive — command issued, returns `SUCCESS`
  - Test start: service already active — returns `SKIPPED`, no command issued
  - Test start: command fails — returns `FAILED` with stderr
  - Test start: dry-run — returns `DRY_RUN`, no command issued
  - Test stop: service active — command issued, returns `SUCCESS`
  - Test stop: service already inactive — returns `SKIPPED`
  - Test stop: dry-run — returns `DRY_RUN`
  - _Requirements: 1, 2_

- [ ] 20. Write tests for `restart()`
  - Test: command issued unconditionally, returns `SUCCESS`
  - Test: command fails — returns `FAILED`
  - Test: dry-run — returns `DRY_RUN`, no command issued
  - Test: elevated mode — runner called with `elevated=True`
  - _Requirements: 3_

- [ ] 21. Write tests for `reload()`
  - Test: service active — reload command issued, returns `SUCCESS`
  - Test: service not active — returns `FAILED` without issuing reload command
  - Test: reload command fails — returns `FAILED` with stderr
  - Test: dry-run — returns `DRY_RUN`, no command issued
  - _Requirements: 4_

- [ ] 22. Write tests for `enable()` and `disable()`
  - Test enable: disabled service — command issued, returns `SUCCESS`
  - Test enable: already enabled — returns `SKIPPED`
  - Test enable: command fails — returns `FAILED`
  - Test enable: dry-run — returns `DRY_RUN`
  - Test disable: enabled service — command issued, returns `SUCCESS`
  - Test disable: already disabled — returns `SKIPPED`
  - Test disable: dry-run — returns `DRY_RUN`
  - _Requirements: 5, 6_

- [ ] 23. Write tests for `mask()` and `unmask()`
  - Test mask: unmasked service — command issued, returns `SUCCESS`
  - Test mask: already masked — returns `SKIPPED`
  - Test mask: dry-run — returns `DRY_RUN`
  - Test unmask: masked service — command issued, returns `SUCCESS`
  - Test unmask: not masked — returns `SKIPPED`
  - Test unmask: dry-run — returns `DRY_RUN`
  - _Requirements: 7, 8_

- [ ] 24. Write tests for `daemon_reload()`
  - Test: command issued, returns `SUCCESS`
  - Test: command fails — returns `FAILED`
  - Test: dry-run — returns `DRY_RUN`, no command issued
  - Test: elevated mode — runner called with `elevated=True`
  - _Requirements: 10_

- [ ] 25. Write tests for `get_journal()`
  - Test: default line count — command built with `-n 50`, stdout returned as string
  - Test: custom line count — command built with `-n <custom>`, stdout returned
  - Test: command fails — raises `ServiceQueryError`
  - Test: elevated NOT used — runner called without elevation flag
  - _Requirements: 11_
