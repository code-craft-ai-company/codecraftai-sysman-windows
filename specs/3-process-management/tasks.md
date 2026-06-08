# Implementation Plan — Process Management (Windows)

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

This feature mixes read and write verbs.  Writes are dry-run-aware and
idempotent where the verb's nature permits; reads never elevate.

## Module and Models

- [x] 1. Create the
  `ccai.sysman.windows.processmanagement` module.

- [x] 2. Define `WindowsProcessActionStatus` (alias of the package's
  shared `ActionStatus`: `SUCCESS / FAILED / SKIPPED / DRY_RUN`).

- [x] 3. Define `WindowsProcessPriorityClass` enum (`Idle /
  BelowNormal / Normal / AboveNormal / High / RealTime`).

- [x] 4. Define `WindowsProcessManagerConfig` inheriting from
  `FeatureConfig`, adding
  `powershell_executable: str = "powershell.exe"`,
  `priority_map: dict | None = None`, and
  `wait_poll_interval_seconds: float = 0.2`.

- [x] 5. Define `WindowsProcessQueryError` with
  `command` / `exit_code` / `stderr` and a multi-line message built
  via `os.linesep.join`.

- [x] 6. Define data dataclasses: `WindowsProcessInfo`,
  `WindowsProcessSpec`, `WindowsProcessActionResult`,
  `WindowsProcessWaitResult`.

## Manager — Implemented

- [x] 7. Implement `__init__` (inherited; no overrides needed).

- [x] 8. Implement `list_processes(owner=None)` via
  `Get-Process | Select-Object … | ConvertTo-Json …`.  When
  `owner` is supplied, prepend
  `Get-Process -IncludeUserName | Where-Object { $_.UserName -eq
  '<u>' }`.

- [x] 9. Implement `get_process(pid)` via
  `Get-Process -Id <pid> | Select-Object … | ConvertTo-Json …`.
  Treat the canonical "Cannot find a process" stderr (or empty
  stderr) as the unknown-PID path returning `None`.

- [x] 10. Implement `find_processes_by_name(name)` via
  `Get-Process -Name '<name>' -ErrorAction SilentlyContinue | …`.
  Treat the same "Cannot find" stderr (or empty stderr) as the
  no-match path returning `[]`.

- [x] 11. Implement `kill_process(pid, signal=None)` with
  signal-mapping check (`TERM` / `KILL` → `Stop-Process -Force`;
  others → FAILED), idempotency guard via `get_process`, dry-run
  short-circuit, and default-signal fallback.

- [x] 12. Implement `kill_process_tree(pid, signal=None)` —
  ``taskkill /F /T /PID <pid>``.  Same signal-mapping /
  idempotency / dry-run rules.

- [x] 13. Implement `start_process(spec)` via
  ``Start-Process -FilePath '<exe>' [-ArgumentList @(...)]
  [-WorkingDirectory '<wd>'] -PassThru | Select-Object Id |
  ConvertTo-Json …`` and decode the `Id` from the response to
  populate the result `pid`.

- [x] 14. Implement `set_process_priority(pid, priority)` —
  integer priority is mapped to a `WindowsProcessPriorityClass`
  via the configured `priority_map` (defaults documented in
  `design.md`); idempotency guard via
  `get_process(pid).state == target_class.value`; setter is
  ``(Get-Process -Id <pid>).PriorityClass = '<class>'``.

- [x] 15. Implement `wait_for_process(pid, timeout_seconds=None)`
  via ``Wait-Process -Id <pid> [-Timeout <s>]``.  Map
  `TimeoutException` stderr to `timed_out=True`; never raise on
  `Wait-Process` failure.

## Module-Level Parsers — Implemented

- [x] 16. `_parse_get_process_records` — JSON decode + one-or-many
  coercion + per-record `_build_process_info`.

- [x] 17. `_build_process_info` — Get-Process JSON record →
  `WindowsProcessInfo` mapping.

- [x] 18. `_coerce_priority_class_string` — accept either string
  (PowerShell 7+) or integer (PowerShell 5.1) `PriorityClass`
  serialisation and return the canonical six-letter label.

- [x] 19. `_parse_start_process_pid` — decode the `{"Id": <n>}`
  payload returned by ``Start-Process -PassThru | Select-Object
  Id | ConvertTo-Json``.

- [x] 20. `_priority_int_to_class` — integer-to-class mapping
  honouring `config.priority_map`; "closest key by absolute
  distance" fallback for off-table integers.

- [x] 21. `_escape_single_quote` — PowerShell-quote helper
  (single-quote doubled to escape a literal `'`).

- [x] 22. `_safe_int_from_any` / `_safe_float_from_any` token
  coercion helpers.

## Tests — Implemented

- [x] 23. `test_list_processes_invokes_get_process_and_parses`
- [x] 24. `test_get_process_parses_single_record`
- [x] 25. `test_get_process_returns_none_for_unknown_pid`
- [x] 26. `test_kill_process_invokes_stop_process`
- [x] 27. `test_kill_process_skipped_when_pid_not_found`
- [x] 28. `test_kill_process_dry_run_short_circuits`
- [x] 29. `test_kill_process_unmapped_signal_returns_failed`
- [x] 30. `test_kill_process_tree_uses_taskkill_with_t_flag`
- [x] 31. `test_start_process_invokes_start_process_with_passthru`
- [x] 32. `test_start_process_dry_run_short_circuits`
- [x] 33. `test_set_process_priority_invokes_priorityclass_setter`
- [x] 34. `test_set_process_priority_skipped_when_already_matches`
- [x] 35. `test_find_processes_by_name_parses_get_process_output`
- [x] 36. `test_failed_command_returns_failed_or_raises_on_read`
- [x] 37. `test_reads_never_elevate`

## Gaps (specified but not implemented)

- [ ] 38. **Graceful vs. forced termination split.**  This pilot
  collapses `TERM` and `KILL` to the same `Stop-Process -Force`
  invocation.  A future iteration could send a graceful
  `Stop-Process` (no `-Force`) for `TERM` and reserve `-Force`
  for `KILL`, more closely matching the POSIX semantics.

- [ ] 39. **`UserName` on `Get-Process`.**  The default
  `Get-Process` cmdlet does NOT include `UserName` unless the
  caller passes `-IncludeUserName`.  This pilot only adds the
  flag when an `owner` filter is supplied; the unfiltered listing
  therefore leaves `user=None` on every record.  Surfacing
  `-IncludeUserName` unconditionally would require Administrator
  privileges, which the contract forbids on reads.  Documenting
  the trade-off as a follow-up.

- [ ] 40. **`StartTime` parsing.**  `ConvertTo-Json` serialises
  `StartTime` as the proprietary `/Date(<unix-ms>)/` token.  The
  pilot keeps the raw token on `elapsed`; a future pass should
  decode it to a UTC `datetime` and compute a wall-clock elapsed
  string.

- [ ] 41. **`spec.user` privilege drop.**  `WindowsProcessSpec.user`
  is recorded but does not currently switch the spawn user.  A
  future pass should issue
  `Start-Process -Credential (Get-Credential) -ArgumentList ...`
  when `spec.user` is non-`None`.

- [ ] 42. **`Wait-Process` Python-side polling fallback.**  The
  pilot delegates wait-with-timeout to PowerShell directly.  A
  future pass could implement a
  `_polling_wait` helper that issues `Get-Process -Id <pid>`
  probes at `wait_poll_interval_seconds` cadence so the runner's
  observability is uniform with the Linux / macOS pilots.

- [ ] 43. **`ProcessFilter` parameter set.**  The cross-OS
  contract defines a `ProcessFilter` dataclass with `owner`,
  `name_pattern`, `min_cpu_percent`, and `min_memory_bytes`.
  This pilot exposes `owner` only; the rest are filtered in
  pure Python by the caller.

## Tests — Gaps

- [ ] 44. Per-signal command-construction tests for `KILL` /
  `TERM` paths and `FAILED` paths for `HUP` / `INT` / `QUIT` /
  `USR1` / `USR2` (only `HUP` is exercised today).

- [ ] 45. `wait_for_process` tests covering
  `timed_out=True` and the unknown-PID short-circuit.

- [ ] 46. `set_process_priority` tests for the
  `priority_map=...` override path and for the "closest key"
  fallback (e.g., `priority=15` rounds to BelowNormal).

## Spec-vs-Contract Drift

- [ ] 47. Spec-vs-contract drift discovered while authoring this
  spec:

  - The contract names the data dataclass `ProcessInfo` with
    `name` + `command_line` fields; this implementation uses
    `command` + `argv` matching the Linux / macOS pilots.
  - The contract's `ProcessSignal` is an enum; this pilot
    accepts plain `str` signal names and maps them via
    `_SIGNAL_TO_ACTION`.
  - The contract's `ProcessStartResult` is a separate dataclass;
    this pilot collapses it into `WindowsProcessActionResult`
    with `pid` populated on success — same approach as Ubuntu /
    macOS.
  - The contract's `ProcessWaitResult` carries `elapsed_seconds`;
    this pilot's `WindowsProcessWaitResult` does not surface
    `elapsed_seconds` because PowerShell's `Wait-Process` does
    not return that value.
  - Windows priority is class-valued, but the contract's
    `set_process_priority(pid, priority)` takes an integer.  The
    `priority_map` field on the config is the chosen mechanism
    for letting callers override the default integer-to-class
    mapping; the contract should document that mechanism.

## Follow-ups

- [ ] 48. **Cross-OS contract conformance review**: align field
  names per the drift list so a future generic
  `ProcessManager` facade can wrap `WindowsProcessManager`,
  `MacosProcessManager`, and `UbuntuProcessManager` uniformly.

- [ ] 49. **codecraftai-* reuse check**: confirm that
  `service-management` and `scheduled-tasks` do not duplicate
  the `Get-Process` / `Stop-Process` argv shapes; both should
  delegate to `WindowsProcessManager` for "is this process
  running?" probes.
