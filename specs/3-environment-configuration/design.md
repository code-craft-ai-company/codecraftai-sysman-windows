# Design

## Overview

The environment configuration feature is implemented as a single module,
`ccai.sysman.windows.environmentconfiguration`, that exposes a
`WindowsEnvironmentConfigurator` class.  This spec implements the
environment-configuration contract defined at
`codecraftai-sysman-common/specs/contracts/environment-configuration/contract.md`
for Windows using PowerShell invocations of the .NET
`[Environment]::SetEnvironmentVariable` / `GetEnvironmentVariable` APIs for
the current user and machine scope, and registry cmdlets
(`Get-ItemProperty`, `Set-ItemProperty`, `Remove-ItemProperty`) for the
`HKEY_USERS\<SID>` hive when targeting a different user.

All registry and environment operations go through the `ICommandRunner`
abstraction; an `IFileOperator` is accepted for optional support paths
(e.g., audit log export under `%ProgramData%`) but no env variable storage
depends on it.  Maintaining a dual-abstraction dependency (runner + file
operator) is an extension over the contract but keeps the constructor
symmetric with the contract's examples.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md)
for details on local/remote execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (+ optional file operator)           │
│  - Configures aspects (optional)                            │
│  - Injects into WindowsEnvironmentConfigurator              │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        WindowsEnvironmentConfigurator                       │
│  ccai.sysman.windows.environmentconfiguration           │
│                                                             │
│  set_system_env()    unset_system_env()                     │
│  get_system_env()    list_system_env()                      │
│  set_user_env()      unset_user_env()                       │
│  get_user_env()      list_user_env()                        │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        ICommandRunner                                       │
│  powershell -NoProfile -Command "..."                       │
│    [Environment]::SetEnvironmentVariable / GetEnvVariable   │
│    Set-ItemProperty / Get-ItemProperty / Remove-ItemProperty│
│    NTAccount().Translate([SecurityIdentifier]).Value        │
└────────────────────────┬────────────────────────────────────┘
                         │ implemented via
                         ▼
┌─────────────────────────────────────────────────────────────┐
│   LocalCommandRunner or SSHCommandRunner (WinRM-aware)      │
└────────────────────────┬────────────────────────────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  PersistenceAspect / RetryAspect / TimeoutAspect            │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsEnvironmentConfigurator`

**Location:** `source/packages/ccai/sysman/windows/environmentconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `set_system_env` | `(name, value) → WindowsEnvActionResult` | `SetEnvironmentVariable(..., 'Machine')` |
| `unset_system_env` | `(name) → WindowsEnvActionResult` | Same with `$null` value |
| `get_system_env` | `(name) → WindowsEnvValue \| None` | `GetEnvironmentVariable(..., 'Machine')` |
| `list_system_env` | `() → dict[str, str]` | `Get-ItemProperty HKLM:\...\Environment \| ConvertTo-Json` |
| `set_user_env` | `(user, name, value) → WindowsEnvActionResult` | `SetEnvironmentVariable(..., 'User')` or per-SID registry write |
| `unset_user_env` | `(user, name) → WindowsEnvActionResult` | Same with `$null` or `Remove-ItemProperty` |
| `get_user_env` | `(user, name) → WindowsEnvValue \| None` | Same read variants |
| `list_user_env` | `(user) → dict[str, str]` | `Get-ItemProperty HKCU:\Environment` or per-SID |

### `WindowsEnvironmentConfiguratorConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, system-scope and other-user writes pass `elevated=True` |

---

## Data Models

```python
class WindowsEnvActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

class WindowsEnvScope(Enum):
    SYSTEM = "system"
    USER   = "user"

@dataclass
class WindowsEnvValue:
    name: str
    value: str
    scope: WindowsEnvScope
    source: str | None      # registry path

@dataclass
class WindowsEnvActionResult:
    status: WindowsEnvActionStatus
    scope: WindowsEnvScope
    name: str
    value: str | None
    path: str | None
    user: str | None
    message: str | None

class WindowsEnvConfigError(Exception):
    """Raised when a user-scope read targets an unknown user or a query fails."""
```

---

## Interfaces

### `ICommandRunner`

Imported from `codecraftai-interfaces`.  The configurator takes one instance and
delegates all runtime/persistent operations to it through PowerShell
invocations.

### `IFileOperator` (optional)

Accepted for symmetry with the macOS/Ubuntu configurators; not required by
any verb.  Present so that future features (e.g., optional JSON export of
the full environment) can plug in without a constructor change.

---

## Scope Mapping

| Abstract | Windows backing storage |
|---|---|
| `SYSTEM` | `HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment` via `[Environment]::SetEnvironmentVariable(_, _, 'Machine')` |
| `USER` (current user) | `HKCU:\Environment` via `[Environment]::SetEnvironmentVariable(_, _, 'User')` |
| `USER` (other user) | `HKU:\<SID>\Environment` via `Set-ItemProperty -Path 'Registry::HKEY_USERS\<SID>\Environment' -Name <name> -Value <value>` |

---

## Command Mappings

### Read Commands (no elevation)

| Operation | PowerShell |
|---|---|
| `get_system_env` | `[Environment]::GetEnvironmentVariable('<name>', 'Machine')` |
| `list_system_env` | `Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' \| ConvertTo-Json` |
| `get_user_env` (current) | `[Environment]::GetEnvironmentVariable('<name>', 'User')` |
| `get_user_env` (other) | `(Get-ItemProperty -Path 'Registry::HKEY_USERS\<SID>\Environment' -Name '<name>').'<name>'` |
| `list_user_env` (current) | `Get-ItemProperty -Path 'HKCU:\Environment' \| ConvertTo-Json` |
| `list_user_env` (other) | `Get-ItemProperty -Path 'Registry::HKEY_USERS\<SID>\Environment' \| ConvertTo-Json` |
| user-to-SID translation | `(New-Object System.Security.Principal.NTAccount('<user>')).Translate([System.Security.Principal.SecurityIdentifier]).Value` |

### Write Commands (elevation-aware)

| Operation | PowerShell |
|---|---|
| `set_system_env` | `[Environment]::SetEnvironmentVariable('<name>', '<value>', 'Machine')` |
| `unset_system_env` | `[Environment]::SetEnvironmentVariable('<name>', $null, 'Machine')` |
| `set_user_env` (current) | `[Environment]::SetEnvironmentVariable('<name>', '<value>', 'User')` |
| `set_user_env` (other) | `Set-ItemProperty -Path 'Registry::HKEY_USERS\<SID>\Environment' -Name '<name>' -Value '<value>' -Type String` |
| `unset_user_env` (current) | `[Environment]::SetEnvironmentVariable('<name>', $null, 'User')` |
| `unset_user_env` (other) | `Remove-ItemProperty -Path 'Registry::HKEY_USERS\<SID>\Environment' -Name '<name>'` |

---

## Output Parsing

### `Get-ItemProperty | ConvertTo-Json` Output

Example:

```json
{
    "Path": "C:\\Windows;C:\\Windows\\System32",
    "JAVA_HOME": "C:\\Program Files\\Java\\jdk-21",
    "PSPath": "Microsoft.PowerShell.Core\\Registry::HKEY_...",
    "PSParentPath": "...",
    "PSChildName": "Environment",
    "PSDrive": "HKLM",
    "PSProvider": "Microsoft.PowerShell.Core\\Registry"
}
```

Parser uses `json.loads`, drops the pseudo-keys `PSPath`, `PSParentPath`,
`PSChildName`, `PSDrive`, `PSProvider`, and returns `dict[str, str]`.  Values
that are lists (from `REG_MULTI_SZ`) are joined with `os.pathsep` and
retained as strings for contract compatibility.

### `[Environment]::GetEnvironmentVariable` Output

Plain stdout: either the variable value as a single line, or empty string
when unset.  Parser treats empty stdout as "not set" and returns `None`.

### NTAccount Translate Output

Single line, e.g. `S-1-5-21-1234567890-1234567890-1234567890-1001`.  Parser
strips whitespace; empty output signals "user not found".

---

## Current-User Detection

```python
def _is_current_user(username: str) -> bool:
    # Run `[Environment]::UserName` once per configurator; cache the result.
    if self._current_user is None:
        rr = self._runner.run(
            args=["powershell", "-NoProfile", "-Command", "[Environment]::UserName"],
            elevated=False,
        )
        self._current_user = rr.stdout.strip()
    return username.split("\\")[-1].lower() == self._current_user.lower()
```

Used by `set_user_env` / `unset_user_env` to pick between the `'User'` scope
call and the per-SID registry write, and to suppress elevation for
same-user writes.

---

## Idempotency Guards

| Method | Guard | Mechanism |
|---|---|---|
| `set_system_env` | Current machine-scope value equals requested | `GetEnvironmentVariable('Machine')` + compare |
| `unset_system_env` | Variable absent from machine scope | Same |
| `set_user_env` | Current user-scope value equals requested | `GetEnvironmentVariable('User')` or `Get-ItemProperty` |
| `unset_user_env` | Variable absent from user scope | Same |

---

## Error Handling

| Exception | Raised When |
|---|---|
| `WindowsEnvConfigError` | `get_user_env` / `list_user_env` called with unknown user; `list_system_env` failing for a non-absence reason |

For user-scope writes, unknown-user is surfaced as
`WindowsEnvActionResult(status=FAILED, message="user not found: <user>")` —
not raised — per contract.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of the system.*

### Property 1: `set_system_env` Command Construction
*For any* `(name, value)`, `set_system_env` should invoke PowerShell running
`[Environment]::SetEnvironmentVariable('<name>', '<value>', 'Machine')`.
**Validates: Requirements 1.1**

### Property 2: `set_system_env` Idempotency
*For any* `(name, value)` where the current Machine-scope value already
matches, `set_system_env` should return `status=SKIPPED`.
**Validates: Requirements 1.2**

### Property 3: `set_system_env` Dry-Run
*For any* call with `dry_run=True`, `set_system_env` should return
`status=DRY_RUN` without invoking the mutating runner path.
**Validates: Requirements 1.5, 11.1**

### Property 4: `set_system_env` Elevation
*For any* call with `elevated=True`, `set_system_env` should pass
`elevated=True` to the runner.
**Validates: Requirements 1.6, 9.1**

### Property 5: `unset_system_env` Idempotency
*For any* name absent from Machine scope, `unset_system_env` should return
`status=SKIPPED`.
**Validates: Requirements 2.1**

### Property 6: `unset_system_env` Null Value Command
*For any* name present in Machine scope, `unset_system_env` should invoke
`SetEnvironmentVariable('<name>', $null, 'Machine')`.
**Validates: Requirements 2.2**

### Property 7: `get_system_env` Command
*For any* call, `get_system_env` should invoke
`GetEnvironmentVariable('<name>', 'Machine')`.
**Validates: Requirements 3.1**

### Property 8: `get_system_env` Absent
*For any* name where stdout is empty or the command exits non-zero,
`get_system_env` should return `None`.
**Validates: Requirements 3.3**

### Property 9: `list_system_env` JSON Parse
*For any* successful `ConvertTo-Json` output, `list_system_env` should drop
the PowerShell pseudo-keys and return a `dict[str, str]`.
**Validates: Requirements 4.2**

### Property 10: `list_system_env` Empty Registry Key
*For any* call where the registry key is absent, `list_system_env` should
return an empty dict.
**Validates: Requirements 4.3**

### Property 11: `list_system_env` Error Raises
*For any* call that fails for a non-absence reason, `list_system_env` should
raise `WindowsEnvConfigError`.
**Validates: Requirements 4.4**

### Property 12: `set_user_env` Unknown User
*For any* unknown user, `set_user_env` should return `status=FAILED` with a
descriptive message and not invoke the mutating runner path.
**Validates: Requirements 5.1**

### Property 13: `set_user_env` Current-User Command
*For any* target user equal to the current user, `set_user_env` should invoke
`SetEnvironmentVariable('<name>', '<value>', 'User')`.
**Validates: Requirements 5.2**

### Property 14: `set_user_env` Other-User Command
*For any* target user different from the current user, `set_user_env` should
invoke `Set-ItemProperty -Path 'Registry::HKEY_USERS\<SID>\Environment'
-Name '<name>' -Value '<value>' -Type String`.
**Validates: Requirements 5.3**

### Property 15: `set_user_env` Idempotency
*For any* call where the current user-scope value already matches,
`set_user_env` should return `status=SKIPPED`.
**Validates: Requirements 5.4**

### Property 16: `set_user_env` Dry-Run
*For any* call with `dry_run=True`, `set_user_env` should return
`status=DRY_RUN`.
**Validates: Requirements 5.6, 11.1**

### Property 17: `set_user_env` Same-User Elevation Suppression
*For any* call where the target user equals the current user,
`set_user_env` should pass `elevated=False` even when config has
`elevated=True`.
**Validates: Requirements 5.7, 9.3**

### Property 18: `unset_user_env` Unknown User
*For any* unknown user, `unset_user_env` should return `status=FAILED` with
a descriptive message.
**Validates: Requirements 6.1**

### Property 19: `unset_user_env` Current-User Null Command
*For any* current-user target with the variable present, `unset_user_env`
should invoke `SetEnvironmentVariable('<name>', $null, 'User')`.
**Validates: Requirements 6.3**

### Property 20: `unset_user_env` Other-User Remove Command
*For any* other-user target with the variable present, `unset_user_env`
should invoke `Remove-ItemProperty -Path
'Registry::HKEY_USERS\<SID>\Environment' -Name '<name>'`.
**Validates: Requirements 6.4**

### Property 21: `get_user_env` Unknown User
*For any* unknown user, `get_user_env` should raise
`WindowsEnvConfigError`.
**Validates: Requirements 7.1**

### Property 22: `get_user_env` Current-User Command
*For any* current-user target, `get_user_env` should invoke
`GetEnvironmentVariable('<name>', 'User')`.
**Validates: Requirements 7.2**

### Property 23: `get_user_env` Other-User Command
*For any* other-user target, `get_user_env` should invoke
`(Get-ItemProperty -Path 'Registry::HKEY_USERS\<SID>\Environment' -Name
'<name>').'<name>'`.
**Validates: Requirements 7.3**

### Property 24: `list_user_env` Unknown User
*For any* unknown user, `list_user_env` should raise
`WindowsEnvConfigError`.
**Validates: Requirements 8.1**

### Property 25: `list_user_env` Empty Key
*For any* call where the target registry key is absent, `list_user_env`
should return an empty dict.
**Validates: Requirements 8.4**

### Property 26: SID Translation
*For any* call to `_resolve_sid(username)`, the manager should invoke the
documented NTAccount-to-SID PowerShell.
**Validates: Requirements 10.1**

### Property 27: SID Translation Failure
*For any* failing NTAccount translation (empty output or non-zero exit), the
caller should surface user-not-found via the contract-defined behaviour
(FAILED for writes, raise for reads).
**Validates: Requirements 10.3**

### Property 28: Domain-Qualified Names
*For any* username containing a backslash (e.g., `DOMAIN\alice`),
`_resolve_sid` should pass the name through to `NTAccount` unchanged.
**Validates: Requirements 10.4**

### Property 29: Elevation for Other-User Writes
*For any* user-scope write targeting a different user with `elevated=True`,
the runner should be invoked with `elevated=True`.
**Validates: Requirements 9.2**

### Property 30: No Elevation for Reads
*For any* read operation, the runner should never be invoked with
`elevated=True`.
**Validates: Requirements 9.5**

### Property 31: Dry-Run Reads Still Run
*For any* call with `dry_run=True`, the read commands should still be
executed so that `SKIPPED` vs `DRY_RUN` can be distinguished.
**Validates: Requirements 11.3**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
└── environmentconfiguration.py   ← WindowsEnvironmentConfigurator, config,
                                     models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_environmentconfiguration.py
```

---

## Usage Examples

### Local Execution

```python
from ccai.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.windows.fileoperators import LocalFileOperator
from ccai.sysman.windows.environmentconfiguration import (
    WindowsEnvironmentConfigurator,
    WindowsEnvironmentConfiguratorConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_operator = LocalFileOperator(runner=runner)

env_cfg = WindowsEnvironmentConfigurator(
    runner=runner,
    file_operator=file_operator,
    config=WindowsEnvironmentConfiguratorConfig(elevated=True, dry_run=False),
)

result = env_cfg.set_system_env(name="JAVA_HOME", value=r"C:\Program Files\Java\jdk-21")
print(f"Set system env: {result.status}")

result = env_cfg.set_user_env(user="alice", name="NODE_ENV", value="production")
print(f"Set user env: {result.status}")

user_vars = env_cfg.list_user_env(user="alice")
print(f"User vars: {user_vars}")
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
from ccai.sysman.windows.environmentconfiguration import (
    WindowsEnvironmentConfigurator, WindowsEnvironmentConfiguratorConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-win", username="admin"))
storage = SQLiteStorage(path=r"C:\ProgramData\CodeCraftAI\commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

file_operator = SSHFileOperator(runner=runner)
env_cfg = WindowsEnvironmentConfigurator(
    runner=runner,
    file_operator=file_operator,
    config=WindowsEnvironmentConfiguratorConfig(elevated=True),
)

env_cfg.set_system_env(name="LANG", value="en_US.UTF-8")
```

### Dry-Run Mode

```python
cfg = WindowsEnvironmentConfiguratorConfig(elevated=True, dry_run=True)
env_cfg = WindowsEnvironmentConfigurator(runner=runner, file_operator=file_operator, config=cfg)

result = env_cfg.set_system_env(name="DANGEROUS_VAR", value="value")
assert result.status == WindowsEnvActionStatus.DRY_RUN
```

---

## Testing Strategy

1. `MockCommandRunner` keyed on command args prefix returns canned
   `[Environment]::GetEnvironmentVariable`, `[Environment]::
   SetEnvironmentVariable`, `Get-ItemProperty`, and NTAccount-translate
   stdout values.
2. `MockFileOperator` is present but largely unused except by tests that
   opt into future support-file features.
3. `_resolve_sid` coverage test uses canned outputs for known/unknown users
   including a domain-qualified `DOMAIN\alice` name.
4. `_is_current_user` test asserts that the cached current-user is
   consulted before deciding between current-user and per-SID code paths.
5. `list_system_env` / `list_user_env` parser tests assert that PowerShell
   pseudo-keys are dropped from the returned dict.
6. Dry-run matrix test iterates every write verb and asserts no mutating
   runner call occurs while read commands still execute.
7. Elevation matrix test asserts `elevated=True` flows to system-scope
   writes and to other-user writes, and is absent from same-user writes and
   all reads.
8. Unknown-user tests for each of `set_user_env`, `unset_user_env`,
   `get_user_env`, `list_user_env` assert the contract-defined behaviour.
9. No test requires Administrator access or a live Windows host.

---

## Scope Mapping

| Abstract | Windows |
|---|---|
| `SYSTEM` | `HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment` |
| `USER` (current) | `HKCU:\Environment` |
| `USER` (other) | `HKU:\<SID>\Environment` |

---

## Extensions Beyond the Contract

- **NTAccount-to-SID helper.**  `_resolve_sid` translates user names into
  SIDs so callers can address any local or domain user without learning
  SID syntax.
- **Same-user elevation suppression.**  Writes targeting the current user
  never pass `elevated=True`, even when the config says so.
- **`IFileOperator` accepted for constructor symmetry.**  Not required by
  any current verb but kept present so future support-file features (audit
  log export etc.) can plug in without a breaking constructor change.
