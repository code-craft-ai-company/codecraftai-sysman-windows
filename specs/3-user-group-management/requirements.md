# Requirements

## Introduction

The user and group management feature provides the `automojo-sysman-windows` package
with the ability to manage local users and local groups on Windows systems.  This spec
implements the user-group-management contract defined at
`automojo-sysman-common/specs/contracts/user-group-management/contract.md` for
Windows using the `Microsoft.PowerShell.LocalAccounts` module (`New-LocalUser`,
`Remove-LocalUser`, `Set-LocalUser`, `Get-LocalUser`, `Disable-LocalUser`,
`Enable-LocalUser`, `New-LocalGroup`, `Remove-LocalGroup`, `Get-LocalGroup`,
`Add-LocalGroupMember`, `Remove-LocalGroupMember`, `Get-LocalGroupMember`).  All
invocations flow through `powershell.exe -NoProfile -NonInteractive -Command` and emit
`ConvertTo-Json` for parseable output.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
users or groups should exist — it provides the capability to *manipulate them*
reliably, idempotently, and with full auditability.  All system commands are executed
exclusively through the `automojo-interfaces` command-runner abstraction, enabling
dry-run simulation, auditing, and test mocking without requiring a live system.
Passwords passed to `set_password` and `create_user` are converted to a
`SecureString` via `ConvertTo-SecureString -AsPlainText -Force` *inside* the
PowerShell script body, so the plaintext is never exposed to the Windows process
command line; additionally the `WindowsUserGroupActionResult.command` field records
only the literal argv and replaces any `$plaintext` parameter with `"<redacted>"`.

---

## Requirements

### Requirement 1: Create a Local User

**User Story:** As a maintenance coordinator, I want to create a local user on a
Windows system from a supplied `WindowsUserSpec`, so that the account becomes available
for interactive or service use with the full name, description, and optional password
the spec requires.

#### Acceptance Criteria

1. WHEN the coordinator provides a `WindowsUserSpec` THE SYSTEM SHALL construct a
   PowerShell invocation of `New-LocalUser -Name <name>` augmented with `-FullName`,
   `-Description`, `-Password` (converted from plaintext to a `SecureString` inside the
   script body), `-AccountNeverExpires`, and `-UserMayNotChangePassword` when the
   corresponding spec / config fields require them, and dispatch it through the
   command runner with `elevated=True` when configured.
2. WHEN `WindowsUserSpec.system_account` is `True` THE SYSTEM SHALL NOT alter the
   argv (Windows has no POSIX-style "system UID range") and SHALL record a detail on
   the result noting that Windows local accounts do not have a system-account
   classification.
3. WHEN a user with the same name already exists (detected via
   `Get-LocalUser -Name <name>`) THE SYSTEM SHALL skip execution and return a
   `WindowsUserGroupActionResult` with `status=SKIPPED` and `kind="user"`.
4. WHEN the `New-LocalUser` invocation succeeds THE SYSTEM SHALL return a
   `WindowsUserGroupActionResult` with `status=SUCCESS`.
5. WHEN the invocation fails THE SYSTEM SHALL return a `WindowsUserGroupActionResult`
   with `status=FAILED` and the captured exit code and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsUserGroupActionResult` with `status=DRY_RUN` and the sanitised
   argv (password replaced with `"<redacted>"`) in `command`.

---

### Requirement 2: Delete a Local User

**User Story:** As a maintenance coordinator, I want to delete a local user,
optionally removing their profile directory, so that dismissed accounts leave no
orphaned resources.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_user(name, remove_home=True)` THE SYSTEM SHALL
   dispatch a PowerShell script that first calls `Remove-LocalUser -Name <name>` and
   then removes the profile directory under `C:\Users\<name>` via
   `Remove-Item -Recurse -Force`, through the runner with `elevated=True` when
   configured.
2. WHEN `remove_home=False` THE SYSTEM SHALL dispatch only
   `Remove-LocalUser -Name <name>`.
3. WHEN the target user does not exist (detected via `Get-LocalUser -Name <name>`
   returning an `ObjectNotFound` error) THE SYSTEM SHALL skip execution and return a
   `WindowsUserGroupActionResult` with `status=SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `WindowsUserGroupActionResult`
   with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `WindowsUserGroupActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 3: Modify a Local User

**User Story:** As a maintenance coordinator, I want to modify a subset of an
existing user's properties, so that full name, description, or password-change
policies can be updated without recreating the account.

#### Acceptance Criteria

1. WHEN the coordinator calls `modify_user(name, changes)` THE SYSTEM SHALL dispatch
   a `Set-LocalUser -Name <name>` invocation with `-FullName <comment>`,
   `-Description <comment>`, `-AccountExpires`, or other flags corresponding to
   non-`None` fields of `WindowsUserModSpec`, through the runner with `elevated=True`
   when configured.
2. WHEN `WindowsUserModSpec.home` is set THE SYSTEM SHALL additionally issue a
   `Move-Item <old_home> <new_home>` when `move_home=True`, and also update the user's
   `Profile` path via the registry under `HKLM\SOFTWARE\Microsoft\Windows NT\
   CurrentVersion\ProfileList\<SID>` using `Set-ItemProperty`.
3. WHEN the target user does not exist THE SYSTEM SHALL raise
   `WindowsUserNotFoundError` before any command is dispatched.
4. WHEN all non-`None` fields already match the current `WindowsUserInfo` THE SYSTEM
   SHALL skip execution and return a `WindowsUserGroupActionResult` with
   `status=SKIPPED`.
5. WHEN every write succeeds THE SYSTEM SHALL return a
   `WindowsUserGroupActionResult` with `status=SUCCESS`.
6. WHEN any single write fails THE SYSTEM SHALL return a
   `WindowsUserGroupActionResult` with `status=FAILED` and a `detail` identifying
   which property failed.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 4: Query a Single User

**User Story:** As a maintenance coordinator, I want to query a single local user's
record, so that downstream logic can branch on existence and properties without
parsing `Get-LocalUser`'s formatter output directly.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_user(name)` THE SYSTEM SHALL dispatch
   `Get-LocalUser -Name <name> | Select-Object Name,SID,Description,FullName,Enabled,
   PasswordRequired,LastLogon | ConvertTo-Json -Depth 3` through the command runner
   without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON payload into a
   `WindowsUserInfo` with `name`, `uid` (the integer RID component of `SID`), `gid`
   (always `0` for Windows — primary-group semantics do not apply), `comment` (from
   `Description` or `FullName`), `home` (resolved from
   `HKLM\...\ProfileList\<SID>\ProfileImagePath`), `shell` (always `""`), and
   `locked=not Enabled`.
3. WHEN `Get-LocalUser` writes `ObjectNotFound` to stderr THE SYSTEM SHALL return
   `None`.
4. WHEN the command exits non-zero for any other reason THE SYSTEM SHALL raise a
   `WindowsUserGroupQueryError`.

---

### Requirement 5: List All Local Users

**User Story:** As a maintenance coordinator, I want to list every local user, so
that reporting and audit workflows enumerate the set of maintenance targets.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_users()` THE SYSTEM SHALL dispatch
   `Get-LocalUser | Select-Object Name,SID,Description,FullName,Enabled |
   ConvertTo-Json -Depth 3` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON array and materialise
   each entry into a `WindowsUserInfo`, with `home` filled on demand only when
   `get_user(name)` is called (to avoid one registry read per listed account).
3. WHEN the command fails THE SYSTEM SHALL raise a `WindowsUserGroupQueryError`.

---

### Requirement 6: Set a User's Password

**User Story:** As a maintenance coordinator, I want to set a user's password
without leaking it into audit logs, so that credentials remain confidential.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_password(name, password)` THE SYSTEM SHALL build
   a PowerShell script that takes the plaintext via a parameterised `param([string]
   $Plaintext)`, converts it with `ConvertTo-SecureString -AsPlainText -Force`, and
   invokes `Set-LocalUser -Name <name> -Password $secure`.  The `$Plaintext`
   parameter value is passed via the runner's stdin rather than on the command
   line.
2. WHEN the `WindowsUserGroupActionResult` is constructed THE SYSTEM SHALL set
   `command` to the PowerShell argv *excluding* the plaintext — the `$Plaintext`
   value is never stored in `command`; if any argument appears to contain the
   plaintext the manager SHALL replace it with the literal `"<redacted>"` before
   returning.
3. WHEN the target user does not exist THE SYSTEM SHALL raise
   `WindowsUserNotFoundError` before any command is dispatched.
4. WHEN the PowerShell invocation fails THE SYSTEM SHALL return a
   `WindowsUserGroupActionResult` with `status=FAILED` and the captured exit code
   and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 7: Lock a User Account

**User Story:** As a maintenance coordinator, I want to disable a user account so
the user cannot log in, without deleting the account.

#### Acceptance Criteria

1. WHEN the coordinator calls `lock_account(name)` THE SYSTEM SHALL dispatch
   `Disable-LocalUser -Name <name>` through the command runner with `elevated=True`
   when configured.
2. WHEN the current `WindowsUserInfo.locked` is already `True` THE SYSTEM SHALL skip
   execution and return a `WindowsUserGroupActionResult` with `status=SKIPPED`.
3. WHEN the target user does not exist THE SYSTEM SHALL raise
   `WindowsUserNotFoundError`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsUserGroupActionResult` with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `WindowsUserGroupActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 8: Unlock a User Account

**User Story:** As a maintenance coordinator, I want to re-enable a previously
disabled account so that the user can resume interactive use.

#### Acceptance Criteria

1. WHEN the coordinator calls `unlock_account(name)` THE SYSTEM SHALL dispatch
   `Enable-LocalUser -Name <name>` through the command runner with `elevated=True`
   when configured.
2. WHEN the current `WindowsUserInfo.locked` is already `False` THE SYSTEM SHALL
   skip execution and return a `WindowsUserGroupActionResult` with `status=SKIPPED`.
3. WHEN the target user does not exist THE SYSTEM SHALL raise
   `WindowsUserNotFoundError`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsUserGroupActionResult` with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `WindowsUserGroupActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 9: Create a Local Group

**User Story:** As a maintenance coordinator, I want to create a local group from a
supplied `WindowsGroupSpec`, so that downstream user memberships can target it.

#### Acceptance Criteria

1. WHEN the coordinator provides a `WindowsGroupSpec` THE SYSTEM SHALL construct a
   `New-LocalGroup -Name <name>` invocation augmented with `-Description <desc>`
   when `comment` is set, and dispatch the argv with `elevated=True` when configured.
2. WHEN a group with the same name already exists (detected via
   `Get-LocalGroup -Name <name>`) THE SYSTEM SHALL skip execution and return a
   `WindowsUserGroupActionResult` with `status=SKIPPED` and `kind="group"`.
3. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsUserGroupActionResult` with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `WindowsUserGroupActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 10: Delete a Local Group

**User Story:** As a maintenance coordinator, I want to delete a local group so
that stale group entries do not accumulate after their last referring user is
removed.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_group(name)` THE SYSTEM SHALL dispatch
   `Remove-LocalGroup -Name <name>` through the command runner with `elevated=True`
   when configured.
2. WHEN the target group does not exist THE SYSTEM SHALL skip execution and return
   a `WindowsUserGroupActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsUserGroupActionResult` with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `WindowsUserGroupActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 11: Query a Single Group

**User Story:** As a maintenance coordinator, I want to query a single group's
record, so that membership checks can branch on current state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_group(name)` THE SYSTEM SHALL dispatch
   `Get-LocalGroup -Name <name> | Select-Object Name,SID,Description |
   ConvertTo-Json -Depth 3` through the command runner without elevation, and then
   dispatch `Get-LocalGroupMember -Group <name> | Select-Object Name,SID |
   ConvertTo-Json -Depth 3` to enumerate members.
2. WHEN the command succeeds THE SYSTEM SHALL parse both JSON payloads and return a
   `WindowsGroupInfo` with `name`, `gid` (the integer RID component of `SID`), and
   `members` (usernames from the member enumeration, with `DOMAIN\user` entries
   reduced to the short-name form).
3. WHEN `Get-LocalGroup` returns `ObjectNotFound` THE SYSTEM SHALL return `None`.
4. WHEN `Get-LocalGroup` exits non-zero for any other reason THE SYSTEM SHALL raise
   a `WindowsUserGroupQueryError`.

---

### Requirement 12: List All Local Groups

**User Story:** As a maintenance coordinator, I want to list every local group, so
that reporting workflows can enumerate groups for cross-reference.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_groups()` THE SYSTEM SHALL dispatch
   `Get-LocalGroup | Select-Object Name,SID,Description | ConvertTo-Json -Depth 3`
   through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON array into
   `WindowsGroupInfo` records with `members=[]` — callers wanting membership lists
   MUST use `get_group(name)` which enumerates members separately.
3. WHEN the command fails THE SYSTEM SHALL raise a `WindowsUserGroupQueryError`.

---

### Requirement 13: Add a User to a Group

**User Story:** As a maintenance coordinator, I want to add a user to a
supplementary group, so that the user inherits the permissions associated with the
group.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_to_group(user, group)` THE SYSTEM SHALL dispatch
   `Add-LocalGroupMember -Group <group> -Member <user>` through the command runner
   with `elevated=True` when configured.
2. WHEN the target user does not exist THE SYSTEM SHALL raise
   `WindowsUserNotFoundError` before any command is dispatched.
3. WHEN the target group does not exist THE SYSTEM SHALL raise
   `WindowsGroupNotFoundError` before any command is dispatched.
4. WHEN the user is already a member of the group THE SYSTEM SHALL skip execution
   and return a `WindowsUserGroupActionResult` with `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsUserGroupActionResult` with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `WindowsUserGroupActionResult`
   with `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 14: Remove a User from a Group

**User Story:** As a maintenance coordinator, I want to remove a user from a
supplementary group, so that the user loses the permissions associated with the
group.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_from_group(user, group)` THE SYSTEM SHALL
   dispatch `Remove-LocalGroupMember -Group <group> -Member <user>` through the
   command runner with `elevated=True` when configured.
2. WHEN the target user does not exist THE SYSTEM SHALL raise
   `WindowsUserNotFoundError` before any command is dispatched.
3. WHEN the target group does not exist THE SYSTEM SHALL raise
   `WindowsGroupNotFoundError` before any command is dispatched.
4. WHEN the user is not a member of the group THE SYSTEM SHALL skip execution and
   return a `WindowsUserGroupActionResult` with `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsUserGroupActionResult` with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `WindowsUserGroupActionResult`
   with `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `WindowsUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 15: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all user and group write
commands to run with Administrator privileges, so that write operations succeed on
a standard Windows system where the process does not already hold an Administrator
token.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for every write (create_user, delete_user,
   modify_user, set_password, lock_account, unlock_account, create_group,
   delete_group, add_to_group, remove_from_group).
2. WHEN performing read-only operations (get_user, list_users, get_group,
   list_groups) THE SYSTEM SHALL NOT pass `elevated=True`, regardless of
   configuration.
3. WHEN the process already holds an Administrator token (the command runner
   reports `is_elevated=True`) THE SYSTEM SHALL NOT pass `elevated=True` as a
   redundant request.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no
   elevation and allow the command runner to surface the resulting Windows
   `ERROR_ACCESS_DENIED` (exit code `5`) as a `WindowsUserGroupActionResult` with
   `status=FAILED` for writes or as a `WindowsUserGroupQueryError` for reads.
