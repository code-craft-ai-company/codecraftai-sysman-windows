# Design

## Overview

The user and group management feature is implemented as two cooperating classes in a single
module, `automojo.sysman.linux.windows.usergroupmanagement`:

- **`UserManager`** — handles user lifecycle, group membership, SSH keys, lock state, and
  password expiry.
- **`GroupManager`** — handles group lifecycle.

Both classes are constructed with the same `ICommandRunner` and
`UserGroupManagerConfig`.  The coordinator instantiates them directly and calls their
methods to perform discrete maintenance actions.

All system interaction is delegated to the command runner.  Reads from `/etc/passwd`,
`/etc/group`, and `authorized_keys` are performed via `getent` and `cat` through the
runner.  No direct file system access or `subprocess` calls are made inside the module.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└──────────────┬──────────────────────────┘
               │ calls
       ┌───────┴───────┐
       ▼               ▼
┌─────────────┐  ┌──────────────┐   automojo.sysman.linux.windows.usergroupmanagement
│ UserManager │  │ GroupManager │
│             │  │              │
│ create()    │  │ create()     │
│ delete()    │  │ delete()     │
│ modify()    │  │ query()      │
│ query()     │  └──────┬───────┘
│ add_to_group│         │
│ remove_from_│         │
│ add_ssh_key │         │
│ remove_ssh_ │         │
│ lock()      │         │
│ unlock()    │         │
│ expire_pw() │         │
│ set_pw_age()│         │
└──────┬──────┘         │
       └────────┬────────┘
                │ delegates to
                ▼
┌─────────────────────────────────────────┐
│        ICommandRunner                   │  automojo-interfaces
│  run(args, elevated=False) → RunResult  │
└─────────────────────────────────────────┘
```

---

## Components

### `UserManager`

**Location:** `source/packages/automojo/sysman/linux/windows/usergroupmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `create` | `(spec: UserSpec) → UserActionResult` | Create a local user |
| `delete` | `(username, remove_home=False) → UserActionResult` | Delete a local user |
| `modify` | `(username, spec: UserModSpec) → UserActionResult` | Modify user properties |
| `query` | `(username) → UserInfo \| None` | Query user from `/etc/passwd` |
| `add_to_group` | `(username, groupname) → UserActionResult` | Add user to supplementary group |
| `remove_from_group` | `(username, groupname) → UserActionResult` | Remove user from group |
| `add_ssh_key` | `(username, public_key) → UserActionResult` | Append SSH key |
| `remove_ssh_key` | `(username, public_key) → UserActionResult` | Remove SSH key |
| `lock` | `(username) → UserActionResult` | Lock user account |
| `unlock` | `(username) → UserActionResult` | Unlock user account |
| `expire_password` | `(username) → UserActionResult` | Force immediate password expiry |
| `set_password_age` | `(username, spec: PasswordAgeSpec) → UserActionResult` | Set password age policy |

### `GroupManager`

| Method | Signature | Description |
|---|---|---|
| `create` | `(spec: GroupSpec) → GroupActionResult` | Create a local group |
| `delete` | `(groupname) → GroupActionResult` | Delete a local group |
| `query` | `(groupname) → GroupInfo \| None` | Query group from `/etc/group` |

### `UserGroupManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |

---

## Data Models

### Input Specs

```python
@dataclass
class UserSpec:
    username: str
    uid: int | None = None
    home: str | None = None
    shell: str | None = None
    primary_group: str | None = None
    comment: str | None = None
    system_account: bool = False

@dataclass
class UserModSpec:
    shell: str | None = None
    home: str | None = None
    move_home: bool = False
    primary_group: str | None = None
    comment: str | None = None
    uid: int | None = None

@dataclass
class GroupSpec:
    groupname: str
    gid: int | None = None
    system_group: bool = False

@dataclass
class PasswordAgeSpec:
    max_days: int | None = None
    min_days: int | None = None
    expiry_date: str | None = None   # ISO 8601 date string: YYYY-MM-DD
```

### Result Types

```python
@dataclass
class UserActionResult:
    status: ActionStatus
    username: str
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class GroupActionResult:
    status: ActionStatus
    groupname: str
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

class ActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"
```

### Query Result Types

```python
@dataclass
class UserInfo:
    username: str
    uid: int
    gid: int
    comment: str
    home: str
    shell: str
    locked: bool           # derived from /etc/shadow '!' prefix on password hash

@dataclass
class GroupInfo:
    groupname: str
    gid: int
    members: list[str]
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

### User Operations

| Operation | Command |
|---|---|
| Query user | `getent passwd <username>` |
| Query shadow lock state | `passwd -S <username>` |
| Create user (basic) | `useradd [options] <username>` |
| Delete user | `userdel [-r] <username>` |
| Modify user | `usermod [options] <username>` |
| Lock account | `usermod -L <username>` |
| Unlock account | `usermod -U <username>` |
| Add to group | `usermod -aG <groupname> <username>` |
| Remove from group | `gpasswd -d <username> <groupname>` |
| Force password expiry | `passwd --expire <username>` |
| Set max password age | `chage -M <days> <username>` |
| Set min password age | `chage -m <days> <username>` |
| Set account expiry | `chage -E <date> <username>` |

### SSH Key Operations

| Operation | Command |
|---|---|
| Read home directory | `getent passwd <username>` (parse field 6) |
| Check `.ssh` dir exists | `stat <homedir>/.ssh` |
| Create `.ssh` dir | `mkdir -p <homedir>/.ssh` then `chmod 0700` then `chown` |
| Read `authorized_keys` | `cat <homedir>/.ssh/authorized_keys` |
| Write `authorized_keys` | `tee <homedir>/.ssh/authorized_keys` with content on stdin |
| Set `authorized_keys` mode | `chmod 0600 <homedir>/.ssh/authorized_keys` |

### Group Operations

| Operation | Command |
|---|---|
| Query group | `getent group <groupname>` |
| Create group | `groupadd [options] <groupname>` |
| Delete group | `groupdel <groupname>` |

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `UserManager.create()` | User exists | `getent passwd <username>` exit 0 |
| `UserManager.delete()` | User absent | `getent passwd <username>` exit 2 |
| `UserManager.modify()` | User absent | raises `UserNotFoundError` |
| `UserManager.add_to_group()` | User already in group | parse `getent group <groupname>` members |
| `UserManager.remove_from_group()` | User not in group | parse `getent group <groupname>` members |
| `UserManager.add_ssh_key()` | Key already in file | `cat authorized_keys` line match |
| `UserManager.remove_ssh_key()` | Key absent from file | `cat authorized_keys` line match |
| `UserManager.lock()` | Account already locked | `passwd -S` output contains `L` flag |
| `UserManager.unlock()` | Account already unlocked | `passwd -S` output contains `P` flag |
| `GroupManager.create()` | Group exists | `getent group <groupname>` exit 0 |
| `GroupManager.delete()` | Group absent | `getent group <groupname>` exit 2 |

---

## SSH Authorized Keys Management

Keys are managed line-by-line in pure Python after reading the file via `cat`.
Matching is done on the full key string (type + base64 + optional comment).
The modified line list is serialised back to a string and written via `tee` with
`stdin`.  This avoids any shell escaping of key content.

Directory and file creation for missing `.ssh` paths follows this sequence:

```
1. mkdir -p <homedir>/.ssh             (elevated)
2. chmod 0700 <homedir>/.ssh           (elevated)
3. chown <username>:<username> .ssh    (elevated)
4. tee <homedir>/.ssh/authorized_keys  (elevated, stdin=full key file content)
5. chmod 0600 authorized_keys          (elevated)
6. chown <username>:<username> authorized_keys  (elevated)
```

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UserNotFoundError` | `modify()`, `add_to_group()`, `remove_from_group()`, `add_ssh_key()`, `remove_ssh_key()`, `lock()`, `unlock()`, `expire_password()`, `set_password_age()` called for a username that does not exist |
| `GroupNotFoundError` | `add_to_group()` or `remove_from_group()` called for a group that does not exist |
| `UserQueryError` | `query()` fails for a reason other than user-not-found |
| `UserKeyError` | `add_ssh_key()` or `remove_ssh_key()` called with an empty key string |

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── usergroupmanagement.py        ← UserManager, GroupManager, all specs,
                                     result types, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_usergroupmanagement.py
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` injected at construction time.
- `getent passwd` and `getent group` mock responses drive idempotency guard behaviour.
- `passwd -S` mock responses drive lock/unlock skip detection.
- SSH key tests supply a mock `cat authorized_keys` response and verify the `tee` stdin
  content the runner receives.
- `PasswordAgeSpec` tests verify the correct `chage` flags are assembled for each
  combination of fields.
- No test requires root access, real system users, or a live PAM stack.
