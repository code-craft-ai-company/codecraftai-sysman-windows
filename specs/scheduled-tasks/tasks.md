# Implementation Plan

- [ ] 1. Create the `scheduledtasks` module file
  - Create `source/packages/automojo/sysman/linux/windows/scheduledtasks.py`
  - Add module-level docstring: manage cron drop-in files and systemd timer unit pairs
    on Windows Linux systems via the file-operator and command-runner abstractions
  - _Requirements: 1–10_

- [ ] 2. Define `TaskType` enum
  - Values: `CRON = "cron"`, `SYSTEMD = "systemd"`
  - _Requirements: 3, 8, 9_

- [ ] 3. Define `CronJobEntry` dataclass
  - Fields: `name`, `schedule`, `command`, `user`, `comment` (optional)
  - _Requirements: 1_

- [ ] 4. Define `SystemdTimerEntry` dataclass
  - Fields: `name`, `description`, `on_calendar`, `exec_start`, `enabled`,
    `after` (optional), `wants_by` (optional, defaults to `"timers.target"` in template)
  - _Requirements: 5_

- [ ] 5. Define `ScheduledTaskInfo` dataclass
  - Fields: `name`, `task_type`, `schedule`, `command`, `user`, `enabled`, `active`,
    `readable` (default `True`)
  - _Requirements: 3, 8, 9_

- [ ] 6. Define `ScheduledTaskResult` dataclass
  - Fields: `success`, `name`, `dry_run` (default `False`), `error` (optional)
  - _Requirements: 1, 2, 5, 6, 7_

- [ ] 7. Define `ScheduledTaskQueryError` exception class
  - Subclass `Exception`; used when query commands fail for non-absence reasons
  - _Requirements: 8, 9_

- [ ] 8. Define `ScheduledTaskConfig` dataclass
  - Fields: `dry_run: bool = False`, `cron_dir: str = "/etc/cron.d"`,
    `unit_dir: str = "/etc/systemd/system"`
  - _Requirements: 1–10_

- [ ] 9. Implement `ScheduledTaskManager.__init__`
  - Accept `file_operator: IFileOperator`, `command_runner: ICommandRunner`,
    and `config: ScheduledTaskConfig`
  - Store all three as instance attributes
  - _Requirements: 1–10_

- [ ] 10. Implement private `_render_cron_file(entry: CronJobEntry) → str`
  - Emit optional comment line, managed-by line, and schedule/user/command line
  - _Requirements: 1_

- [ ] 11. Implement `add_cron_job(entry: CronJobEntry) → ScheduledTaskResult`
  - Early return dry-run result when `config.dry_run=True`
  - Render file content with `_render_cron_file`
  - Call `file_operator.write_text(path, content, elevated=True)`
  - Return success or failure `ScheduledTaskResult`
  - _Requirements: 1, 10_

- [ ] 12. Implement `remove_cron_job(name: str) → ScheduledTaskResult`
  - Early return dry-run result when `config.dry_run=True`
  - Check `file_operator.exists(path, elevated=True)`; return not-found failure if absent
  - Call `file_operator.delete(path, elevated=True)`
  - Return success or failure `ScheduledTaskResult`
  - _Requirements: 2, 10_

- [ ] 13. Implement `list_cron_jobs() → list[ScheduledTaskInfo]`
  - Call `file_operator.list_dir(config.cron_dir)`; iterate filenames
  - For each file: read content, skip `#` and blank lines, parse first data line as
    `schedule user command` with `maxsplit=2`
  - Return `ScheduledTaskInfo(readable=False)` for files that cannot be parsed
  - _Requirements: 3_

- [ ] 14. Implement `validate_cron_expression(expression: str) → bool`
  - Handle `@` shorthands: `@reboot`, `@daily`, `@weekly`, `@monthly`, `@yearly`,
    `@hourly`
  - Split remaining expression into five fields; return `False` if count differs
  - Validate each field against its range using `_validate_cron_field`
  - No external calls; pure Python
  - _Requirements: 4_

- [ ] 15. Implement private `_validate_cron_field(field: str, min_val: int, max_val: int) → bool`
  - Accept `"*"`, `"*/n"`, `"a-b"`, `"a,b,c"`, or bare integer string
  - Return `False` for values outside `[min_val, max_val]` or invalid syntax
  - _Requirements: 4_

- [ ] 16. Implement private `_render_service_unit(entry: SystemdTimerEntry) → str`
  - Produce `[Unit]`, `[Service]` sections; include `After=` only when `entry.after` set
  - _Requirements: 5_

- [ ] 17. Implement private `_render_timer_unit(entry: SystemdTimerEntry) → str`
  - Produce `[Unit]`, `[Timer]`, `[Install]` sections
  - `WantedBy` defaults to `"timers.target"` when `entry.wants_by` is `None`
  - _Requirements: 5_

- [ ] 18. Implement `create_systemd_timer(entry: SystemdTimerEntry) → ScheduledTaskResult`
  - Early return dry-run result when `config.dry_run=True`
  - Write `.service` then `.timer` via `file_operator.write_text(..., elevated=True)`
  - Run `systemctl daemon-reload` with `elevated=True`
  - Run `systemctl enable --now <name>.timer` when `entry.enabled=True`
  - Return success or failure `ScheduledTaskResult`
  - _Requirements: 5, 10_

- [ ] 19. Implement `remove_systemd_timer(name: str) → ScheduledTaskResult`
  - Early return dry-run result when `config.dry_run=True`
  - Check existence of `.timer` or `.service` file; return not-found failure if both absent
  - Run `systemctl disable --now <name>.timer` (ignore non-zero exit)
  - Delete each unit file that exists via `file_operator.delete(..., elevated=True)`
  - Run `systemctl daemon-reload` with `elevated=True`
  - Return success or failure `ScheduledTaskResult`
  - _Requirements: 6, 10_

- [ ] 20. Implement `set_systemd_timer_enabled(name: str, enabled: bool) → ScheduledTaskResult`
  - Early return dry-run result when `config.dry_run=True`
  - Run `systemctl enable --now` or `systemctl disable --now` depending on `enabled`
  - Return success or failure `ScheduledTaskResult`
  - _Requirements: 7, 10_

- [ ] 21. Implement `list_systemd_timers() → list[ScheduledTaskInfo]`
  - Run `systemctl list-timers --all --no-legend --no-pager` with `elevated=True`
  - Raise `ScheduledTaskQueryError` on non-zero exit
  - Parse each non-empty output line: extract `UNIT` column, strip `.timer`, set
    `active=True` when `NEXT` column is not `n/a`
  - Return list of `ScheduledTaskInfo(task_type=SYSTEMD, ...)`
  - _Requirements: 8_

- [ ] 22. Implement `query_task(name: str, task_type: str) → ScheduledTaskInfo | None`
  - Dispatch on `task_type`: `"cron"` → `_query_cron_task`, `"systemd"` → `_query_systemd_task`
  - _Requirements: 9_

- [ ] 23. Implement private `_query_cron_task(name: str) → ScheduledTaskInfo | None`
  - Return `None` when `file_operator.exists` is `False`
  - Read and parse file; return `ScheduledTaskInfo` or unreadable variant
  - Raise `ScheduledTaskQueryError` on unexpected read failure
  - _Requirements: 9_

- [ ] 24. Implement private `_query_systemd_task(name: str) → ScheduledTaskInfo | None`
  - Run `systemctl show <name>.timer --no-pager` with `elevated=True`
  - Return `None` when stderr/stdout indicates unit not found
  - Parse key=value output for `ActiveState` and `UnitFileState`
  - Run `systemctl show <name>.service` to extract `ExecStart`
  - Raise `ScheduledTaskQueryError` on unexpected command failure
  - _Requirements: 9_

- [ ] 25. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_scheduledtasks.py`
  - Implement `MockFileOperator` with `write_text`, `delete`, `read_text`, `list_dir`,
    `exists` call recording
  - Implement `MockCommandRunner` with per-command-prefix configurable `(exit_code,
    stdout, stderr)` and call recording
  - Define helper `make_cron_entry(...)` and `make_timer_entry(...)` for concise tests
  - _Requirements: 1–10_

- [ ] 26. Write tests for `add_cron_job`
  - Test: file written to correct path with rendered schedule/user/command line
  - Test: comment line present when `entry.comment` set; absent when `None`
  - Test: managed-by comment always present
  - Test: success result returned on write success
  - Test: failure result returned when write raises
  - Test: dry-run — zero `write_text` calls, `dry_run=True` on result
  - _Requirements: 1, 10_

- [ ] 27. Write tests for `remove_cron_job`
  - Test: `delete` called with correct path and `elevated=True`
  - Test: not-found failure returned when `exists` returns `False`
  - Test: failure result returned when `delete` raises
  - Test: dry-run — zero `delete` calls, `dry_run=True` on result
  - _Requirements: 2, 10_

- [ ] 28. Write tests for `list_cron_jobs`
  - Test: empty `list_dir` → empty list
  - Test: one valid file → one `ScheduledTaskInfo` with correct fields
  - Test: multiple files → list in directory order
  - Test: unreadable file → `ScheduledTaskInfo(readable=False)`
  - _Requirements: 3_

- [ ] 29. Write tests for `validate_cron_expression`
  - Test: `"0 2 * * *"` → `True`
  - Test: `"*/15 * * * *"` → `True`
  - Test: `"0,30 8-17 * * 1-5"` → `True`
  - Test: `"@daily"` → `True`; `"@reboot"` → `True`
  - Test: `"60 * * * *"` → `False` (minute out of range)
  - Test: `"0 25 * * *"` → `False` (hour out of range)
  - Test: `"* * *"` → `False` (too few fields)
  - Test: `"@invalid"` → `False`
  - _Requirements: 4_

- [ ] 30. Write tests for `create_systemd_timer`
  - Test: `.service` and `.timer` files both written with correct content
  - Test: `daemon-reload` called after writes
  - Test: `enable --now` called when `entry.enabled=True`
  - Test: `enable` NOT called when `entry.enabled=False`
  - Test: failure result on service file write error
  - Test: failure result on `daemon-reload` command failure
  - Test: dry-run — zero writes, zero commands, `dry_run=True`
  - _Requirements: 5, 10_

- [ ] 31. Write tests for `remove_systemd_timer`
  - Test: `disable --now` called, both unit files deleted, `daemon-reload` called
  - Test: not-found failure when neither unit file exists
  - Test: deletes only existing files (partial install scenario)
  - Test: failure result on file delete error
  - Test: dry-run — zero deletes, zero commands, `dry_run=True`
  - _Requirements: 6, 10_

- [ ] 32. Write tests for `set_systemd_timer_enabled`
  - Test: `enabled=True` → `systemctl enable --now <name>.timer` called
  - Test: `enabled=False` → `systemctl disable --now <name>.timer` called
  - Test: failure result on command error
  - Test: dry-run — zero commands, `dry_run=True`
  - _Requirements: 7, 10_

- [ ] 33. Write tests for `list_systemd_timers`
  - Test: empty stdout → empty list
  - Test: one timer line → one `ScheduledTaskInfo` with correct name and `active` flag
  - Test: `NEXT` column `"n/a"` → `active=False`
  - Test: command failure → `ScheduledTaskQueryError` raised
  - _Requirements: 8_

- [ ] 34. Write tests for `query_task` (cron)
  - Test: file absent → `None` returned
  - Test: file present and parseable → `ScheduledTaskInfo` with correct fields
  - Test: file present but unparseable → `ScheduledTaskInfo(readable=False)`
  - _Requirements: 9_

- [ ] 35. Write tests for `query_task` (systemd)
  - Test: unit not found (systemctl indicates absence) → `None` returned
  - Test: unit found → `ScheduledTaskInfo` with `enabled` and `active` populated
  - Test: command failure (unexpected) → `ScheduledTaskQueryError` raised
  - _Requirements: 9_
