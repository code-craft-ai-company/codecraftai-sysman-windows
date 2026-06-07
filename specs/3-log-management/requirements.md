# Requirements

## Introduction

The log management feature provides the `codecraftai-sysman-windows` package with the
ability to inspect, tail, rotate, truncate, and configure rotation of logs on Windows
systems.  This spec implements the log-management contract defined at
`codecraftai-sysman-common/specs/contracts/log-management/contract.md` for Windows
using PowerShell's `Get-WinEvent` cmdlet for Event Log channels, `wevtutil` for clear
and size-limit commands, and the `IFileOperator` abstraction for plain-text log files
under `%ProgramData%`.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
logs to retrieve or rotate — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All system commands are executed exclusively
through the `codecraftai-interfaces` command-runner abstraction, and all text-log file I/O
is delegated to the `IFileOperator` abstraction.  No `subprocess`, no direct `open`,
and no direct `pathlib` calls are made inside this module, enabling dry-run
simulation, auditing, and test mocking without requiring a live Windows host.

The abstract log-source identifier on Windows is either an Event Log channel name
(e.g., `Application`, `System`, `Microsoft-Windows-Security-Auditing/Operational`) or
a filesystem path for plain text logs (e.g.,
`C:\ProgramData\MyApp\logs\app.log`).

---

## Requirements

### Requirement 1: Retrieve Recent Log Entries

**User Story:** As a maintenance coordinator, I want to retrieve recent entries from a
named log source with optional filters (lines, since, until, severity, pattern), so
that I can inspect recent activity without streaming the full history.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_log(source, lines, since, until, severity, pattern)`
   and `source` resolves to an Event Log channel THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "Get-WinEvent -LogName '<source>' -MaxEvents <lines>
   [-FilterHashtable @{StartTime='<since>'; EndTime='<until>'; Level=<level>}]
   | ConvertTo-Json -Depth 5"`.
2. WHEN `source` resolves to a filesystem path THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "Get-Content -Path '<source>' -Tail <lines>"`
   (when `lines` is supplied) or `Get-Content -Path '<source>'` otherwise, with
   subsequent in-process filtering by `since`, `until`, `severity`, and `pattern`.
3. WHEN the command succeeds THE SYSTEM SHALL parse stdout into a `WindowsLogEntries`
   record whose `entries` list is in chronological order.
4. WHEN `lines` is supplied and the parsed entry count equals `lines` THE SYSTEM
   SHALL set `truncated=True`.
5. WHEN the source does not exist (Get-WinEvent exit 1 with a "No events were
   found" message, or `Test-Path` is `False` for a file) THE SYSTEM SHALL return an
   empty `WindowsLogEntries`.
6. WHEN any other failure occurs THE SYSTEM SHALL raise `WindowsLogQueryError`.
7. WHEN `get_log` is invoked against any channel other than `Security` THE SYSTEM
   SHALL NOT pass `elevated=True`.  For the `Security` channel THE SYSTEM SHALL pass
   `elevated=True` (documented exception allowed by the contract).

---

### Requirement 2: Tail a Log Source

**User Story:** As a maintenance coordinator, I want to follow a log source as it
emits entries, so that I can observe live activity.

#### Acceptance Criteria

1. WHEN the coordinator calls `tail_log(source, follow=True)` and `source` resolves
   to an Event Log channel THE SYSTEM SHALL invoke a polling loop
   `Get-WinEvent -LogName '<source>' -MaxEvents 1 -Oldest` driven by the streaming
   runner API, yielding parsed `WindowsLogEntry` values.
2. WHEN `source` resolves to a filesystem path THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "Get-Content -Path '<source>' -Wait -Tail 0"`
   via the streaming API and yield one `WindowsLogEntry` per line.
3. WHEN `follow=False` THE SYSTEM SHALL invoke `Get-WinEvent -LogName '<source>'
   -MaxEvents <lines>` or `Get-Content -Tail <lines>` and terminate after the
   backlog is yielded.
4. WHEN the underlying stream is interrupted THE SYSTEM SHALL propagate the
   interrupt cleanly.
5. WHEN the source does not exist THE SYSTEM SHALL raise `WindowsLogQueryError` on
   first iteration.
6. WHEN `tail_log` is invoked THE SYSTEM SHALL follow the same elevation policy as
   `get_log`.

---

### Requirement 3: List Available Log Sources

**User Story:** As a maintenance coordinator, I want to enumerate the available log
sources, so that I can build tooling that does not hard-code channel or path names.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_logs()` THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "Get-WinEvent -ListLog * | ConvertTo-Json
   -Depth 3"` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL produce one `WindowsLogInfo` per
   channel with `source=<LogName>`, `path=<LogFilePath> | None`, `size_bytes=
   <FileSize> | None`, and `rotating=True` iff `IsLogFull` is `False` AND
   `MaximumSizeInBytes > 0`.
3. WHEN the command fails THE SYSTEM SHALL raise `WindowsLogQueryError`.

---

### Requirement 4: Rotate a Log Source

**User Story:** As a maintenance coordinator, I want to trigger rotation for a named
log source, so that old content is archived and the channel starts fresh.

#### Acceptance Criteria

1. WHEN the coordinator calls `rotate_log(source)` and `source` resolves to an
   Event Log channel THE SYSTEM SHALL invoke
   `wevtutil epl <source> <archive-path>` to export, followed by
   `wevtutil cl <source>` to clear — both via the command runner.
2. WHEN `source` resolves to a filesystem path THE SYSTEM SHALL invoke
   `IFileOperator.copy(src=source, dst="<source>.<YYYYMMDDTHHMMSS>")` then
   `IFileOperator.write_text(path=source, content="", elevated=elevated)`.
3. WHEN every sub-step succeeds THE SYSTEM SHALL return
   `WindowsLogActionResult(status=SUCCESS, ...)`.
4. WHEN any sub-step fails THE SYSTEM SHALL return `status=FAILED` with exit code,
   stderr, and command populated from the failing step.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke any command or file operation
   and SHALL return `status=DRY_RUN` with `detail` describing the planned archive
   path (Windows log-management has no native simulation CLI so the documented
   exception from the contract is NOT used here).
6. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the runner and
   file operator.

---

### Requirement 5: Truncate a Log Source

**User Story:** As a maintenance coordinator, I want to clear a log source without
archiving, so that disk usage is reclaimed immediately.

#### Acceptance Criteria

1. WHEN the coordinator calls `truncate_log(source)` and `source` resolves to an
   Event Log channel THE SYSTEM SHALL invoke `wevtutil cl <source>` via the
   command runner.
2. WHEN `source` resolves to a filesystem path THE SYSTEM SHALL invoke
   `IFileOperator.write_text(path=source, content="", elevated=elevated)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the operation fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke any command or file operation
   and SHALL return `status=DRY_RUN`.
6. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` for all paths.

---

### Requirement 6: Configure Log Rotation

**User Story:** As a maintenance coordinator, I want to persist a rotation policy
for a named source, so that Windows manages retention automatically.

#### Acceptance Criteria

1. WHEN the coordinator calls `configure_rotation(source, policy)` and `source`
   resolves to an Event Log channel THE SYSTEM SHALL invoke
   `wevtutil sl <source> /ms:<max_size_bytes> /rt:<true|false>` via the command
   runner, where `/rt:true` encodes `overflow_action="RETAIN"` and `/rt:false`
   encodes `overflow_action="OVERWRITE"`.
2. WHEN `source` resolves to a filesystem path THE SYSTEM SHALL render the policy
   into a JSON drop-in and write it to
   `C:\ProgramData\CodeCraftAI\logrotation\<basename>.json` via
   `IFileOperator.write_text`.
3. WHEN the current policy (read via `wevtutil gl <source>` or the drop-in file)
   matches the requested policy THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN the write or command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke any command or file operation
   and SHALL return `status=DRY_RUN` with the planned command/content in `detail`.

---

### Requirement 7: Remove a Rotation Policy

**User Story:** As a maintenance coordinator, I want to remove a rotation policy for
a source, so that Windows reverts to its default behaviour (for channels) or our
drop-in is removed (for files).

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_rotation_config(source)` and `source` resolves
   to an Event Log channel THE SYSTEM SHALL invoke
   `wevtutil sl <source> /ms:20971520 /rt:false` (the documented default) via the
   command runner.  If the current settings already match THE SYSTEM SHALL return
   `status=SKIPPED`.
2. WHEN `source` resolves to a filesystem path and the drop-in does not exist THE
   SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the drop-in exists THE SYSTEM SHALL invoke `IFileOperator.delete`.
4. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke any command or file operation
   and SHALL return `status=DRY_RUN`.

---

### Requirement 8: Query a Rotation Policy

**User Story:** As a maintenance coordinator, I want to inspect the current rotation
policy for a source, so that I can decide whether to update or remove it.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_rotation_config(source)` and `source` resolves
   to an Event Log channel THE SYSTEM SHALL invoke `wevtutil gl <source>` via the
   command runner and parse the key/value output into a `WindowsRotationPolicy`.
2. WHEN `source` resolves to a filesystem path and the drop-in does not exist THE
   SYSTEM SHALL return `None`.
3. WHEN the drop-in exists THE SYSTEM SHALL read it via `IFileOperator.read_text`
   and deserialise the JSON into a `WindowsRotationPolicy`.
4. WHEN parsing or reading fails THE SYSTEM SHALL raise `WindowsLogQueryError`.
5. WHEN `get_rotation_config` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 9: Query Disk Usage

**User Story:** As a maintenance coordinator, I want to report disk usage for a
named source, so that retention tuning is driven by real figures.

#### Acceptance Criteria

1. WHEN the coordinator calls `query_disk_usage(source)` and `source` resolves to
   an Event Log channel THE SYSTEM SHALL invoke `wevtutil gl <source>` and extract
   the `fileSize` field (bytes).
2. WHEN `source` resolves to a file THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "(Get-Item -Path '<source>').Length"` and parse
   stdout as an integer.
3. WHEN the command succeeds THE SYSTEM SHALL return `WindowsLogDiskUsage(source,
   size_human, size_bytes)` where `size_human` is computed in pure Python.
4. WHEN the command fails THE SYSTEM SHALL raise `WindowsLogQueryError`.

---

### Requirement 10: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all write operations to run as
Administrator, so that `wevtutil cl`, `wevtutil sl`, and writes under
`%ProgramData%` succeed.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the runner and
   file operator for `rotate_log`, `truncate_log`, `configure_rotation`, and
   `remove_rotation_config`.
2. WHEN performing non-`Security` reads THE SYSTEM SHALL NOT pass `elevated=True`.
3. WHEN performing `Security`-channel reads THE SYSTEM SHALL pass `elevated=True`
   (contract-permitted exception).
4. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` on every path
   (writes will then fail at the runner if Administrator is required).
5. WHEN a non-Administrator runner hits `Access is denied` during a read THE
   SYSTEM SHALL raise `WindowsLogQueryError` with captured stderr.

---

### Requirement 11: Dry-Run Semantics

**User Story:** As a maintenance coordinator, I want every write verb to honour
dry-run mode, so that plans can be validated without system change.

#### Acceptance Criteria

1. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` for every write
   verb (`rotate_log`, `truncate_log`, `configure_rotation`,
   `remove_rotation_config`) without invoking any mutating runner or file-operator
   method.  Windows has no native simulation CLI for these operations, so the
   contract-permitted `rotate_log` exception is NOT exercised.
2. WHEN `dry_run=True` THE SYSTEM SHALL populate `detail` with a human-readable
   description of the planned command or content change.
3. WHEN `dry_run=False` THE SYSTEM SHALL execute the normal code path regardless
   of other configuration.
