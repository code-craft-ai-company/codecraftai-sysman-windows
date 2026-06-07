# Design

## Overview

The service-management feature is implemented as a single module,
`ccai.sysman.windows.servicemanagement`, that exposes a `WindowsServiceManager`
class.  This spec implements the service-management contract defined at
`codecraftai-sysman-common/specs/contracts/service-management/contract.md` for
Windows using PowerShell's `Start-Service`, `Stop-Service`, `Restart-Service`,
`Get-Service`, and `Set-Service` cmdlets through an `ICommandRunner`.

All state queries used for idempotency guards go through `Get-Service -Name <name>
| Select-Object Name,Status,StartType,DisplayName | ConvertTo-Json` which returns a
JSON object parsed in pure Python into a `WindowsServiceStatus`.  Writes construct
PowerShell commands via `["powershell.exe", "-NoProfile", "-Command", "<cmdlet
invocation>"]`.  The feature works identically whether commands execute locally
(via `WindowsLocalCommandRunner`) or remotely over SSH (via
`WindowsSSHCommandRunner`).

Windows has no native `mask` or `reload` primitives.  The manager aliases:

- `reload_service` → `restart_service` (with a descriptive `message` on the
  result).
- `mask_service` / `unmask_service` → `disable_service` / `enable_service` (also
  documented via `message`).

Windows per-user services (labels ending in `_<sessionid>`) are out of scope for
the base contract.  Every `USER`-scope call returns a typed failure without
invoking the runner.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsServiceManager                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsServiceManager                            │
│  ccai.sysman.windows.servicemanagement                  │
│                                                             │
│  start_service()  stop_service()  restart_service()         │
│  reload_service() enable_service()  disable_service()       │
│  mask_service()   unmask_service()                          │
│  get_service_status()  list_services()  reload_daemon()     │
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
┌──────────────────────────┐  ┌──────────────────────────┐
│ WindowsLocalCommandRunner│  │ WindowsSSHCommandRunner  │
│ - subprocess.run()       │  │ - paramiko connection    │
│ - host-token elevation   │  │ - remote OpenSSH Server  │
└──────────────────────────┘  └──────────────────────────┘
```

---

## Components

### `WindowsServiceManager`

**Location:** `source/packages/ccai/sysman/windows/servicemanagement.py`

| Method | Signature | Description |
|---|---|---|
| `start_service` | `(name, scope) → WindowsServiceActionResult` | `Start-Service -Name <name>` |
| `stop_service` | `(name, scope) → WindowsServiceActionResult` | `Stop-Service -Name <name> -Force` |
| `restart_service` | `(name, scope) → WindowsServiceActionResult` | `Restart-Service -Name <name> -Force` |
| `reload_service` | `(name, scope) → WindowsServiceActionResult` | Alias to restart with message |
| `enable_service` | `(name, scope) → WindowsServiceActionResult` | `Set-Service -Name <name> -StartupType Automatic` |
| `disable_service` | `(name, scope) → WindowsServiceActionResult` | `Set-Service -Name <name> -StartupType Disabled` |
| `mask_service` | `(name, scope) → WindowsServiceActionResult` | Alias to disable with message |
| `unmask_service` | `(name, scope) → WindowsServiceActionResult` | Alias to enable with message |
| `get_service_status` | `(name, scope) → WindowsServiceStatus` | `Get-Service | Select-Object | ConvertTo-Json` |
| `list_services` | `(scope) → list[WindowsServiceInfo]` | `Get-Service | Select-Object | ConvertTo-Json` |
| `reload_daemon` | `(scope) → WindowsServiceActionResult` | no-op |

### `WindowsServiceManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no write dispatches |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `powershell_path` | `str` | `"powershell.exe"` | Override with `pwsh.exe` for PowerShell 7+ |

---

## Data Models

### Enums

```python
class WindowsServiceActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

class WindowsServiceScope(Enum):
    SYSTEM = "system"
    USER   = "user"
```

### Result / Info Types

```python
@dataclass
class WindowsServiceActionResult:
    status: WindowsServiceActionStatus
    service_name: str
    scope: WindowsServiceScope
    message: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    command: list[str] | None = None

@dataclass
class WindowsServiceStatus:
    service_name: str
    scope: WindowsServiceScope
    active_state: str        # "active" | "inactive" | "activating" | "deactivating" | "not-found"
    enabled_state: str       # "enabled" | "disabled" | "static" | "not-found"
    main_pid: int | None = None
    description: str | None = None

@dataclass
class WindowsServiceInfo:
    service_name: str
    scope: WindowsServiceScope
    active_state: str
    enabled_state: str
    description: str | None

class WindowsServiceQueryError(Exception):
    """Raised when a read fails for an unexpected reason."""
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

---

## Scope Mapping

| `WindowsServiceScope` | Behaviour |
|---|---|
| `SYSTEM` | Commands execute against the SCM via `powershell.exe` |
| `USER` | Every call returns a typed failure (write) or raises `WindowsServiceQueryError` (read) without invoking the runner |

### Extensions beyond the contract

The contract's §Service-Name and Scope Mapping Windows row notes that per-user
services (`_<sessionid>` suffixed) exist but are "out of scope for the base
contract".  This spec implements that by treating `USER` scope as a first-class
failure rather than silently dispatching a plausibly-wrong command.  A future
extension spec may surface per-user service management behind an opt-in feature
flag.

---

## Command Mappings

### Write Commands

| Operation | PowerShell cmdlet invocation |
|---|---|
| Start | `Start-Service -Name <name>` |
| Stop | `Stop-Service -Name <name> -Force` |
| Restart | `Restart-Service -Name <name> -Force` |
| Reload | `Restart-Service -Name <name> -Force` (alias) |
| Enable | `Set-Service -Name <name> -StartupType Automatic` |
| Disable | `Set-Service -Name <name> -StartupType Disabled` |
| Mask | `Set-Service -Name <name> -StartupType Disabled` (alias) |
| Unmask | `Set-Service -Name <name> -StartupType Automatic` (alias) |

All write commands are wrapped in
`["powershell.exe", "-NoProfile", "-Command", "<cmdlet invocation>"]`.  System-scope
writes pass `elevated=True` when `config.elevated=True`.

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Status | `Get-Service -Name <name> \| Select-Object Name,Status,StartType,DisplayName \| ConvertTo-Json -Compress` |
| List | `Get-Service \| Select-Object Name,Status,StartType,DisplayName \| ConvertTo-Json -Compress` |

---

## Output Parsing

### `Get-Service | ConvertTo-Json` Single Object

```json
{"Name":"W3SVC","Status":4,"StartType":2,"DisplayName":"World Wide Web Publishing Service"}
```

`Status` and `StartType` are `System.ServiceProcess.ServiceControllerStatus` and
`System.ServiceProcess.ServiceStartMode` enum integers.  The manager maps them via
the following tables:

| `Status` int | Meaning | `active_state` |
|---|---|---|
| 1 | Stopped | `inactive` |
| 2 | StartPending | `activating` |
| 3 | StopPending | `deactivating` |
| 4 | Running | `active` |
| 5 | ContinuePending | `activating` |
| 6 | PausePending | `deactivating` |
| 7 | Paused | `inactive` |

| `StartType` int | Meaning | `enabled_state` |
|---|---|---|
| 0 | Boot | `static` |
| 1 | System | `static` |
| 2 | Automatic | `enabled` |
| 3 | Manual | `static` |
| 4 | Disabled | `disabled` |

`DisplayName` populates `description`.  `main_pid` is not populated by this query;
callers that need it can query WMI separately (out of scope for the base
contract).

### `Get-Service | ConvertTo-Json` Array

```json
[
  {"Name":"W3SVC","Status":4,"StartType":2,"DisplayName":"..."},
  {"Name":"BITS","Status":1,"StartType":3,"DisplayName":"..."}
]
```

A PowerShell quirk: when only one service matches, `ConvertTo-Json` returns the
bare object rather than a one-element array.  The parser normalises by coercing a
bare object to `[object]`.

### `NoServiceFoundForGivenName` Error

```
Get-Service : Cannot find any service with service name 'XYZ'.
CategoryInfo : ObjectNotFound: (XYZ:String) [Get-Service], ServiceCommandException
FullyQualifiedErrorId : NoServiceFoundForGivenName,Microsoft.PowerShell.Commands.GetServiceCommand
```

Detected via a substring search on `FullyQualifiedErrorId :
NoServiceFoundForGivenName`; the manager returns
`active_state="not-found"`, `enabled_state="not-found"` WITHOUT raising.

---

## Idempotency Guards

| Method | Guard | Expected state to skip |
|---|---|---|
| `start_service` | `active_state == "active"` | already running |
| `stop_service` | `active_state == "inactive"` | already stopped |
| `restart_service` | never skips | — |
| `reload_service` | `active_state != "active"` → `FAILED` | must be active |
| `enable_service` | `enabled_state == "enabled"` | already Automatic |
| `disable_service` | `enabled_state == "disabled"` | already Disabled |
| `mask_service` | `enabled_state == "masked"` (i.e. Disabled — aliased) | already masked |
| `unmask_service` | `enabled_state != "masked"` | not masked |
| `reload_daemon` | never skips (but is a no-op) | — |

All idempotency probes go through `get_service_status` which does NOT pass
`elevated=True`.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `WindowsServiceQueryError` | `list_services` fails unexpectedly; `get_service_status`'s `Get-Service` fails with a non-"NoServiceFoundForGivenName" stderr; USER scope on a read verb |

All write failures surface as `WindowsServiceActionResult(status=FAILED, …)`.

---

## Correctness Properties

### Property 1: Start Command Construction

*For any* `start_service(name, SYSTEM)` whose guard does not skip, the runner is
invoked with `["powershell.exe", "-NoProfile", "-Command", f"Start-Service -Name
{name}"]`.

**Validates: Requirements 1.1**

### Property 2: Start Idempotency

*For any* service already `Running`, `start_service` returns `SKIPPED` without
dispatching the start command.

**Validates: Requirements 1.2**

### Property 3: Stop Command

*For any* `stop_service` whose guard does not skip, the runner is invoked with
`Stop-Service -Name <name> -Force`.

**Validates: Requirements 2.1**

### Property 4: Stop Idempotency

*For any* service already `Stopped`, `stop_service` returns `SKIPPED`.

**Validates: Requirements 2.2**

### Property 5: Restart Unconditional

*For any* `restart_service`, the runner is invoked regardless of state (unless
dry-run).

**Validates: Requirements 3.1, 3.5**

### Property 6: Reload Alias

*For any* `reload_service` when active, the runner is invoked with
`Restart-Service` and the result's `message` contains
`"aliased to restart"`.

**Validates: Requirements 4.1**

### Property 7: Reload Precondition

*For any* `reload_service` when not active, the result is `FAILED` with
`message="service is not active; cannot reload"` and the runner is NOT invoked.

**Validates: Requirements 4.2**

### Property 8: Enable Command

*For any* `enable_service` whose guard does not skip, the runner is invoked with
`Set-Service -Name <name> -StartupType Automatic`.

**Validates: Requirements 5.1**

### Property 9: Enable Idempotency

*For any* service with `StartType = Automatic`, `enable_service` returns
`SKIPPED`.

**Validates: Requirements 5.2**

### Property 10: Disable Command

*For any* `disable_service` whose guard does not skip, the runner is invoked with
`Set-Service -Name <name> -StartupType Disabled`.

**Validates: Requirements 6.1**

### Property 11: Disable Idempotency

*For any* service with `StartType = Disabled`, `disable_service` returns
`SKIPPED`.

**Validates: Requirements 6.2**

### Property 12: Mask Alias

*For any* `mask_service` whose guard does not skip, the runner is invoked with
`Set-Service -Name <name> -StartupType Disabled` and the result's `message`
contains `"aliased to disable"`.

**Validates: Requirements 7.1**

### Property 13: Unmask Alias

*For any* `unmask_service` whose guard does not skip, the runner is invoked with
`Set-Service -Name <name> -StartupType Automatic` and the result's `message`
contains `"aliased to enable"`.

**Validates: Requirements 8.1**

### Property 14: Status Query Command

*For any* `get_service_status`, the runner is invoked with the
`Get-Service | Select-Object | ConvertTo-Json -Compress` pipeline without
elevation.

**Validates: Requirements 9.1**

### Property 15: Status Parse Single Object

*For any* `ConvertTo-Json` output representing one service, the parser produces a
`WindowsServiceStatus` with correctly mapped `active_state` and `enabled_state`.

**Validates: Requirements 9.2**

### Property 16: Status Not Found

*For any* `NoServiceFoundForGivenName` error, the parser returns a status with
`active_state="not-found"` and `enabled_state="not-found"` without raising.

**Validates: Requirements 9.3**

### Property 17: Status Unexpected Error

*For any* `Get-Service` failure with a non-`NoServiceFoundForGivenName` stderr, a
`WindowsServiceQueryError` is raised.

**Validates: Requirements 9.4**

### Property 18: List Services Parse Array

*For any* `ConvertTo-Json` output representing an array, `list_services` produces
one `WindowsServiceInfo` per entry.

**Validates: Requirements 10.2**

### Property 19: List Services Parse Single

*For any* `ConvertTo-Json` output representing a single object,
`list_services` still produces a one-element list.

**Validates: Requirements 10.2**

### Property 20: List Services Error

*For any* failure of the list command, `WindowsServiceQueryError` is raised.

**Validates: Requirements 10.3**

### Property 21: Reload Daemon No-Op

*For any* `reload_daemon`, the runner is NOT invoked and the result has
`status=SUCCESS` with a descriptive `message`.

**Validates: Requirements 11.1**

### Property 22: USER Scope Rejection (Writes)

*For any* write verb called with `scope=USER`, the result has `status=FAILED`,
`message="USER scope not supported on Windows…"`, and the runner is NOT invoked.

**Validates: Requirements 1.6, 2.6, 3.6, 4.6, 5.6, 6.6, 7.6, 8.6, 12.2**

### Property 23: USER Scope Rejection (Reads)

*For any* read verb called with `scope=USER`, `WindowsServiceQueryError` is raised
and the runner is NOT invoked.

**Validates: Requirements 9.5, 10.4, 12.2**

### Property 24: Invalid Scope

*For any* call with a scope other than `SYSTEM` or `USER`, `ValueError` is raised.

**Validates: Requirements 12.3**

### Property 25: Scope Echo On Result

*For any* write result, `WindowsServiceActionResult.scope` equals the scope that
was supplied.

**Validates: Requirements 12.4**

### Property 26: System Writes Elevate

*For any* write with `config.elevated=True` and `scope=SYSTEM`, the runner is
invoked with `elevated=True`.

**Validates: Requirements 13.1**

### Property 27: Reads Do Not Elevate

*For any* read or idempotency-probe call, the runner is NEVER invoked with
`elevated=True`.

**Validates: Requirements 13.2, 13.3**

### Property 28: Dry-Run Safety (All Writes)

*For any* write verb with `config.dry_run=True`, the runner is NOT invoked and the
result has `status=DRY_RUN`.

**Validates: Requirements 1.5, 2.5, 3.4, 4.5, 5.5, 6.5, 7.5, 8.5, 11.2**

### Property 29: Status Mapping

*For any* write command result from the runner, `status=SUCCESS` iff
`exit_code==0`; otherwise `status=FAILED` with captured fields.

**Validates: Requirements 1.3, 1.4, 2.3, 2.4, 3.2, 3.3, 4.3, 4.4, 5.3, 5.4, 6.3,
6.4, 7.3, 7.4, 8.3, 8.4**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
└── servicemanagement.py       ← WindowsServiceManager, config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_servicemanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.windows.runners import (
    WindowsLocalCommandRunner, WindowsLocalRunnerConfig,
)
from ccai.sysman.windows.servicemanagement import (
    WindowsServiceManager, WindowsServiceManagerConfig, WindowsServiceScope,
)

runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
config = WindowsServiceManagerConfig(elevated=True, dry_run=False)
svc_mgr = WindowsServiceManager(runner=runner, config=config)

result = svc_mgr.restart_service(name="W3SVC", scope=WindowsServiceScope.SYSTEM)
print(f"restart W3SVC: {result.status.value}")

status = svc_mgr.get_service_status(name="W3SVC", scope=WindowsServiceScope.SYSTEM)
print(f"active={status.active_state} enabled={status.enabled_state}")
```

### Aspect Composition (SSH)

```python
from ccai.sysman.windows.runners import (
    WindowsSSHCommandRunner, WindowsSSHRunnerConfig,
)
from ccai.sysman.windows.aspects import (
    RetryAspect, RetryAspectConfig,
)

runner = WindowsSSHCommandRunner(
    WindowsSSHRunnerConfig(host="win-prod-01", username="Administrator"),
)
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3)))

svc_mgr = WindowsServiceManager(
    runner=runner,
    config=WindowsServiceManagerConfig(elevated=True),
)
svc_mgr.enable_service(name="BITS", scope=WindowsServiceScope.SYSTEM)
```

### Dry-Run Preview

```python
config = WindowsServiceManagerConfig(elevated=True, dry_run=True)
svc_mgr = WindowsServiceManager(runner=runner, config=config)
result = svc_mgr.stop_service(name="W3SVC", scope=WindowsServiceScope.SYSTEM)
assert result.status.value == "dry_run"
```

---

## Testing Strategy

1. **MockCommandRunner** keyed on argv prefix lets each test queue the responses
   for `Get-Service` and the action command separately.
2. One command-construction test per write verb asserts the exact PowerShell
   cmdlet invocation.
3. Idempotency tests are driven by canned `ConvertTo-Json` outputs.
4. `reload_service` precondition is tested with an inactive-state probe.
5. `mask_service` / `unmask_service` tests assert the runner is called with the
   disable/enable cmdlet and the `message` field contains the alias marker.
6. `get_service_status` is tested for every documented `Status`/`StartType`
   integer mapping and for the `NoServiceFoundForGivenName` error.
7. `list_services` parsing is tested with both single-object and array JSON
   outputs.
8. `reload_daemon` is tested to NOT invoke the runner and to return a descriptive
   message.
9. USER-scope rejection is tested per verb (writes → FAILED result, reads →
   `WindowsServiceQueryError`).
10. Invalid-scope test asserts `ValueError` is raised.
11. Elevation tests assert `elevated=True` for SYSTEM writes and never for reads
    or probes.
12. No test requires Administrator privileges or a live Windows SCM.
