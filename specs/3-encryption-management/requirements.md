# Requirements

## Introduction

The encryption management feature provides the
`automojo-sysman-windows` package with the ability to enable /
disable BitLocker on a Windows volume, query encryption status, list
encrypted volumes, rotate the BitLocker password protector, back up
the BitLocker recovery password to a filesystem path, and unlock a
locked BitLocker volume by passing a password.  This spec implements
the encryption-management contract defined at
`automojo-sysman-common/specs/contracts/encryption-management/contract.md`
for Windows by wrapping the PowerShell `BitLocker` module's
`Enable-BitLocker`, `Disable-BitLocker`, `Get-BitLockerVolume`,
`Add-BitLockerKeyProtector`, `Remove-BitLockerKeyProtector`, and
`Unlock-BitLocker` cmdlets.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* volumes should be encrypted, *which*
passwords should be used, or *whether* a recovery-key backup should
be archived to off-host storage — it provides the capability to
*manipulate BitLocker state* reliably, idempotently, and with full
auditability.  All cmdlet invocations are executed exclusively
through the `automojo-interfaces` command-runner abstraction;
plaintext password bytes are wrapped via
`ConvertTo-SecureString -AsPlainText -Force` inside the same
PowerShell scriptblock and the recorded argv passes through a
regex-based sanitisation step that replaces every literal password
with `<redacted>` before the argv is captured on the result.

Windows Server BitLocker installation, AD-based recovery-key
escrow (`Backup-BitLockerKeyProtector` to AD), and Microsoft Key
Management Server (MBAM) integration are NOT in scope here.
TPM provisioning (`Initialize-Tpm`, `Set-TpmOwnerAuth`) is also
out of scope; this feature assumes the TPM is already provisioned
when the host's BitLocker policy requires one.

---

## Requirements

### Requirement 1: Enable BitLocker on a Volume

**User Story:** As a maintenance coordinator, I want to enable
BitLocker on a volume by passing a password, so that the volume's
contents are protected at rest.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_encryption(volume, spec)` THE
   SYSTEM SHALL dispatch a PowerShell scriptblock that calls
   `Enable-BitLocker -MountPoint '<letter>:' -EncryptionMethod
   <cipher> -PasswordProtector -Password $secure
   -RecoveryPasswordProtector -SkipHardwareTest` through the
   command runner with `elevated=True` when configured.
2. WHEN `spec.cipher` is None THE SYSTEM SHALL substitute
   `config.default_cipher` (default `XtsAes256`).
3. WHEN `spec.recovery_key_protector` is True (default) THE SYSTEM
   SHALL append `-RecoveryPasswordProtector` to the cmdlet so a
   48-digit recovery password is generated.
4. WHEN the volume's `ProtectionStatus=On` or
   `VolumeStatus=FullyEncrypted` (probed via
   `Get-BitLockerVolume`) THE SYSTEM SHALL skip execution and
   return `WindowsEncryptionActionResult(status=SKIPPED)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsEncryptionActionResult(status=DRY_RUN)`.
6. THE SYSTEM SHALL set `requires_reboot=True` on the returned
   result.
7. THE SYSTEM SHALL replace every plaintext password in the
   recorded `command` field with the sentinel `<redacted>` via the
   :func:`_sanitise_script_passwords` regex.

---

### Requirement 2: Disable BitLocker on a Volume (Destructive)

**User Story:** As a maintenance coordinator, I want a guarded path
to disable BitLocker on a volume, so that decommissioning is
auditable and accidental destruction is impossible.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_encryption(volume)` THE
   SYSTEM SHALL dispatch a PowerShell scriptblock that calls
   `Disable-BitLocker -MountPoint '<letter>:'` through the command
   runner with `elevated=True` when configured.
2. WHEN `config.allow_destructive` is False THE SYSTEM SHALL NOT
   dispatch the disable command and SHALL return
   `WindowsEncryptionActionResult(status=FAILED,
   detail="destructive_operation_requires_explicit_confirm")`.
3. WHEN the volume is already plaintext (probed via
   `Get-BitLockerVolume`) THE SYSTEM SHALL skip execution and
   return `WindowsEncryptionActionResult(status=SKIPPED)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsEncryptionActionResult(status=DRY_RUN)`.
5. THE SYSTEM SHALL set `requires_reboot=True` on the returned
   result for non-skip outcomes.

---

### Requirement 3: Get Encryption Status for a Volume

**User Story:** As a maintenance coordinator, I want to query the
BitLocker status of a single volume, so that I can branch on
encrypted / unlocked / cipher / protector fields without parsing
cmdlet output myself.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_encryption_status(volume)` THE
   SYSTEM SHALL dispatch
   `Get-BitLockerVolume -MountPoint '<letter>:' | ConvertTo-Json
   -Compress -Depth 5` through the command runner WITHOUT
   elevation.
2. THE SYSTEM SHALL parse the JSON record and return a
   `WindowsEncryptionStatus` populated with `is_encrypted`
   (True when `ProtectionStatus=On` or `VolumeStatus=FullyEncrypted`
   or `EncryptionPercentage>=100`), `is_unlocked` (True when
   `LockStatus=Unlocked`), `cipher` (`EncryptionMethod` field),
   `progress_percent` (`EncryptionPercentage` field), and
   `protector_types` (the list of `KeyProtectorType` strings from
   `KeyProtector[]`).
3. WHEN the cmdlet exits non-zero THE SYSTEM SHALL return a
   record with `is_encrypted=False` and `cipher=None`.
4. THE SYSTEM SHALL NEVER pass `elevated=True` to the runner for
   this verb.

---

### Requirement 4: List Encrypted Volumes

**User Story:** As a maintenance coordinator, I want to enumerate
every BitLocker-managed volume on the host, so that I can compare
actual encryption state against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_encrypted_volumes()` THE
   SYSTEM SHALL dispatch
   `Get-BitLockerVolume | ConvertTo-Json -Compress -Depth 5`
   through the command runner WITHOUT elevation.
2. THE SYSTEM SHALL parse the JSON array (or single-object payload
   normalised to a list) and emit one
   `WindowsEncryptedVolumeInfo` per entry whose
   `ProtectionStatus=On` or `VolumeStatus=FullyEncrypted`.
3. THE SYSTEM SHALL NEVER include plaintext volumes in the
   returned list.
4. WHEN the cmdlet exits non-zero THE SYSTEM SHALL return an empty
   list rather than raise.

---

### Requirement 5: Rotate the Password Protector

**User Story:** As a maintenance coordinator, I want to rotate the
BitLocker password protector on a volume, so that compromised
credentials can be replaced.

#### Acceptance Criteria

1. WHEN the coordinator calls `rotate_key(volume, new_passphrase)`
   THE SYSTEM SHALL dispatch a PowerShell scriptblock that:
   1. Wraps the new password via
      `ConvertTo-SecureString -AsPlainText -Force '<pw>'`.
   2. Captures the existing password protector's
      `KeyProtectorId` via `Get-BitLockerVolume`.
   3. Calls `Add-BitLockerKeyProtector -MountPoint '<vol>'
      -PasswordProtector -Password $pw`.
   4. Calls `Remove-BitLockerKeyProtector -MountPoint '<vol>'
      -KeyProtectorId <oldId>`.
2. THE SYSTEM SHALL emit the add-then-remove ordering so the volume
   never has zero password protectors mid-rotation.
3. THE SYSTEM SHALL NOT skip rotation — the new password is opaque
   to the manager.
4. THE SYSTEM SHALL replace the new password in the recorded
   `command` field with `<redacted>` via the
   :func:`_sanitise_script_passwords` regex.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsEncryptionActionResult(status=DRY_RUN)`.

---

### Requirement 6: Back Up the Recovery Password

**User Story:** As a maintenance coordinator, I want to back up the
BitLocker recovery password on a volume to a filesystem path, so
that a copy is available for recovery in the event of credential
loss.

#### Acceptance Criteria

1. WHEN the coordinator calls `backup_recovery_key(volume, path)`
   THE SYSTEM SHALL dispatch a PowerShell scriptblock that:
   1. Pulls the volume's `KeyProtector` array via
      `Get-BitLockerVolume`.
   2. Filters to `KeyProtectorType -eq 'RecoveryPassword'` and
      selects the first matching entry.
   3. Writes the entry's `RecoveryPassword` field to `path` via
      `Set-Content -Path '<path>' -Value $rk.RecoveryPassword
      -Encoding UTF8`.
2. THE SYSTEM SHALL NOT skip — backup is always dispatched.
3. THE SYSTEM SHALL NOT include the recovery password value in the
   recorded `command` field on the returned result.
4. WHEN no recovery-password protector exists on the volume THE
   SYSTEM SHALL surface PowerShell's `Write-Error` output as a
   FAILED result.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsEncryptionActionResult(status=DRY_RUN)`.

---

### Requirement 7: Unlock an Encrypted Volume

**User Story:** As a maintenance coordinator, I want to unlock a
locked BitLocker volume by password, so that the plaintext
filesystem becomes available.

#### Acceptance Criteria

1. WHEN the coordinator calls `unlock_volume(volume, passphrase)`
   THE SYSTEM SHALL dispatch a PowerShell scriptblock that wraps
   the password via
   `ConvertTo-SecureString -AsPlainText -Force '<pw>'` and calls
   `Unlock-BitLocker -MountPoint '<letter>:' -Password $pw`
   through the command runner with `elevated=True` when
   configured.
2. WHEN the volume is already unlocked (probed via
   `Get-BitLockerVolume`'s `LockStatus` field) THE SYSTEM SHALL
   skip execution and return
   `WindowsEncryptionActionResult(status=SKIPPED)`.
3. THE SYSTEM SHALL replace the password in the recorded `command`
   field with `<redacted>`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsEncryptionActionResult(status=DRY_RUN)`.

---

### Requirement 8: Elevation Policy

**User Story:** As a maintenance coordinator, I want every write
verb to honour the configured elevation flag without having to
re-pass it to each call, so that infrastructure code does not
duplicate UAC-style routing.

#### Acceptance Criteria

1. WHEN `config.elevated` is True THE SYSTEM SHALL pass
   `elevated=True` to the runner for every write verb
   (`enable_encryption`, `disable_encryption`, `rotate_key`,
   `backup_recovery_key`, `unlock_volume`).
2. WHEN performing read-only operations
   (`get_encryption_status`, `list_encrypted_volumes`, all
   idempotency probes) THE SYSTEM SHALL NEVER pass
   `elevated=True`.
3. WHEN `config.elevated` is False THE SYSTEM SHALL NEVER pass
   `elevated=True`.

---

### Requirement 9: Password Hygiene on Argv

**User Story:** As a security-conscious operator, I want to be
certain that no plaintext password ever appears in a recorded
argv, log line, or audit capture from this manager, so that
secrets do not leak through ordinary observability.

#### Acceptance Criteria

1. THE SYSTEM SHALL build every PowerShell scriptblock such that
   plaintext passwords appear ONLY inside
   `ConvertTo-SecureString -AsPlainText -Force '<pw>'` literals.
2. THE SYSTEM SHALL apply :func:`_sanitise_script_passwords` to
   every scriptblock before recording the argv on a result; the
   regex replaces every embedded password with the literal
   `<redacted>`.
3. THE SYSTEM SHALL NEVER include a plaintext password value in
   the `command` field on any returned
   `WindowsEncryptionActionResult`.

---

### Requirement 10: Destructive Operation Guard

**User Story:** As a security-conscious operator, I want
destructive encryption operations to be opt-in, so that an
accidental call to a destructive verb cannot trigger a multi-hour
decrypt pass.

#### Acceptance Criteria

1. THE SYSTEM SHALL treat `disable_encryption` as a destructive
   verb because `Disable-BitLocker` triggers a multi-hour decrypt
   pass that rewrites every block.
2. WHEN `config.allow_destructive` is False AND the destructive
   verb is invoked AND the volume is currently encrypted THE
   SYSTEM SHALL return
   `WindowsEncryptionActionResult(status=FAILED,
   detail="destructive_operation_requires_explicit_confirm")`
   without dispatching the destructive command.
3. WHEN `config.allow_destructive` is True THE SYSTEM SHALL
   dispatch the destructive command after verifying the
   idempotency guard.

---

### Requirement 11: Observability

**User Story:** As a maintenance coordinator, I want the manager's
result objects to carry enough metadata to drive cross-OS
reporting without OS-specific branching, so that operator-facing
dashboards remain shape-stable.

#### Acceptance Criteria

1. THE SYSTEM SHALL populate every
   `WindowsEncryptionActionResult` with `volume`, `command`, and
   `status`.
2. THE SYSTEM SHALL set `requires_reboot=True` on every result
   from `enable_encryption` and `disable_encryption` non-skip
   outcomes.
3. THE SYSTEM SHALL surface the runner's exit code, stdout, and
   stderr on every result whose status is `SUCCESS` or `FAILED`.
