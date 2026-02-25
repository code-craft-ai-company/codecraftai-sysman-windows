# Design

## Overview

The service management feature is implemented as a single module,
`automojo.sysman.linux.windows.servicemanagement`, that exposes a `ServiceManager` class.
The coordinator instantiates `ServiceManager` and calls its methods to perform discrete
maintenance actions against the `systemd` service manager via `systemctl` and `journalctl`.

All system interaction is delegated to a command runner provided by `automojo-interfaces`.
`ServiceManager` never directly calls `subprocess` or any shell; it only constructs command
argument lists and passes them to the runner.  This keeps business logic separate from
execution and makes the feature fully testable via a mock runner without root access or a
live `systemd` instance.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│            ServiceManager               │  automojo.sysman.linux.windows.servicemanagement
│                                         │
│  start()        stop()                  │
│  restart()      reload()                │
│  enable()       disable()               │
│  mask()         unmask()                │
│  query()        daemon_reload()         │
│  get_journal()                          │
└───────────────────┬─────────────────────┘
                    │ delegates to
                    ▼
┌─────────────────────────────────────────┐
│        ICommandRunner                   │  automojo-interfaces
│                                         │
│  run(args, elevated=False) → RunResult  │
└─────────────────────────────────────────┘
```

---

## Components

### `ServiceManager`

**Location:** `source/packages/automojo/sysman/linux/windows/servicemanagement.py`

The primary public class.  Constructed with an `ICommandRunner` and a
`ServiceManagerConfig`.

| Method | Signature | Description |
|---|---|---|
| `start` | `(name) → ServiceActionResult` | Start a service |
| `stop` | `(name) → ServiceActionResult` | Stop a service |
| `restart` | `(name) → ServiceActionResult` | Restart a service |
| `reload` | `(name) → ServiceActionResult` | Send reload signal to a service |
| `enable` | `(name) → ServiceActionResult` | Enable a service at boot |
| `disable` | `(name) → ServiceActionResult` | Disable a service at boot |
| `mask` | `(name) → ServiceActionResult` | Mask a service |
| `unmask` | `(name) → ServiceActionResult` | Unmask a service |
| `query` | `(name) → ServiceInfo` | Query active and enabled state |
| `daemon_reload` | `() → ServiceActionResult` | Reload the systemd daemon |
| `get_journal` | `(name, lines=50) → str` | Retrieve recent journal output |

### `ServiceManagerConfig`

**Location:** `source/packages/automojo/sysman/linux/windows/servicemanagement.py`

A dataclass holding runtime configuration for `ServiceManager`.

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands are passed with `elevated=True` |

---

## Data Models

### `ServiceActionResult`

Returned by all write operations (start, stop, restart, reload, enable, disable, mask,
unmask, daemon-reload).

```python
@dataclass
class ServiceActionResult:
    status: ServiceActionStatus   # SUCCESS | FAILED | SKIPPED | DRY_RUN
    service_name: str
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None     # the command that was (or would have been) run
```

### `ServiceActionStatus`

```python
class ServiceActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"   # already in desired state
    DRY_RUN  = "dry_run"   # would have acted, but dry_run=True
```

### `ServiceInfo`

Returned by `query()`.

```python
@dataclass
class ServiceInfo:
    service_name: str
    active_state: str    # "active" | "inactive" | "failed" | "not-found" | ...
    enabled_state: str   # "enabled" | "disabled" | "masked" | "not-found" | ...
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

`ServiceManager` depends on this interface, injected at construction time.

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult` (from `automojo-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    command: list[str]
```

---

## Command Mappings

| Operation | Command |
|---|---|
| Start service | `systemctl start <name>` |
| Stop service | `systemctl stop <name>` |
| Restart service | `systemctl restart <name>` |
| Reload service | `systemctl reload <name>` |
| Enable service | `systemctl enable <name>` |
| Disable service | `systemctl disable <name>` |
| Mask service | `systemctl mask <name>` |
| Unmask service | `systemctl unmask <name>` |
| Reload daemon | `systemctl daemon-reload` |
| Query active state | `systemctl is-active <name>` |
| Query enabled state | `systemctl is-enabled <name>` |
| Query journal | `journalctl -u <name> --no-pager -n <lines>` |

---

## Idempotency Guards

State is determined by calling `query()` before issuing write commands.
`query()` runs both `is-active` and `is-enabled` and stores the results in a `ServiceInfo`.

| Method | Guard condition | Expected state to skip |
|---|---|---|
| `start()` | `active_state == "active"` | Already running |
| `stop()` | `active_state == "inactive"` | Already stopped |
| `enable()` | `enabled_state == "enabled"` | Already enabled |
| `disable()` | `enabled_state == "disabled"` | Already disabled |
| `mask()` | `enabled_state == "masked"` | Already masked |
| `unmask()` | `enabled_state != "masked"` | Not masked, nothing to undo |
| `reload()` | `active_state != "active"` | Must be running; returns `FAILED` |

`restart()` and `daemon_reload()` are unconditional — they never skip.

---

## `systemctl is-active` and `is-enabled` Exit Codes

`systemctl is-active` and `is-enabled` return non-zero exit codes for non-active or
non-enabled states respectively.  These are not errors; they are informational.
`ServiceManager` always treats the stdout string as the state value and never treats a
non-zero exit code from these two commands as a failure.

| `is-active` stdout | Meaning |
|---|---|
| `active` | Service is running |
| `inactive` | Service is stopped normally |
| `failed` | Service has failed |
| `activating` | Service is starting |
| `not-found` | Unit does not exist |

| `is-enabled` stdout | Meaning |
|---|---|
| `enabled` | Starts at boot |
| `disabled` | Does not start at boot |
| `masked` | Completely blocked |
| `static` | Enabled by dependency only |
| `not-found` | Unit does not exist |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `ServiceQueryError` | `is-active`, `is-enabled`, or `journalctl` returns an unexpected error (not a state-informational non-zero) |

All other failures from write operations are surfaced as a `ServiceActionResult` with
`status=FAILED` so the coordinator can decide how to respond without catching exceptions.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── servicemanagement.py          ← ServiceManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_servicemanagement.py ← unit tests using MockCommandRunner
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` injected into `ServiceManager` at construction time.
- Tests verify the *arguments* passed to the runner, not live systemd state.
- `MockCommandRunner` is configured per-test to return specific `RunResult` values for
  `is-active` and `is-enabled` so that idempotency guard logic is exercised in isolation.
- Each public write method has: success path, skip/idempotency path, failure path, dry-run.
- `query()` is tested for each meaningful `active_state` and `enabled_state` value.
- `get_journal()` is tested for default line count and a custom line count.
- No test requires root access, a live `systemctl`, or `journald`.
