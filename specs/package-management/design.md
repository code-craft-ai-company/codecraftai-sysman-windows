# Design

## Overview

The package management feature is implemented as a single module,
`automojo.sysman.linux.windows.packagemanagement`, that exposes a `PackageManager` class.
The coordinator instantiates `PackageManager` and calls its methods to perform discrete
maintenance actions.

All system interaction is delegated to a command runner provided by `automojo-interfaces`.
The `PackageManager` never directly calls `subprocess` or shell commands; it only constructs
command argument lists and passes them to the runner.  This keeps business logic separate from
execution and makes the feature fully testable via a mock runner without root access or a
live system.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
│                                         │
│   coordinator.perform_maintenance(...)  │
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│            PackageManager               │  automojo.sysman.linux.windows.packagemanagement
│                                         │
│  install()   remove()   update()        │
│  query()     list_updates()             │
│  install_local()                        │
│  hold()      release_hold()             │
│  group_install()  group_remove()        │
└───────────────────┬─────────────────────┘
                    │ delegates to
                    ▼
┌─────────────────────────────────────────┐
│        ICommandRunner                   │  automojo-interfaces
│                                         │
│  run(args, elevated=False) → RunResult  │
└─────────────────────────────────────────┘
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
  LiveCommandRunner       MockCommandRunner
  (dnf / rpm / sudo)      (test doubles)
```

---

## Components

### `PackageManager`

**Location:** `source/packages/automojo/sysman/linux/windows/packagemanagement.py`

The primary public class.  Constructed with an `ICommandRunner` and a `PackageManagerConfig`.

| Method | Description |
|---|---|
| `install(name, version=None)` | Install a package, optionally at a specific version |
| `remove(name)` | Remove an installed package |
| `update(name, version=None)` | Update a package, optionally to a specific version |
| `query(name)` → `PackageInfo \| None` | Return installed package info or None |
| `list_updates()` → `list[PackageUpdateInfo]` | List all packages with available updates |
| `install_local(filepath)` | Install from a local `.rpm` file path |
| `hold(name)` | Pin a package version using dnf versionlock |
| `release_hold(name)` | Remove a dnf versionlock pin |
| `group_install(group_name)` | Install a dnf package group |
| `group_remove(group_name)` | Remove a dnf package group |

### `PackageManagerConfig`

**Location:** `source/packages/automojo/sysman/linux/windows/packagemanagement.py`

A dataclass holding runtime configuration for `PackageManager`.

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands are prefixed with sudo |

---

## Data Models

### `PackageActionResult`

Returned by all write operations (install, remove, update, hold, group install/remove).

```python
@dataclass
class PackageActionResult:
    status: PackageActionStatus   # SUCCESS | FAILED | SKIPPED | DRY_RUN
    package_name: str
    version: str | None           # resolved version after action, if known
    exit_code: int | None         # raw exit code from command runner
    stdout: str | None
    stderr: str | None
    command: list[str] | None     # the command that was (or would have been) run
```

### `PackageActionStatus`

```python
class PackageActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"   # already in desired state, no action needed
    DRY_RUN  = "dry_run"   # would have acted, but dry_run=True
```

### `PackageInfo`

Returned by `query()`.

```python
@dataclass
class PackageInfo:
    name: str
    version: str
    release: str
    arch: str
```

### `PackageUpdateInfo`

Returned as elements of `list_updates()`.

```python
@dataclass
class PackageUpdateInfo:
    name: str
    installed_version: str
    available_version: str
    arch: str
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

`PackageManager` depends on this interface, injected at construction time.

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

### Elevation Strategy

When `PackageManagerConfig.elevated` is `True`, the `PackageManager` passes
`elevated=True` to `ICommandRunner.run()`.  The runner is responsible for prepending
`sudo` or using whatever privilege escalation mechanism is appropriate for the environment.
`PackageManager` has no knowledge of how elevation is implemented.

---

## Command Mappings

| Operation | Command |
|---|---|
| Install by name | `dnf install -y <name>` |
| Install at version | `dnf install -y <name>-<version>` |
| Remove | `dnf remove -y <name>` |
| Update latest | `dnf update -y <name>` |
| Update to version | `dnf install -y <name>-<version>` |
| Query installed | `rpm -q --queryformat '%{NAME} %{VERSION} %{RELEASE} %{ARCH}\n' <name>` |
| List updates | `dnf check-update` |
| Install local file | `dnf install -y <filepath>` |
| Hold version | `dnf versionlock add <name>` |
| Release hold | `dnf versionlock delete <name>` |
| Group install | `dnf groupinstall -y "<group>"` |
| Group remove | `dnf groupremove -y "<group>"` |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `PackageQueryError` | `rpm -q` or `dnf check-update` returns an unexpected non-zero exit code |
| `PackageFileNotFoundError` | `install_local()` is called with a path that does not exist on disk |

All other failures are surfaced as a `PackageActionResult` with `status=FAILED` so the
coordinator can decide how to respond without catching exceptions for normal operational
failures.

### `dnf check-update` Exit Code 100

`dnf check-update` exits with code `100` when updates are available (not an error) and
code `0` when the system is fully up to date.  `PackageManager` treats both as success.
Any other exit code raises `PackageQueryError`.

### Idempotency Guards

Before executing write commands, `PackageManager` calls `query()` to determine current
state.  If the system is already in the desired state the method returns `SKIPPED` without
invoking `dnf`.  This makes all write operations safe to call multiple times.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── packagemanagement.py          ← PackageManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_packagemanagement.py ← unit tests using MockCommandRunner
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` injected into `PackageManager` at construction time.
- Tests verify the *arguments* passed to the runner, not system state.
- Each public method has at minimum: a success path test, a skip/idempotency test, a
  failure path test, and a dry-run test.
- `list_updates()` has an additional test for exit code `100` being treated as success.
- `install_local()` has a test for `PackageFileNotFoundError` before the runner is called.
- No test requires root access or a live `dnf` installation.
