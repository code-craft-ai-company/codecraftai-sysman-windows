# Requirements

## Introduction

The environment configuration feature provides the `automojo-sysman-linux-windows` package
with the ability to manage shell environment variables on a Windows Linux system.  This
covers setting, unsetting, and querying variables in system-wide profile drop-in files
(`/etc/profile.d/`), user-level shell rc files (`.bashrc`, `.bash_profile`), and
`/etc/environment`.

A maintenance coordinator drives all operations by calling into this feature's public
interfaces.  The feature does not decide *which* variables to set — it provides the
primitives to apply, remove, and inspect environment configuration reliably, idempotently,
and with full auditability.  File reads and writes use the `automojo-interfaces`
file-operator abstraction; command execution uses the command-runner abstraction.  This
ensures dry-run simulation, auditing, and test mocking without requiring a live system.

---

## Requirements

### Requirement 1: Set a System-Wide Environment Variable

**User Story:** As a maintenance coordinator, I want to write an environment variable
export to a drop-in file under `/etc/profile.d/`, so that the variable is available to
all login shells on the system without modifying the base profile script.

#### Acceptance Criteria

1. WHEN the coordinator provides a variable name, value, and drop-in filename THE SYSTEM
   SHALL write or update `export <NAME>="<value>"` in
   `/etc/profile.d/<filename>`.
2. WHEN the variable is already present with the same value in the target file THE SYSTEM
   SHALL skip the write and return an `EnvActionResult` with status `SKIPPED`.
3. WHEN the variable is present with a different value THE SYSTEM SHALL update the
   existing line in place and return an `EnvActionResult` with status `SUCCESS`.
4. WHEN the file does not exist THE SYSTEM SHALL create it with a `#!/usr/bin/env bash`
   shebang line followed by the export statement and return an `EnvActionResult` with
   status `SUCCESS`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT modify any file and SHALL return an
   `EnvActionResult` with status `DRY_RUN`.

---

### Requirement 2: Unset a System-Wide Environment Variable

**User Story:** As a maintenance coordinator, I want to remove an environment variable
export from a drop-in file under `/etc/profile.d/`, so that the variable is no longer
exported to login shells.

#### Acceptance Criteria

1. WHEN the coordinator provides a variable name and drop-in filename THE SYSTEM SHALL
   remove the line matching `export <NAME>=...` from
   `/etc/profile.d/<filename>`.
2. WHEN the variable is not present in the file THE SYSTEM SHALL skip the operation and
   return an `EnvActionResult` with status `SKIPPED`.
3. WHEN the variable is removed and the file becomes empty (only a shebang remains or no
   content lines remain) THE SYSTEM SHALL delete the file.
4. WHEN the command succeeds THE SYSTEM SHALL return an `EnvActionResult` with status
   `SUCCESS`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT modify any file and SHALL return an
   `EnvActionResult` with status `DRY_RUN`.

---

### Requirement 3: Set a Variable in `/etc/environment`

**User Story:** As a maintenance coordinator, I want to write an environment variable to
`/etc/environment`, so that it is available to PAM-authenticated sessions and services
that source this file.

#### Acceptance Criteria

1. WHEN the coordinator provides a variable name and value THE SYSTEM SHALL write or
   update `<NAME>="<value>"` (no `export` keyword) in `/etc/environment`.
2. WHEN the variable is already present with the same value THE SYSTEM SHALL skip the
   write and return an `EnvActionResult` with status `SKIPPED`.
3. WHEN the variable is present with a different value THE SYSTEM SHALL update the
   existing line in place and return an `EnvActionResult` with status `SUCCESS`.
4. WHEN the variable is not present THE SYSTEM SHALL append `<NAME>="<value>"` to the
   file and return an `EnvActionResult` with status `SUCCESS`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT modify any file and SHALL return an
   `EnvActionResult` with status `DRY_RUN`.

---

### Requirement 4: Unset a Variable from `/etc/environment`

**User Story:** As a maintenance coordinator, I want to remove an environment variable
from `/etc/environment`, so that it is no longer injected into PAM-authenticated sessions.

#### Acceptance Criteria

1. WHEN the coordinator provides a variable name THE SYSTEM SHALL remove the line matching
   `<NAME>=...` from `/etc/environment`.
2. WHEN the variable is not present THE SYSTEM SHALL skip the operation and return an
   `EnvActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return an `EnvActionResult` with status
   `SUCCESS`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT modify any file and SHALL return an
   `EnvActionResult` with status `DRY_RUN`.

---

### Requirement 5: Set a Variable in a User Shell RC File

**User Story:** As a maintenance coordinator, I want to write an environment variable
export to a specific user's shell rc file (`.bashrc` or `.bash_profile`), so that the
variable is available in that user's interactive and login shells.

#### Acceptance Criteria

1. WHEN the coordinator provides a username, variable name, value, and target rc file name
   THE SYSTEM SHALL write or update `export <NAME>="<value>"` in the user's home
   directory rc file.
2. WHEN the variable is already present with the same value THE SYSTEM SHALL skip the
   write and return an `EnvActionResult` with status `SKIPPED`.
3. WHEN the variable is present with a different value THE SYSTEM SHALL update the
   existing line in place and return an `EnvActionResult` with status `SUCCESS`.
4. WHEN the file does not exist THE SYSTEM SHALL create it and append the export statement.
5. WHEN the specified user does not exist THE SYSTEM SHALL return an `EnvActionResult`
   with status `FAILED` and a descriptive error message.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT modify any file and SHALL return an
   `EnvActionResult` with status `DRY_RUN`.

---

### Requirement 6: Unset a Variable from a User Shell RC File

**User Story:** As a maintenance coordinator, I want to remove an environment variable
export from a specific user's shell rc file, so that the variable is no longer set in
that user's shell sessions.

#### Acceptance Criteria

1. WHEN the coordinator provides a username, variable name, and rc file name THE SYSTEM
   SHALL remove the line matching `export <NAME>=...` from the user's rc file.
2. WHEN the variable is not present in the file THE SYSTEM SHALL skip the operation and
   return an `EnvActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return an `EnvActionResult` with status
   `SUCCESS`.
4. WHEN the specified user does not exist THE SYSTEM SHALL return an `EnvActionResult`
   with status `FAILED` and a descriptive error message.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT modify any file and SHALL return an
   `EnvActionResult` with status `DRY_RUN`.

---

### Requirement 7: Query System-Wide Environment Variables

**User Story:** As a maintenance coordinator, I want to read all variables defined in a
specific `/etc/profile.d/` drop-in file or in `/etc/environment`, so that I can make
conditional decisions about what changes are needed without reading raw file contents.

#### Acceptance Criteria

1. WHEN the coordinator requests variables from a `/etc/profile.d/<filename>` THE SYSTEM
   SHALL read the file and return a `dict[str, str]` of variable names to values.
2. WHEN the coordinator requests variables from `/etc/environment` THE SYSTEM SHALL read
   the file and return a `dict[str, str]` of variable names to values.
3. WHEN the file does not exist THE SYSTEM SHALL return an empty dict.
4. WHEN a line does not match the expected format (`export NAME=value` or `NAME=value`)
   THE SYSTEM SHALL skip that line without raising an error.

---

### Requirement 8: Query User Shell RC File Variables

**User Story:** As a maintenance coordinator, I want to read all exported variables
defined in a specific user's shell rc file, so that I can audit or conditionally update
only what is needed.

#### Acceptance Criteria

1. WHEN the coordinator provides a username and rc file name THE SYSTEM SHALL read the
   file and return a `dict[str, str]` of variable names to values.
2. WHEN the file does not exist THE SYSTEM SHALL return an empty dict.
3. WHEN the specified user does not exist THE SYSTEM SHALL raise an
   `EnvConfigError` with a descriptive message.
4. WHEN a line does not match `export NAME=value` THE SYSTEM SHALL skip it without
   raising an error.

---

### Requirement 9: Elevated Privilege for System-Wide Files

**User Story:** As a maintenance coordinator, I want system-wide file writes
(`/etc/profile.d/` and `/etc/environment`) to use elevated privileges, so that the
operation succeeds on a standard system where the maintenance process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured to use elevated writes THE SYSTEM SHALL pass
   `elevated=True` to the file operator for all writes to `/etc/profile.d/` and
   `/etc/environment`.
2. WHEN performing read-only operations (queries) THE SYSTEM SHALL NOT use elevation.
3. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation
   and allow the file operator to surface the resulting permission error.
