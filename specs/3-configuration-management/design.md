# Design

## Overview

The configuration management feature is implemented as a single module,
`ccai.sysman.windows.configurationmanagement`, that exposes a
`WindowsConfigurationManager` class.  This spec implements the
configuration-management contract defined at
`codecraftai-sysman-common/specs/contracts/configuration-management/contract.md`
for Windows using PowerShell's registry cmdlets (`Get-ItemProperty`,
`Set-ItemProperty`, `Remove-ItemProperty`, `Get-Item`), `reg.exe` for
whole-hive import/export, and the `IFileOperator` abstraction for plain-text
files under `%ProgramData%`.

All content mutation is delegated to either the `ICommandRunner` (for registry
paths and ACL commands) or the `IFileOperator` (for plain-text files).
Maintaining a dual-abstraction dependency (runner + file operator) is an
extension over the contract — the contract allows but does not require
`IFileOperator`.  This package uses both because Windows configuration spans
two fundamentally different storage backends: registry and text file.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over WinRM / SSH (via `SSHCommandRunner`).
When using the remote runner, the coordinator can track reliability metrics and
apply execution aspects (persistence, retry, timeout) without any changes to
the `WindowsConfigurationManager` code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md)
for details on local/remote execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Configures aspects (optional)                            │
│  - Injects both into WindowsConfigurationManager            │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        WindowsConfigurationManager                          │
│  ccai.sysman.windows.configurationmanagement            │
│                                                             │
│  read_setting()     write_setting()                         │
│  remove_setting()   list_settings()                         │
│  write()            diff()                                  │
│  validate()         backup_configuration()                  │
│  restore_configuration()                                    │
│  set_permissions()  set_ownership()                         │
│  reload_configuration()                                     │
└────────────┬────────────────────────────┬───────────────────┘
             │ delegates to               │ delegates to
             ▼                            ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   Get-ItemProperty / │         │   read_text / write  │
│   Set-ItemProperty / │         │   delete / exists    │
│   reg.exe / icacls / │         │                      │
│   Restart-Service    │         │                      │
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
           └────────────────┬───────────────┘
                            ▼
          ┌──────────────────────────────────┐
          │   LocalCommandRunner or          │
          │   SSHCommandRunner               │
          └─────────────┬────────────────────┘
                        │ optional
                        ▼
          ┌──────────────────────────────────┐
          │   PersistenceAspect /            │
          │   RetryAspect / TimeoutAspect    │
          └──────────────────────────────────┘
```

---

## Components

### `WindowsConfigurationManager`

**Location:** `source/packages/ccai/sysman/windows/configurationmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `read_setting` | `(source, key) → WindowsSettingValue` | Registry or file read |
| `write_setting` | `(source, key, value) → WindowsConfigActionResult` | Registry or file write |
| `remove_setting` | `(source, key) → WindowsConfigActionResult` | Registry or file delete |
| `list_settings` | `(source) → list[WindowsSettingValue]` | Enumerate keys |
| `write` | `(source, content, mode=None, owner=None, group=None) → WindowsConfigActionResult` | Whole-source write |
| `diff` | `(source, desired_content) → WindowsConfigDiffResult` | Unified diff |
| `validate` | `(source, validator_id) → WindowsConfigValidationResult` | Run validator argv |
| `backup_configuration` | `(source) → WindowsConfigActionResult` | `reg export` or `Copy-Item` |
| `restore_configuration` | `(backup_path) → WindowsConfigActionResult` | `reg import` or `Copy-Item` |
| `set_permissions` | `(source, mode) → WindowsConfigActionResult` | `icacls` or registry ACL |
| `set_ownership` | `(source, user=None, group=None) → WindowsConfigActionResult` | Same |
| `reload_configuration` | `(service) → WindowsConfigActionResult` | `Restart-Service` |

### `WindowsConfigurationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |

### `ValidatorRegistry`

```python
WINDOWS_VALIDATORS: dict[str, Callable[[str], list[str]]] = {
    "certificate":   lambda path: ["certutil", "-verify", path],
    "groupPolicy":   lambda _:    ["gpupdate", "/target:Computer", "/force"],
    "sfc":           lambda _:    ["sfc", "/verifyonly"],
    "testPath":      lambda path: ["powershell", "-NoProfile", "-Command",
                                   f"Test-Path -Path '{path}'"],
    "xml":           lambda path: ["powershell", "-NoProfile", "-Command",
                                   f"[xml](Get-Content -Path '{path}')"],
    "json":          lambda path: ["powershell", "-NoProfile", "-Command",
                                   f"Get-Content -Path '{path}' | ConvertFrom-Json"],
}
```

---

## Data Models

```python
class WindowsConfigActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class WindowsConfigActionResult:
    status: WindowsConfigActionStatus
    source: str
    key: str | None
    backup_path: str | None
    diff: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class WindowsSettingValue:
    source: str
    key: str
    value: str
    value_type: str | None    # "REG_SZ" / "REG_EXPAND_SZ" / "REG_DWORD" /
                              # "REG_QWORD" / "REG_MULTI_SZ" / "REG_BINARY" /
                              # "ini" / "xml" / "json"

@dataclass
class WindowsConfigDiffResult:
    source: str
    has_diff: bool
    diff: str

@dataclass
class WindowsConfigValidationResult:
    source: str
    validator_id: str
    valid: bool
    exit_code: int
    stdout: str
    stderr: str

@dataclass
class WindowsConfigFileInfo:
    source: str
    exists: bool
    mode: str | None
    owner: str | None
    group: str | None
    size_bytes: int | None

class WindowsConfigFileNotFoundError(Exception): ...
class WindowsConfigTemplateError(Exception): ...
class WindowsConfigValidatorNotFoundError(Exception): ...
class WindowsConfigSettingNotFoundError(Exception): ...
class WindowsConfigQueryError(Exception): ...
```

---

## Source Classification

`_classify_source(source)` returns one of `"registry"`, `"file"`:

1. If `source.startswith("HK")` with a known hive prefix (`HKLM:`,
   `HKCU:`, `HKU:`, `HKCR:`, `HKCC:`) → `"registry"`.
2. Otherwise → `"file"`.

---

## Command Mappings

### Read Commands

| Operation | Command | Elevation |
|---|---|---|
| `read_setting` — registry | `powershell -NoProfile -Command "(Get-ItemProperty -Path '<path>' -Name '<key>').'<key>'"` | `HKLM:\SECURITY\` only |
| `read_setting` — file | `IFileOperator.read_text(path=source)` | No |
| `list_settings` — registry | `powershell -NoProfile -Command "Get-Item -Path '<path>' \| Select-Object -ExpandProperty Property"` | `HKLM:\SECURITY\` only |
| `list_settings` — file | `IFileOperator.read_text` | No |
| `diff` — registry | `reg export '<path>' <tmp> /y` + `IFileOperator.read_text` | No |
| `diff` — file | `IFileOperator.read_text` | No |
| `validate` | validator-specific argv | No |
| `stat` for file | `powershell -NoProfile -Command "Get-Item -Path '<path>' \| Select-Object Length,LastWriteTime; icacls '<path>'"` | No |

### Write Commands (elevation-aware)

| Operation | Command / Call |
|---|---|
| `write_setting` — registry | `Set-ItemProperty -Path '<path>' -Name '<key>' -Value '<value>' -Type <type>` |
| `write_setting` — file | `IFileOperator.write_text(path, rewritten, elevated=<cfg>)` |
| `remove_setting` — registry | `Remove-ItemProperty -Path '<path>' -Name '<key>'` |
| `remove_setting` — file | `IFileOperator.write_text(path, rewritten, elevated=<cfg>)` |
| `write` — registry | `IFileOperator.write_text(temp_reg, content, ...)` + `reg import <temp_reg>` + `IFileOperator.delete(temp_reg)` |
| `write` — file | `IFileOperator.write_text` + optional ACL follow-up |
| `backup_configuration` — registry | `reg export '<path>' '<backup>' /y` |
| `backup_configuration` — file | `powershell Copy-Item -LiteralPath '<source>' -Destination '<backup>' -Force` |
| `restore_configuration` — registry | `reg import '<backup>'` |
| `restore_configuration` — file | `powershell Copy-Item -LiteralPath '<backup>' -Destination '<original>' -Force` |
| `set_permissions` — file | `icacls '<source>' /grant:r <ace>` |
| `set_permissions` — registry | `powershell Set-Acl -Path '<path>' -AclObject $acl` |
| `set_ownership` — file | `icacls '<source>' /setowner '<user>'` |
| `set_ownership` — registry | `powershell ... $acl.SetOwner(...)` |
| `reload_configuration` | `powershell Restart-Service -Name '<service>' -Force` |

---

## `Set-ItemProperty` Value Type Inference

`_infer_reg_type(value)` returns the registry type flag:

| Python input | Type |
|---|---|
| `bool` | `DWord` (0/1) |
| `int` | `DWord` when ≤ 32-bit, else `QWord` |
| `str` with `%VAR%` pattern | `ExpandString` |
| `str` (default) | `String` |
| `list[str]` | `MultiString` |
| `bytes` | `Binary` |

---

## Output Parsing

### `Get-ItemProperty | ConvertTo-Json` Output

Single property value comes out as a JSON scalar or array:

```json
"Production"
```

or

```json
["line1", "line2"]
```

Parser uses `json.loads(text)`; for arrays produces `MultiString`.

### `icacls '<path>'` Output

```
C:\ProgramData\MyApp\app.config BUILTIN\Administrators:(F)
                                NT AUTHORITY\SYSTEM:(F)
                                BUILTIN\Users:(RX)
Successfully processed 1 files;...
```

Parser extracts the SID/ACE tuples by splitting on `:(` and comparing set-wise
for idempotency.

### Format Parsers (Pure Python)

- INI via `configparser`.
- XML via `xml.etree.ElementTree` (namespace-preserving).
- JSON via `json.loads` / `json.dumps` with `indent=2`.
- Shell `KEY=VALUE` via regex (for `.env` files under `%ProgramData%`).

---

## Idempotency Guards

| Method | Guard | Mechanism |
|---|---|---|
| `write_setting` — registry | Current `Get-ItemProperty` value equals desired | Runner + JSON parse + compare |
| `write_setting` — file | Current parsed value equals desired | Parse + compare |
| `remove_setting` — registry | `Get-ItemProperty` exits non-zero / key absent | Runner exit-code |
| `remove_setting` — file | Key absent | Parse + compare |
| `write` | Current content equals desired | `read_text` or `reg export` + compare |
| `set_permissions` | Current ACL matches requested ACE | `icacls` parse + set compare |
| `set_ownership` | Current owner matches requested | `icacls` parse + string compare |
| `reload_configuration` | None — operational action | — |

---

## Backup Naming

- File sources: `<source>.bak.<YYYYMMDDTHHMMSS>`.
- Registry sources:
  `C:\ProgramData\CodeCraftAI\backups\<registry-basename>.<YYYYMMDDTHHMMSS>.reg`.

`_restore_route(backup_path)` pattern-matches the path to decide between
`reg import` and `Copy-Item`.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `WindowsConfigFileNotFoundError` | `backup_configuration`, `restore_configuration`, `set_permissions`, `set_ownership` target a missing source |
| `WindowsConfigTemplateError` | Template helper missing a variable |
| `WindowsConfigValidatorNotFoundError` | Unknown validator id |
| `WindowsConfigSettingNotFoundError` | `read_setting` missing key or source |
| `WindowsConfigQueryError` | Any other read-path failure |

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of the system.*

### Property 1: `read_setting` Registry Command
*For any* registry source and key, `read_setting` should invoke
`Get-ItemProperty -Path '<path>' -Name '<key>'` via PowerShell.
**Validates: Requirements 1.1**

### Property 2: `read_setting` File Parse
*For any* plain-file source, `read_setting` should call
`IFileOperator.read_text` and parse via the format-appropriate parser.
**Validates: Requirements 1.2**

### Property 3: `read_setting` Missing Key
*For any* missing key, `read_setting` should raise
`WindowsConfigSettingNotFoundError`.
**Validates: Requirements 1.3**

### Property 4: `read_setting` Missing Source
*For any* missing source, `read_setting` should raise
`WindowsConfigSettingNotFoundError` with `detail="source missing"`.
**Validates: Requirements 1.4**

### Property 5: `read_setting` Security Hive Elevation
*For any* registry path rooted under `HKLM:\SECURITY\`, `read_setting` should
pass `elevated=True`.
**Validates: Requirements 1.6**

### Property 6: `write_setting` Registry Type Inference
*For any* registry source and value, `write_setting` should invoke
`Set-ItemProperty` with `-Type <type>` derived from `_infer_reg_type`.
**Validates: Requirements 2.1**

### Property 7: `write_setting` File Rewrite
*For any* plain-file source, `write_setting` should read, mutate, and write via
`IFileOperator`.
**Validates: Requirements 2.2**

### Property 8: `write_setting` Idempotency
*For any* source where the current value already equals the desired value,
`write_setting` should return `status=SKIPPED`.
**Validates: Requirements 2.3**

### Property 9: `write_setting` Dry-Run
*For any* call with `dry_run=True`, `write_setting` should return
`status=DRY_RUN` and not invoke the runner or file operator.
**Validates: Requirements 2.7**

### Property 10: `remove_setting` Registry Command
*For any* registry source, `remove_setting` should invoke
`Remove-ItemProperty -Path '<path>' -Name '<key>'`.
**Validates: Requirements 3.2**

### Property 11: `remove_setting` File Rewrite
*For any* plain-file source, `remove_setting` should rewrite the file without
the key via `IFileOperator.write_text`.
**Validates: Requirements 3.3**

### Property 12: `remove_setting` Idempotency
*For any* source where the key is absent, `remove_setting` should return
`status=SKIPPED`.
**Validates: Requirements 3.1**

### Property 13: `list_settings` Registry Enum
*For any* registry source, `list_settings` should invoke `Get-Item -Path
'<path>' | Select-Object -ExpandProperty Property` and then fetch values
per-name.
**Validates: Requirements 4.2**

### Property 14: `list_settings` File Parse
*For any* plain-file source, `list_settings` should parse the file and return
`WindowsSettingValue` entries preserving order.
**Validates: Requirements 4.3**

### Property 15: `list_settings` Missing Source
*For any* missing source, `list_settings` should return an empty list.
**Validates: Requirements 4.1**

### Property 16: `write` Registry Sequence
*For any* registry source, `write` should stage the content to a temp file via
`write_text`, invoke `reg import`, and delete the temp file.
**Validates: Requirements 5.2**

### Property 17: `write` File Idempotency
*For any* plain-file source where current content equals desired, `write`
should return `status=SKIPPED`.
**Validates: Requirements 5.3**

### Property 18: `diff` Registry
*For any* registry source, `diff` should invoke `reg export ... /y` to a temp
file and diff it against the desired `.reg` content.
**Validates: Requirements 6.2**

### Property 19: `diff` File
*For any* plain-file source, `diff` should compute the unified diff in pure
Python.
**Validates: Requirements 6.1**

### Property 20: `validate` Unknown Id
*For any* unregistered id, `validate` should raise
`WindowsConfigValidatorNotFoundError`.
**Validates: Requirements 7.4**

### Property 21: `validate` Exit-Code Mapping
*For any* registered validator, exit 0 should produce `valid=True`, non-zero
`valid=False`.
**Validates: Requirements 7.2, 7.3**

### Property 22: `backup_configuration` Registry
*For any* registry source, `backup_configuration` should invoke `reg export
'<path>' '<backup>' /y` with a timestamped path under
`C:\ProgramData\CodeCraftAI\backups\`.
**Validates: Requirements 8.2**

### Property 23: `backup_configuration` File
*For any* file source, `backup_configuration` should invoke `Copy-Item
-LiteralPath '<source>' -Destination '<backup>' -Force` with a timestamped
path.
**Validates: Requirements 8.3**

### Property 24: `backup_configuration` Missing
*For any* missing source, `backup_configuration` should raise
`WindowsConfigFileNotFoundError`.
**Validates: Requirements 8.1**

### Property 25: `restore_configuration` Routing
*For any* backup path, `restore_configuration` should pattern-match the path
and dispatch `reg import` or `Copy-Item`.
**Validates: Requirements 9.1, 9.2**

### Property 26: `set_permissions` File Idempotency
*For any* file source whose current ACL matches the requested mode,
`set_permissions` should return `status=SKIPPED`.
**Validates: Requirements 10.4**

### Property 27: `set_ownership` Idempotency
*For any* source whose current owner matches, `set_ownership` should return
`status=SKIPPED`.
**Validates: Requirements 11.4**

### Property 28: `reload_configuration` Command
*For any* service name other than `"none"`, `reload_configuration` should
invoke `Restart-Service -Name '<service>' -Force`.
**Validates: Requirements 12.1**

### Property 29: `reload_configuration` No-Service Sentinel
*For any* call with `service == "none"`, `reload_configuration` should return
`status=SKIPPED`.
**Validates: Requirements 12.5**

### Property 30: Elevation for Writes
*For any* write operation with `elevated=True`, the runner or file operator
should be invoked with `elevated=True`.
**Validates: Requirements 13.1**

### Property 31: No Elevation for Non-Security Reads
*For any* read operation against a non-Security source, the runner and file
operator should never be invoked with `elevated=True`.
**Validates: Requirements 13.2**

### Property 32: Security Hive Read Elevation
*For any* read rooted under `HKLM:\SECURITY\`, the runner should be invoked
with `elevated=True`.
**Validates: Requirements 13.3**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
└── configurationmanagement.py   ← WindowsConfigurationManager, config, models,
                                    exceptions, ValidatorRegistry

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_configurationmanagement.py
```

---

## Usage Examples

### Local Execution

```python
from ccai.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.windows.fileoperators import LocalFileOperator
from ccai.sysman.windows.configurationmanagement import (
    WindowsConfigurationManager,
    WindowsConfigurationManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_operator = LocalFileOperator(runner=runner)

cfg_mgr = WindowsConfigurationManager(
    runner=runner,
    file_operator=file_operator,
    config=WindowsConfigurationManagerConfig(elevated=True, dry_run=False),
)

# Registry write
result = cfg_mgr.write_setting(
    source=r"HKLM:\SOFTWARE\Vendor\Product",
    key="DataDir",
    value=r"D:\Data",
)
print(f"Write registry setting: {result.status}")

# File write
result = cfg_mgr.write_setting(
    source=r"C:\ProgramData\MyApp\app.ini",
    key="Database.Host",
    value="db01",
)
print(f"Write INI setting: {result.status}")

validation = cfg_mgr.validate(
    source=r"C:\ProgramData\MyApp\manifest.xml",
    validator_id="xml",
)
print(f"XML valid: {validation.valid}")

reload_result = cfg_mgr.reload_configuration(service="MyAppService")
print(f"Restart service: {reload_result.status}")
```

### Remote Execution with Aspects

```python
from ccai.sysman.windows.runners import SSHCommandRunner, SSHRunnerConfig
from ccai.sysman.windows.aspects import (
    PersistenceAspect, PersistenceAspectConfig,
    RetryAspect, RetryAspectConfig,
    TimeoutAspect, TimeoutAspectConfig,
)
from ccai.sysman.windows.storage import SQLiteStorage
from ccai.sysman.windows.fileoperators import SSHFileOperator
from ccai.sysman.windows.configurationmanagement import (
    WindowsConfigurationManager, WindowsConfigurationManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-win", username="admin"))
storage = SQLiteStorage(path=r"C:\ProgramData\CodeCraftAI\commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

file_operator = SSHFileOperator(runner=runner)
cfg_mgr = WindowsConfigurationManager(
    runner=runner,
    file_operator=file_operator,
    config=WindowsConfigurationManagerConfig(elevated=True),
)

backup = cfg_mgr.backup_configuration(source=r"HKLM:\SOFTWARE\Vendor\Product")
print(f"Registry backup at: {backup.backup_path}")
```

### Dry-Run Mode

```python
cfg = WindowsConfigurationManagerConfig(elevated=True, dry_run=True)
cfg_mgr = WindowsConfigurationManager(runner=runner, file_operator=file_operator, config=cfg)

result = cfg_mgr.write_setting(
    source=r"HKLM:\SOFTWARE\Vendor\Product",
    key="LogLevel",
    value="Debug",
)
assert result.status == WindowsConfigActionStatus.DRY_RUN
print(result.command)  # would invoke Set-ItemProperty
```

---

## Testing Strategy

1. `MockCommandRunner` keyed on command args prefix returns canned
   `Get-ItemProperty`, `Get-Item`, `icacls`, and `reg export` stdout.
2. `MockFileOperator` backed by an in-memory `dict[str, str]` drives
   plain-file idempotency tests.
3. `_infer_reg_type` coverage test iterates every Python input type and
   asserts the chosen type flag.
4. `_classify_source` test matrix asserts every hive prefix plus drive-letter
   file paths.
5. `list_settings` registry test uses a representative
   `Get-Item | ExpandProperty Property` stdout and verifies per-name dispatch.
6. `backup_configuration` registry test monkey-patches `datetime.utcnow` and
   asserts the computed backup path under
   `C:\ProgramData\CodeCraftAI\backups\`.
7. Security-hive elevation test proves `HKLM:\SECURITY\*` reads elevate while
   other hives do not.
8. Dry-run matrix test iterates every write verb and asserts no mutating
   runner/file-operator call.
9. Elevation matrix test asserts `elevated=True` flows to writes and is absent
   for non-Security reads.
10. No test requires Administrator access or a live Windows host.

---

## Scope Mapping

| Abstract | Windows |
|---|---|
| Configuration source (registry) | `HKLM:\SOFTWARE\<vendor>\<product>`, `HKCU:\Software\...`, etc. |
| Configuration source (file) | Absolute path under `%ProgramData%` or user-profile location |
| Supported file formats | INI, XML, JSON, `KEY=VALUE` |
| Validators | `certificate`, `groupPolicy`, `sfc`, `testPath`, `xml`, `json` |
| Reload mechanism | `Restart-Service -Name <service> -Force`; `none` sentinel for registry-only |
| File backup naming | `<source>.bak.<YYYYMMDDTHHMMSS>` |
| Registry backup naming | `C:\ProgramData\CodeCraftAI\backups\<basename>.<YYYYMMDDTHHMMSS>.reg` |

---

## Extensions Beyond the Contract

- **Dual abstraction dependency.**  Uses `IFileOperator` for plain-file I/O and
  `ICommandRunner` for registry + ACL + validator + reload calls.
- **Security-hive read elevation.**  Exercises the contract-permitted
  elevation exception for `HKLM:\SECURITY\`.
- **`"none"` service sentinel for reload.**  Windows registry changes often
  take effect without a service restart; this sentinel avoids forcing callers
  to special-case the absence of a backing service.
- **`.reg`-format whole-source writes.**  `write(source=<registry>, content=
  "<.reg text>")` supports applying a full-hive export atomically via
  `reg import`.
