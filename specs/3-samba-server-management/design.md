# Design

## Overview

The Windows samba-server-management feature is implemented as a
single module,
``automojo.sysman.windows.sambaservermanagement``, exposing a
``WindowsSambaServerManager`` class that wraps Windows's native SMB
surface (``New-SmbShare`` / ``Remove-SmbShare`` / ``Get-SmbShare``
/ ``Grant-SmbShareAccess`` / ``Revoke-SmbShareAccess`` /
``Get-SmbShareAccess``) through the ``ICommandRunner``
abstraction.

**Native SMB vs Samba.**  The Samba project's binary is NOT used on
Windows.  There is no ``smb.conf``, no ``smbpasswd``, and no
``pdbedit``.  Authentication routes through the local account
database (or a domain controller for domain-joined hosts), so SMB
user creation / removal is delegated to ``user-group-management``.

**See Also:**
- ``automojo-sysman-common/specs/contracts/samba-server-management/contract.md``
  — the cross-OS abstract contract.
- ``automojo-sysman-linux-ubuntu/specs/3-samba-server-management/``
  — Ubuntu's implementation, which uses the Samba project's
  ``smb.conf`` / ``smbpasswd`` / ``pdbedit`` toolchain.
- ``3-service-management`` — ``LanmanServer`` Windows service
  lifecycle.
- ``3-firewall-management`` — opening SMB ports through the
  Windows Firewall.
- ``3-user-group-management`` — local accounts and (when joined)
  domain credentials.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│          WindowsSambaServerManager                          │
│                                                             │
│  Writes:                                                    │
│    add_share(), remove_share(),                             │
│    set_share_permissions(), reload_config() (no-op)         │
│  Delegated writes:                                          │
│    add_samba_user(), remove_samba_user()                    │
│  Reads:                                                     │
│    list_shares(), get_share(), list_samba_users()           │
│                                                             │
│  Helpers:                                                   │
│    _build_powershell_argv()                                 │
│    _lookup_share_by_name_via_runner()                       │
│    _read_share_access_via_runner()                          │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
       ┌─────────────────────┐
       │   ICommandRunner    │
       └─────────────────────┘
```

Every cmdlet flows through a ``powershell.exe -NoProfile
-NonInteractive -Command "<scriptblock>"`` argv shape so that
``$PROFILE`` cannot mutate cmdlet behaviour.

---

## Data Models

```python
WindowsSambaActionStatus = ActionStatus  # alias from featurebase


@dataclass
class WindowsSambaManagerConfig(FeatureConfig):
    powershell_executable: str = "powershell.exe"
    delegate_user_mgmt: bool = True


@dataclass
class WindowsSambaActionResult(FeatureActionResult):
    share_name: str | None = None
    username: str | None = None
    message: str | None = None


@dataclass
class WindowsSambaShareSpec:
    name: str
    path: str
    description: str | None = None
    read_only: bool = False
    valid_users: list[str] | None = None
    read_list: list[str] | None = None
    write_list: list[str] | None = None
    browseable: bool = True
    guest_ok: bool = False


@dataclass
class WindowsSambaShareInfo:
    name: str
    path: str
    description: str | None
    read_only: bool
    valid_users: list[str] | None
    read_list: list[str] | None
    write_list: list[str] | None
    browseable: bool
    guest_ok: bool
    is_active: bool
    connection_count: int | None


@dataclass
class WindowsSambaShareAcl:
    read_users: list[str]
    write_users: list[str]
    full_access_users: list[str]
    denied_users: list[str]


@dataclass
class WindowsSambaUserInfo:
    username: str
    enabled: bool
    last_password_change: str | None
    description: str | None
```

---

## ACL Application — Grant / Revoke Dispatch

``set_share_permissions`` takes a desired ``WindowsSambaShareAcl``
and emits a sequence of cmdlet invocations to bring the share's
access map into line:

| ACL field | Cmdlet | AccessRight |
|-----------|--------|-------------|
| ``read_users`` | ``Grant-SmbShareAccess`` | ``Read`` |
| ``write_users`` | ``Grant-SmbShareAccess`` | ``Change`` |
| ``full_access_users`` | ``Grant-SmbShareAccess`` | ``Full`` |
| ``denied_users`` | ``Revoke-SmbShareAccess`` | n/a |

Idempotency: before dispatching, the manager probes
``Get-SmbShareAccess -Name '<name>' | ConvertTo-Json`` to build
the existing access map.  Each grant / revoke is suppressed when
the desired state already matches.

---

## Reload Config — No-Op SUCCESS

Windows SMB share commits are synchronous — ``New-SmbShare`` /
``Remove-SmbShare`` update both the registry and the running
service in a single transaction.  There is no per-share reload
analogue to Linux's ``smbcontrol`` or macOS's ``launchctl
kickstart``.  ``reload_config`` therefore returns
``status=SUCCESS`` immediately without invoking the runner.  The
recorded ``command`` field is a sanitised placeholder argv (a
PowerShell scriptblock that emits a comment) so callers can
inspect the no-op as if it had been a real dispatch.

---

## User Management Delegation

The same delegation pattern as macOS: ``add_samba_user`` and
``remove_samba_user`` always return ``FAILED`` with::

    detail = "add_samba_user delegated to user-group-management"
    message = (
        "Windows SMB authentication uses the local / domain user "
        "directory directly; create or remove the underlying "
        "account through the user-group-management feature before "
        "publishing or withdrawing share access."
    )

The recorded ``command`` field is a sanitised placeholder
PowerShell scriptblock (``"# delegated: New-LocalUser"``) and
NEVER contains the password.

---

## Testing Strategy

- ``_MockCommandRunner`` with pre-seeded ``RunResult`` values
  keyed by argv tuple (and by PowerShell-script-prefix where
  helpful, mirroring the NFS test harness).
- Parser tests for ``Get-SmbShare`` JSON output.
- Tests that ``add_samba_user`` / ``remove_samba_user`` always
  return ``FAILED`` with the delegation explanation, and that the
  recorded ``command`` field NEVER contains the password.
- Tests that ``set_share_permissions`` dispatches both grants and
  revokes correctly.
- Test that ``reload_config`` returns ``SUCCESS`` without
  invoking the runner (no-op verification).
