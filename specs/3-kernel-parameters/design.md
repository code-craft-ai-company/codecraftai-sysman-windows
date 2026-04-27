# Design

## Overview

The kernel parameters feature is implemented as a single module,
`automojo.sysman.windows.kernelparameters`, that exposes a
`WindowsKernelParameterManager` class.  This spec implements the
kernel-parameters contract defined at
`automojo-sysman-common/specs/contracts/kernel-parameters/contract.md` for
Windows.

Windows has no `sysctl(8)` equivalent.  The manager translates a bundled
subset of dotted parameter names into registry-path / value-name pairs and
dispatches PowerShell `Get-ItemProperty`, `Set-ItemProperty`, and
`Remove-ItemProperty` cmdlets via an `ICommandRunner`.  Parameters not in
the mapping table return `FAILED` with `message="parameter_not_supported"`
— which the contract explicitly permits and requires to be non-silent.

Many Windows registry writes do not take effect until reboot.  The mapping
table flags each entry with `requires_reboot` and the manager copies that
flag onto returned results.  `reload_parameters()` is a documented no-op
because Windows has no equivalent to `sysctl --system` — registry writes
persist immediately and the kernel re-reads them on the next boot (or
immediately for some keys via driver-specific mechanisms that the manager
does not attempt to trigger).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH (WinRM) execution, reliability tracking, and
aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or WinRM)                     │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsKernelParameterManager        │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           WindowsKernelParameterManager                     │
│  automojo.sysman.windows.kernelparameters                   │
│                                                             │
│  get_parameter()                set_parameter()             │
│  list_parameters()              reload_parameters()         │
│  remove_persisted_parameter()   get_mapping_table()         │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ WinRMCommandRunner   │
│ - subprocess w/ UAC  │      │ - remote PowerShell  │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect   - RetryAspect   - TimeoutAspect      │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsKernelParameterManager`

**Location:** `source/packages/automojo/sysman/windows/kernelparameters.py`

| Method | Signature | Description |
|---|---|---|
| `get_parameter` | `(name) → WindowsParameterValue \| None` | Registry read |
| `set_parameter` | `(name, value, persistent) → WindowsKernelParameterActionResult` | Registry write |
| `list_parameters` | `() → dict[str, str]` | Iterate mapping table + read each |
| `reload_parameters` | `() → WindowsKernelParameterActionResult` | Documented no-op (`SUCCESS`) |
| `remove_persisted_parameter` | `(name) → WindowsKernelParameterActionResult` | `Remove-ItemProperty` |
| `get_mapping_table` | `() → dict[str, MappingEntry]` | Returns a copy of the bundled table |

### `WindowsKernelParameterManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutations dispatch |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `persist_dir` | `str \| None` | `None` | Unused on Windows; contract-mandated field |

### `MappingEntry` (internal)

| Field | Type | Description |
|---|---|---|
| `registry_path` | `str` | e.g., `HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters` |
| `value_name` | `str` | e.g., `KeepAliveTime` |
| `reg_type` | `str` | One of `DWord \| String \| ExpandString \| MultiString \| QWord` |
| `requires_reboot` | `bool` | Whether a reboot is required to apply |

---

## Data Models

```python
class WindowsKernelParameterActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class WindowsParameterValue:
    name: str
    value: str
    source: str | None          # "<registry-path>\<value-name>"

@dataclass
class WindowsKernelParameterActionResult:
    status: WindowsKernelParameterActionStatus
    name: str
    current_value: str | None
    applied_value: str | None
    persistent: bool
    requires_reboot: bool
    source: str | None
    diff: str | None            # always None for Windows (no file diffs)
    message: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

class WindowsKernelParameterQueryError(Exception):
    """Raised for unexpected registry read failures."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

Windows does not use `IFileOperator` for kernel parameters — all state
lives in the registry.

---

## Bundled Mapping Table (sample)

The real table lives in the module; a representative subset is shown here
for spec-review purposes.  The mapping is intentionally conservative — only
parameters with a meaningful cross-OS analog are included.

| Dotted name | Registry path | Value name | Type | `requires_reboot` |
|---|---|---|---|---|
| `net.ipv4.tcp_keepalive_time` | `HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters` | `KeepAliveTime` | `DWord` | `False` |
| `net.ipv4.tcp_keepalive_intvl` | `HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters` | `KeepAliveInterval` | `DWord` | `False` |
| `net.ipv4.ip_forward` | `HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters` | `IPEnableRouter` | `DWord` | `True` |
| `net.core.somaxconn` | `HKLM:\SYSTEM\CurrentControlSet\Services\AFD\Parameters` | `ListenBackLog` | `DWord` | `True` |
| `net.ipv4.tcp_syncookies` | `HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters` | `EnableSYNAttackProtection` | `DWord` | `True` |
| `kernel.disable_ipv6` | `HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters` | `DisabledComponents` | `DWord` | `True` |
| `net.smbv1.enabled` | `HKLM:\SYSTEM\CurrentControlSet\Services\LanmanServer\Parameters` | `SMB1` | `DWord` | `True` |
| `kernel.lsa_credguard` | `HKLM:\SYSTEM\CurrentControlSet\Control\Lsa` | `LsaCfgFlags` | `DWord` | `True` |

The table is exposed through `get_mapping_table()` as a copy so callers
can enumerate supported names.  Names outside the table are rejected with
`FAILED message="parameter_not_supported"`.

---

## Command Mappings

### Write Commands

| Operation | Command |
|---|---|
| Set parameter | `powershell.exe -NoProfile -NonInteractive -Command "Set-ItemProperty -Path '<path>' -Name '<value-name>' -Value '<value>' -Type <type>; exit $LASTEXITCODE"` |
| Remove parameter | `powershell.exe -NoProfile -NonInteractive -Command "Remove-ItemProperty -Path '<path>' -Name '<value-name>'; exit $LASTEXITCODE"` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Get one parameter | `powershell.exe -NoProfile -NonInteractive -Command "(Get-ItemProperty -Path '<path>' -Name '<value-name>').'<value-name>'; exit $LASTEXITCODE"` |

### `reload_parameters` — No Command

Returns `SUCCESS` / `DRY_RUN` without invoking the runner.  The contract's
verb set is preserved, the Windows implementation just has nothing to do.

---

## Output Parsing

### `Get-ItemProperty` output

PowerShell emits the registry-value content as a single line of stdout
when the script expression evaluates `(Get-ItemProperty ...).<name>`.  The
parser strips trailing whitespace and stores the raw string.  A non-zero
exit with stderr matching `Property .* does not exist` → `None`.  A
non-zero exit matching `Cannot find path` → `None`.  Any other non-zero
exit raises `WindowsKernelParameterQueryError`.

### `Set-ItemProperty` / `Remove-ItemProperty` output

These cmdlets emit no stdout on success.  Exit code 0 → SUCCESS.  Non-zero
→ FAILED with stderr preserved.

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `set_parameter` | Current registry value equals desired | `Get-ItemProperty` read |
| `remove_persisted_parameter` | Registry value does not currently exist | `Get-ItemProperty` read |

`reload_parameters` has no guard — it's a no-op.  `list_parameters` is a
read and needs no guard.

---

## PowerShell Argument Escaping

The manager constructs script strings using PowerShell single-quoted
literals.  Single quotes inside values are escaped by doubling (`'` → `''`).
Backticks, dollar signs, and double quotes require no escaping inside
single-quoted strings.  This is a pure-Python string operation executed
before the argv list is passed to the runner.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `WindowsKernelParameterQueryError` | Unexpected non-zero exit from `Get-ItemProperty` (any failure that is not "property not found" or "path not found") |

All write failures are surfaced as `WindowsKernelParameterActionResult`
with `status=FAILED`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Get-Parameter Mapped Name Dispatches Get-ItemProperty

*For any* parameter name present in the mapping table, `get_parameter()` should construct a PowerShell `Get-ItemProperty` command referencing the table's path and value name.

**Validates: Requirements 1.1**

### Property 2: Get-Parameter Unmapped Name Returns None

*For any* parameter name NOT in the mapping table, `get_parameter()` should return `None` without invoking the runner.

**Validates: Requirements 1.3**

### Property 3: Get-Parameter Missing Property Returns None

*For any* mapped parameter whose underlying registry value does not exist, `get_parameter()` should return `None`.

**Validates: Requirements 1.4**

### Property 4: Get-Parameter Unexpected Failure Raises

*For any* mapped parameter whose `Get-ItemProperty` fails for a non "property not found" reason, `get_parameter()` should raise `WindowsKernelParameterQueryError`.

**Validates: Requirements 1.5**

### Property 5: Set-Parameter Rejects Unmapped Names

*For any* parameter name NOT in the mapping table, `set_parameter()` should return `FAILED` with `message="parameter_not_supported: <name>"` without invoking the runner.

**Validates: Requirements 2.1**

### Property 6: Set-Parameter Idempotency

*For any* mapped parameter whose current registry value already equals the requested value, `set_parameter()` should return `SKIPPED`.

**Validates: Requirements 2.2**

### Property 7: Set-Parameter Command Construction

*For any* mapped parameter with a differing current value, `set_parameter()` should dispatch `Set-ItemProperty -Path ... -Name ... -Value ... -Type ...` using the mapping table's `reg_type`.

**Validates: Requirements 2.3, 2.4**

### Property 8: Requires-Reboot Propagation

*For any* `set_parameter` or `remove_persisted_parameter` against a mapped entry flagged `requires_reboot=True`, the returned result should set `requires_reboot=True`.

**Validates: Requirements 2.5**

### Property 9: Set-Parameter Persistent Field Always True

*For any* successful `set_parameter` result, the `persistent` field should be `True` (all Windows registry writes persist).

**Validates: Requirements 2.6**

### Property 10: Set-Parameter Failure Populates Diagnostics

*For any* non-zero exit from `Set-ItemProperty`, the result should be `FAILED` with the exit code, captured stderr, and the `command` that was attempted.

**Validates: Requirements 2.7**

### Property 11: List-Parameters Iterates Mapping Table

*For any* non-empty mapping table, `list_parameters()` should invoke `Get-ItemProperty` once per entry and collect results into a `dict[str, str]`.

**Validates: Requirements 3.1, 3.2**

### Property 12: List-Parameters Omits Missing Properties

*For any* mapping entry whose registry value is not set, `list_parameters()` should omit it from the returned dict.

**Validates: Requirements 3.3**

### Property 13: List-Parameters Raises On Unexpected Failure

*For any* mapping entry whose read fails for an unexpected reason, `list_parameters()` should raise `WindowsKernelParameterQueryError`.

**Validates: Requirements 3.4**

### Property 14: List-Parameters Empty Mapping

*For any* empty mapping table, `list_parameters()` should return `{}` without invoking the runner.

**Validates: Requirements 3.5**

### Property 15: No Full Enumeration

*For any* call to `list_parameters()`, the manager MUST NOT attempt to enumerate every Windows kernel tunable (there is no `sysctl -a` equivalent).

**Validates: Requirements 3.6**

### Property 16: Reload Is No-Op Success

*For any* call to `reload_parameters()` in non-dry-run mode, the runner should NOT be invoked and the result should be `SUCCESS` with `message="Windows applies kernel parameters on next boot or immediately per-key; no reload supported"`.

**Validates: Requirements 4.1, 4.2**

### Property 17: Reload Dry-Run Is Dry-Run

*For any* call to `reload_parameters()` with `dry_run=True`, the result should be `DRY_RUN` with the same message.

**Validates: Requirements 4.3**

### Property 18: Reload Never Raises

*For any* call to `reload_parameters()`, the method should not raise.

**Validates: Requirements 4.4**

### Property 19: Remove-Persisted Rejects Unmapped Names

*For any* parameter name NOT in the mapping table, `remove_persisted_parameter()` should return `FAILED` with `message="parameter_not_supported"` without invoking the runner.

**Validates: Requirements 5.1**

### Property 20: Remove-Persisted Idempotency

*For any* mapped parameter whose registry value does not currently exist, `remove_persisted_parameter()` should return `SKIPPED`.

**Validates: Requirements 5.2**

### Property 21: Remove-Persisted Command Construction

*For any* mapped parameter with an extant value, `remove_persisted_parameter()` should dispatch `Remove-ItemProperty -Path ... -Name ...`.

**Validates: Requirements 5.3**

### Property 22: Remove-Persisted Propagates Requires-Reboot

*For any* successful `remove_persisted_parameter` against a reboot-required entry, the result's `requires_reboot` should be `True`.

**Validates: Requirements 5.4**

### Property 23: Mapping Table Accessor Returns Copy

*For any* call to `get_mapping_table()`, mutations to the returned dict should NOT affect the manager's internal table.

**Validates: Requirements 6.2**

### Property 24: PowerShell Argv Shape

*For any* dispatched command, the argv should begin with `["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ...]`.

**Validates: Requirements 7.1**

### Property 25: PowerShell Script Trailing Exit

*For any* dispatched script, the inner PowerShell script should end with `; exit $LASTEXITCODE`.

**Validates: Requirements 7.3**

### Property 26: PowerShell Single-Quote Escaping

*For any* value containing a single quote, the value embedded in the script should have the single quote doubled (`'` → `''`).

**Validates: Requirements 7.4**

### Property 27: Dry-Run Short-Circuit

*For any* write operation (`set_parameter`, `remove_persisted_parameter`) with `dry_run=True`, the runner should never be invoked, and the result status should be `DRY_RUN`.

**Validates: Requirements 2.8, 5.6, 9.1**

### Property 28: Dry-Run Populates Command Argv

*For any* write operation in dry-run mode, the result's `command` field should equal the argv that would have been executed.

**Validates: Requirements 9.2**

### Property 29: Dry-Run Still Reads

*For any* write operation in dry-run mode, the idempotency-guard reads should still execute.

**Validates: Requirements 9.3**

### Property 30: Elevation For HKLM Writes

*For any* write operation with `elevated=True` in the config, the runner should be invoked with `elevated=True`.

**Validates: Requirements 8.1**

### Property 31: No Elevation For Reads

*For any* read operation (`get_parameter`, `list_parameters`), the runner should never be invoked with `elevated=True` regardless of the elevation configuration.

**Validates: Requirements 8.2**

---

## Module Layout

```
source/packages/automojo/sysman/windows/
├── __init__.py
└── kernelparameters.py         ← WindowsKernelParameterManager, config,
                                   mapping table, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/windows/
    └── test_kernelparameters.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.windows.kernelparameters import (
    WindowsKernelParameterManager,
    WindowsKernelParameterManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = WindowsKernelParameterManagerConfig(elevated=True, dry_run=False)
kp_mgr = WindowsKernelParameterManager(runner=runner, config=config)

# Set a mapped parameter
result = kp_mgr.set_parameter(name="net.ipv4.tcp_keepalive_time", value="7200000", persistent=True)
print(f"Set: {result.status}; requires_reboot={result.requires_reboot}")

# Read a mapped parameter
value = kp_mgr.get_parameter(name="net.ipv4.tcp_keepalive_time")
if value is not None:
    print(f"tcp_keepalive_time = {value.value}")

# Attempt an unmapped parameter
bad = kp_mgr.set_parameter(name="vm.swappiness", value="10", persistent=True)
assert bad.status == WindowsKernelParameterActionStatus.FAILED
assert "parameter_not_supported" in bad.message
```

### WinRM Execution with Aspects

```python
from automojo.sysman.windows.runners import WinRMCommandRunner, WinRMRunnerConfig
from automojo.sysman.windows.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from automojo.sysman.windows.storage import SQLiteStorage
from automojo.sysman.windows.kernelparameters import (
    WindowsKernelParameterManager,
    WindowsKernelParameterManagerConfig,
)

runner = WinRMCommandRunner(WinRMRunnerConfig(host="win-host", username="Administrator"))

storage = SQLiteStorage(path="C:\\ProgramData\\Automojo\\commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

kp_mgr = WindowsKernelParameterManager(
    runner=runner,
    config=WindowsKernelParameterManagerConfig(elevated=True),
)

kp_mgr.set_parameter(name="net.smbv1.enabled", value="0", persistent=True)
# reload is documented no-op on Windows
reload_result = kp_mgr.reload_parameters()
print(reload_result.message)
```

### Dry-Run Mode

```python
config = WindowsKernelParameterManagerConfig(elevated=True, dry_run=True)
kp_mgr = WindowsKernelParameterManager(runner=runner, config=config)

result = kp_mgr.set_parameter(name="net.ipv4.ip_forward", value="1", persistent=True)
assert result.status == WindowsKernelParameterActionStatus.DRY_RUN
print(result.command)       # ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ...]
print(result.requires_reboot)  # True (ip_forward is reboot-required)
```

---

## Testing Strategy

### Unit Testing

- Tests use a `MockCommandRunner` keyed on command arg-prefix.
- `Get-ItemProperty` mock responses drive `get_parameter` and idempotency
  guards.
- Mapping-table fixtures override the bundled table in tests with a
  deterministic small set so coverage is stable.
- Dry-run tests assert zero calls to mutating runner methods.
- Mapping-rejection tests assert zero runner invocations for unmapped
  names.
- PowerShell script construction is tested with representative values
  including single quotes, to verify escape doubling.
- The documented no-op behavior of `reload_parameters` is tested
  explicitly — the runner must not be invoked and the result must be
  `SUCCESS` with the documented message.
- No test requires a live Windows host.

### Property-Based Testing

The implementation uses `hypothesis` for property-based testing.  Each
correctness property is implemented as a property-based test that:

- Generates random but valid inputs (mapped parameter names, arbitrary
  string values including those with embedded single quotes).
- Executes the operation under test against the mock runner.
- Verifies the property holds across all generated inputs.
- Runs at least 100 iterations per property test.

Property-based tests are tagged as
`# Feature: kernel-parameters (windows), Property N: <property description>`.

---

## Scope Mapping

| Contract verb | Windows implementation | Notes |
|---|---|---|
| `get_parameter` | `Get-ItemProperty` via PowerShell | Unmapped → `None`. |
| `set_parameter` | `Set-ItemProperty` via PowerShell | Unmapped → `FAILED`. |
| `list_parameters` | Iterate mapping table, read each | Bounded by table. |
| `reload_parameters` | No-op, documented `SUCCESS` | No `sysctl --system` equivalent. |
| `remove_persisted_parameter` | `Remove-ItemProperty` via PowerShell | Unmapped → `FAILED`. |

---

## Extensions beyond the contract

- **Bundled mapping table.**  Windows has no `sysctl` namespace; the
  manager ships a curated dotted-name → registry-path table.  The
  contract's "Unsupported parameters" clause permits this rejection
  pattern.  The mapping table is exposed via `get_mapping_table()` so
  callers can enumerate supported names.
- **Requires-reboot flag.**  Each mapping entry carries `requires_reboot`;
  the manager copies it into every returned result.  This is the
  contract-mandated behavior for OSes with reboot-required writes.
- **Documented no-op `reload_parameters`.**  The method is retained for
  cross-OS API uniformity but returns `SUCCESS` without running any
  command because Windows has no equivalent.
- **PowerShell dispatch convention.**  Every command is wrapped in the
  uniform `powershell.exe -NoProfile -NonInteractive -Command
  "... ; exit $LASTEXITCODE"` envelope so aspects see a stable argv
  shape.

## Out of Scope

- Full registry enumeration (there is no `sysctl -a` equivalent).
- Driver-specific IOCTLs for immediate-apply of registry changes that
  normally require reboot.
- Group Policy integration — handled by `security-compliance`.
- Boot-loader / `bcdedit` kernel parameters — separate feature.
- Mass-apply from a structured policy document (YAML / TOML).
- Registry-key ACL management.
