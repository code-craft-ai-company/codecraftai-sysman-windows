# Requirements

## Introduction

The command-execution feature provides the `codecraftai-sysman-windows` package with the
ability to dispatch OS-level commands, ordered sequences, and short inline PowerShell
script fragments through an `ICommandRunner` abstraction.  This spec implements the
command-execution contract defined at
`codecraftai-sysman-common/specs/contracts/command-execution/contract.md` for Windows
using `subprocess.run` (wrapped by `WindowsLocalCommandRunner`) and `paramiko` SSH
(wrapped by `WindowsSSHCommandRunner` in `codecraftai-protocol-ssh`, using an OpenSSH
Server target).  It covers single argv-based command execution, PowerShell-backed
script fragments via `powershell.exe -NoProfile -Command <...>`, and ordered command
sequences with a stop-on-failure policy.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interface.  The feature does not decide *what* to execute — it
provides the capability to *do it* reliably, dry-run-aware, auditable, and typed.  All
system interaction happens through `ICommandRunner`; no direct `subprocess` call lives
in the executor module itself except for the temp-file-backed script path which uses
`tempfile.NamedTemporaryFile` (with a `.ps1` suffix) and still routes execution through
the runner.

Windows elevation is **not scriptable at subprocess level**: Windows `runas` is
interactive-only and cannot acquire an Administrator token non-interactively.  Because
of that, when the caller passes `elevated=True`, the executor inspects the effective
process token via `WindowsPrincipal.IsInRole(Administrator)` (exposed through a tiny
helper that wraps `ctypes`).  If the caller's token is NOT already elevated the
executor returns a `WindowsCommandResult` with `success=False` and a typed
`elevation-not-available` stderr; the runner is never invoked.  This makes the
`elevated=True` path an *assertion* that the host process was launched with the
Administrator token (e.g., via Scheduled Task with "Run with highest privileges" or a
UAC-elevated parent process).

---

## Requirements

### Requirement 1: Execute a Single Command

**User Story:** As a maintenance coordinator, I want to run a single argv-based command
through the executor, so that I get a typed `WindowsCommandResult` regardless of the
underlying runner (local subprocess or SSH).

#### Acceptance Criteria

1. WHEN the coordinator calls `run(args, elevated, working_dir, env, timeout_seconds)`
   THE SYSTEM SHALL invoke `ICommandRunner.run` once with the same `args` list, the
   supplied `elevated` value, a resolved cwd, a merged env, and the supplied timeout.
2. WHEN the runner returns `exit_code == 0` and `timed_out == False` THE SYSTEM SHALL
   return a `WindowsCommandResult` with `success=True`, `exit_code=0`, populated
   `stdout` and `stderr`, `timed_out=False`, `dry_run=False`, and populated
   `started_at`, `finished_at`, and `duration_ms`.
3. WHEN the runner returns a non-zero exit code THE SYSTEM SHALL return a
   `WindowsCommandResult` with `success=False`, the observed `exit_code`, the captured
   `stdout` and `stderr`, and `timed_out=False`.
4. WHEN the runner reports `timed_out=True` THE SYSTEM SHALL return a
   `WindowsCommandResult` with `success=False` and `timed_out=True`, injecting a
   standardised timeout message into `stderr` if the runner did not populate one.
5. WHEN `args` is an empty list THE SYSTEM SHALL return a `WindowsCommandResult` with
   `success=False` and a descriptive `stderr` without invoking the runner.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke the runner and SHALL return a
   `WindowsCommandResult` with `success=True`, `dry_run=True`, `exit_code=0`, empty
   `stdout`/`stderr`, and preserving the supplied `args` and `elevated`.

---

### Requirement 2: Execute an Ordered Sequence of Commands

**User Story:** As a maintenance coordinator, I want to run a list of
`WindowsCommandSpec` objects as an ordered sequence with a stop-on-failure policy, so
that I can compose multi-step maintenance workflows atomically.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_sequence(commands, stop_on_failure=True)` and every
   command returns `success=True` THE SYSTEM SHALL return a `WindowsSequenceResult`
   with `all_succeeded=True`, `failed_at=None`, and one `WindowsCommandResult` per spec
   in the original order.
2. WHEN `stop_on_failure=True` and the N-th command returns `success=False` THE SYSTEM
   SHALL NOT invoke the runner for any subsequent command and SHALL set `failed_at=N`
   and `all_succeeded=False`.
3. WHEN `stop_on_failure=False` THE SYSTEM SHALL run every command in the sequence and
   SHALL set `failed_at` to the index of the first failing command or `None` when all
   succeeded.
4. WHEN `commands` is an empty list THE SYSTEM SHALL return a `WindowsSequenceResult`
   with `all_succeeded=True`, `failed_at=None`, and an empty `results` list without
   invoking the runner.
5. WHEN dry-run mode is active THE SYSTEM SHALL build one synthetic dry-run
   `WindowsCommandResult` per spec and SHALL NOT invoke the runner for any spec.
6. WHEN a spec carries per-command `elevated`, `working_dir`, `env`, or
   `timeout_seconds` THE SYSTEM SHALL forward those values to the corresponding `run`
   invocation.

---

### Requirement 3: Execute a PowerShell Script

**User Story:** As a maintenance coordinator, I want to run a short inline PowerShell
script fragment through the executor, so that I can express multi-command logic without
authoring a separate `.ps1` file.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_script(script, elevated, working_dir, env,
   timeout_seconds)` THE SYSTEM SHALL write the script text to a temporary file under
   the system temp directory with `suffix=".ps1"` and invoke the runner with
   `args=["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
   "<tmpfile>"]`, forwarding the remaining arguments.
2. WHEN the runner returns THE SYSTEM SHALL delete the temporary file in a `finally`
   block regardless of success, failure, or exception.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT create any file and SHALL return a
   `WindowsCommandResult` with `success=True`, `dry_run=True`, and
   `args=["powershell.exe", "-NoProfile", "-File", "<dry-run>"]`.
4. WHEN the `script` argument is an empty string THE SYSTEM SHALL return a
   `WindowsCommandResult` with `success=False` and a descriptive `stderr` without
   creating a temp file.
5. WHEN the filesystem denies temp-file creation THE SYSTEM SHALL return a
   `WindowsCommandResult` with `success=False` carrying the OSError text in `stderr`
   and SHALL NOT invoke the runner.

---

### Requirement 4: Working Directory Precondition

**User Story:** As a maintenance coordinator, I want the executor to reject invocations
targeting a non-existent `working_dir` up front, so that I get a clear error without
waiting for the runner to fail opaquely.

#### Acceptance Criteria

1. WHEN `working_dir` is `None` THE SYSTEM SHALL forward `cwd=None` to the runner and
   the runner SHALL use its own default.
2. WHEN `working_dir` is supplied and `pathlib.Path(working_dir).is_dir()` is `True` THE
   SYSTEM SHALL forward the absolute path via `cwd` to the runner.
3. WHEN `working_dir` is supplied and the path does not exist or is not a directory THE
   SYSTEM SHALL return a `WindowsCommandResult` with `success=False`,
   `exit_code=None`, and a `stderr` of the form `working_dir does not exist: <path>`
   WITHOUT invoking the runner.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT evaluate the `working_dir`
   precondition and SHALL return a dry-run `WindowsCommandResult`.

---

### Requirement 5: Environment Merging

**User Story:** As a maintenance coordinator, I want caller-supplied environment
overrides layered on top of the process environment, so that I can inject per-command
variables (e.g., `POWERSHELL_TELEMETRY_OPTOUT=1`, `PATHEXT=.EXE;.CMD;.BAT`) without
leaking them into the parent process.

#### Acceptance Criteria

1. WHEN `env` is `None` THE SYSTEM SHALL pass `env=None` to the runner so the runner
   uses its own default inheritance rules.
2. WHEN `env` is supplied THE SYSTEM SHALL compute the merged env as
   `{**os.environ, **env}` and SHALL pass that merged dict to the runner, where caller
   keys override process-environment keys on conflict.
3. WHEN `env` is an empty dict THE SYSTEM SHALL still pass `{**os.environ}` (i.e., a
   fresh copy) to the runner so that downstream aspects do not mutate the caller's
   dict.
4. WHEN the runner is `WindowsLocalCommandRunner` THE SYSTEM SHALL rely on the runner
   to resolve executable extensions (`.exe`, `.cmd`, `.bat`) via the merged `PATH` and
   `PATHEXT`.

---

### Requirement 6: Timeout Propagation

**User Story:** As a maintenance coordinator, I want `timeout_seconds` to propagate to
the runner and surface as a typed field on the result, so that I can detect timeouts
without pattern-matching stderr.

#### Acceptance Criteria

1. WHEN `timeout_seconds` is supplied THE SYSTEM SHALL forward it to the runner's
   `timeout` argument.
2. WHEN `timeout_seconds` is `None` and `config.default_timeout_seconds` is set THE
   SYSTEM SHALL forward the config default.
3. WHEN both are `None` THE SYSTEM SHALL forward `timeout=None`, giving the runner
   latitude to enforce its own default.
4. WHEN the runner returns `timed_out=True` THE SYSTEM SHALL set
   `WindowsCommandResult.timed_out=True` and `success=False`, and if `stderr` is
   empty, SHALL inject `Command timed out after <N> seconds`.

---

### Requirement 7: Exit-Code to Success Mapping

**User Story:** As a maintenance coordinator, I want a uniform boolean `success` field
that follows the `exit_code == 0` convention, so that I do not have to re-derive it in
the caller.

#### Acceptance Criteria

1. WHEN the runner returns `exit_code=0` and `timed_out=False` THE SYSTEM SHALL set
   `success=True`.
2. WHEN the runner returns any non-zero `exit_code` THE SYSTEM SHALL set
   `success=False`.
3. WHEN the runner returns `timed_out=True` THE SYSTEM SHALL set `success=False`
   regardless of `exit_code`.
4. WHEN the result is synthetic `DRY_RUN` THE SYSTEM SHALL set `success=True` and
   `exit_code=0`.

---

### Requirement 8: Elevation Policy and Pre-Check

**User Story:** As a maintenance coordinator, I want clear, typed behaviour when I ask
for elevated execution on Windows, so that I do not silently run with reduced
privileges.

#### Acceptance Criteria

1. WHEN the coordinator passes `elevated=True` THE SYSTEM SHALL call the executor's
   `_is_process_elevated()` helper before invoking the runner.  The helper SHALL
   evaluate the current token using
   `WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator)`
   (via `ctypes` wrappers) and return `True` when the host process holds the
   Administrator token.
2. WHEN `elevated=True` AND the host process IS elevated THE SYSTEM SHALL invoke the
   runner with `elevated=True`.
3. WHEN `elevated=True` AND the host process IS NOT elevated THE SYSTEM SHALL return a
   `WindowsCommandResult` with `success=False`, `exit_code=None`, and `stderr`
   containing `elevation requested but host process is not Administrator` WITHOUT
   invoking the runner.
4. WHEN the coordinator passes `elevated=False` THE SYSTEM SHALL invoke the runner
   with `elevated=False` regardless of the host process elevation state.
5. WHEN dry-run mode is active THE SYSTEM SHALL reflect the requested `elevated` value
   verbatim on the returned `WindowsCommandResult.elevated` WITHOUT consulting the
   elevation pre-check.
6. WHEN the runner is `WindowsSSHCommandRunner` THE SYSTEM SHALL skip the local
   elevation pre-check and delegate the elevation decision to the remote OpenSSH
   session's token (the SSH server enforces its own Administrator context).

---

### Requirement 9: Dry-Run Safety for All Verbs

**User Story:** As a maintenance coordinator, I want all three verbs to short-circuit in
dry-run mode, so that I can preview an entire plan without touching the system.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` and `run` is called THE SYSTEM SHALL NOT invoke the
   runner and SHALL return `WindowsCommandResult(success=True, dry_run=True,
   exit_code=0, args=args, elevated=elevated)`.
2. WHEN `config.dry_run=True` and `run_sequence` is called THE SYSTEM SHALL build one
   synthetic dry-run `WindowsCommandResult` per spec and return a
   `WindowsSequenceResult(all_succeeded=True, dry_run=True, results=[...])`.
3. WHEN `config.dry_run=True` and `run_script` is called THE SYSTEM SHALL NOT create a
   temp file and SHALL return a synthetic `WindowsCommandResult` whose
   `args=["powershell.exe", "-NoProfile", "-File", "<dry-run>"]`.
4. WHEN a `WindowsCommandResult` is synthetic the `started_at`, `finished_at`, and
   `duration_ms` fields SHALL be `None`.

---

### Requirement 10: Composition with Aspects

**User Story:** As a maintenance coordinator, I want to attach persistence, retry, and
timeout aspects to the runner without touching the executor, so that cross-cutting
concerns compose transparently.

#### Acceptance Criteria

1. WHEN aspects are attached to the runner THE SYSTEM SHALL produce
   `WindowsCommandResult` values whose `success`, `exit_code`, `args`, `elevated`, and
   `dry_run` fields match those produced without aspects (modulo retry-induced flips
   and timing fields).
2. WHEN a retry aspect flips a failure to a success THE SYSTEM SHALL report
   `success=True` without mutating `args`, `elevated`, or `dry_run`.
3. WHEN a persistence aspect observes the run THE SYSTEM SHALL NOT mutate the returned
   `WindowsCommandResult`.
4. WHEN a timeout aspect wraps the runner THE SYSTEM SHALL still surface
   `timed_out=True` on the returned `WindowsCommandResult` when the aspect forces a
   timeout.
