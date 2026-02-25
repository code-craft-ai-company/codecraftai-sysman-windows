# Design

## Overview

The kernel parameters feature is implemented as two cooperating classes in a single module,
`automojo.sysman.linux.windows.kernelparameters`:

- **`KernelParamManager`** — manages sysctl kernel parameters at runtime and in persisted
  drop-in files under `/etc/sysctl.d/`.
- **`KernelModuleManager`** — manages kernel module load/unload at runtime and persisted
  boot-time load configuration under `/etc/modules-load.d/`.

Both classes are constructed with the same `ICommandRunner` and `KernelManagerConfig`.
The coordinator instantiates them directly and calls their methods to perform discrete
maintenance actions.

All system interaction is delegated to the command runner.  No direct file system access
or `subprocess` calls are made inside the module.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└──────────────┬──────────────────────────┘
               │ calls
       ┌───────┴────────────┐
       ▼                    ▼
┌──────────────────┐  ┌─────────────────────┐  automojo.sysman.linux.windows.kernelparameters
│ KernelParam      │  │ KernelModule        │
│ Manager          │  │ Manager             │
│                  │  │                     │
│ read()           │  │ load()              │
│ write()          │  │ unload()            │
│ persist()        │  │ query_loaded()      │
│ remove_persisted │  │ persist_load()      │
│ apply_all()      │  │ remove_persisted_   │
│ query_all()      │  │   load()            │
└──────┬───────────┘  └──────┬──────────────┘
       └──────────┬───────────┘
                  │ delegates to
                  ▼
┌─────────────────────────────────────────┐
│        ICommandRunner                   │  automojo-interfaces
│  run(args, elevated=False) → RunResult  │
└─────────────────────────────────────────┘
```

---

## Components

### `KernelParamManager`

**Location:** `source/packages/automojo/sysman/linux/windows/kernelparameters.py`

| Method | Signature | Description |
|---|---|---|
| `read` | `(key) → KernelParamInfo \| None` | Read current runtime value of a sysctl key |
| `write` | `(key, value) → KernelParamActionResult` | Set a sysctl key at runtime |
| `persist` | `(key, value, filename) → KernelParamActionResult` | Write key to drop-in file |
| `remove_persisted` | `(key, filename) → KernelParamActionResult` | Remove key from drop-in file |
| `apply_all` | `() → KernelParamActionResult` | Apply all persisted sysctl settings |
| `query_all` | `() → list[KernelParamInfo]` | Return all current sysctl key-value pairs |

### `KernelModuleManager`

| Method | Signature | Description |
|---|---|---|
| `load` | `(name, params=None) → KernelModuleActionResult` | Load a module with optional params |
| `unload` | `(name) → KernelModuleActionResult` | Unload a module |
| `query_loaded` | `() → list[KernelModuleInfo]` | List all loaded modules |
| `persist_load` | `(name, filename) → KernelModuleActionResult` | Add module to boot load file |
| `remove_persisted_load` | `(name, filename) → KernelModuleActionResult` | Remove module from boot load file |

### `KernelManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands or file writes are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `sysctl_dir` | `str` | `"/etc/sysctl.d"` | Drop-in directory for sysctl persistence |
| `modules_load_dir` | `str` | `"/etc/modules-load.d"` | Drop-in directory for module persistence |

---

## Data Models

### Kernel Parameter Types

```python
@dataclass
class KernelParamInfo:
    key: str
    value: str

class KernelParamActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class KernelParamActionResult:
    status: KernelParamActionStatus
    key: str
    current_value: str | None     # value before the action
    applied_value: str | None     # value after the action (or would-be in DRY_RUN)
    filename: str | None          # drop-in filename, for persist/remove operations
    diff: str | None              # unified diff for DRY_RUN file writes
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Kernel Module Types

```python
@dataclass
class KernelModuleInfo:
    name: str
    size_bytes: int
    use_count: int

class KernelModuleActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class KernelModuleActionResult:
    status: KernelModuleActionStatus
    module_name: str
    filename: str | None          # drop-in filename, for persist operations
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
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
        stdin: str | None = None,
    ) -> RunResult: ...
```

---

## Command Mappings

### Kernel Parameter Commands

| Operation | Command |
|---|---|
| Read single parameter | `sysctl -n <key>` |
| Write parameter at runtime | `sysctl -w <key>=<value>` |
| Apply all persisted parameters | `sysctl --system` |
| Read all parameters | `sysctl -a` |
| Read drop-in file | `cat <sysctl_dir>/<filename>.conf` |
| Write drop-in file | `tee <sysctl_dir>/<filename>.conf` with content on stdin |

### Kernel Module Commands

| Operation | Command |
|---|---|
| Load module | `modprobe <name> [param=value ...]` |
| Unload module | `modprobe -r <name>` |
| List loaded modules | `lsmod` |
| Read module boot-load file | `cat <modules_load_dir>/<filename>.conf` |
| Write module boot-load file | `tee <modules_load_dir>/<filename>.conf` with content on stdin |

---

## Drop-in File Management

### sysctl Drop-in Files

Files in `/etc/sysctl.d/` use a simple `key = value` format, one entry per line, with
`#` comment lines.  `KernelParamManager` reads the file via `cat`, parses it in pure
Python, and performs the requested mutation in memory before writing back via `tee`.

**Parsing rules:**
- Lines beginning with `#` or empty lines are preserved verbatim.
- Non-comment lines are split on the first `=` character; leading/trailing whitespace is
  stripped from both key and value.
- Inline comments (` # ...`) after a value are preserved during reads but stripped during
  comparison to avoid false positive mismatches.

**Persist logic:**
1. Read file (empty string if absent).
2. Scan lines for a matching key.
3. If found with same value → `SKIPPED`.
4. If found with different value → replace the line.
5. If not found → append the line.
6. Write updated content via `tee`.

**Remove logic:**
1. Read file; if absent → `SKIPPED`.
2. Scan lines for matching key; if absent → `SKIPPED`.
3. Remove the matching line; write updated content via `tee`.

### modules-load Drop-in Files

Files in `/etc/modules-load.d/` contain one module name per line with `#` comment support.

**Persist logic:**
1. Read file (empty string if absent).
2. Scan non-comment lines for exact module name match.
3. If found → `SKIPPED`.
4. If not found → append module name line; write via `tee`.

**Remove logic:**
1. Read file; if absent → `SKIPPED`.
2. Scan non-comment lines; if name absent → `SKIPPED`.
3. Remove matching line; write updated content via `tee`.

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `KernelParamManager.write()` | Current value matches desired | `sysctl -n <key>` |
| `KernelParamManager.persist()` | Key present with same value in file | `cat` + in-memory parse |
| `KernelParamManager.remove_persisted()` | Key absent from file or file absent | `cat` + in-memory parse |
| `KernelModuleManager.load()` | Module name present in `lsmod` output | `lsmod` parse |
| `KernelModuleManager.unload()` | Module name absent from `lsmod` output | `lsmod` parse |
| `KernelModuleManager.persist_load()` | Module name present in file | `cat` + in-memory parse |
| `KernelModuleManager.remove_persisted_load()` | Module name absent or file absent | `cat` + in-memory parse |

---

## `sysctl -a` Output Parsing

`sysctl -a` produces lines in the form `key = value`.  Lines that `sysctl` cannot read
due to permission restrictions are emitted to stderr and do not appear in stdout.  The
parser splits each stdout line on the first ` = ` separator and ignores any lines that do
not match this pattern.

## `lsmod` Output Parsing

`lsmod` produces a fixed-width table.  The first line is a header (`Module  Size  Used by`).
Subsequent lines have the format `<name>  <size>  <use_count>  [dependents]`.  The parser
splits on whitespace and reads the first three fields, discarding the dependents column.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `KernelParamQueryError` | `sysctl -n` or `sysctl -a` fails for an unexpected reason |
| `KernelModuleQueryError` | `lsmod` returns a non-zero exit code |

All other failures from write operations are surfaced as action results with `status=FAILED`.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── kernelparameters.py           ← KernelParamManager, KernelModuleManager,
                                     config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_kernelparameters.py
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix.
- `sysctl -n`, `sysctl -a`, and `lsmod` mock responses drive idempotency guards.
- `cat` mock responses supply drop-in file content for persist/remove tests.
- `sysctl_dir` and `modules_load_dir` in config are overridden in tests to avoid touching
  system paths.
- `tee` stdin content is verified in persist tests to confirm correct file rendering.
- `lsmod` and `sysctl -a` parsing tests are pure Python using representative output strings.
- No test requires root access or a live kernel.
