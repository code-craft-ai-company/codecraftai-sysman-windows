# Design

## Overview

The environment configuration feature is implemented as a single module,
`automojo.sysman.linux.windows.environmentconfiguration`, that exposes an
`EnvironmentConfigurator` class.  The coordinator instantiates `EnvironmentConfigurator`
and calls its methods to set, unset, and query environment variables in system-wide and
per-user configuration files.

All file reads and writes go through the `IFileOperator` abstraction from
`automojo-interfaces`.  User home directory resolution is the only place where
`pwd.getpwnam` is called directly, isolated in a private helper.  No subprocess calls
are made; no direct open/write calls touch the filesystem outside the file operator.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│        EnvironmentConfigurator          │  automojo.sysman.linux.windows.environmentconfiguration
│                                         │
│  set_system_var()   unset_system_var()  │
│  set_etc_env_var()  unset_etc_env_var() │
│  set_user_var()     unset_user_var()    │
│  query_system()     query_etc_env()     │
│  query_user()                           │
└───────────────────┬─────────────────────┘
                    │ delegates to
                    ▼
┌─────────────────────────────────────────┐
│        IFileOperator                    │  automojo-interfaces
│  read_text(path) → str                  │
│  write_text(path, content, elevated)    │
│  delete(path, elevated)                 │
│  exists(path) → bool                   │
└─────────────────────────────────────────┘
```

---

## Components

### `EnvironmentConfigurator`

**Location:** `source/packages/automojo/sysman/linux/windows/environmentconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `set_system_var` | `(name, value, filename) → EnvActionResult` | Set `export NAME="value"` in `/etc/profile.d/<filename>` |
| `unset_system_var` | `(name, filename) → EnvActionResult` | Remove `export NAME=...` from `/etc/profile.d/<filename>` |
| `set_etc_env_var` | `(name, value) → EnvActionResult` | Set `NAME="value"` in `/etc/environment` |
| `unset_etc_env_var` | `(name) → EnvActionResult` | Remove `NAME=...` from `/etc/environment` |
| `set_user_var` | `(username, name, value, rc_file) → EnvActionResult` | Set `export NAME="value"` in `~<username>/<rc_file>` |
| `unset_user_var` | `(username, name, rc_file) → EnvActionResult` | Remove `export NAME=...` from `~<username>/<rc_file>` |
| `query_system` | `(filename) → dict[str, str]` | Read variables from `/etc/profile.d/<filename>` |
| `query_etc_env` | `() → dict[str, str]` | Read variables from `/etc/environment` |
| `query_user` | `(username, rc_file) → dict[str, str]` | Read variables from `~<username>/<rc_file>` |

### `EnvironmentConfiguratorConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no files are written or deleted |
| `elevated` | `bool` | `False` | When `True`, system-wide writes pass `elevated=True` |

---

## Data Models

### Result Types

```python
class EnvActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class EnvActionResult:
    status: EnvActionStatus
    path: str | None          # file that was (or would be) modified
    name: str | None          # variable name
    value: str | None         # new value (None for unset operations)
    message: str | None       # human-readable detail for failures/skips
```

### Exception

```python
class EnvConfigError(Exception):
    """Raised when a query operation cannot complete (e.g., unknown user)."""
```

---

## File Format Conventions

### `/etc/profile.d/<filename>` (set_system_var / unset_system_var)

- Created with a `#!/usr/bin/env bash` shebang when the file does not exist.
- Each managed variable occupies exactly one line: `export NAME="value"`.
- Values containing double quotes are escaped with `\"`.
- Lines not matching `export NAME=...` for the target name are left untouched.
- After removing a variable, if the remaining file content has no non-shebang,
  non-blank lines the file is deleted.

### `/etc/environment` (set_etc_env_var / unset_etc_env_var)

- No shebang line.
- Each managed variable occupies exactly one line: `NAME="value"`.
- Values containing double quotes are escaped with `\"`.
- Lines not matching `NAME=...` for the target name are left untouched.

### `~<username>/<rc_file>` (set_user_var / unset_user_var)

- File created with mode 0644 if it does not exist.
- Each managed variable occupies exactly one line: `export NAME="value"`.
- Values containing double quotes are escaped with `\"`.
- Lines not matching `export NAME=...` for the target name are left untouched.

---

## Parsing

### `_parse_export_lines(text: str) -> dict[str, str]`

Used by `query_system()` and `query_user()`.

1. Split text into lines.
2. For each line: strip whitespace; skip blank lines and lines starting with `#`.
3. Match against `^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$`.
4. Strip optional surrounding double quotes from the value.
5. Return `{name: value}`.

### `_parse_env_lines(text: str) -> dict[str, str]`

Used by `query_etc_env()`.

1. Split text into lines.
2. For each line: strip whitespace; skip blank lines and lines starting with `#`.
3. Match against `^([A-Za-z_][A-Za-z0-9_]*)=(.*)$`.
4. Strip optional surrounding double quotes from the value.
5. Return `{name: value}`.

---

## Line Update Strategy

All set and unset operations use an **in-place line rewrite** approach:

```
current_lines = file_operator.read_text(path).splitlines(keepends=True)
new_lines = _apply_change(current_lines, name, value_or_None)
file_operator.write_text(path, "".join(new_lines), elevated=elevated)
```

`_apply_change` for **set** operations:
1. Scan lines for one matching the target variable name.
2. If found with same value: return lines unchanged (caller returns `SKIPPED`).
3. If found with different value: replace that line; return updated lines.
4. If not found: append new line; return updated lines.

`_apply_change` for **unset** operations:
1. Scan lines for one matching the target variable name.
2. If not found: return lines unchanged (caller returns `SKIPPED`).
3. If found: remove that line; return updated lines.

Trailing newline is preserved: if the original file ends with `\n` the rewritten file
does too.

---

## User Home Directory Resolution

```python
def _resolve_home(username: str) -> str:
    import pwd
    try:
        return pwd.getpwnam(username).pw_dir
    except KeyError:
        raise EnvConfigError(f"User not found: {username}")
```

Called by `set_user_var`, `unset_user_var`, and `query_user` before any file operation.

---

## Idempotency Guards

| Method | Guard | Mechanism |
|---|---|---|
| `set_system_var` | Variable already present with same value | Parse file, compare |
| `unset_system_var` | Variable not present in file | Parse file, check presence |
| `set_etc_env_var` | Variable already present with same value | Parse file, compare |
| `unset_etc_env_var` | Variable not present in file | Parse file, check presence |
| `set_user_var` | Variable already present with same value | Parse file, compare |
| `unset_user_var` | Variable not present in file | Parse file, check presence |

When a file does not exist:
- **set** operations: skip the read, write a new file.
- **unset** operations: return `SKIPPED` immediately.

---

## File Deletion After Unset

For `unset_system_var`: after removing the target line, if the remaining lines contain
no non-shebang non-blank content, `file_operator.delete(path, elevated=elevated)` is
called instead of `write_text`.

For `unset_etc_env_var` and `unset_user_var`: empty files are written rather than
deleted (preserving the file as a managed artifact).

---

## Dry-Run Behaviour

When `EnvironmentConfiguratorConfig.dry_run=True` all write and delete calls to the file
operator are skipped.  Parse operations (reads) still occur so that the guard logic can
determine what *would* happen, enabling accurate `SKIPPED` vs `DRY_RUN` reporting.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── environmentconfiguration.py   ← EnvironmentConfigurator, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_environmentconfiguration.py
```

---

## Testing Strategy

- All tests use a `MockFileOperator` backed by an in-memory `dict[str, str]` mapping
  paths to file contents.
- `_parse_export_lines` and `_parse_env_lines` are tested in pure Python with
  representative multi-line strings including blank lines, comments, and values with
  embedded quotes.
- User resolution is tested by monkeypatching `pwd.getpwnam`.
- The post-unset file deletion path for `/etc/profile.d/` is tested by verifying
  `MockFileOperator.delete` is called when only the shebang line remains.
- Dry-run tests verify that `MockFileOperator.write_text` and `.delete` are never called
  while reads still occur.
- No test requires root access or a live filesystem.
