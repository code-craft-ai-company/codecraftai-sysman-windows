# Design

## Overview

The configuration management feature is implemented as a single module,
`automojo.sysman.linux.windows.configurationmanagement`, that exposes a
`ConfigurationManager` class.  The coordinator instantiates `ConfigurationManager` and
calls its methods to read, write, diff, backup, restore, validate, and adjust permissions
on system configuration files.

All file system mutations are delegated to a command runner provided by
`automojo-interfaces` using `tee`, `cp`, `chmod`, and `chown` commands.  Read operations
use `cat` and `stat` through the same runner.  This ensures dry-run mode, elevation, and
auditing apply uniformly to every operation without direct file system access in the module.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│        ConfigurationManager             │  automojo.sysman.linux.windows.configurationmanagement
│                                         │
│  write()           write_template()     │
│  backup()          restore()            │
│  diff()            validate()           │
│  set_permissions() set_ownership()      │
│  set_ini_key()     remove_ini_key()     │
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

### `ConfigurationManager`

**Location:** `source/packages/automojo/sysman/linux/windows/configurationmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `write` | `(path, content, mode=None, owner=None, group=None) → ConfigActionResult` | Write content to a file |
| `write_template` | `(path, template, variables, mode=None, owner=None, group=None) → ConfigActionResult` | Render template and write |
| `backup` | `(path) → ConfigActionResult` | Timestamped copy of a file |
| `restore` | `(backup_path) → ConfigActionResult` | Restore from a backup file |
| `diff` | `(path, desired_content) → ConfigDiffResult` | Unified diff of current vs desired |
| `validate` | `(path, validator_id) → ConfigValidationResult` | Run syntax validator |
| `set_permissions` | `(path, mode) → ConfigActionResult` | Apply chmod to file |
| `set_ownership` | `(path, user=None, group=None) → ConfigActionResult` | Apply chown to file |
| `set_ini_key` | `(path, section, key, value) → ConfigActionResult` | Update one INI key |
| `remove_ini_key` | `(path, section, key) → ConfigActionResult` | Remove one INI key |

### `ConfigurationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no writes, copies, or chmod/chown are executed |
| `elevated` | `bool` | `False` | When `True`, write commands run with `elevated=True` |

### `ValidatorRegistry`

A module-level dict mapping validator identifier strings to command builder callables.

```python
VALIDATORS: dict[str, Callable[[str], list[str]]] = {
    "nginx":   lambda path: ["nginx",      "-t", "-c", path],
    "sshd":    lambda path: ["sshd",       "-t", "-f", path],
    "httpd":   lambda path: ["httpd",      "-t", "-f", path],
    "named":   lambda path: ["named-checkconf",  path],
    "rsyslog": lambda path: ["rsyslogd",   "-N1", "-f", path],
    "sudoers": lambda path: ["visudo",     "-c", "-f", path],
}
```

Additional validators can be registered at runtime via `ConfigurationManager.register_validator()`.

---

## Data Models

### `ConfigActionResult`

```python
@dataclass
class ConfigActionResult:
    status: ConfigActionStatus    # SUCCESS | FAILED | SKIPPED | DRY_RUN
    path: str
    backup_path: str | None       # populated by backup() and restore()
    diff: str | None              # populated in DRY_RUN results when content changes
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### `ConfigActionStatus`

```python
class ConfigActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"
```

### `ConfigDiffResult`

```python
@dataclass
class ConfigDiffResult:
    path: str
    has_diff: bool
    diff: str          # unified diff string; empty string when has_diff=False
```

### `ConfigValidationResult`

```python
@dataclass
class ConfigValidationResult:
    path: str
    validator_id: str
    valid: bool
    exit_code: int
    stdout: str
    stderr: str
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

The `stdin` parameter is used by `write()` and `set_ini_key()` to pipe content through
`tee`.

---

## Command Mappings

| Operation | Command |
|---|---|
| Read file content | `cat <path>` |
| Read file stat | `stat -c "%a %U %G" <path>` (mode, user, group) |
| Write file | `tee <path>` with content on stdin |
| Copy (backup/restore) | `cp -p <src> <dst>` |
| Set permissions | `chmod <mode> <path>` |
| Set ownership | `chown <user>:<group> <path>` |
| Validate | validator-specific command (see `ValidatorRegistry`) |

---

## File Writing Strategy

All writes go through `tee <path>` with content passed as `stdin` to the runner.  This
avoids shell expansion issues with special characters in content and keeps the file write
within the runner's permission and dry-run envelope.

### Write Idempotency

Before writing, `ConfigurationManager` reads the current file content with `cat`.  If the
read returns a non-zero exit code (file does not exist) the current content is treated as
empty string.  A unified diff is computed in pure Python using `difflib.unified_diff`.  If
the diff is empty, `SKIPPED` is returned without writing.

### Template Rendering

Templates are rendered using Python's `string.Template` with `safe_substitute` to detect
missing keys.  After rendering, the result is passed to the standard write path.  Missing
keys cause a `ConfigTemplateError` to be raised before any file is touched.

### INI Key Management

INI files are read via `cat`, parsed in memory using Python's `configparser`, mutated, and
serialised back to a string.  The serialised string is compared to the current file content.
If unchanged, `SKIPPED` is returned.  Otherwise the new content is written via `tee`.
`configparser` preserves comments in sections it does not modify (Python 3.2+).

### Backup Naming

```
<filepath>.bak.<YYYYMMDDTHHMMSS>
```

Example: `/etc/nginx/nginx.conf.bak.20240315T143022`

Timestamp is generated in pure Python from `datetime.utcnow()` at call time.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `ConfigFileNotFoundError` | `backup()`, `restore()`, `set_permissions()`, `set_ownership()` called for a path that does not exist |
| `ConfigTemplateError` | A required variable is absent from the template variables dict |
| `ConfigValidatorNotFoundError` | `validate()` called with an unregistered validator identifier |

All other failures are surfaced as `ConfigActionResult` with `status=FAILED`.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── configurationmanagement.py    ← ConfigurationManager, config, models, exceptions,
                                     ValidatorRegistry

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_configurationmanagement.py
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` injected at construction time.
- `MockCommandRunner` is configured to return specific `RunResult` values keyed on the
  command args prefix (e.g. `["cat", ...]`, `["tee", ...]`, `["stat", ...]`).
- Diff computation is tested in pure Python without a runner (it uses `difflib` directly).
- Template rendering is tested in pure Python for both success and missing-variable cases.
- INI key management is tested by supplying mock `cat` output and verifying the `tee` stdin
  content that the runner receives.
- No test requires root access or real files on disk.
