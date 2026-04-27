# Design

## Overview

The ssh-server-configuration feature is implemented as a single
module, `automojo.sysman.windows.sshserverconfiguration`, that
exposes a `WindowsSshServerConfigurationManager` class.  This spec
implements the ssh-server-configuration contract defined at
`automojo-sysman-common/specs/contracts/ssh-server-configuration/contract.md`
for Windows using the OpenSSH server feature shipped with current
Windows Server / Windows 10 / Windows 11 releases.

All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct filesystem access or
`subprocess` calls are made inside the module — file I/O routes
through the runner via PowerShell `Get-Content` and `Set-Content`,
and service interaction routes through PowerShell `Restart-Service`.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH or WinRM (via
`SSHCommandRunner` or a future `WinRMCommandRunner`).

**See Also:** [Command Runner Architecture](../1-command-execution/design.md) for
details on local/remote execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or remote)                    │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsSshServerConfigurationManager │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        WindowsSshServerConfigurationManager                 │
│  automojo.sysman.windows.sshserverconfiguration             │
│                                                             │
│  get_sshd_setting()       set_sshd_setting()                │
│  remove_sshd_setting()    list_sshd_settings()              │
│  add_match_block()        remove_match_block()              │
│  list_match_blocks()      reload_sshd()                     │
│  test_sshd_config()       list_authorized_keys()            │
│  add_authorized_key()     remove_authorized_key()           │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output, stdin) → RunResult     │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - UAC for elevation  │      │ - reliability metrics│
│ - local execution    │      │ - connection retry   │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsSshServerConfigurationManager`

**Location:**
`source/packages/automojo/sysman/windows/sshserverconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `get_sshd_setting` | `(keyword) → WindowsSshSettingValue \| None` | Read a top-level keyword |
| `set_sshd_setting` | `(keyword, value) → WindowsSshActionResult` | Upsert a top-level keyword |
| `remove_sshd_setting` | `(keyword) → WindowsSshActionResult` | Drop a top-level keyword |
| `list_sshd_settings` | `() → dict[str, str]` | Enumerate top-level keywords |
| `add_match_block` | `(criteria, settings) → WindowsSshActionResult` | Upsert a Match block |
| `remove_match_block` | `(criteria) → WindowsSshActionResult` | Drop a Match block |
| `list_match_blocks` | `() → list[WindowsSshMatchBlock]` | Enumerate Match blocks |
| `reload_sshd` | `() → WindowsSshActionResult` | `Restart-Service -Name 'sshd'` |
| `test_sshd_config` | `() → WindowsSshActionResult` | `sshd.exe -t` |
| `list_authorized_keys` | `(user, *, is_admin=False) → list[WindowsSshAuthorizedKey]` | Read per-user OR admin file |
| `add_authorized_key` | `(user, key, *, is_admin=False) → WindowsSshActionResult` | Append to per-user OR admin file |
| `remove_authorized_key` | `(user, fingerprint, *, is_admin=False) → WindowsSshActionResult` | Drop from per-user OR admin file |

### `WindowsSshServerConfigurationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `sshd_config_path` | `str` | `r"C:\ProgramData\ssh\sshd_config"` | Primary `sshd_config` |
| `authorized_keys_path_template` | `str` | `r"C:\Users\{user}\.ssh\authorized_keys"` | Per-user key path |
| `admin_authorized_keys_path` | `str` | `r"C:\ProgramData\ssh\administrators_authorized_keys"` | Admin shared key path |
| `service_name` | `str` | `"sshd"` | Windows service name |
| `powershell_executable` | `str` | `"powershell.exe"` | Override for cmdlet runner |

---

## Data Models

### Result Types

```python
class WindowsSshActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class WindowsSshActionResult(FeatureActionResult):
    status: WindowsSshActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    keyword: str | None
    path: str | None
    user: str | None
    message: str | None
```

### Query Result Types

```python
@dataclass
class WindowsSshSettingValue:
    keyword: str
    value: str
    source_file: str

@dataclass
class WindowsSshMatchBlock:
    criteria: str
    settings: dict[str, str]

@dataclass
class WindowsSshAuthorizedKey:
    key_type: str
    key_data: str
    comment: str | None
    fingerprint: str | None
    options: str | None
```

### Exceptions

```python
class WindowsSshQueryError(Exception):
    """Raised when a read fails for a reason other than 'not present'."""
```

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
        stdin: str | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

The manager consumes only `args`, `elevated`, and `stdin`.  Reads use
PowerShell `Get-Content`; writes use PowerShell `Set-Content` reading
the rendered content from stdin (`[Console]::In.ReadToEnd()`).  No
`IFileOperator` is consumed.

---

## Configuration Path Mapping

| OS | Config path |
|----|-------------|
| RHEL | `/etc/ssh/sshd_config` |
| Ubuntu | `/etc/ssh/sshd_config` |
| macOS | `/etc/ssh/sshd_config` |
| **Windows** | **`%ProgramData%\ssh\sshd_config`** (default `C:\ProgramData\ssh\sshd_config`) |

| OS | Reload mechanism |
|----|-----------------|
| RHEL | `systemctl reload sshd` |
| Ubuntu | `systemctl reload ssh` |
| macOS | `launchctl kickstart -k system/com.openssh.sshd` |
| **Windows** | **`Restart-Service -Name 'sshd'`** |

The Windows divergence is the path syntax (backslashes, `%ProgramData%`
or its expanded `C:\ProgramData\` form) and the lack of a true
`reload` — Windows OpenSSH must be fully restarted to pick up
configuration changes.

---

## Admin-vs-Non-Admin Authorized Keys (Windows-specific)

Windows OpenSSH stamps the stock `sshd_config` with:

```
Match Group administrators
    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
```

This means:

- For ordinary users, `AuthorizedKeysFile` defaults to
  `.ssh/authorized_keys` resolved against `%UserProfile%`, i.e.
  `%UserProfile%\.ssh\authorized_keys` (typically
  `C:\Users\<user>\.ssh\authorized_keys`).
- For users in the `Administrators` group, the path is
  `%ProgramData%\ssh\administrators_authorized_keys` (a single
  file shared across all administrators on the host).

The manager's authorized-key verbs accept an `is_admin: bool` keyword
that selects between the two paths:

| `is_admin` | File written |
|------------|--------------|
| `False` (default) | `authorized_keys_path_template` rendered with `{user}` |
| `True` | `admin_authorized_keys_path` (verbatim, ignores `user`) |

This is the most important Windows-specific consideration in the
ssh-server-configuration surface.  Callers MUST decide because
Windows determines admin status from group membership at runtime,
not from the username.

---

## sshd_config Parsing

The grammar, parser shape, and renderer shape are identical to the
Linux / macOS siblings:

- Each non-comment, non-empty line is `<Keyword> <value>` or `Match
  <criteria>`.
- The keyword is case-insensitive.
- A `Match <criteria>` line opens a block; subsequent setting lines
  belong to the block until the next `Match` or EOF.

`_parse_sshd_config(content) → (top_settings, match_blocks)`:

- `top_settings: dict[str, str]` — keyword (original case) to value;
  last occurrence wins on read.
- `match_blocks: list[WindowsSshMatchBlock]` — every `Match` block
  in declaration order.

`_render_sshd_config(top_settings, match_blocks) → str`:

1. Top-level settings in dict order.
2. For each `Match` block: a `Match <criteria>` line followed by
   four-space-indented child settings.
3. Trailing newline.

Comments and blank lines are not preserved across mutation; this is
documented in `tasks.md` as a follow-up.

---

## Match Block Idempotency

`add_match_block` and `remove_match_block` use whitespace-collapsed
criteria comparison.  Settings are compared as-rendered: a block
exists if the rendered `sshd_config` after upsert is byte-equal to
the existing file.

The stock `Match Group administrators` block is expected to be
present in every Windows host's `sshd_config`.  Coordinators that
need to modify its child settings do so through `add_match_block`
with the criteria `"Group administrators"` — the manager replaces
the block's settings in place.

---

## authorized_keys Parsing

### Grammar

```
[options ]<key-type> <key-data> [<comment>]
```

Recognised key types: `ssh-rsa`, `ssh-dss`, `ssh-ed25519`,
`ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521`,
`sk-ecdsa-sha2-nistp256@openssh.com`, `sk-ssh-ed25519@openssh.com`.

### Fingerprint Computation

```python
digest = hashlib.sha256(base64.b64decode(key_data)).digest()
fingerprint = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
```

Matches `ssh-keygen -lf` output exactly.

---

## Command Mappings

### Write Commands

| Operation | Command (script) | stdin |
|---|---|---|
| Set keyword | `Set-Content -Path '<path>' ...` | rendered file |
| Remove keyword | `Set-Content -Path '<path>' ...` | rendered file |
| Add Match block | `Set-Content -Path '<path>' ...` | rendered file |
| Remove Match block | `Set-Content -Path '<path>' ...` | rendered file |
| Add authorized key | `Set-Content -Path '<path>' ...` | rendered file |
| Remove authorized key | `Set-Content -Path '<path>' ...` | rendered file |
| Reload | `Restart-Service -Name 'sshd'` | — |
| Test config | `sshd.exe -t` (no PowerShell wrapper) | — |

The `Set-Content` script reads from stdin via `$content =
[Console]::In.ReadToEnd();` and then invokes `Set-Content -Path
'<path>' -Value $content -NoNewline -Encoding utf8`.  This pattern is
shared with `WindowsConfigurationManager` so its semantics are well
understood.

### Read Commands (no elevation)

| Operation | Command (script) |
|---|---|
| Read sshd_config | `Get-Content -Path '<path>' -Raw -ErrorAction Stop` |
| Read authorized_keys | `Get-Content -Path '<path>' -Raw -ErrorAction Stop` |

All read commands pass `elevated=False` regardless of
`config.elevated`.

### PowerShell Argv Shape

Every PowerShell-based command is dispatched as:

```python
[
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    "<script>",
]
```

`-NoProfile` and `-NonInteractive` keep dispatch deterministic and
fast.  Path values are interpolated into the script using single
quotes; embedded single quotes are doubled per
`_ps_escape_single_quoted`.  This is the same convention used by
`WindowsConfigurationManager`.

---

## Idempotency Guards

| Method | Guard | Comparison |
|---|---|---|
| `set_sshd_setting` | Rendered content matches existing | Byte-equal |
| `remove_sshd_setting` | Keyword absent OR file absent | Pure-Python |
| `add_match_block` | Rendered content matches existing | Byte-equal |
| `remove_match_block` | No block with matching criteria | Whitespace-collapsed |
| `add_authorized_key` | Fingerprint already present | SHA-256 |
| `remove_authorized_key` | Fingerprint not present | SHA-256 |
| `reload_sshd` | (none — always executes) | — |
| `test_sshd_config` | (none — always executes) | — |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `WindowsSshQueryError` | Reserved for future read paths.  The pilot's `Get-Content`-based reads treat any non-zero exit as "absent" |

All other failures from write operations are surfaced as
`WindowsSshActionResult` with `status=FAILED`.  Writes do not raise.

---

## Correctness Properties

### Property 1: Read Returns Parsed Value

*For any* `sshd_config` containing a top-level `<keyword> <value>`,
`get_sshd_setting(keyword)` returns a `WindowsSshSettingValue` with
matching fields.

**Validates: Requirements 1.1, 1.2**

### Property 2: Read Returns None When Absent

*For any* `sshd_config` not containing the keyword,
`get_sshd_setting(keyword)` returns `None`.

**Validates: Requirements 1.3, 1.4**

### Property 3: Read Never Elevates

*For any* call to a read verb, the runner is invoked with
`elevated=False` regardless of `config.elevated`.

**Validates: Requirements 1.5, 4.3, 7.3, 10.5, 13.2**

### Property 4: Set Idempotency

*For any* `sshd_config` already containing `<keyword> <value>`,
`set_sshd_setting(keyword, value)` returns `SKIPPED` without invoking
`Set-Content`.

**Validates: Requirement 2.2**

### Property 5: Set Replaces Existing Value

*For any* `sshd_config` containing `<keyword> <other-value>`,
`set_sshd_setting(keyword, value)` writes a rendered file in which
the keyword's value is `value`.

**Validates: Requirement 2.3**

### Property 6: Set Creates Missing File

*For any* call to `set_sshd_setting(keyword, value)` when the file
does not exist, the manager writes a single-line file with `<keyword>
<value>`.

**Validates: Requirement 2.4**

### Property 7: Remove Skipped When Absent

*For any* `sshd_config` not containing the keyword,
`remove_sshd_setting(keyword)` returns `SKIPPED`.

**Validates: Requirements 3.1, 3.2**

### Property 8: List Returns Top-Level Only

*For any* `sshd_config` with both top-level settings and a `Match`
block, `list_sshd_settings()` returns only the top-level settings.

**Validates: Requirement 4.2**

### Property 9: Add Match Block Idempotency

*For any* `sshd_config` already containing a `Match` block with
identical criteria and settings, `add_match_block(criteria, settings)`
returns `SKIPPED`.

**Validates: Requirement 5.2**

### Property 10: Add Match Block Replaces Settings

*For any* `sshd_config` containing a `Match` block with the matching
criteria but different settings, `add_match_block(criteria, settings)`
writes a rendered file in which the block's settings equal `settings`.

**Validates: Requirement 5.3**

### Property 11: Remove Match Block Idempotency

*For any* `sshd_config` without a matching `Match` block,
`remove_match_block(criteria)` returns `SKIPPED`.

**Validates: Requirement 6.1**

### Property 12: List Match Blocks Returns Declaration Order

*For any* `sshd_config` with multiple `Match` blocks,
`list_match_blocks()` returns them in declaration order.

**Validates: Requirement 7.2**

### Property 13: Reload Command Construction

*For any* call to `reload_sshd()`, the manager dispatches a
PowerShell argv whose script contains `Restart-Service -Name 'sshd'`.

**Validates: Requirement 8.1**

### Property 14: Test Config Command Construction

*For any* call to `test_sshd_config()`, the manager invokes
`sshd.exe -t` via the runner (no PowerShell wrapper).

**Validates: Requirement 9.1**

### Property 15: Test Config Failure Maps to FAILED

*For any* `sshd.exe -t` invocation returning non-zero, the result has
`status=FAILED` with `stderr` populated.

**Validates: Requirement 9.3**

### Property 16: Per-User Authorized-Keys Path

*For any* call to `list_authorized_keys(user, is_admin=False)`, the
manager reads `authorized_keys_path_template.format(user=user)`.

**Validates: Requirement 10.2**

### Property 17: Admin Authorized-Keys Path

*For any* call to `list_authorized_keys(user, is_admin=True)`, the
manager reads `admin_authorized_keys_path` and ignores the supplied
`user` for path resolution.

**Validates: Requirement 10.3**

### Property 18: Authorized-Key Parser Recognises Common Types

*For any* `authorized_keys` line of the form `<key-type> <key-data>
[<comment>]` with `<key-type>` in the recognised set, the parser
populates `key_type`, `key_data`, and `comment` correctly.

**Validates: Requirement 10.4**

### Property 19: Add Authorized Key Routes by `is_admin`

*For any* call to `add_authorized_key(user, key, is_admin=True)`,
the manager writes `admin_authorized_keys_path` rather than the
per-user template.

**Validates: Requirement 11.3**

### Property 20: Add Authorized Key Idempotency

*For any* user / file with an existing entry whose fingerprint
matches the supplied key, `add_authorized_key` returns `SKIPPED`.

**Validates: Requirement 11.4**

### Property 21: Add Authorized Key Format Validation

*For any* `key` that does not parse as an OpenSSH `authorized_keys`
line, `add_authorized_key` returns `FAILED` with `message="key line
failed to parse"` without invoking the runner.

**Validates: Requirement 11.2**

### Property 22: Remove Authorized Key By Fingerprint

*For any* user / file with multiple entries,
`remove_authorized_key(user, fingerprint)` writes a file in which no
remaining line's fingerprint equals the supplied fingerprint.

**Validates: Requirement 12.3**

### Property 23: Remove Authorized Key Idempotency

*For any* user / file without a matching fingerprint,
`remove_authorized_key` returns `SKIPPED`.

**Validates: Requirement 12.2**

### Property 24: Dry-Run Safety

*For any* write verb with `dry_run=True`, the runner is never invoked
for the write command and the result status is `DRY_RUN`.

**Validates: Requirements 2.5, 3.4, 5.5, 6.4, 8.5, 9.4, 11.6, 12.5**

### Property 25: Elevation for Write Operations

*For any* write verb invoked with `config.elevated=True`, the runner
is invoked with `elevated=True` for the mutating command.

**Validates: Requirement 13.1**

### Property 26: Error Result Consistency

*For any* failed runner command, the returned
`WindowsSshActionResult` has `status=FAILED` and contains the
`exit_code`, `stdout`, `stderr`, and `command` from the runner.

**Validates: Requirements 2.6, 3.5, 5.6, 8.4, 9.3, 11.7, 12.6**

---

## Module Layout

```
source/packages/automojo/sysman/windows/
├── __init__.py
└── sshserverconfiguration.py   ← WindowsSshServerConfigurationManager,
                                   config, models, exceptions, parser
                                   helpers

source/testroots/testbase/
└── automojo/tests/sysman/windows/
    └── test_sshserverconfiguration.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.windows.runners import (
    LocalCommandRunner,
    LocalRunnerConfig,
)
from automojo.sysman.windows.sshserverconfiguration import (
    WindowsSshServerConfigurationManager,
    WindowsSshServerConfigurationManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = WindowsSshServerConfigurationManagerConfig(elevated=True)
mgr = WindowsSshServerConfigurationManager(runner=runner, config=config)

# Disable root login
result = mgr.set_sshd_setting(keyword="PermitRootLogin", value="no")
print(result.status)

# Add a Match block restricting password auth for a service account
mgr.add_match_block(
    criteria="User svc-deploy",
    settings={"PasswordAuthentication": "no"},
)

# Authorise a domain-admin's key into the shared admin file
mgr.add_authorized_key(
    user="alice",
    key="ssh-ed25519 AAAA...== alice@admin",
    is_admin=True,
)

# Validate, then restart
test_result = mgr.test_sshd_config()
if test_result.status.name == "SUCCESS":
    mgr.reload_sshd()
```

### SSH Execution with Aspects

```python
from automojo.sysman.windows.runners import (
    SSHCommandRunner,
    SSHRunnerConfig,
)
from automojo.sysman.windows.aspects import (
    PersistenceAspect,
    PersistenceAspectConfig,
)
from automojo.sysman.windows.storage import SQLiteStorage
from automojo.sysman.windows.sshserverconfiguration import (
    WindowsSshServerConfigurationManager,
    WindowsSshServerConfigurationManagerConfig,
)

runner = SSHCommandRunner(
    SSHRunnerConfig(host="remote-windows", username="admin"),
)
runner.add_aspect(
    aspect=PersistenceAspect(
        PersistenceAspectConfig(
            storage_backend=SQLiteStorage(
                path=r"C:\ProgramData\maintenance\commands.db",
            ),
        ),
    ),
)

mgr = WindowsSshServerConfigurationManager(
    runner=runner,
    config=WindowsSshServerConfigurationManagerConfig(elevated=True),
)

mgr.set_sshd_setting(keyword="PasswordAuthentication", value="no")
mgr.reload_sshd()
```

### Dry-Run Mode

```python
config = WindowsSshServerConfigurationManagerConfig(
    elevated=True, dry_run=True,
)
mgr = WindowsSshServerConfigurationManager(runner=runner, config=config)

plan = mgr.set_sshd_setting(keyword="PermitRootLogin", value="no")
assert plan.status.name == "DRY_RUN"
# plan.command will be the full PowerShell argv that would have been
# dispatched.
```

---

## Testing Strategy

- All tests use a `_MockCommandRunner` keyed on argv tuple.
- A `_read_argv(path)` helper reproduces the manager's PowerShell
  `Get-Content` argv exactly so the runner mock can key on it.
- A `_write_argv(path)` helper does the same for `Set-Content`.
- Authorized-key tests synthesise OpenSSH-shaped lines using a
  `_build_test_key()` helper that emits a deterministic ed25519
  body and the matching SHA-256 fingerprint.
- A dedicated test proves `reload_sshd` invokes `Restart-Service
  -Name 'sshd'` (the Windows-specific reload mechanism).
- A dedicated test proves `add_authorized_key(user, key,
  is_admin=True)` writes the admin path
  (``%ProgramData%\ssh\administrators_authorized_keys``) rather than
  the per-user template — the central Windows-specific behaviour.
- A dedicated test proves the manager honours `elevated=True` on
  write paths and `elevated=False` on read paths regardless of
  config.
- A dedicated test proves `add_authorized_key` returns `FAILED` with
  a descriptive `message` when the key text fails to parse.
