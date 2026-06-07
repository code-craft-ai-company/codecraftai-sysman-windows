# Implementation Plan — Windows Command Execution

Checked items (`[x]`) are implemented and covered by tests.  Unchecked items (`[ ]`)
identify remaining work.

> **Note on layout:** The Layer 1 implementation that ships in this batch follows the
> Ubuntu / macOS *runner* pattern — `WindowsLocalCommandRunner` /
> `WindowsSshCommandRunner` plus an `aspects/` sub-package — rather than the standalone
> `WindowsCommandExecutor` originally drafted here.  The runner pattern is now locked
> in across all three OS packages and is what every Layer 3 feature manager consumes.
> The original `WindowsCommandExecutor` items (1-36 below) are kept for traceability
> but mapped onto the runner pattern in the *Runner-Pattern Implementation* section
> at the bottom.

## Module and Models

- [ ] 1. Create the `ccai.sysman.windows.commandexecution` module and the sibling
  `_elevation.py` helper module

- [ ] 2. Define `WindowsCommandExecutionStatus` enum (SUCCESS / FAILED / SKIPPED /
  DRY_RUN)

- [ ] 3. Define `WindowsCommandExecutorConfig` dataclass (dry_run,
  default_timeout_seconds, powershell_path, skip_local_elevation_check)

- [ ] 4. Define `WindowsCommandSpec` dataclass (args, elevated, working_dir, env,
  timeout_seconds, label)

- [ ] 5. Define `WindowsCommandResult` and `WindowsSequenceResult` dataclasses with
  timing fields and elevation echo

## WindowsCommandExecutor — Implemented

- [ ] 6. Implement `__init__` accepting an `ICommandRunner` and a
  `WindowsCommandExecutorConfig`; cache the elevation result on first call

- [ ] 7. Implement `_is_process_elevated()` helper using
  `ctypes.windll.shell32.IsUserAnAdmin`

- [ ] 8. Implement `run(args, *, elevated, working_dir, env, timeout_seconds)` —
  dry-run short-circuit, empty-args rejection, elevation pre-check when
  `elevated=True` and `skip_local_elevation_check=False`, working-dir check, env
  merge, runner dispatch, timing, result mapping

- [ ] 9. Implement env merge helper producing `{**os.environ, **env}` (copying even
  when `env == {}`)

- [ ] 10. Implement timeout fallback (per-call → config default → `None`) and
  `stderr` injection when runner returns `timed_out=True` with empty `stderr`

- [ ] 11. Implement `run_sequence(commands, *, stop_on_failure)` — empty-list
  short-circuit, dry-run per-spec synthesis, stop-on-failure break, `failed_at`
  computation

- [ ] 12. Implement `run_script(script, *, elevated, working_dir, env,
  timeout_seconds)` — empty-script guard,
  `tempfile.NamedTemporaryFile(suffix=".ps1", encoding="utf-8-sig")`, `try/finally`
  cleanup via `Path.unlink(missing_ok=True)`, PowerShell argv composition

- [ ] 13. Implement OSError guard around tempfile creation, returning
  `success=False` without invoking the runner

## WindowsCommandExecutor — Gaps (specified but not implemented)

- [ ] 14. PowerShell 7+ (`pwsh.exe`) negotiation: detect pwsh presence and fall back
  to `powershell.exe` when absent

- [ ] 15. Streaming `Write-Progress` / PowerShell stream routing — the base contract
  does not require streaming; defer

- [ ] 16. Typed classification of `Operation requires elevation` (0x80070005) vs
  generic access-denied stderr patterns

- [ ] 17. JEA (Just Enough Administration) endpoint support for constrained
  remote-only elevation — future enhancement

## Tests — Implemented

- [ ] 18. `test_run_success_maps_zero_exit_to_success_true`
- [ ] 19. `test_run_failure_maps_nonzero_to_success_false`
- [ ] 20. `test_run_timeout_flag_surfaces_with_synthetic_stderr`
- [ ] 21. `test_run_empty_args_returns_failure_without_runner_call`
- [ ] 22. `test_run_dry_run_does_not_invoke_runner`
- [ ] 23. `test_elevation_prechecks_fails_when_not_admin`
- [ ] 24. `test_elevation_prechecks_passes_when_admin`
- [ ] 25. `test_skip_local_elevation_check_forwards_true_without_probe`
- [ ] 26. `test_working_dir_missing_returns_failure_without_runner_call`
- [ ] 27. `test_env_merge_caller_wins_on_conflict`
- [ ] 28. `test_run_sequence_stop_on_failure_stops_at_first_failure`
- [ ] 29. `test_run_script_creates_and_deletes_ps1_tempfile`
- [ ] 30. `test_run_script_dry_run_does_not_create_tempfile`
- [ ] 31. `test_run_script_powershell_argv_has_noprofile_bypass_file`
- [ ] 32. `test_run_script_empty_script_returns_failure_without_runner_call`

## Tests — Gaps

- [ ] 33. Test that `config.default_timeout_seconds` fallback applies when per-call
  `timeout_seconds` is `None`

- [ ] 34. Test that aspect composition (Retry flipping fail→success) preserves
  `args`, `elevated`, and `dry_run` on the result

- [ ] 35. Test that `WindowsCommandResult.started_at`, `finished_at`, and
  `duration_ms` are `None` in dry-run results and populated otherwise

- [ ] 36. Test that dry-run bypasses the elevation pre-check even when
  `elevated=True` and the host process is NOT elevated

## Follow-ups

- [ ] 37. **Cross-OS contract conformance review**: verify that
  `WindowsCommandResult` / `WindowsSequenceResult` / `WindowsCommandSpec` field shapes
  match the cross-OS contract at
  `codecraftai-sysman-common/specs/contracts/command-execution/contract.md` exactly,
  and that the elevation pre-check extension is documented in the contract's Windows
  row.

- [ ] 38. **Elevation-helper review**: decide whether
  `ctypes.windll.shell32.IsUserAnAdmin` or a `WindowsPrincipal.IsInRole` equivalent is
  the more robust probe under UAC token linking scenarios, and validate behaviour
  under linked-token (split-token) local admin accounts.

## Runner-Pattern Implementation (this batch)

Source files under `source/packages/ccai/sysman/windows/runners/`:

- [x] R1. `models.py` — `WindowsRunResult`, `WindowsRunRecord`,
  `WindowsExecutionContext`, `WindowsSshErrorType`,
  `WindowsSshReliabilityMetrics`.  `WindowsRunResult` carries an
  `elevation_blocked: bool` field in addition to the cross-OS shape.
- [x] R2. `local.py` — `WindowsLocalCommandRunner` /
  `WindowsLocalRunnerConfig` using `subprocess.run`.  Performs an
  `IsUserAnAdmin` pre-check at construction time, caches the result on
  `self._is_elevated`, and refuses `elevated=True` calls when the host is not
  elevated by returning a typed FAILED `WindowsRunResult` (no raise).
  Forwards `creationflags=CREATE_NO_WINDOW` on Windows hosts when
  `suppress_console_window=True`.  No `sudo` prefix; argv passes through
  unchanged.  `skip_elevation_check` config flag bypasses the probe for
  constrained-runtime hosts.
- [x] R3. `ssh.py` — `WindowsSshCommandRunner` skeleton.  Construction
  succeeds; `run()` raises `NotImplementedError` on non-dry-run paths (no SSH
  library is in `pyproject.toml`).  The local-runner elevation pre-check is
  intentionally NOT mirrored on the SSH runner; remote elevation is the
  remote SSH server's responsibility.
- [x] R4. `aspects/base.py` — `IWindowsExecutionAspect` Protocol.
- [x] R5. `aspects/persistence.py` — `WindowsPersistenceAspect` /
  `WindowsPersistenceAspectConfig`.  Records `elevation_blocked` alongside
  the standard fields.
- [x] R6. `aspects/retry.py` — `WindowsRetryAspect` / `WindowsRetryAspectConfig`
  / `WindowsRetryDecision`.  Refuses to retry an `elevation_blocked` result
  (re-running without first relaunching elevated would block again).
- [x] R7. `aspects/timeout.py` — `WindowsTimeoutAspect` /
  `WindowsTimeoutAspectConfig`.
- [x] R8. `runners/__init__.py` and `aspects/__init__.py` package re-exports.

Tests under
`source/testroots/testbase/ccai/tests/sysman/windows/`:

- [x] R9. `test_localcommandrunner.py` (20 tests) — port of Ubuntu/macOS plus
  Windows-specific `test_elevation_check_at_construction`,
  `test_run_with_elevated_returns_failed_when_not_elevated`,
  `test_run_with_elevated_succeeds_when_elevated`,
  `test_run_with_elevated_skips_check_when_skip_flag_set`,
  `test_creation_flags_include_no_window_when_configured`,
  `test_creation_flags_zero_when_suppress_disabled`,
  `test_elevation_block_count_increments_on_block`,
  `test_run_does_not_prepend_sudo_or_anything_when_elevated`.
- [x] R10. `test_aspects.py` (7 tests) — port of Ubuntu/macOS plus
  `test_retry_aspect_does_not_retry_on_elevation_block`.
