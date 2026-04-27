# Requirements

## Introduction

The configuration management feature provides the `automojo-sysman-windows`
package with the ability to read, write, diff, validate, back up, restore, and
adjust permissions/ownership on Windows configuration artifacts.  This spec
implements the configuration-management contract defined at
`automojo-sysman-common/specs/contracts/configuration-management/contract.md`
for Windows.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *what*
configuration values to apply — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All system commands are executed
exclusively through the `automojo-interfaces` command-runner abstraction
(PowerShell-based), and all text-file I/O is delegated to the `IFileOperator`
abstraction.  No `subprocess`, no direct `open`, and no direct `pathlib` calls
are made inside this module.

The abstract configuration-source identifier on Windows is either a registry
path (e.g., `HKLM:\SOFTWARE\Vendor\Product`) or a plain filesystem path
(e.g., `C:\ProgramData\MyApp\app.config`).  Format detection is driven by
source prefix / suffix with an explicit caller override available.

---

## Requirements

### Requirement 1: Read a Setting

**User Story:** As a maintenance coordinator, I want to read a single setting
from a configuration source, so that I can inspect it without having to parse
the whole source.

#### Acceptance Criteria

1. WHEN the coordinator calls `read_setting(source, key)` and `source` resolves
   to a registry path THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "(Get-ItemProperty -Path '<path>' -Name
   '<key>').'<key>'"` via the command runner and return a
   `WindowsSettingValue` with `value_type=<REG_* type>`.
2. WHEN `source` resolves to a plain file THE SYSTEM SHALL read via
   `IFileOperator.read_text` and parse using the format-appropriate parser
   (INI, XML, JSON, `KEY=VALUE`).
3. WHEN the key is not present THE SYSTEM SHALL raise
   `WindowsConfigSettingNotFoundError`.
4. WHEN the source does not exist THE SYSTEM SHALL raise
   `WindowsConfigSettingNotFoundError` with `detail="source missing"`.
5. WHEN any other failure occurs THE SYSTEM SHALL raise
   `WindowsConfigQueryError`.
6. WHEN `read_setting` is invoked THE SYSTEM SHALL NOT pass `elevated=True`
   except for registry paths rooted under `HKLM:\SECURITY\` which always
   elevate (documented exception).

---

### Requirement 2: Write a Setting

**User Story:** As a maintenance coordinator, I want to write a single setting
in a configuration source, so that I can change a value without rewriting the
whole source.

#### Acceptance Criteria

1. WHEN the coordinator calls `write_setting(source, key, value)` and `source`
   resolves to a registry path THE SYSTEM SHALL infer the value type
   (`String` / `ExpandString` / `DWord` / `QWord` / `MultiString` / `Binary`)
   and invoke
   `powershell -NoProfile -Command "Set-ItemProperty -Path '<path>' -Name
   '<key>' -Value '<value>' -Type <type>"` via the runner with
   `elevated=elevated`.
2. WHEN `source` resolves to a plain file THE SYSTEM SHALL read via
   `IFileOperator.read_text`, mutate the key in memory, re-serialise, and
   write via `IFileOperator.write_text` with `elevated=elevated`.
3. WHEN the current value already equals the requested value THE SYSTEM SHALL
   return `status=SKIPPED`.
4. WHEN the source (registry key or file) does not exist THE SYSTEM SHALL
   create it (via `New-Item` for registry, `write_text` for files).
5. WHEN the write succeeds THE SYSTEM SHALL return `status=SUCCESS`.
6. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.
7. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with the
   planned `command` / `diff` in the result and no mutating call.

---

### Requirement 3: Remove a Setting

**User Story:** As a maintenance coordinator, I want to remove a single setting
from a configuration source, so that the system falls back to its default.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_setting(source, key)` and the key is not
   present THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the key is present and `source` is a registry path THE SYSTEM SHALL
   invoke `powershell -NoProfile -Command "Remove-ItemProperty -Path '<path>'
   -Name '<key>'"`.
3. WHEN the key is present and `source` is a plain file THE SYSTEM SHALL
   rewrite the file via `write_text`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN the operation fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 4: List Settings

**User Story:** As a maintenance coordinator, I want to list every setting in a
configuration source, so that I can enumerate options.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_settings(source)` and the source does not
   exist THE SYSTEM SHALL return an empty list.
2. WHEN `source` resolves to a registry path THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "Get-Item -Path '<path>' | Select-Object -
   ExpandProperty Property"` plus per-name
   `Get-ItemProperty -Path '<path>' -Name '<each>' | ConvertTo-Json`.
3. WHEN `source` resolves to a plain file THE SYSTEM SHALL parse the file and
   return one `WindowsSettingValue` per key, preserving order.
4. WHEN parsing fails THE SYSTEM SHALL raise `WindowsConfigQueryError`.
5. WHEN `list_settings` is invoked THE SYSTEM SHALL NOT pass `elevated=True`
   (except for `HKLM:\SECURITY\`).

---

### Requirement 5: Write Whole-Source Content

**User Story:** As a maintenance coordinator, I want to overwrite an entire
configuration file with deterministic content, so that I can apply a whole-file
template.

#### Acceptance Criteria

1. WHEN the coordinator calls `write(source, content, mode, owner, group)` and
   `source` resolves to a plain file THE SYSTEM SHALL call
   `IFileOperator.write_text(path=source, content=content, elevated=elevated)`
   when content differs from current.
2. WHEN `source` resolves to a registry path THE SYSTEM SHALL treat `content`
   as a `.reg`-format export, write it to a temp file via `write_text`, invoke
   `reg import <tmp>` via the runner, and delete the temp file via
   `IFileOperator.delete`.
3. WHEN content matches current THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN `mode`/`owner`/`group` are supplied for a file source THE SYSTEM SHALL
   invoke `icacls <path> /grant <user>:<perm>` via the runner for ownership,
   and `icacls <path> /inheritance:<e|r>` plus `icacls <path> /grant
   <user>:(M,RX)` for permission changes.  When `mode`/`owner`/`group` are
   supplied for a registry source THE SYSTEM SHALL return `status=FAILED` with
   `detail="registry paths use ACL-based permissions via set_permissions"`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with the full
   diff in `diff`.

---

### Requirement 6: Diff Current vs Desired

**User Story:** As a maintenance coordinator, I want to compute a diff between
current and desired content, so that I can preview changes.

#### Acceptance Criteria

1. WHEN the coordinator calls `diff(source, desired_content)` and `source` is
   a plain file THE SYSTEM SHALL read via `IFileOperator.read_text` and
   compute the unified diff using `difflib.unified_diff`.
2. WHEN `source` is a registry path THE SYSTEM SHALL invoke
   `reg export '<path>' <tmp> /y` to get the current `.reg` export, read the
   file via `IFileOperator.read_text`, diff against `desired_content`, and
   delete the temp file via `IFileOperator.delete`.
3. WHEN the source does not exist THE SYSTEM SHALL treat current as empty.
4. WHEN current equals desired THE SYSTEM SHALL return `has_diff=False`.
5. WHEN `diff` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 7: Validate a Source

**User Story:** As a maintenance coordinator, I want to validate a
configuration source using a registered validator, so that I can catch errors
before a reload.

#### Acceptance Criteria

1. WHEN the coordinator calls `validate(source, validator_id)` and
   `validator_id` is registered THE SYSTEM SHALL invoke the validator argv
   (e.g., `gpupdate /target:Computer /force`, `certutil -verify <path>`,
   `sfc /verifyonly`, `powershell Test-Path '<path>'`).
2. WHEN exit code 0 THE SYSTEM SHALL return `valid=True`.
3. WHEN exit code is non-zero THE SYSTEM SHALL return `valid=False`.
4. WHEN `validator_id` is not registered THE SYSTEM SHALL raise
   `WindowsConfigValidatorNotFoundError`.
5. WHEN `dry_run=True` THE SYSTEM SHALL still invoke the validator.

---

### Requirement 8: Backup a Source

**User Story:** As a maintenance coordinator, I want to take a timestamped
backup of a configuration source, so that I can recover from a bad change.

#### Acceptance Criteria

1. WHEN the coordinator calls `backup_configuration(source)` and the source
   does not exist THE SYSTEM SHALL raise `WindowsConfigFileNotFoundError`.
2. WHEN `source` resolves to a registry path THE SYSTEM SHALL invoke
   `reg export '<path>' <backup_path> /y` where `<backup_path>` is
   `C:\ProgramData\Automojo\backups\<registry-basename>.<YYYYMMDDTHHMMSS>.reg`.
3. WHEN `source` resolves to a file THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "Copy-Item -LiteralPath '<source>'
   -Destination '<backup_path>' -Force"` where `<backup_path>` is
   `<source>.bak.<YYYYMMDDTHHMMSS>`.
4. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS` with
   `backup_path`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with the
   planned `backup_path`.

---

### Requirement 9: Restore from a Backup

**User Story:** As a maintenance coordinator, I want to restore a backup over
its original source, so that I can roll back.

#### Acceptance Criteria

1. WHEN the coordinator calls `restore_configuration(backup_path)` and the
   path ends in `.reg` under the backup directory THE SYSTEM SHALL invoke
   `reg import '<backup_path>'`.
2. WHEN the backup path matches `<source>.bak.<YYYYMMDDTHHMMSS>` THE SYSTEM
   SHALL derive the original path and invoke `Copy-Item -LiteralPath
   '<backup_path>' -Destination '<original>' -Force`.
3. WHEN the backup does not exist THE SYSTEM SHALL raise
   `WindowsConfigFileNotFoundError`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 10: Set Permissions

**User Story:** As a maintenance coordinator, I want to apply ACLs to a
configuration source, so that access matches policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_permissions(source, mode)` and the source
   does not exist THE SYSTEM SHALL raise `WindowsConfigFileNotFoundError`.
2. WHEN `source` is a file THE SYSTEM SHALL translate the octal `mode` to an
   `icacls` ACE string (`0o644` → `Users:(RX),<owner>:(M)`) and invoke
   `icacls '<source>' /grant:r <ace>` via the runner.
3. WHEN `source` is a registry path THE SYSTEM SHALL set the ACL via
   `powershell -NoProfile -Command "$acl = Get-Acl -Path '<path>'; ...;
   Set-Acl -Path '<path>' -AclObject $acl"`.
4. WHEN the current ACL already matches the requested mode THE SYSTEM SHALL
   return `status=SKIPPED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 11: Set Ownership

**User Story:** As a maintenance coordinator, I want to change the owner of a
configuration source, so that ownership matches policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_ownership(source, user, group)` and the
   source does not exist THE SYSTEM SHALL raise
   `WindowsConfigFileNotFoundError`.
2. WHEN `source` is a file THE SYSTEM SHALL invoke
   `icacls '<source>' /setowner '<user>'` via the runner.
3. WHEN `source` is a registry path THE SYSTEM SHALL set the owner via
   `powershell -NoProfile -Command "$acl = Get-Acl -Path '<path>';
   $acl.SetOwner([System.Security.Principal.NTAccount]'<user>');
   Set-Acl -Path '<path>' -AclObject $acl"`.
4. WHEN the current owner already matches THE SYSTEM SHALL return
   `status=SKIPPED`.
5. WHEN `group` is supplied THE SYSTEM SHALL raise
   `WindowsConfigValidatorNotFoundError` IF the `group` concept is not
   applicable to the source type; today both file and registry paths accept a
   group-SID argument via `icacls /setowner` and the PowerShell equivalent.
   Mixing the two without a clear mapping SHALL return `status=FAILED`.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 12: Reload Configuration

**User Story:** As a maintenance coordinator, I want to trigger the service
that owns a configuration to reload, so that changes take effect.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_configuration(service)` THE SYSTEM
   SHALL invoke `powershell -NoProfile -Command "Restart-Service -Name
   '<service>' -Force"` via the runner.
2. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN the service in question is a registry-only configuration (no backing
   service) the caller supplies the literal string `"none"` AND THE SYSTEM
   SHALL return `status=SKIPPED` with `detail="no service attached; registry
   changes take effect on next read"`.

---

### Requirement 13: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all write operations to
run as Administrator, so that writes under `HKLM:\` and `%ProgramData%`
succeed.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the runner
   and file operator for every write verb.
2. WHEN performing non-Security reads THE SYSTEM SHALL NOT pass
   `elevated=True`.
3. WHEN performing reads rooted under `HKLM:\SECURITY\` THE SYSTEM SHALL pass
   `elevated=True` (contract-permitted exception).
4. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` everywhere.
