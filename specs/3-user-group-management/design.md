# Design

## Overview

The user and group management feature is implemented as a single module,
`automojo.sysman.windows.usergroupmanagement`, that exposes a
`WindowsUserGroupManager` class.  This spec implements the user-group-management
contract defined at
`automojo-sysman-common/specs/contracts/user-group-management/contract.md` for
Windows using the `Microsoft.PowerShell.LocalAccounts` module.  Every verb translates
to a `powershell.exe -NoProfile -NonInteractive -Command <script>` invocation that
uses `New-LocalUser`, `Remove-LocalUser`, `Set-LocalUser`, `Get-LocalUser`,
`Disable-LocalUser`, `Enable-LocalUser`, `New-LocalGroup`, `Remove-LocalGroup`,
`Get-LocalGroup`, `Add-LocalGroupMember`, `Remove-LocalGroupMember`, and
`Get-LocalGroupMember`.  Read paths emit their result through `ConvertTo-Json` so
parsing is pure Python `json.loads` with no text-format assumptions.

All system interaction is delegated to a command runner implementing the
`ICommandRunner` interface.  No direct .NET interop, WMI interop, or `subprocess`
calls are made inside the module.

Passwords are delivered to PowerShell via a parameterised script whose `$Plaintext`
parameter value is streamed on the runner's stdin rather than the command line.  The
script converts the plaintext to a `SecureString` before invoking the cmdlet, and the
manager stores only a sanitised argv (with `"<redacted>"` in place of any parameter
that might otherwise carry plaintext) in `WindowsUserGroupActionResult.command`.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over WinRM / SSH (via `SSHCommandRunner`
configured for Windows).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/remote execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH/WinRM)                 │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsUserGroupManager              │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsUserGroupManager                          │
│  automojo.sysman.windows.usergroupmanagement                │
│                                                             │
│  create_user()    delete_user()    modify_user()            │
│  get_user()       list_users()     set_password()           │
│  lock_account()   unlock_account()                          │
│  create_group()   delete_group()                            │
│  get_group()      list_groups()                             │
│  add_to_group()   remove_from_group()                       │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, stdin, capture_output) → RunResult     │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - WinRM / SSH link   │
│ - UAC elevation      │      │ - reliability metrics│
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

### `WindowsUserGroupManager`

**Location:** `source/packages/automojo/sysman/windows/usergroupmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `create_user` | `(spec) → WindowsUserGroupActionResult` | `New-LocalUser` with SecureString password |
| `delete_user` | `(name, remove_home) → WindowsUserGroupActionResult` | `Remove-LocalUser` [+ profile removal] |
| `modify_user` | `(name, changes) → WindowsUserGroupActionResult` | `Set-LocalUser` + optional profile rename |
| `get_user` | `(name) → WindowsUserInfo \| None` | `Get-LocalUser` + registry profile lookup |
| `list_users` | `() → list[WindowsUserInfo]` | `Get-LocalUser` → ConvertTo-Json |
| `set_password` | `(name, password) → WindowsUserGroupActionResult` | `Set-LocalUser -Password` with stdin-piped plaintext |
| `lock_account` | `(name) → WindowsUserGroupActionResult` | `Disable-LocalUser` |
| `unlock_account` | `(name) → WindowsUserGroupActionResult` | `Enable-LocalUser` |
| `create_group` | `(spec) → WindowsUserGroupActionResult` | `New-LocalGroup` |
| `delete_group` | `(name) → WindowsUserGroupActionResult` | `Remove-LocalGroup` |
| `get_group` | `(name) → WindowsGroupInfo \| None` | `Get-LocalGroup` + `Get-LocalGroupMember` |
| `list_groups` | `() → list[WindowsGroupInfo]` | `Get-LocalGroup` (members omitted) |
| `add_to_group` | `(user, group) → WindowsUserGroupActionResult` | `Add-LocalGroupMember` |
| `remove_from_group` | `(user, group) → WindowsUserGroupActionResult` | `Remove-LocalGroupMember` |

### `WindowsUserGroupManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `powershell_path` | `str` | `"powershell.exe"` | Full path to the PowerShell executable |
| `profile_root` | `str` | `"C:\\Users"` | Root directory for user profiles |

---

## Data Models

### Result Types

```python
class WindowsUserGroupActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class WindowsUserGroupActionResult:
    status: WindowsUserGroupActionStatus
    target: str                          # user or group name
    kind: str                            # "user" or "group"
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None            # sanitised — password placeholders are "<redacted>"
```

### Spec / Info Types

```python
@dataclass
class WindowsUserSpec:
    name: str
    uid: int | None                      # target RID; advisory only — SAM allocates
    home: str | None                     # informational; New-LocalUser does not set profile path
    shell: str | None                    # ignored — Windows has no login-shell concept
    primary_group: str | None            # ignored — Windows users do not have a primary group
    comment: str | None                  # → -Description
    system_account: bool                 # documented-only
    password: str | None                 # plaintext; sent via stdin to PowerShell script

@dataclass
class WindowsUserModSpec:
    shell: str | None                    # ignored
    home: str | None                     # new profile path
    move_home: bool                      # when True, Move-Item is issued
    primary_group: str | None            # ignored
    comment: str | None                  # → -Description
    uid: int | None                      # ignored (RIDs are immutable)

@dataclass
class WindowsUserInfo:
    name: str
    uid: int                             # RID from SID
    gid: int                             # always 0 — no primary-group concept
    comment: str                         # Description or FullName
    home: str                            # profile path from ProfileList registry
    shell: str                           # always ""
    locked: bool                         # not Enabled

@dataclass
class WindowsGroupSpec:
    name: str
    gid: int | None                      # ignored — SAM allocates
    system_group: bool                   # documented-only
    comment: str | None                  # → -Description

@dataclass
class WindowsGroupInfo:
    name: str
    gid: int                             # RID from SID
    members: list[str]                   # short-name usernames
```

### Exceptions

```python
class WindowsUserNotFoundError(Exception):
    """Raised when a user-targeting verb cannot locate the user in the local SAM."""

class WindowsGroupNotFoundError(Exception):
    """Raised when a group-targeting verb cannot locate the group in the local SAM."""

class WindowsUserGroupQueryError(Exception):
    """Raised when a Get-LocalUser / Get-LocalGroup / Get-LocalGroupMember query
    fails unexpectedly."""
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
        stdin: str | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

No `IFileOperator` is required: every read and write path flows through PowerShell.

---

## User-vs-Group Mapping

Windows stores local users and groups in the local Security Account Manager (SAM)
database.  The SAM allocates a SID per account of the form
`S-1-5-21-<domain>-<RID>` where RID is the 32-bit relative identifier.  The
`WindowsUserInfo.uid` / `WindowsGroupInfo.gid` fields are populated from the RID
component of the SID.  Windows has no primary-group concept for local users, so
`WindowsUserInfo.gid` is always `0`.

Home directories on Windows are "profile paths" stored under
`HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\<SID>\ProfileImagePath`.
`New-LocalUser` does not create the profile directly — Windows creates the profile
on first logon.  `WindowsUserGroupManager.get_user` issues a registry query through
PowerShell (`Get-ItemProperty "HKLM:\\...\\ProfileList\\<SID>"`) to resolve the
profile path.

Login-shell semantics do not exist on Windows; `WindowsUserInfo.shell` is always
`""` and any non-`None` `WindowsUserSpec.shell` is silently ignored.

---

## Command Mappings

All commands execute under `powershell.exe -NoProfile -NonInteractive -Command
"<script>"`.  Scripts that need the plaintext password are written with
`param([string]$Plaintext)` as the first statement and receive the plaintext via
stdin.

### User Write Scripts

| Operation | PowerShell script (abbreviated) |
|---|---|
| create_user | `param([string]$Plaintext); $sec = ConvertTo-SecureString -String $Plaintext -AsPlainText -Force; New-LocalUser -Name '<name>' -FullName '<comment>' -Description '<comment>' -Password $sec` |
| delete_user (+home) | `Remove-LocalUser -Name '<name>'; Remove-Item -Recurse -Force '<profile_root>\\<name>'` |
| delete_user | `Remove-LocalUser -Name '<name>'` |
| modify_user | `Set-LocalUser -Name '<name>' -FullName '<comment>' -Description '<comment>'` (plus `Move-Item` + `Set-ItemProperty` when `move_home=True`) |
| set_password | `param([string]$Plaintext); $sec = ConvertTo-SecureString -String $Plaintext -AsPlainText -Force; Set-LocalUser -Name '<name>' -Password $sec` |
| lock_account | `Disable-LocalUser -Name '<name>'` |
| unlock_account | `Enable-LocalUser -Name '<name>'` |

### Group Write Scripts

| Operation | PowerShell script |
|---|---|
| create_group | `New-LocalGroup -Name '<name>' [-Description '<desc>']` |
| delete_group | `Remove-LocalGroup -Name '<name>'` |
| add_to_group | `Add-LocalGroupMember -Group '<group>' -Member '<user>'` |
| remove_from_group | `Remove-LocalGroupMember -Group '<group>' -Member '<user>'` |

### Read Scripts (no elevation)

| Operation | Script |
|---|---|
| get_user | `Get-LocalUser -Name '<name>' \| Select-Object Name,SID,Description,FullName,Enabled,PasswordRequired,LastLogon \| ConvertTo-Json -Depth 3` + `Get-ItemProperty "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\<SID>" -Name ProfileImagePath \| ConvertTo-Json` |
| list_users | `Get-LocalUser \| Select-Object Name,SID,Description,FullName,Enabled \| ConvertTo-Json -Depth 3` |
| get_group | `Get-LocalGroup -Name '<name>' \| Select-Object Name,SID,Description \| ConvertTo-Json -Depth 3` + `Get-LocalGroupMember -Group '<name>' \| Select-Object Name,SID \| ConvertTo-Json -Depth 3` |
| list_groups | `Get-LocalGroup \| Select-Object Name,SID,Description \| ConvertTo-Json -Depth 3` |

All write commands pass `elevated=True` when the config enables it.  Read commands
never elevate.

---

## Output Parsing

### `Get-LocalUser | ConvertTo-Json` (single user)

```json
{
  "Name": "alice",
  "SID": "S-1-5-21-1234567890-1234567890-1234567890-1001",
  "Description": "Example user",
  "FullName": "Alice Example",
  "Enabled": true,
  "PasswordRequired": true,
  "LastLogon": "/Date(1713868800000)/"
}
```

Parser rules:

- `Name` → `name`.
- `SID` → the trailing decimal integer is the `uid` (RID).
- `Description` (fall back to `FullName`) → `comment`.
- `Enabled` → `locked = not Enabled`.

Collection form returns a JSON array — the parser calls `json.loads` and iterates.

### `Get-ItemProperty "HKLM:\...\ProfileList\<SID>" | ConvertTo-Json`

```json
{
  "ProfileImagePath": "C:\\Users\\alice"
}
```

The parser extracts `ProfileImagePath` and stores it as `WindowsUserInfo.home`.  A
missing registry key (profile never created) yields `home=""`.

### `Get-LocalGroup | ConvertTo-Json`

```json
{
  "Name": "Administrators",
  "SID": "S-1-5-32-544",
  "Description": "Administrators have complete and unrestricted access"
}
```

`SID` → `gid` (the last decimal component).

### `Get-LocalGroupMember | ConvertTo-Json`

```json
[
  { "Name": "BUILTIN\\Administrator", "SID": "S-1-5-21-...-500" },
  { "Name": "DESKTOP-ABC\\alice",     "SID": "S-1-5-21-...-1001" }
]
```

The parser strips the leading `BUILTIN\` or `<host>\` prefix from `Name` so
`WindowsGroupInfo.members` contains short-name usernames only.

### `ObjectNotFound` error detection

`Get-LocalUser` / `Get-LocalGroup` emit a non-zero exit and stderr text containing
`ObjectNotFound` or `User ... was not found` when the target is missing.  The parser
returns `None` in this case rather than raising.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `create_user` | user already exists | `get_user(name) is not None` |
| `delete_user` | user does not exist | `get_user(name) is None` |
| `modify_user` | every non-`None` field already matches | compare `WindowsUserInfo` |
| `set_password` | no idempotency guard (always runs when user exists) | — |
| `lock_account` | already disabled | `get_user(name).locked is True` |
| `unlock_account` | already enabled | `get_user(name).locked is False` |
| `create_group` | group already exists | `get_group(name) is not None` |
| `delete_group` | group does not exist | `get_group(name) is None` |
| `add_to_group` | user already in `members` | `user in get_group(group).members` |
| `remove_from_group` | user not in `members` | `user not in get_group(group).members` |

Preconditions raised before any idempotency check:

- `modify_user`, `set_password`, `lock_account`, `unlock_account`, `add_to_group`,
  `remove_from_group` raise `WindowsUserNotFoundError` when `get_user(name)` returns
  `None`.
- `add_to_group`, `remove_from_group` raise `WindowsGroupNotFoundError` when
  `get_group(group)` returns `None`.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `WindowsUserNotFoundError` | A user-targeting write is called with a non-existent user |
| `WindowsGroupNotFoundError` | A group-targeting write is called with a non-existent group |
| `WindowsUserGroupQueryError` | A `Get-*` cmdlet exits non-zero for a reason other than `ObjectNotFound` |

All other failures are surfaced as `WindowsUserGroupActionResult(status=FAILED, ...)`
carrying the runner's exit code, stdout, and stderr.  Windows error code `5`
(`ERROR_ACCESS_DENIED`) is not special-cased; it flows through the same `FAILED`
path.

### Password Confidentiality

Plaintext passwords are delivered via the runner's stdin as the value of the
PowerShell script's `$Plaintext` parameter.  The argv stored in
`WindowsUserGroupActionResult.command` is:

```
["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "<script>"]
```

The `<script>` contains literal `$Plaintext` references but not the plaintext
itself.  The manager additionally walks the argv list and replaces any substring
that matches the supplied plaintext with `"<redacted>"` as a defence-in-depth
measure for cases where a caller embeds the plaintext inadvertently.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: create_user Command Construction

*For any* `WindowsUserSpec`, `create_user()` should dispatch a PowerShell script that calls `New-LocalUser -Name <name>` with `-Description` / `-FullName` / `-Password` arguments reflecting the non-`None` spec fields.

**Validates: Requirements 1.1**

### Property 2: create_user System Account Documented-Only

*For any* `WindowsUserSpec` with `system_account=True`, `create_user()` should NOT alter the argv but SHALL record a detail noting Windows has no system-account classification.

**Validates: Requirements 1.2**

### Property 3: create_user Idempotency

*For any* user already present in the local SAM, `create_user()` should return `SKIPPED`.

**Validates: Requirements 1.3**

### Property 4: delete_user Profile Cleanup

*For any* `delete_user(name, remove_home=True)`, the dispatched script should include both `Remove-LocalUser` and `Remove-Item -Recurse -Force '<profile_root>\<name>'`.

**Validates: Requirements 2.1**

### Property 5: delete_user Idempotency

*For any* user not present, `delete_user()` should return `SKIPPED`.

**Validates: Requirements 2.3**

### Property 6: modify_user Field Selection

*For any* `WindowsUserModSpec`, `modify_user()` should emit only the cmdlet parameters corresponding to non-`None` fields, and should additionally issue `Move-Item` + `Set-ItemProperty` on the profile registry key when `move_home=True`.

**Validates: Requirements 3.1, 3.2**

### Property 7: modify_user Precondition

*For any* non-existent user, `modify_user()` should raise `WindowsUserNotFoundError`.

**Validates: Requirements 3.3**

### Property 8: modify_user Idempotency

*For any* `WindowsUserModSpec` whose fields all match the current `WindowsUserInfo`, `modify_user()` should return `SKIPPED`.

**Validates: Requirements 3.4**

### Property 9: get_user JSON Parsing

*For any* `Get-LocalUser` JSON output, `get_user()` should return a `WindowsUserInfo` with `name`, `uid` (from the SID RID), `comment`, `home` (from the profile registry), and `locked` (from `not Enabled`).

**Validates: Requirements 4.1, 4.2**

### Property 10: get_user Missing Returns None

*For any* name that yields `ObjectNotFound`, `get_user()` should return `None`.

**Validates: Requirements 4.3**

### Property 11: list_users Parsing

*For any* successful `Get-LocalUser | ConvertTo-Json` array, `list_users()` should materialise each entry into `WindowsUserInfo` with `home=""` until `get_user` is called individually.

**Validates: Requirements 5.2**

### Property 12: set_password Stdin Delivery

*For any* `set_password(name, password)` call, the plaintext MUST be delivered via the runner's stdin and MUST NOT appear in the returned `WindowsUserGroupActionResult.command`.

**Validates: Requirements 6.1, 6.2**

### Property 13: set_password Precondition

*For any* non-existent user, `set_password()` should raise `WindowsUserNotFoundError`.

**Validates: Requirements 6.3**

### Property 14: lock_account Idempotency

*For any* user whose `WindowsUserInfo.locked` is `True`, `lock_account()` should return `SKIPPED` without dispatching `Disable-LocalUser`.

**Validates: Requirements 7.2**

### Property 15: unlock_account Idempotency

*For any* user whose `WindowsUserInfo.locked` is `False`, `unlock_account()` should return `SKIPPED` without dispatching `Enable-LocalUser`.

**Validates: Requirements 8.2**

### Property 16: create_group Command Construction

*For any* `WindowsGroupSpec`, `create_group()` should dispatch `New-LocalGroup -Name <name>` with `-Description` when `comment` is set.

**Validates: Requirements 9.1**

### Property 17: create_group Idempotency

*For any* group already present, `create_group()` should return `SKIPPED`.

**Validates: Requirements 9.2**

### Property 18: delete_group Idempotency

*For any* group not present, `delete_group()` should return `SKIPPED`.

**Validates: Requirements 10.2**

### Property 19: get_group Member Enumeration

*For any* existing group, `get_group()` should issue both `Get-LocalGroup` and `Get-LocalGroupMember` and return a `WindowsGroupInfo` whose `members` list contains short-name usernames (domain prefix stripped).

**Validates: Requirements 11.1, 11.2**

### Property 20: list_groups Parsing

*For any* successful `Get-LocalGroup | ConvertTo-Json` array, `list_groups()` should materialise each entry into a `WindowsGroupInfo` with `members=[]`.

**Validates: Requirements 12.1, 12.2**

### Property 21: add_to_group Command Construction

*For any* existing user and group, `add_to_group(user, group)` should dispatch `Add-LocalGroupMember -Group <group> -Member <user>`.

**Validates: Requirements 13.1**

### Property 22: add_to_group Preconditions

*For any* non-existent user, `add_to_group()` should raise `WindowsUserNotFoundError`; for any non-existent group, `WindowsGroupNotFoundError`.

**Validates: Requirements 13.2, 13.3**

### Property 23: add_to_group Membership Idempotency

*For any* user already listed in `WindowsGroupInfo.members`, `add_to_group()` should return `SKIPPED`.

**Validates: Requirements 13.4**

### Property 24: remove_from_group Command Construction

*For any* existing user and group with the user as a member, `remove_from_group()` should dispatch `Remove-LocalGroupMember -Group <group> -Member <user>`.

**Validates: Requirements 14.1**

### Property 25: remove_from_group Idempotency

*For any* user not listed in `WindowsGroupInfo.members`, `remove_from_group()` should return `SKIPPED`.

**Validates: Requirements 14.4**

### Property 26: Dry-Run Safety

*For any* write operation with `dry_run=True`, the runner MUST NOT be invoked and the result status MUST be `DRY_RUN`.

**Validates: Requirements 1.6, 2.6, 3.7, 6.5, 7.6, 8.6, 9.5, 10.5, 13.7, 14.7**

### Property 27: Elevation for Writes

*For any* write with `elevated=True` in the config, the runner MUST be invoked with `elevated=True`.

**Validates: Requirements 15.1**

### Property 28: No Elevation for Reads

*For any* read operation (`get_user`, `list_users`, `get_group`, `list_groups`), the runner MUST NEVER be invoked with `elevated=True`.

**Validates: Requirements 15.2**

### Property 29: Error Result Consistency

*For any* non-zero exit from a write cmdlet, the returned `WindowsUserGroupActionResult` should have `status=FAILED`, and carry the runner's exit code and stderr.

**Validates: Requirements 1.5, 2.5, 3.6, 6.4, 7.5, 8.5, 9.4, 10.4, 13.6, 14.6**

---

## Module Layout

```
source/packages/automojo/sysman/windows/
├── __init__.py
└── usergroupmanagement.py        ← WindowsUserGroupManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/windows/
    └── test_usergroupmanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.windows.usergroupmanagement import (
    WindowsUserGroupManager,
    WindowsUserGroupManagerConfig,
    WindowsUserSpec,
    WindowsGroupSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())

config = WindowsUserGroupManagerConfig(elevated=True, dry_run=False)
mgr = WindowsUserGroupManager(runner=runner, config=config)

user_spec = WindowsUserSpec(
    name="svc-build",
    uid=None,
    home=None,
    shell=None,
    primary_group=None,
    comment="Build Service Account",
    system_account=False,
    password="InitialPass#1",
)
result = mgr.create_user(spec=user_spec)
print(f"create_user: {result.status}")

group_spec = WindowsGroupSpec(name="BuildOps", gid=None, system_group=False,
                              comment="Build-pipeline operators")
mgr.create_group(spec=group_spec)

mgr.add_to_group(user="svc-build", group="BuildOps")

mgr.set_password(name="svc-build", password="RotatedByVault#42")

mgr.lock_account(name="svc-build")

user_info = mgr.get_user(name="svc-build")
print(f"locked: {user_info.locked}")
```

### SSH/WinRM Execution with Aspects

```python
from automojo.sysman.windows.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.windows.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from automojo.sysman.windows.storage import SQLiteStorage
from automojo.sysman.windows.usergroupmanagement import (
    WindowsUserGroupManager, WindowsUserGroupManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-win", username="admin"))

storage = SQLiteStorage(path=r"C:\ProgramData\automojo\commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=30.0)))

mgr = WindowsUserGroupManager(
    runner=runner,
    config=WindowsUserGroupManagerConfig(elevated=True),
)

for user in mgr.list_users():
    print(f"{user.name}\t{user.uid}\t{user.locked}")
```

### Dry-Run Mode

```python
config = WindowsUserGroupManagerConfig(elevated=True, dry_run=True)
mgr = WindowsUserGroupManager(runner=runner, config=config)

result = mgr.set_password(name="alice", password="NewSecret!")
assert result.status == WindowsUserGroupActionStatus.DRY_RUN
assert "NewSecret!" not in (result.command or [])
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` that pattern-matches on the PowerShell script
  body rather than argv positions, because `-Command` always wraps the real command.
- `Get-LocalUser | ConvertTo-Json` mock responses drive every user-level idempotency
  guard.
- `Get-LocalGroup | ConvertTo-Json` + `Get-LocalGroupMember | ConvertTo-Json` mock
  responses drive group-level idempotency.
- A dedicated test verifies that `set_password` streams the plaintext over stdin and
  that the returned `WindowsUserGroupActionResult.command` contains no plaintext.
- Precondition tests assert `WindowsUserNotFoundError` / `WindowsGroupNotFoundError`
  raise before any runner call.
- Dry-run tests prove every write short-circuits to `DRY_RUN` without invoking the
  runner.
- Elevation tests prove writes receive `elevated=True` when configured and reads
  never do.
- JSON parsing tests cover single-object and array-form output from
  `ConvertTo-Json`, and the `ObjectNotFound` → `None` mapping for `get_user` /
  `get_group`.
- No test requires Administrator privileges or a live Windows session.

---

## Scope Mapping

| Contract verb | Windows implementation |
|---|---|
| `create_user` | `New-LocalUser` (with SecureString password from stdin) |
| `delete_user` | `Remove-LocalUser` [+ `Remove-Item` profile cleanup] |
| `modify_user` | `Set-LocalUser` [+ `Move-Item` + `Set-ItemProperty` for profile] |
| `get_user` | `Get-LocalUser` + `Get-ItemProperty ProfileList` |
| `list_users` | `Get-LocalUser` |
| `set_password` | `Set-LocalUser -Password` (SecureString from stdin) |
| `lock_account` | `Disable-LocalUser` |
| `unlock_account` | `Enable-LocalUser` |
| `create_group` | `New-LocalGroup` |
| `delete_group` | `Remove-LocalGroup` |
| `get_group` | `Get-LocalGroup` + `Get-LocalGroupMember` |
| `list_groups` | `Get-LocalGroup` |
| `add_to_group` | `Add-LocalGroupMember` |
| `remove_from_group` | `Remove-LocalGroupMember` |

---

## Extensions Beyond the Contract

None in this Windows implementation.  Domain-account management, Active Directory
integration, and User-Account-Control policy edits are intentionally deferred; when
added they will live behind their own feature flags so a coordinator writing against
the contract does not have to special-case Windows.
