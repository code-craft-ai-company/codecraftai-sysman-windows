# Design

This spec implements the encryption-management contract defined at
`automojo-sysman-common/specs/contracts/encryption-management/contract.md`
for Windows using BitLocker via the PowerShell BitLocker module.

## Overview

The encryption management feature is implemented as a single module,
`automojo.sysman.windows.encryptionmanagement`, that exposes a
`WindowsEncryptionManager` class.  The coordinator instantiates
`WindowsEncryptionManager` and calls its methods to perform discrete
BitLocker actions: enable / disable encryption on a volume, query
encryption status, list encrypted volumes, rotate the BitLocker
password protector, back up the BitLocker recovery password to a
filesystem path, and unlock a locked BitLocker volume.

This feature wraps the PowerShell `BitLocker` module's
`Enable-BitLocker`, `Disable-BitLocker`, `Get-BitLockerVolume`,
`Add-BitLockerKeyProtector`, `Remove-BitLockerKeyProtector`, and
`Unlock-BitLocker` cmdlets.  Every verb is dispatched as a single
PowerShell scriptblock through the injected `ICommandRunner` so the
manager works identically under local execution and SSH
(WinRM-tunneled) execution.

This feature **complements — and does not duplicate** —
`storage-management` (volume creation), `filesystem-management`
(`mkfs` / mount), `service-management` (the `BDESVC` BitLocker
service), and `package-management` (the `BitLocker` Windows feature
itself).  The `WindowsEncryptionManager` brings BitLocker state into
the desired shape; everything outside that verb set lives in another
feature.

All command execution is delegated to a command runner implementing
the `ICommandRunner` interface.  The `WindowsEncryptionManager` never
directly calls `subprocess`, shell commands, or `open()`; it only
constructs argument lists and hands them to the injected runner.
This keeps business logic separate from execution and makes the
feature fully testable via mock dependencies without administrator
access or a live BitLocker subsystem.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH / WinRM (via
`SSHCommandRunner`).  Under SSH the coordinator can track
reliability metrics and apply execution aspects (persistence, retry,
timeout) without any changes to the `WindowsEncryptionManager` code.

**See Also:**
[Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.  The cross-OS contract is at
`automojo-sysman-common/specs/contracts/encryption-management/contract.md`.
The Ubuntu sibling is at
`automojo-sysman-linux-ubuntu/specs/3-encryption-management/`; the
macOS sibling is at
`automojo-sysman-macos/specs/3-encryption-management/`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH/WinRM)                 │
│  - Configures aspects (optional)                            │
│  - Supplies passphrase / recovery-key bytes                 │
│  - Sets allow_destructive=True only when destructive intent │
│    is explicit                                              │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsEncryptionManager                         │
│  automojo.sysman.windows.encryptionmanagement               │
│                                                             │
│  enable_encryption()       disable_encryption()             │
│  get_encryption_status()                                    │
│  list_encrypted_volumes()                                   │
│  rotate_key()              backup_recovery_key()            │
│  unlock_volume()                                            │
│                                                             │
│  Internal probes / builders:                                │
│    _is_encrypted()    Get-BitLockerVolume probe             │
│    _is_unlocked()     LockStatus probe                      │
│    _build_enable_script()                                   │
│    _build_rotate_script()                                   │
│    _build_backup_script()                                   │
│    _build_unlock_script()                                   │
│    _sanitise_command_passwords()                            │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner.run(args, elevated=, stdin=)
          │   args = ["powershell.exe", "-NoProfile",
          │           "-NonInteractive", "-Command", "<script>"]
          ▼
┌─────────────────────────────────────────────────────────────┐
│               LocalCommandRunner / SSHCommandRunner         │
└─────────┬───────────────────────────────────────────────────┘
          │ optional aspect composition
          ▼
┌─────────────────────────────────────────────────────────────┐
│   PersistenceAspect  RetryAspect  TimeoutAspect             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsEncryptionManager`

**Location:**
`source/packages/automojo/sysman/windows/encryptionmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `enable_encryption` | `(volume, spec) → WindowsEncryptionActionResult` | `Enable-BitLocker -MountPoint '<vol>' -EncryptionMethod <cipher> -PasswordProtector -Password $pw -RecoveryPasswordProtector -SkipHardwareTest`; idempotent via `Get-BitLockerVolume`'s `ProtectionStatus` / `VolumeStatus` |
| `disable_encryption` | `(volume) → WindowsEncryptionActionResult` | `Disable-BitLocker -MountPoint '<vol>'`; DESTRUCTIVE; gated by `config.allow_destructive`; idempotent via `Get-BitLockerVolume` |
| `get_encryption_status` | `(volume) → WindowsEncryptionStatus` | `Get-BitLockerVolume -MountPoint '<vol>' \| ConvertTo-Json -Compress -Depth 5`; pure-Python parse |
| `list_encrypted_volumes` | `() → list[WindowsEncryptedVolumeInfo]` | `Get-BitLockerVolume \| ConvertTo-Json -Compress -Depth 5`; emits one row per encrypted volume |
| `rotate_key` | `(volume, new_passphrase) → WindowsEncryptionActionResult` | Two-step add-then-remove: `Add-BitLockerKeyProtector -PasswordProtector -Password $pw` then `Remove-BitLockerKeyProtector -KeyProtectorId <oldId>`; unconditional |
| `backup_recovery_key` | `(volume, path) → WindowsEncryptionActionResult` | Capture `(Get-BitLockerVolume).KeyProtector` filtered to `KeyProtectorType -eq 'RecoveryPassword'`, then `Set-Content -Path '<path>' -Value $rk.RecoveryPassword -Encoding UTF8`; unconditional |
| `unlock_volume` | `(volume, passphrase) → WindowsEncryptionActionResult` | `Unlock-BitLocker -MountPoint '<vol>' -Password $pw`; idempotent via `Get-BitLockerVolume`'s `LockStatus` field |

### `WindowsEncryptionManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no command is dispatched; the would-have-been argv is recorded |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` to the runner |
| `powershell_executable` | `str` | `"powershell.exe"` | Path to the PowerShell executable.  Set to `"pwsh.exe"` to target PowerShell 7+ |
| `allow_destructive` | `bool` | `False` | When `True`, `disable_encryption` is permitted to dispatch `Disable-BitLocker` |
| `default_cipher` | `str` | `"XtsAes256"` | Default `-EncryptionMethod` token passed to `Enable-BitLocker` when the spec does not override |

---

## Data Models

### `WindowsEncryptionActionStatus`

Aliased to the shared `automojo.sysman.windows.featurebase.ActionStatus`:

| Value | Meaning |
|---|---|
| `SUCCESS` | The cmdlet ran and exited 0. |
| `FAILED` | The cmdlet ran and exited non-zero, or a safety rail blocked dispatch. |
| `SKIPPED` | The idempotency guard determined no action was needed. |
| `DRY_RUN` | Dry-run mode short-circuited the cmdlet. |

### `WindowsEncryptionActionResult`

Inherits `status` / `detail` / `exit_code` / `stdout` / `stderr` /
`command` from `FeatureActionResult`.

| Field | Type | Description |
|---|---|---|
| `volume` | `Optional[str]` | Volume identifier (drive letter `"C:"`, GPT volume path) the operation targeted. |
| `device` | `Optional[str]` | Underlying physical device path when resolvable, else `None`.  Today the manager does not resolve drive letters to device paths because BitLocker addresses volumes by mount point. |
| `requires_reboot` | `bool` | `True` for `enable_encryption` / `disable_encryption` non-skip outcomes; `False` for `unlock_volume`, `rotate_key`, and `backup_recovery_key`. |

### `WindowsEncryptionSpec`

| Field | Type | Default | Description |
|---|---|---|---|
| `cipher` | `Optional[str]` | `None` | `-EncryptionMethod` value.  `None` falls back to `WindowsEncryptionManagerConfig.default_cipher`.  Recognised values include `XtsAes128`, `XtsAes256`, `Aes128`, `Aes256`. |
| `key_size` | `Optional[int]` | `None` | Reserved.  `Enable-BitLocker` does not accept an explicit key size; the cipher token implies it. |
| `passphrase` | `Optional[str]` | `None` | Plaintext password used to wrap the BitLocker volume key with a `PasswordProtector`.  When set, the manager emits `-PasswordProtector -Password $secure`. |
| `recovery_key_protector` | `bool` | `True` | When `True`, the manager emits `-RecoveryPasswordProtector` so a 48-digit recovery password is generated alongside the password protector. |
| `mount_point` | `Optional[str]` | `None` | Reserved.  BitLocker addresses volumes via `-MountPoint <letter>:` already; this field exists for cross-OS shape parity. |

### `WindowsEncryptionStatus`

| Field | Type | Description |
|---|---|---|
| `volume` | `str` | Volume identifier (the mount point as reported by `Get-BitLockerVolume.MountPoint`). |
| `is_encrypted` | `bool` | `True` when `ProtectionStatus=On` or `VolumeStatus=FullyEncrypted` or `EncryptionPercentage>=100`. |
| `is_unlocked` | `bool` | `True` when `LockStatus=Unlocked` (or numeric `0`). |
| `cipher` | `Optional[str]` | `EncryptionMethod` field. |
| `key_size` | `Optional[int]` | Reserved; BitLocker does not surface an explicit key-size value distinct from the method. |
| `progress_percent` | `Optional[float]` | `EncryptionPercentage` reported by the cmdlet.  Useful while a conversion pass is in flight. |
| `protector_types` | `list[str]` | `KeyProtectorType` strings extracted from the `KeyProtector[]` array, e.g. `["Tpm", "RecoveryPassword"]`. |

### `WindowsEncryptedVolumeInfo`

| Field | Type | Description |
|---|---|---|
| `volume` | `str` | Mount point (`"C:"`). |
| `device` | `Optional[str]` | Underlying physical device path when resolvable, else `None`. |
| `mount_point` | `Optional[str]` | Mount point copied from `volume` for cross-OS shape parity. |
| `is_encrypted` | `bool` | Always `True` (the list is filtered to encrypted volumes). |
| `is_unlocked` | `bool` | `True` when `LockStatus=Unlocked`. |
| `cipher` | `Optional[str]` | `EncryptionMethod` field. |
| `key_size` | `Optional[int]` | Reserved. |
| `progress_percent` | `Optional[float]` | `EncryptionPercentage` field. |
| `protector_types` | `list[str]` | `KeyProtectorType` strings from `KeyProtector[]`. |

### `WindowsEncryptionQueryError`

Raised when an encryption read fails for a reason other than the
target simply being absent from the host's BitLocker surface.

| Constructor parameter | Type | Description |
|---|---|---|
| `command` | `list` | The argv that failed. |
| `exit_code` | `Optional[int]` | The runner's exit code. |
| `stderr` | `str` | The runner's captured stderr. |

The exception's message is a multi-line block built from a list of
lines and joined with `os.linesep`, in keeping with the project's
multiline-error convention.  The current source defines this
exception type but does NOT raise it from the read paths today —
the read paths fall back to empty / plain records on failure.  See
the Error Handling section below for the gap.

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

Every PowerShell scriptblock dispatched by the manager is built into
the canonical argv shape:

```
["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "<script>"]
```

`-NoProfile` prevents `$PROFILE` from mutating cmdlet behaviour.
`-NonInteractive` ensures PowerShell never blocks waiting for
interactive input on a runner that has no controlling terminal
(critical for the SSH / WinRM execution path).  `-Command` accepts
a single scriptblock string that the manager builds in pure Python.

The first element of the argv (`powershell.exe`) is taken from
`WindowsEncryptionManagerConfig.powershell_executable`; the
`-NoProfile`, `-NonInteractive`, `-Command` tokens are module-level
constants.

---

## Command Mappings

All commands are dispatched through
`["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "<script>"]`.
The script text is single-line, semicolon-joined where multiple
statements are required, and uses single-quoted PowerShell strings
for every interpolated value.  PowerShell single quotes are escaped
by doubling (handled by `_ps_escape_single_quoted`).

### Write Verbs

| Operation | PowerShell scriptblock |
|---|---|
| Enable encryption (with password + recovery) | `$pw = ConvertTo-SecureString -AsPlainText -Force '<pw>'; Enable-BitLocker -MountPoint 'C:' -EncryptionMethod XtsAes256 -PasswordProtector -Password $pw -RecoveryPasswordProtector -SkipHardwareTest` |
| Enable encryption (no password) | `Enable-BitLocker -MountPoint 'C:' -EncryptionMethod XtsAes256 -RecoveryPasswordProtector -SkipHardwareTest` |
| Disable encryption | `Disable-BitLocker -MountPoint 'C:'` |
| Rotate key (add then remove) | `$pw = ConvertTo-SecureString -AsPlainText -Force '<pw>'; $old = (Get-BitLockerVolume -MountPoint 'C:').KeyProtector \| Where-Object KeyProtectorType -eq 'Password'; Add-BitLockerKeyProtector -MountPoint 'C:' -PasswordProtector -Password $pw; if ($old) { Remove-BitLockerKeyProtector -MountPoint 'C:' -KeyProtectorId $old.KeyProtectorId }` |
| Backup recovery key to file | `$rk = (Get-BitLockerVolume -MountPoint 'C:').KeyProtector \| Where-Object KeyProtectorType -eq 'RecoveryPassword' \| Select-Object -First 1; if ($rk) { Set-Content -Path 'C:\BitLockerKeys\C-recovery.txt' -Value $rk.RecoveryPassword -Encoding UTF8 } else { Write-Error 'No RecoveryPassword protector present.' }` |
| Unlock volume | `$pw = ConvertTo-SecureString -AsPlainText -Force '<pw>'; Unlock-BitLocker -MountPoint 'C:' -Password $pw` |

`-SkipHardwareTest` is always passed to `Enable-BitLocker` so the
cmdlet does not request a reboot for the BitLocker hardware
self-test (the self-test requires user interaction at the Recovery
screen on the next boot).

`Set-Content` is preferred over `Out-File` for the recovery-key
backup because `Set-Content -Encoding UTF8` writes plain UTF-8
without a byte-order mark; `Out-File` defaults to UTF-16-with-BOM
which many recovery-key restore tools do not handle.

All write commands pass `elevated=config.elevated` to the runner.

### Read Verbs (no elevation)

| Operation | PowerShell scriptblock |
|---|---|
| Get one volume's status | `Get-BitLockerVolume -MountPoint 'C:' \| ConvertTo-Json -Compress -Depth 5` |
| List all volumes | `Get-BitLockerVolume \| ConvertTo-Json -Compress -Depth 5` |

The same probe scriptblock is used internally as the idempotency
probe by `enable_encryption`, `disable_encryption`, and
`unlock_volume`.  Reads NEVER pass `elevated=True`, regardless of
the `config.elevated` setting.

---

## Output Parsing

### `Get-BitLockerVolume | ConvertTo-Json -Compress -Depth 5`

`ConvertTo-Json` emits a JSON array when the source pipeline
produced multiple items and a bare object when it produced exactly
one item.  The module-level helper `_normalize_to_list` smooths both
shapes into a list before the per-record parser runs.

A representative single-volume response (after pretty-printing — the
cmdlet emits this on one line because of `-Compress`):

```json
{
  "ComputerName": "WIN-HOST",
  "MountPoint": "C:",
  "VolumeStatus": "FullyEncrypted",
  "ProtectionStatus": "On",
  "LockStatus": "Unlocked",
  "EncryptionMethod": "XtsAes256",
  "EncryptionPercentage": 100,
  "KeyProtector": [
    {"KeyProtectorType": "Tpm", "KeyProtectorId": "{ABC123}"},
    {"KeyProtectorType": "RecoveryPassword", "KeyProtectorId": "{DEF456}"}
  ]
}
```

A representative multi-volume response:

```json
[
  {"MountPoint":"C:","ProtectionStatus":"On","VolumeStatus":"FullyEncrypted",
   "EncryptionPercentage":100,"LockStatus":"Unlocked","EncryptionMethod":"XtsAes256",
   "KeyProtector":[{"KeyProtectorType":"Tpm","KeyProtectorId":"{ABC}"}]},
  {"MountPoint":"D:","ProtectionStatus":"Off","VolumeStatus":"FullyDecrypted",
   "EncryptionPercentage":0,"LockStatus":"Unlocked","EncryptionMethod":"None",
   "KeyProtector":[]}
]
```

### Field-to-`WindowsEncryptionStatus` Mapping

| BitLocker JSON field | `WindowsEncryptionStatus` field | Mapping |
|---|---|---|
| `MountPoint` | `volume` | Used as-is when the string is non-empty; else falls back to the caller-supplied `volume` argument. |
| `ProtectionStatus` | (drives) `is_encrypted` | `True` when string `"On"` (case-insensitive) or numeric `1`. |
| `VolumeStatus` | (drives) `is_encrypted` | `True` when string `"FullyEncrypted"` (case-insensitive) or numeric `2`. |
| `EncryptionPercentage` | (drives) `is_encrypted`; `progress_percent` | `is_encrypted=True` when `>=100`; `progress_percent` is `float(value)` when numeric. |
| `LockStatus` | `is_unlocked` | `True` when string `"Unlocked"` (case-insensitive) or numeric `0`. |
| `EncryptionMethod` | `cipher` | Used as-is when the string is non-empty; else `None`. |
| `KeyProtector[].KeyProtectorType` | `protector_types[i]` | Each entry's type is collected (string preserved; numeric coerced via `str()`). |

The parser recognises both the modern PowerShell shape (string
enums for `ProtectionStatus`, `VolumeStatus`, `LockStatus`,
`KeyProtectorType`) and the legacy shape (numeric enums on older
hosts).  The numeric mapping follows Microsoft's published
`BitLockerVolumeStatus` / `BitLockerProtectionStatus` /
`BitLockerLockStatus` / `BitLockerKeyProtectorType` enums.

The `_is_record_encrypted` helper is the canonical OR-of-three
check used by `get_encryption_status`, `list_encrypted_volumes`,
and the `_is_encrypted` idempotency probe.  Keeping the predicate
in one place ensures the manager's idempotency guards and read
verbs agree on what "encrypted" means.

The `_extract_protector_types` helper iterates `record["KeyProtector"]`
(when it is a list of dicts), pulls out each entry's
`KeyProtectorType`, and tolerates both string and numeric shapes.

### Failure Surface

When the underlying `Get-BitLockerVolume` cmdlet exits non-zero
(typical when the drive letter does not exist or BitLocker is not
installed), the read verbs return:

- `get_encryption_status` → `WindowsEncryptionStatus(volume=<arg>, is_encrypted=False, is_unlocked=False, cipher=None, ...)`
- `list_encrypted_volumes` → `[]`

Read verbs do NOT raise on cmdlet failure today; the
`WindowsEncryptionQueryError` exception type is defined but unused
in the read paths.  See Error Handling for the documented gap.

---

## Idempotency Guards

| Method | State checked | Guard outcome |
|---|---|---|
| `enable_encryption` | `_is_encrypted(volume)` returns `True` (volume already has `ProtectionStatus=On` or `VolumeStatus=FullyEncrypted` or `EncryptionPercentage>=100`) | `SKIPPED` with detail `"volume <vol> is already encrypted"` |
| `disable_encryption` | `_is_encrypted(volume)` returns `False` (volume already plaintext) | `SKIPPED` with detail `"volume <vol> is not encrypted"` |
| `rotate_key` | None | Always dispatched (the new password is opaque to the manager) |
| `backup_recovery_key` | None | Always dispatched |
| `unlock_volume` | `_is_unlocked(volume)` returns `True` (`LockStatus=Unlocked`) | `SKIPPED` with detail `"volume <vol> is already unlocked"` |

Probes use the same `Get-BitLockerVolume -MountPoint '<vol>' |
ConvertTo-Json -Compress -Depth 5` script as the public read verbs
and never elevate.  When the probe cmdlet exits non-zero or returns
empty, the helpers `_is_encrypted` / `_is_unlocked` both default to
`False` — the safe direction for the guard (proceed with the write
rather than wrongly skip).

The idempotency guard for `enable_encryption` is intentionally
state-only — it does NOT compare the existing cipher / key-size /
protector list against the spec.  A coordinator that re-runs
`enable_encryption` against an already-encrypted volume with a
different cipher gets a SKIPPED rather than a re-format; this is
the safe direction because re-formatting would destroy the existing
volume key.

---

## Destructive-Operation Guard

`disable_encryption` is treated as destructive because
`Disable-BitLocker` triggers a multi-hour decrypt pass that rewrites
every block on the volume.  The manager refuses to dispatch
`Disable-BitLocker` unless `WindowsEncryptionManagerConfig.allow_destructive=True`.

The guard fires AFTER the dry-run check and AFTER the idempotency
check (a volume that is already plaintext returns SKIPPED, not
FAILED-on-destructive).  When the guard fires the manager returns:

```python
WindowsEncryptionActionResult(
    status=FAILED,
    detail="destructive_operation_requires_explicit_confirm",
    stderr=(
        "disable_encryption is destructive; set "
        "config.allow_destructive=True to proceed"
    ),
    command=<the would-have-been argv>,
    volume=<vol>,
    requires_reboot=False,
)
```

The literal detail string `"destructive_operation_requires_explicit_confirm"`
is exposed as the module-level constant `_DESTRUCTIVE_GUARD_DETAIL`
so tests and downstream coordinators can match against it without
duplicating the string.

The guard mirrors the identical pattern in the Ubuntu and macOS
encryption managers and the Wave-2.1 storage-management
`delete_pool` / `delete_volume` verbs.  Coordinators that drive a
"factory-reset" workflow are expected to set the flag in their own
config layer; the default-False guard prevents accidental data loss
during routine reconfiguration.

---

## Passphrase Redaction

BitLocker is unique among the Wave-2.2 encryption managers in that
its native CLI does NOT accept passphrase bytes on stdin.
`Enable-BitLocker -Password` and `Unlock-BitLocker -Password`
require a `[SecureString]` value, which in PowerShell is
constructed only by an in-process call such as
`ConvertTo-SecureString -AsPlainText -Force '<pw>'`.  The manager
therefore embeds the plaintext password as a single-quoted literal
INSIDE the PowerShell scriptblock that `-Command` consumes.

The plaintext password value never leaves the runner-bound argv
list.  But the same argv is recorded on the
`WindowsEncryptionActionResult.command` field for audit / replay /
dashboard purposes — and a recorded argv that carries the
plaintext password is a credential leak.  To close the gap, the
manager runs every recorded argv through a redaction pass before
returning the result.

The redaction is performed by the module-level helper
`_sanitise_script_passwords`:

```python
_SECURE_STRING_PATTERN = re.compile(
    r"ConvertTo-SecureString\s+-AsPlainText\s+-Force\s+'((?:[^']|'')*)'",
)

def _sanitise_script_passwords(*, script: str) -> str:
    rtnval = _SECURE_STRING_PATTERN.sub(
        "ConvertTo-SecureString -AsPlainText -Force '{}'".format(_REDACTED),
        script,
    )
    return rtnval
```

The regex matches the `ConvertTo-SecureString -AsPlainText -Force
'<pw>'` literal (tolerating PowerShell's `''`-style single-quote
escaping inside the literal) and replaces the captured password
with the sentinel `<redacted>`.  The wrapper is preserved so audit
reviewers can still see exactly which cmdlet was invoked and that
a SecureString conversion took place.

The instance-level helper
`WindowsEncryptionManager._sanitise_command_passwords` runs the
sanitiser over the LAST element of an argv list (the scriptblock
that follows `-Command`), returning a new argv list with that
element redacted.  Every public verb that may include a plaintext
password in its scriptblock — `enable_encryption`, `rotate_key`,
`unlock_volume` — passes the sanitised argv as the recorded
`command` while passing the unsanitised argv as the value
dispatched to the runner.

`_sanitise_script_passwords` mirrors verbatim the redaction
contract used by `automojo.sysman.windows.usergroupmanagement`'s
`_SECURE_STRING_PATTERN`.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `WindowsEncryptionQueryError` | Defined for read failures; multi-line message built from a list with `os.linesep.join` carrying `COMMAND` / `EXIT_CODE` / `STDERR` lines.  Currently NOT raised by the implemented read paths — they fall back to empty / plain records.  Reserved for the strict-read mode follow-up. |

### Failure Surfaces

- **Write verbs**: a non-zero runner exit emits
  `WindowsEncryptionActionResult(status=FAILED, exit_code=<rc>,
  stdout=<so>, stderr=<se>, command=<sanitised>, volume=<vol>,
  requires_reboot=<bool>)`.  The runner's `RunResult` fields are
  surfaced verbatim.
- **Destructive guard**: `disable_encryption` without
  `allow_destructive` emits FAILED with
  `detail=_DESTRUCTIVE_GUARD_DETAIL` and a synthesized `stderr`
  explaining the configuration toggle.
- **Read verbs**: a non-zero probe exit returns a stub
  `WindowsEncryptionStatus(is_encrypted=False, is_unlocked=False,
  cipher=None, ...)` (single-volume read) or an empty list (list
  read).  Read verbs do NOT raise today.

The contract-mandated `EncryptionVolumeNotFoundError` and
`EncryptionCredentialError` types are not implemented in this
surface; they are tracked as Spec-vs-Contract Drift below.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true
across all valid executions of a system — essentially, a formal
statement about what the system should do.  Properties serve as the
bridge between human-readable specifications and machine-verifiable
correctness guarantees.*

### Property 1: Enable Command Construction

*For any* `(volume, spec)` and resolved `cipher`, `enable_encryption` should dispatch a PowerShell scriptblock containing `Enable-BitLocker -MountPoint '<vol>' -EncryptionMethod <cipher>` followed by `-PasswordProtector -Password $pw` when `spec.passphrase` is set, `-RecoveryPasswordProtector` when `spec.recovery_key_protector=True`, and always `-SkipHardwareTest`.

**Validates: Requirements 1.1, 1.2, 1.3**

### Property 2: Default-Cipher Fallback

*For any* `spec` whose `cipher` field is `None`, `enable_encryption` should substitute `config.default_cipher` (default `"XtsAes256"`).

**Validates: Requirements 1.2**

### Property 3: Enable Idempotency

*For any* `volume` whose `Get-BitLockerVolume` record reports `ProtectionStatus=On` or `VolumeStatus=FullyEncrypted`, `enable_encryption` should return `SKIPPED` without dispatching `Enable-BitLocker`.

**Validates: Requirements 1.4**

### Property 4: Enable Reboot Flag

*For any* successful, dry-run, or failed-non-skip outcome of `enable_encryption`, the returned result should carry `requires_reboot=True`.

**Validates: Requirements 1.6**

### Property 5: Disable Command Construction

*For any* `volume`, `disable_encryption` should dispatch a PowerShell scriptblock containing `Disable-BitLocker -MountPoint '<vol>'`.

**Validates: Requirements 2.1**

### Property 6: Destructive Guard Fires by Default

*For any* call to `disable_encryption` against an encrypted volume with `config.allow_destructive=False` (default), the manager should return `FAILED` with `detail="destructive_operation_requires_explicit_confirm"` and should NOT invoke the runner with `Disable-BitLocker`.

**Validates: Requirements 2.2, 10.1, 10.2**

### Property 7: Destructive Guard Releases on Opt-In

*For any* call to `disable_encryption` against an encrypted volume with `config.allow_destructive=True`, the manager should dispatch `Disable-BitLocker` after the idempotency guard clears.

**Validates: Requirements 10.3**

### Property 8: Disable Idempotency

*For any* `volume` whose `Get-BitLockerVolume` record reports the volume already plaintext, `disable_encryption` should return `SKIPPED` without dispatching `Disable-BitLocker` or running the destructive guard.

**Validates: Requirements 2.3**

### Property 9: Status Read Command Construction

*For any* `volume`, `get_encryption_status` should dispatch `Get-BitLockerVolume -MountPoint '<vol>' | ConvertTo-Json -Compress -Depth 5` without elevation.

**Validates: Requirements 3.1, 3.4**

### Property 10: Status Field Population

*For any* successful `Get-BitLockerVolume` JSON record, `get_encryption_status` should populate `is_encrypted` from the `ProtectionStatus` / `VolumeStatus` / `EncryptionPercentage` triple, `is_unlocked` from `LockStatus`, `cipher` from `EncryptionMethod`, `progress_percent` from `EncryptionPercentage`, and `protector_types` from `KeyProtector[].KeyProtectorType`.

**Validates: Requirements 3.2**

### Property 11: Status Read Failure Surface

*For any* `Get-BitLockerVolume` cmdlet exit-code-non-zero, `get_encryption_status` should return a `WindowsEncryptionStatus` with `is_encrypted=False`, `is_unlocked=False`, and `cipher=None`.

**Validates: Requirements 3.3**

### Property 12: List Command Construction

*For any* call to `list_encrypted_volumes`, the manager should dispatch `Get-BitLockerVolume | ConvertTo-Json -Compress -Depth 5` without elevation.

**Validates: Requirements 4.1**

### Property 13: List Filters Plaintext Volumes

*For any* JSON record whose `ProtectionStatus` is not `On` and whose `VolumeStatus` is not `FullyEncrypted` and whose `EncryptionPercentage` is below 100, `list_encrypted_volumes` should NOT emit a `WindowsEncryptedVolumeInfo` for that record.

**Validates: Requirements 4.2, 4.3**

### Property 14: List Tolerates Cmdlet Failure

*For any* `Get-BitLockerVolume` cmdlet exit-code-non-zero in `list_encrypted_volumes`, the method should return an empty list rather than raise.

**Validates: Requirements 4.4**

### Property 15: List Tolerates Single-Object Payload

*For any* `Get-BitLockerVolume` JSON output that is a bare object rather than an array (PowerShell's single-item collapse), `list_encrypted_volumes` should fold it into a one-element list before iterating.

**Validates: Requirements 4.2**

### Property 16: Rotate Command Construction

*For any* `(volume, new_passphrase)`, `rotate_key` should dispatch a single PowerShell scriptblock that wraps the new password via `ConvertTo-SecureString`, captures the existing password protector via `Get-BitLockerVolume`, calls `Add-BitLockerKeyProtector -PasswordProtector` with the new password, and then calls `Remove-BitLockerKeyProtector -KeyProtectorId <oldId>`.

**Validates: Requirements 5.1**

### Property 17: Rotate Add-Then-Remove Ordering

*For any* `rotate_key` scriptblock, the literal `Add-BitLockerKeyProtector` should appear in the scriptblock text BEFORE `Remove-BitLockerKeyProtector`, so the volume never has zero password protectors mid-rotation.

**Validates: Requirements 5.2**

### Property 18: Backup Command Construction

*For any* `(volume, path)`, `backup_recovery_key` should dispatch a PowerShell scriptblock that filters `(Get-BitLockerVolume).KeyProtector` to `KeyProtectorType -eq 'RecoveryPassword'` and writes the captured `RecoveryPassword` to `<path>` via `Set-Content -Encoding UTF8`.

**Validates: Requirements 6.1, 6.3**

### Property 19: Backup Surfaces Missing Protector

*For any* volume whose `Get-BitLockerVolume.KeyProtector` array contains no `RecoveryPassword` entry, `backup_recovery_key` should produce a non-zero runner exit (driven by PowerShell's `Write-Error`) and surface a `FAILED` result.

**Validates: Requirements 6.4**

### Property 20: Unlock Command Construction

*For any* `(volume, passphrase)`, `unlock_volume` should dispatch a PowerShell scriptblock that wraps the password via `ConvertTo-SecureString` and calls `Unlock-BitLocker -MountPoint '<vol>' -Password $pw`.

**Validates: Requirements 7.1**

### Property 21: Unlock Idempotency

*For any* `volume` whose `Get-BitLockerVolume.LockStatus` reports `Unlocked`, `unlock_volume` should return `SKIPPED` without dispatching `Unlock-BitLocker`.

**Validates: Requirements 7.2**

### Property 22: Passphrase Redaction in Recorded Argv

*For any* write verb whose dispatched scriptblock contains a `ConvertTo-SecureString -AsPlainText -Force '<pw>'` literal, the `command` field on the returned `WindowsEncryptionActionResult` should carry a copy of that scriptblock with the password value replaced by the literal `<redacted>`.  The plaintext password should NEVER appear in the recorded argv.

**Validates: Requirements 1.7, 5.4, 7.3, 9.1, 9.2, 9.3**

### Property 23: Writes Elevate per Config

*For any* write verb (`enable_encryption`, `disable_encryption`, `rotate_key`, `backup_recovery_key`, `unlock_volume`) called with `config.elevated=True`, the runner should be invoked with `elevated=True`.  When `config.elevated=False`, the runner should NEVER be invoked with `elevated=True`.

**Validates: Requirements 8.1, 8.3**

### Property 24: Reads Never Elevate

*For any* read verb (`get_encryption_status`, `list_encrypted_volumes`) and any idempotency probe (`_is_encrypted`, `_is_unlocked`), the runner should NEVER be invoked with `elevated=True`, regardless of the `config.elevated` setting.

**Validates: Requirements 8.2**

### Property 25: Dry-Run Short-Circuit

*For any* write verb called with `config.dry_run=True`, the runner should NEVER be invoked, and the returned result should carry `status=DRY_RUN` and a `command` field populated with the would-have-been (sanitised) argv.

**Validates: Requirements 1.5, 2.4, 5.5, 6.5, 7.4**

### Property 26: Failed Command Surfaces Stderr

*For any* runner exit-code-non-zero outcome from a write verb, the returned `WindowsEncryptionActionResult` should carry `status=FAILED`, the runner's `exit_code`, and the runner's `stderr`.

**Validates: Requirements 11.3**

### Property 27: PowerShell Argv Shape

*For any* dispatched cmdlet, the argv passed to the runner should begin with `[<powershell_executable>, "-NoProfile", "-NonInteractive", "-Command", <script>]` so that user profile loads and interactive prompts are suppressed.

**Validates: Requirements 1.1, 2.1, 3.1, 4.1, 5.1, 6.1, 7.1**

### Property 28: Result Volume Field

*For any* returned `WindowsEncryptionActionResult`, the `volume` field should match the volume identifier that was supplied to the verb.

**Validates: Requirements 11.1**

---

## Module Layout

```
source/packages/automojo/sysman/windows/
├── __init__.py
├── featurebase.py                ← shared ActionStatus / FeatureConfig / BaseFeatureManager
└── encryptionmanagement.py       ← WindowsEncryptionManager + dataclasses + parsers + redaction

source/testroots/testbase/
└── automojo/tests/sysman/windows/
    └── test_encryptionmanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.windows.encryptionmanagement import (
    WindowsEncryptionManager,
    WindowsEncryptionManagerConfig,
    WindowsEncryptionSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())

config = WindowsEncryptionManagerConfig(
    elevated=True,
    allow_destructive=False,
    default_cipher="XtsAes256",
)
enc_mgr = WindowsEncryptionManager(runner=runner, config=config)

# Enable BitLocker on C: with a password protector + recovery key
spec = WindowsEncryptionSpec(passphrase="hunter2-please-change-me")
result = enc_mgr.enable_encryption(volume="C:", spec=spec)
print(f"Enable: {result.status} (reboot required: {result.requires_reboot})")

# Back up the recovery password to a local file
backup_result = enc_mgr.backup_recovery_key(
    volume="C:", path="C:\\BitLockerKeys\\C-recovery.txt",
)
print(f"Recovery key backup: {backup_result.status}")

# Query state without elevation
status = enc_mgr.get_encryption_status(volume="C:")
print(f"Encrypted: {status.is_encrypted}, cipher: {status.cipher}")
print(f"Protectors: {status.protector_types}")

# Rotate the password protector
rotate_result = enc_mgr.rotate_key(volume="C:", new_passphrase="new-stronger-pw")
print(f"Rotate: {rotate_result.status}")
```

### SSH Execution with Aspects

```python
from automojo.sysman.windows.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.windows.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from automojo.sysman.windows.storage import SQLiteStorage
from automojo.sysman.windows.encryptionmanagement import (
    WindowsEncryptionManager,
    WindowsEncryptionManagerConfig,
    WindowsEncryptionSpec,
)

ssh_config = SSHRunnerConfig(
    host="10.0.0.50",
    username="Administrator",
    key_filename="C:\\Users\\admin\\.ssh\\id_rsa",
)
runner = SSHCommandRunner(ssh_config)

storage = SQLiteStorage(path="C:\\maintenance\\commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=300.0)))

enc_mgr = WindowsEncryptionManager(
    runner=runner,
    config=WindowsEncryptionManagerConfig(
        elevated=True,
        allow_destructive=True,   # opt in only when the workflow demands it
    ),
)

spec = WindowsEncryptionSpec(passphrase="hunter2", cipher="XtsAes256")
enc_mgr.enable_encryption(volume="D:", spec=spec)
enc_mgr.unlock_volume(volume="D:", passphrase="hunter2")
```

### Dry-Run Mode

```python
config = WindowsEncryptionManagerConfig(elevated=True, dry_run=True)
enc_mgr = WindowsEncryptionManager(runner=runner, config=config)

spec = WindowsEncryptionSpec(passphrase="hunter2")
result = enc_mgr.enable_encryption(volume="C:", spec=spec)
assert result.status.value == "dry_run"

# The recorded argv is the redacted form — no plaintext password leaks
recorded_script = result.command[-1]
assert "hunter2" not in recorded_script
assert "<redacted>" in recorded_script
```

---

## Testing Strategy

The test module
`source/testroots/testbase/automojo/tests/sysman/windows/test_encryptionmanagement.py`
exercises the surface using a `_MockCommandRunner` that records every
`run` call (argv tuple + elevation flag + stdin payload) and answers
from a pre-seeded `results` dict keyed by argv tuple.  No live
PowerShell is invoked.

1. **Per-verb command construction.**  Each write verb has at
   least one test that asserts the dispatched scriptblock contains
   the expected cmdlet and its key flags
   (`test_enable_encryption_invokes_enable_bitlocker`,
   `test_disable_encryption_invokes_disable_bitlocker_when_allowed`,
   `test_rotate_key_invokes_add_then_remove_bitlocker_key_protector`,
   `test_backup_recovery_key_writes_to_path`,
   `test_unlock_volume_passes_passphrase_via_secure_string`).
2. **Per-verb idempotency.**  Tests assert SKIPPED when the
   relevant `Get-BitLockerVolume` probe returns the matching state
   (`test_enable_encryption_skipped_when_already_encrypted`,
   `test_unlock_volume_skipped_when_unlocked`).
3. **Destructive guard.**
   `test_disable_encryption_requires_allow_destructive` proves
   FAILED with `_DESTRUCTIVE_GUARD_DETAIL` and zero
   `Disable-BitLocker` dispatches.
4. **Read-verb parsing.**
   `test_get_encryption_status_parses_output` and
   `test_list_encrypted_volumes_filters_correctly` cover the
   single-record and multi-record JSON paths.
5. **Argv-hygiene.**
   `test_passphrase_redacted_in_recorded_command` scans the
   recorded `command` field and asserts the plaintext password is
   absent and the `<redacted>` sentinel is present.
6. **Elevation policy.**  `test_writes_elevate` and
   `test_reads_never_elevate` lock the runner's `elevated` flag to
   the contract.
7. **Module-level parser tests.**
   `test_parse_get_bitlockervolume_handles_single_object`,
   `test_is_record_encrypted_recognises_string_and_int_protection`,
   and `test_sanitise_script_passwords_redacts_literal` cover the
   pure-Python helpers in isolation.
8. **Failure surface.**  `test_failed_command_surfaces_stderr`
   asserts that a runner non-zero exit propagates `exit_code` and
   `stderr` onto the result.

No test requires an elevated session, a live BitLocker subsystem,
or a TPM.  Every probe response is seeded into the mock runner's
`results` dict.

---

## Scope Mapping

On Windows the abstract `volume` parameter maps to a BitLocker
mount point — i.e. a drive letter:

| Volume identifier | Example | Tool path |
|---|---|---|
| Drive letter (system volume) | `"C:"` | `Enable-BitLocker -MountPoint 'C:'` |
| Drive letter (data volume) | `"D:"` | `Enable-BitLocker -MountPoint 'D:'` |
| Volume GPT path | `"\\?\Volume{guid}\\"` | `Enable-BitLocker -MountPoint '<gpt>'` (BitLocker accepts the GPT form) |

Unlike `firewall-management` (which has the abstract `zone`
concept), `encryption-management` has no group / zone hierarchy on
Windows.  BitLocker is fixed-disk + volume-keyed: every operation
targets exactly one mount point.

The `volume` argument is never validated against an enumeration —
the manager hands the value to PowerShell verbatim (after
single-quote escaping) and lets `Enable-BitLocker` /
`Get-BitLockerVolume` reject unknown drive letters with their
native error.

---

## Extensions Beyond the Contract

The Windows surface adds three Windows-specific extensions on top
of the abstract contract.  All are additive; none break the shape
expected by a cross-OS coordinator that consumes only the
contract-required fields.

| Extension | Surface | Rationale |
|---|---|---|
| `requires_reboot: bool` on the result | `WindowsEncryptionActionResult.requires_reboot` | `Enable-BitLocker` and `Disable-BitLocker` against the system volume modify the running boot volume's encryption posture and require a reboot to fully take effect.  Coordinators use this flag to schedule the post-encryption reboot without re-querying the volume. |
| `protector_types: list[str]` on the status | `WindowsEncryptionStatus.protector_types`, `WindowsEncryptionEncryptedVolumeInfo.protector_types` | BitLocker's `KeyProtector[]` is a first-class concept.  A coordinator that tracks "is the recovery key escrowed" or "is the TPM bound" needs the protector types without parsing `Get-BitLockerVolume` itself. |
| Recovery-password backup pattern | `backup_recovery_key(volume, path)` writes a UTF-8 file via `Set-Content` rather than returning the key as a string | The cross-OS contract describes recovery-key backup as `(volume, path) → result`.  On macOS the implementation captures `fdesetup showrecoverykey`'s stdout into Python and tees to disk; on Linux it emits a `luksHeaderBackup` binary file.  On Windows the recovery password lives inside the `KeyProtector[]` array — the manager filters to the `RecoveryPassword` protector and writes the value to disk inside the same scriptblock so the recovery password never leaves the runner. |

The contract's `EncryptionBackend` enum is implicit on Windows
(every record is `BITLOCKER`); the manager does NOT expose a
`backend` field on the result or status records.  This is tracked
under Spec-vs-Contract Drift below.

The contract's `EncryptionKeySlotState` per-slot enumeration is
projected onto BitLocker via the `protector_types` list rather than
a structured `key_slots` array.  This is also tracked under drift.

---

## Spec-vs-Contract Drift

The Windows implementation diverges from the cross-OS contract in
the following ways.  Each item is intentional; unintentional drift
should be flagged for review.

- **Passphrase redaction is implementation-specific.**  The
  contract requires that the `command` field MUST be sanitised but
  is silent on the redaction mechanism.  The Linux Ubuntu and
  macOS siblings achieve this trivially because their underlying
  CLIs accept passphrases on stdin — the recorded argv is
  automatically passphrase-free.  BitLocker requires the
  passphrase to be embedded inside a PowerShell scriptblock that
  carries `ConvertTo-SecureString -AsPlainText -Force '<pw>'`; the
  manager closes the gap with the `_sanitise_script_passwords`
  regex.  The contract has no field documenting this
  implementation choice.

- **Recovery-key backup writes a file in-band.**  The macOS
  implementation captures `fdesetup showrecoverykey`'s stdout into
  Python and tees to disk on a second runner call.  On Linux the
  backup is a binary `luksHeaderBackup` blob.  The Windows
  implementation builds a single PowerShell scriptblock that does
  the filter-and-write in one runner call so the recovery password
  never crosses the runner boundary as captured stdout.  The
  contract's `(volume, path)` shape is preserved; the
  implementation differs.

- **Protector lifecycle (Add / Remove) is BitLocker-native, not
  in the contract.**  The contract's `rotate_key` verb describes a
  single-call rotation.  BitLocker's native rotation is a
  two-cmdlet flow (`Add-BitLockerKeyProtector` then
  `Remove-BitLockerKeyProtector`) that the manager builds into a
  single scriptblock.  The add-then-remove ordering is the
  BitLocker analogue of the LUKS "last-key safety rail": the
  volume never has zero password protectors mid-rotation.

- **`backend` field missing from result / status records.**  The
  contract's `EncryptionStatus` carries an `EncryptionBackend`
  enum; the Windows implementation omits the field because every
  record is implicitly `BITLOCKER`.  Coordinators that consume the
  contract field from a cross-OS shape MUST default the value to
  `BITLOCKER` for Windows records.

- **`key_slots` field is missing.**  The contract's
  `EncryptionStatus.key_slots` is a list of
  `EncryptionKeySlotState`.  BitLocker exposes the equivalent
  information as the `protector_types` string list; the manager
  surfaces the latter rather than a structured slot list because
  BitLocker's `KeyProtectorId` opaque GUIDs do not map cleanly
  onto the contract's integer `slot` field.

- **`EncryptionVolumeNotFoundError` and
  `EncryptionCredentialError` exceptions are not implemented.**
  The contract requires reads to raise
  `EncryptionVolumeNotFoundError` for unknown volumes and writes
  to raise `EncryptionCredentialError` when a credential is
  required but absent.  The current Windows surface defines only
  `WindowsEncryptionQueryError` and does not raise it from the
  read paths today.  Tracked as a follow-up.

- **`lock_volume` verb is not implemented.**  The contract's verb
  set includes `lock_volume(volume) -> EncryptionActionResult`
  (the inverse of `unlock_volume`).  The Windows surface omits
  this verb because BitLocker does not expose a native cmdlet
  equivalent to `cryptsetup luksClose` — `Lock-BitLocker` exists
  in newer Windows versions but has different semantics.  Tracked
  as a follow-up.
