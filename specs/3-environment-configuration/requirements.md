# Requirements

## Introduction

The environment configuration feature provides the `codecraftai-sysman-windows`
package with the ability to set, unset, and query system-wide and per-user
environment variables on Windows.  This spec implements the
environment-configuration contract defined at
`codecraftai-sysman-common/specs/contracts/environment-configuration/contract.md`
for Windows using the registry (`HKLM:\SYSTEM\CurrentControlSet\Control\Session
Manager\Environment` for system scope, `HKCU:\Environment` for the current
user, and `HKU:\<SID>\Environment` for a named other user).

A maintenance coordinator running in an external package drives all operations
by calling into this feature's public interfaces.  The feature does not decide
*what* variables to apply — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All registry operations go through
the `ICommandRunner` abstraction (PowerShell-based); the `IFileOperator` is
present for the rare `%USERPROFILE%` support file path (e.g., a text-log
record of prior state).  No `subprocess`, no direct `open`, and no direct
`pathlib` calls are made inside the module.

The abstract `EnvScope` on Windows maps to:

- **SYSTEM**: `HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment`
  via `[Environment]::SetEnvironmentVariable(name, value, 'Machine')`.
- **USER** (current user): `HKCU:\Environment` via
  `[Environment]::SetEnvironmentVariable(name, value, 'User')`.
- **USER** (other user, by SID): `HKU:\<SID>\Environment` via
  `Set-ItemProperty -Path "Registry::HKEY_USERS\<SID>\Environment" -Name
  <name> -Value <value>`.

---

## Requirements

### Requirement 1: Set a System-Scope Variable

**User Story:** As a maintenance coordinator, I want to set a system-scope
environment variable, so that the value is visible to every process launched
after a new logon or `WM_SETTINGCHANGE` broadcast.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_system_env(name, value)` THE SYSTEM SHALL
   invoke `powershell -NoProfile -Command "[Environment]::SetEnvironmentVariable
   ('<name>', '<value>', 'Machine')"` via the command runner.
2. WHEN the current machine-scope value (queried via
   `[Environment]::GetEnvironmentVariable('<name>', 'Machine')`) already
   equals the requested value THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with
   `message` populated from stderr.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke the runner's mutating path
   and SHALL return `status=DRY_RUN`.
6. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the runner.

---

### Requirement 2: Unset a System-Scope Variable

**User Story:** As a maintenance coordinator, I want to unset a system-scope
environment variable.

#### Acceptance Criteria

1. WHEN the coordinator calls `unset_system_env(name)` and the variable is
   absent from machine scope THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the variable is present THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "[Environment]::SetEnvironmentVariable
   ('<name>', $null, 'Machine')"` via the command runner.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke the mutating path.
6. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True`.

---

### Requirement 3: Get a System-Scope Variable

**User Story:** As a maintenance coordinator, I want to read a single
system-scope variable.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_system_env(name)` THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable
   ('<name>', 'Machine')"` via the command runner.
2. WHEN the command succeeds with non-empty stdout THE SYSTEM SHALL return a
   `WindowsEnvValue` with `scope=SYSTEM`,
   `source="HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment"`.
3. WHEN stdout is empty or the command exits non-zero THE SYSTEM SHALL return
   `None`.
4. WHEN `get_system_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 4: List System-Scope Variables

**User Story:** As a maintenance coordinator, I want to list every system-scope
variable.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_system_env()` THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "Get-ItemProperty -Path 'HKLM:\SYSTEM\
   CurrentControlSet\Control\Session Manager\Environment' | ConvertTo-Json"`
   via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON and produce a
   `dict[str, str]` excluding the pseudo-keys `PSPath`, `PSParentPath`,
   `PSChildName`, `PSDrive`, `PSProvider`.
3. WHEN the registry key is absent THE SYSTEM SHALL return an empty dict.
4. WHEN the command fails for any other reason THE SYSTEM SHALL raise
   `WindowsEnvConfigError`.
5. WHEN `list_system_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 5: Set a User-Scope Variable

**User Story:** As a maintenance coordinator, I want to set a user-scope
environment variable for a specific user.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_user_env(user, name, value)` and the user
   does not resolve to a SID THE SYSTEM SHALL return `status=FAILED` with
   `message="user not found: <user>"` and SHALL NOT invoke the runner.
2. WHEN the user equals the current user THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "[Environment]::SetEnvironmentVariable
   ('<name>', '<value>', 'User')"` via the command runner.
3. WHEN the user is a different user THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "Set-ItemProperty -Path 'Registry::HKEY_USERS
   \<SID>\Environment' -Name '<name>' -Value '<value>' -Type String"` via the
   command runner.
4. WHEN the current value (queried with the matching `Get` variant) already
   equals the requested value THE SYSTEM SHALL return `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
6. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke the runner's mutating path.
7. WHEN `elevated=True` AND the target user differs from the current user THE
   SYSTEM SHALL pass `elevated=True`.  When they match THE SYSTEM SHALL pass
   `elevated=False`.

---

### Requirement 6: Unset a User-Scope Variable

**User Story:** As a maintenance coordinator, I want to unset a user-scope
environment variable.

#### Acceptance Criteria

1. WHEN the coordinator calls `unset_user_env(user, name)` and the user does
   not resolve to a SID THE SYSTEM SHALL return `status=FAILED` with
   `message="user not found: <user>"`.
2. WHEN the variable is absent from the resolved scope THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the variable is present AND the user equals the current user THE
   SYSTEM SHALL invoke `[Environment]::SetEnvironmentVariable('<name>', $null,
   'User')`.
4. WHEN the variable is present AND the user is a different user THE SYSTEM
   SHALL invoke `Remove-ItemProperty -Path 'Registry::HKEY_USERS\<SID>\
   Environment' -Name '<name>'`.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke the runner's mutating path.
6. WHEN `elevated=True` THE SYSTEM SHALL follow the same elevation policy as
   `set_user_env`.

---

### Requirement 7: Get a User-Scope Variable

**User Story:** As a maintenance coordinator, I want to read a single
user-scope variable.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_user_env(user, name)` and the user does not
   resolve to a SID THE SYSTEM SHALL raise `WindowsEnvConfigError`.
2. WHEN the user equals the current user THE SYSTEM SHALL invoke
   `[Environment]::GetEnvironmentVariable('<name>', 'User')`.
3. WHEN the user is a different user THE SYSTEM SHALL invoke
   `(Get-ItemProperty -Path 'Registry::HKEY_USERS\<SID>\Environment' -Name
   '<name>').'<name>'`.
4. WHEN the query yields empty output or exits non-zero THE SYSTEM SHALL
   return `None`.
5. WHEN the query succeeds with non-empty output THE SYSTEM SHALL return a
   `WindowsEnvValue` with `scope=USER`, `source=<registry path>`.
6. WHEN `get_user_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 8: List User-Scope Variables

**User Story:** As a maintenance coordinator, I want to list every user-scope
variable set for a user.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_user_env(user)` and the user does not
   resolve to a SID THE SYSTEM SHALL raise `WindowsEnvConfigError`.
2. WHEN the user equals the current user THE SYSTEM SHALL invoke
   `Get-ItemProperty -Path 'HKCU:\Environment' | ConvertTo-Json`.
3. WHEN the user is a different user THE SYSTEM SHALL invoke
   `Get-ItemProperty -Path 'Registry::HKEY_USERS\<SID>\Environment' |
   ConvertTo-Json`.
4. WHEN the registry key is absent THE SYSTEM SHALL return an empty dict.
5. WHEN the command succeeds THE SYSTEM SHALL parse the JSON excluding the
   PowerShell pseudo-keys.
6. WHEN `list_user_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want system-scope writes to
run as Administrator, so that registry writes to `HKLM:\` succeed.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` on every runner
   call for system-scope writes.
2. WHEN `elevated=True` AND the user-scope target is a different user THE
   SYSTEM SHALL pass `elevated=True` (required to write into another user's
   hive).
3. WHEN `elevated=True` AND the user-scope target is the current user THE
   SYSTEM SHALL pass `elevated=False` (HKCU writes do not require
   elevation).
4. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` everywhere.
5. WHEN performing reads THE SYSTEM SHALL NEVER pass `elevated=True`.

---

### Requirement 10: User-to-SID Resolution

**User Story:** As a maintenance coordinator, I want user names transparently
resolved to SIDs, so that I can address any local or domain user without
learning the SID syntax.

#### Acceptance Criteria

1. WHEN resolving a user name THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "(New-Object
   System.Security.Principal.NTAccount('<user>')).Translate([System.Security
   .Principal.SecurityIdentifier]).Value"` via the command runner.
2. WHEN the translation succeeds THE SYSTEM SHALL use the resulting SID in
   subsequent registry paths.
3. WHEN the translation fails (exit non-zero or empty output) THE SYSTEM
   SHALL surface a user-not-found condition as described in Requirements 5.1,
   6.1, 7.1, and 8.1.
4. WHEN the user name contains a backslash (e.g., `DOMAIN\alice`) THE SYSTEM
   SHALL pass it through to `NTAccount` unchanged.

---

### Requirement 11: Dry-Run Semantics

**User Story:** As a maintenance coordinator, I want every write verb to honour
dry-run mode, so that plans can be validated without system change.

#### Acceptance Criteria

1. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` for every
   write verb without invoking any mutating runner path.
2. WHEN `dry_run=False` THE SYSTEM SHALL execute the normal code path.
3. WHEN `dry_run=True` THE SYSTEM SHALL still perform reads (`GetEnvironment
   Variable`, `Get-ItemProperty`) so that `SKIPPED` vs `DRY_RUN` can be
   distinguished.
