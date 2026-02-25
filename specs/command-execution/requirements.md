# Requirements

## Introduction

The command execution feature provides the `automojo-sysman-linux-windows` package with
the ability to run arbitrary shell commands on a Windows Linux system as part of a
maintenance workflow.  It covers one-shot command execution, ordered sequences of commands
with configurable failure handling, and interactive shell script execution via a temporary
script file.

A maintenance coordinator drives all operations by calling into this feature's public
interfaces.  The feature does not decide *which* commands to run — it provides the
primitives needed to run them reliably, capture their output, and surface pass/fail
outcomes in a structured, auditable form.  All system commands are executed exclusively
through the `automojo-interfaces` command-runner abstraction, enabling dry-run simulation,
auditing, and test mocking without requiring a live system.

---

## Requirements

### Requirement 1: Run a Single Command

**User Story:** As a maintenance coordinator, I want to execute a single command and
receive its exit code, stdout, and stderr, so that I can perform one-off administrative
tasks and inspect their results.

#### Acceptance Criteria

1. WHEN the coordinator provides a command and its arguments THE SYSTEM SHALL invoke the
   command via the command runner and return a `CommandResult` with exit code, stdout, and
   stderr.
2. WHEN the command exits with code 0 THE SYSTEM SHALL set `success=True` on the returned
   `CommandResult`.
3. WHEN the command exits with a non-zero code THE SYSTEM SHALL set `success=False` on the
   returned `CommandResult`.
4. WHEN `elevated=True` is requested THE SYSTEM SHALL pass `elevated=True` to the command
   runner.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `CommandResult` with `success=True`, empty stdout, empty stderr, exit code 0, and
   `dry_run=True`.

---

### Requirement 2: Run a Sequence of Commands

**User Story:** As a maintenance coordinator, I want to execute an ordered list of
commands and receive a per-command summary of results, so that I can apply multi-step
maintenance procedures in a single call.

#### Acceptance Criteria

1. WHEN the coordinator provides a non-empty list of command specifications THE SYSTEM
   SHALL execute each command in order using the command runner.
2. WHEN all commands exit with code 0 THE SYSTEM SHALL return a `SequenceResult` with
   `all_succeeded=True`.
3. WHEN any command exits with a non-zero code and `stop_on_failure=True` (default) THE
   SYSTEM SHALL stop executing the remaining commands and return a `SequenceResult` with
   `all_succeeded=False` and the index of the first failed command in `failed_at`.
4. WHEN any command exits with a non-zero code and `stop_on_failure=False` THE SYSTEM
   SHALL continue executing all remaining commands and return a `SequenceResult` with
   `all_succeeded=False` containing the full list of `CommandResult` entries.
5. WHEN the coordinator provides an empty list THE SYSTEM SHALL return a `SequenceResult`
   with `all_succeeded=True` and an empty results list.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command, SHALL return a
   `SequenceResult` with `all_succeeded=True`, a dry-run `CommandResult` for every item
   in the list, and `dry_run=True`.

---

### Requirement 3: Run a Shell Script

**User Story:** As a maintenance coordinator, I want to provide a multi-line shell script
as a string and have it executed as a Bash script, so that I can run complex sequences
that require shell features such as variable substitution, pipes, and conditionals.

#### Acceptance Criteria

1. WHEN the coordinator provides a shell script string THE SYSTEM SHALL write the script
   to a temporary file, mark it executable, and invoke it via the command runner using
   `bash <tmpfile>`.
2. WHEN the script exits with code 0 THE SYSTEM SHALL return a `CommandResult` with
   `success=True`, stdout, stderr, and exit code 0.
3. WHEN the script exits with a non-zero code THE SYSTEM SHALL return a `CommandResult`
   with `success=False`, stdout, stderr, and the actual exit code.
4. WHEN the script execution completes (success or failure) THE SYSTEM SHALL delete the
   temporary script file.
5. WHEN `elevated=True` is requested THE SYSTEM SHALL invoke `bash <tmpfile>` with
   `elevated=True` passed to the command runner.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `CommandResult` with `success=True`, empty stdout, empty stderr, exit code 0, and
   `dry_run=True`.

---

### Requirement 4: Capture Working Directory

**User Story:** As a maintenance coordinator, I want to specify a working directory for a
command or script, so that relative paths used inside the command or script resolve
correctly against an expected filesystem location.

#### Acceptance Criteria

1. WHEN the coordinator provides a `working_dir` path alongside a command THE SYSTEM SHALL
   pass the working directory to the command runner so the command executes from that
   path.
2. WHEN no `working_dir` is provided THE SYSTEM SHALL not specify a working directory and
   allow the command runner to use its default.
3. WHEN the specified `working_dir` does not exist THE SYSTEM SHALL return a
   `CommandResult` with `success=False` and an error message in `stderr` without invoking
   the command runner.

---

### Requirement 5: Capture Environment Variables

**User Story:** As a maintenance coordinator, I want to supply additional environment
variables for a command or script, so that the target process sees the environment
expected by the task being performed.

#### Acceptance Criteria

1. WHEN the coordinator provides an `env` mapping THE SYSTEM SHALL merge it with the
   process environment and pass the merged environment to the command runner.
2. WHEN the coordinator does not provide `env` THE SYSTEM SHALL pass no extra environment
   variables and allow the command runner to use its default environment.
3. WHEN a key in the `env` mapping conflicts with an existing environment variable THE
   SYSTEM SHALL use the value from the provided `env` mapping (caller wins).

---

### Requirement 6: Timeout Enforcement

**User Story:** As a maintenance coordinator, I want to specify a timeout for a command or
script, so that a hung process does not block the maintenance workflow indefinitely.

#### Acceptance Criteria

1. WHEN the coordinator provides a `timeout_seconds` value THE SYSTEM SHALL pass it to the
   command runner.
2. WHEN the command runner reports a timeout THE SYSTEM SHALL return a `CommandResult` with
   `success=False`, `timed_out=True`, exit code `None`, and a descriptive message in
   `stderr`.
3. WHEN no `timeout_seconds` is provided THE SYSTEM SHALL not pass a timeout to the
   command runner, deferring to the command runner's default behaviour.

---

### Requirement 7: Output Streaming Callback

**User Story:** As a maintenance coordinator, I want to register a callback that receives
output lines as a command runs, so that long-running commands can produce real-time
progress feedback without waiting for the command to finish.

#### Acceptance Criteria

1. WHEN the coordinator provides an `on_output` callable THE SYSTEM SHALL invoke it with
   each line of stdout as it is received.
2. WHEN the coordinator provides an `on_error` callable THE SYSTEM SHALL invoke it with
   each line of stderr as it is received.
3. WHEN the command completes THE SYSTEM SHALL still return the full stdout and stderr
   text as part of the `CommandResult` regardless of whether callbacks were provided.
4. WHEN no callbacks are provided THE SYSTEM SHALL capture output silently and return it
   in the `CommandResult`.

---

### Requirement 8: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want individual commands and scripts to
run with elevated privileges when the task requires it, so that privileged system
operations succeed without requiring the entire maintenance process to run as root.

#### Acceptance Criteria

1. WHEN the coordinator sets `elevated=True` on a command or script THE SYSTEM SHALL pass
   `elevated=True` to the command runner.
2. WHEN the coordinator does not set `elevated=True` THE SYSTEM SHALL pass `elevated=False`
   to the command runner.
3. WHEN running a sequence of commands the coordinator MAY set `elevated` individually per
   command specification and THE SYSTEM SHALL honour each setting independently.
