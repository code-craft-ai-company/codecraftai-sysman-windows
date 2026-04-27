# Design

## Overview

The filesystem-quotas feature is implemented as a single
module, `automojo.sysman.windows.filesystemquotas`, exposing a
`WindowsFilesystemQuotasManager` class that wraps `fsutil
quota` — the volume-level NTFS user-quota subsystem built into
Windows — through the `ICommandRunner` abstraction.  Every
command is `fsutil`-direct (no PowerShell scriptblock wrapper)
because `fsutil.exe` accepts argv natively and does not benefit
from PowerShell mediation.

This Windows pilot intentionally restricts itself to the
universally-available `fsutil quota` surface.  The richer
File Server Resource Manager (FSRM) `dirquota` surface — which
provides per-directory quotas, templates, and notification
actions — requires a Windows Server installation with the FSRM
role.  FSRM is a separate cmdlet family
(`New-FsrmQuota` / `Get-FsrmQuota` / etc.) and a separate
deployment story, and is documented as a follow-up extension.

The feature follows the standard `ActionStatus` / `Config` /
`ActionResult` / `_dispatch` pattern shared with every other
Windows feature.

| Verb | Kind | Returns |
|------|------|---------|
| `enable_quotas` | Write | `WindowsQuotaActionResult` |
| `disable_quotas` | Write | `WindowsQuotaActionResult` |
| `set_user_quota` | Write | `WindowsQuotaActionResult` |
| `set_group_quota` | Write | `WindowsQuotaActionResult` (always FAILED) |
| `get_user_quota` | Read | `WindowsQuotaInfo \| None` |
| `get_group_quota` | Read | raises `WindowsQuotaQueryError` |
| `list_quotas` | Read | `list[WindowsQuotaInfo]` |
| `get_quota_status` | Read | `WindowsQuotaStatus` |

**See Also:**
- `automojo-sysman-common/specs/contracts/filesystem-quotas/contract.md`
  — cross-OS contract this implementation satisfies.
- `automojo-sysman-linux-redhat/specs/3-filesystem-quotas/`
  — canonical reference; covers the full quota surface.
- `automojo-sysman-linux-ubuntu/specs/3-filesystem-quotas/` and
  `automojo-sysman-macos/specs/3-filesystem-quotas/` — sibling
  pilots; both share the BSD `quota` family shape, which
  Windows does NOT use.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
│  - Declares per-user quota limits                           │
│  - Toggles enforcement; reads current posture               │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│          WindowsFilesystemQuotasManager                     │
│                                                             │
│  Status & query surface (reads, no elevation):              │
│    fsutil quota query <volume> →                            │
│        get_quota_status, get_user_quota, list_quotas        │
│                                                             │
│  Enforcement state (writes):                                │
│    enable_quotas (fsutil quota track + enforce)             │
│    disable_quotas (fsutil quota disable)                    │
│                                                             │
│  Limits (writes):                                           │
│    set_user_quota (fsutil quota modify)                     │
│    set_group_quota → FAILED (NTFS gap)                      │
│                                                             │
│  Group-quota gap (typed FAILED):                            │
│    set_group_quota / get_group_quota produce a typed        │
│    failure with detail="ntfs_group_quota_unsupported".      │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Configuration

```python
@dataclass
class WindowsFilesystemQuotasManagerConfig(FeatureConfig):
    """
        Inherits ``dry_run`` and ``elevated`` from FeatureConfig.

        :param quota_unit_kb: Documentation field.  Windows
            ``fsutil quota`` arguments are in BYTES, not KiB
            blocks like Unix.  This field is preserved as a
            documentation cue (``1`` = bytes-as-bytes) so
            cross-OS callers immediately see that no scaling is
            needed.  Setting this to anything other than 1 has
            NO effect on the manager — the field exists only to
            document the byte-native semantics.
    """
    quota_unit_kb: int = 1
```

---

## Data Models

```python
class WindowsQuotaActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class WindowsQuotaActionResult(FeatureActionResult):
    mountpoint: str | None = None
    principal: str | None = None


class WindowsQuotaPrincipalKind(Enum):
    USER = "user"
    GROUP = "group"


@dataclass
class WindowsQuotaInfo:
    mountpoint: str
    principal: str
    principal_kind: WindowsQuotaPrincipalKind
    used_bytes: int | None
    soft_bytes: int | None
    hard_bytes: int | None
    used_inodes: int | None    # always None on NTFS
    soft_inodes: int | None    # always None on NTFS
    hard_inodes: int | None    # always None on NTFS
    grace_period_seconds: int | None


@dataclass
class WindowsQuotaStatus:
    mountpoint: str
    enabled: bool
    enforcing: bool
    user_quotas_active: bool
    group_quotas_active: bool   # always False on NTFS
    filesystem_type: str | None
```

### Exceptions

```python
class WindowsQuotaQueryError(Exception):
    """Raised when a read fails unexpectedly."""


class WindowsQuotaFilesystemNotMountedError(Exception):
    """
        Reserved for future use.  ``fsutil quota`` itself does
        not provide a clean "not mounted" signal; failed volume
        access surfaces through ``fsutil`` exit codes.
    """
```

---

## Quota-Model Concept Mapping (Cross-OS)

The cross-OS contract speaks in `block_*` and `inode_*` units
expressed as bytes.  Windows is uniquely byte-native:
`fsutil quota modify` takes raw bytes for both soft and hard
limits, and `fsutil quota query` reports usage and limits in
bytes.  No scaling is required.

| Concept | Cross-OS contract | Windows native | Notes |
|---------|-------------------|----------------|-------|
| Soft block limit | `block_soft` (bytes) | `fsutil quota modify` arg in bytes | Pass through. |
| Hard block limit | `block_hard` (bytes) | `fsutil quota modify` arg in bytes | Pass through. |
| Block usage | `block_used` (bytes) | `fsutil quota query` reports bytes | Pass through. |
| Inode soft / hard | `inode_*` (count) | NTFS has no inode-quota concept | Always `None` on read. |
| Grace period | `grace_*` (str) | NTFS has no quota grace-period | Always `None` on read. |
| Filesystem type | `fstype` | always `"ntfs"` | Hard-coded; FAT volumes do not support quotas. |

### NTFS group-quota gap (load-bearing design choice)

NTFS volume-level quotas address Windows security identifiers
(SIDs).  Windows does not have a "primary group" concept the
way POSIX does — a SID can be a user OR a group, but
`fsutil quota` only meaningfully tracks per-USER usage.
Setting a quota on a group SID does not impose a per-group
ceiling because individual users' usage is not aggregated under
their primary group.

Rather than dispatching `fsutil quota modify` against a group
SID and producing a result that does nothing useful, this
manager:

1. **`set_group_quota`** — returns
   `WindowsQuotaActionResult(status=FAILED,
   detail="ntfs_group_quota_unsupported")` without invoking the
   runner, regardless of `dry_run`.  The unsupported-operation
   semantics override the dry-run guard because no future
   invocation can succeed.
2. **`get_group_quota`** — raises
   `WindowsQuotaQueryError` with a message that carries the
   same `"ntfs_group_quota_unsupported"` token.  Reads use
   exceptions (per the cross-OS contract); writes use the
   FAILED result variant.
3. **`list_quotas`** — emits only USER-kind entries.
   Group-kind entries are NEVER produced because no group
   record exists.
4. **`get_quota_status.group_quotas_active`** — always `False`.

Operators needing per-group quotas should use the FSRM
`dirquota` family (Windows Server only); the manager
documents this in the module docstring.

---

## Key Method Designs

### `enable_quotas(mountpoint)`

The manager treats `mountpoint` as the volume root token (e.g.
`"C:"`, `"D:\\"`, or a mount-point path).  `fsutil quota` accepts
either form.

1. Dry-run guard returning DRY_RUN with the planned
   `fsutil quota track` argv.
2. Probe `get_quota_status(mountpoint)`. If `enforcing` →
   `SKIPPED`.
3. Dispatch `["fsutil", "quota", "track", mountpoint]`.
4. On failure return.
5. Dispatch `["fsutil", "quota", "enforce", mountpoint]`.

### `disable_quotas(mountpoint)`

1. Dry-run guard.
2. Probe state.  If `not enabled` → `SKIPPED`.
3. Dispatch `["fsutil", "quota", "disable", mountpoint]`.

### `set_user_quota(mountpoint, user, soft_bytes, hard_bytes)`

1. Dry-run guard with planned argv.
2. Probe `get_user_quota`; SKIPPED on match.
3. Dispatch `["fsutil", "quota", "modify", mountpoint,
   str(hard_bytes), str(soft_bytes), user]`.

### `set_group_quota(...)`

Returns FAILED with `detail="ntfs_group_quota_unsupported"`
WITHOUT consulting `dry_run`. The operation is unsupported by
the underlying tool, not skipped by policy.

### `get_user_quota(mountpoint, user)`

1. Dispatch `["fsutil", "quota", "query", mountpoint]` (no
   elevation).
2. Non-zero → raise `WindowsQuotaQueryError`.
3. Parse the per-user blocks and return the entry whose
   `Quota User Name` matches `user`, or `None`.

### `get_group_quota(...)`

Raises `WindowsQuotaQueryError` immediately with the
`"ntfs_group_quota_unsupported"` token.

### `list_quotas(mountpoint)`

1. Dispatch `fsutil quota query`.
2. Non-zero → raise `WindowsQuotaQueryError`.
3. Parse all per-user blocks; return the list (USER kind
   only).

### `get_quota_status(mountpoint)`

1. Dispatch `fsutil quota query`.
2. Non-zero → raise `WindowsQuotaQueryError`.
3. Parse the header lines:
   - `File System Type Status:  ...` (info only).
   - `Logging Status:  Enabled/Disabled` →
     `user_quotas_active`.
   - `Enforce Limit Status:  Enabled/Disabled` →
     `enforcing` and `enabled`.
4. Return `WindowsQuotaStatus` with `filesystem_type="ntfs"`
   and `group_quotas_active=False`.

---

## Parser Contracts

### `fsutil quota query` parser

Sample output:

```
*  Volume Name = \\?\Volume{...}\
*  File System Type Status: NTFS
*  Logging Status:          Enabled
*  Enforce Limit Status:    Enabled
*  Default Quota Threshold: 0xffffffffffffffff
*  Default Quota Limit:     0xffffffffffffffff

  Quota User Name:          BUILTIN\Administrators (Alias)
  Quota User SID:            S-1-5-32-544
  Change time:              Friday, July 14, 2023 11:01:42 AM
  Quota Used:               1024
  Quota Threshold:          0xffffffffffffffff
  Quota Limit:              0xffffffffffffffff

  Quota User Name:          DOMAIN\alice
  Quota User SID:            S-1-5-21-...-1234
  Change time:              Friday, July 14, 2023 11:02:00 AM
  Quota Used:               1048576
  Quota Threshold:          524288000
  Quota Limit:              1073741824
```

Parse rules:
- Header lines (starting with `*`) carry volume-wide status.
  `Logging Status` → user-quota tracking active.
  `Enforce Limit Status` → enforcement active.
- Per-user blocks start at the first non-`*` `Quota User Name`
  line.
- `Quota Used` / `Quota Threshold` / `Quota Limit` are decimal
  byte values OR hex `0xffff...` sentinels meaning "no limit".
  The parser converts hex sentinels to `None`.

---

## Dry-Run Behaviour

Dry-run short-circuits each supported write verb at the top of
the method body.  The `command` field on the returned
:class:`WindowsQuotaActionResult` records the would-have-been
argv.

`set_group_quota` is the EXCEPTION: it returns `FAILED` with
the `"ntfs_group_quota_unsupported"` detail regardless of
`dry_run`, because the operation is unsupported by the
underlying tool rather than skipped by policy.

---

## Elevation Policy

| Verb | `elevated` |
|------|-----------|
| `enable_quotas` | `config.elevated` |
| `disable_quotas` | `config.elevated` |
| `set_user_quota` | `config.elevated` |
| `set_group_quota` | (no runner call) |
| `get_user_quota` | `False` |
| `get_group_quota` | (no runner call) |
| `list_quotas` | `False` |
| `get_quota_status` | `False` |

---

## Idempotency

| Verb | Probe | Skip condition |
|------|-------|----------------|
| `enable_quotas` | `get_quota_status` | `status.enforcing` |
| `disable_quotas` | `get_quota_status` | `not status.enabled` |
| `set_user_quota` | `get_user_quota` | soft and hard match |
| `set_group_quota` | (none) | always FAILED — no idempotency relevant |

---

## Filesystem-Mapping Table

| (fstype, target) | Supported? | Notes |
|------------------|------------|-------|
| `(ntfs, USER)` | Yes | Canonical NTFS volume-quota surface via `fsutil quota`. |
| `(ntfs, GROUP)` | **No** | `fsutil quota` is per-user only.  Returns FAILED with `detail="ntfs_group_quota_unsupported"`. |
| `(ntfs, PROJECT)` | No | NTFS has no project-quota analogue.  FSRM `dirquota` covers per-directory quotas (out of scope). |
| `(refs, USER)` | No | ReFS does not support quotas at the volume level. |
| `(fat32, *)` | No | FAT* filesystems have no quota subsystem. |

The manager does NOT enforce this table — it dispatches
`fsutil quota` against whatever volume token the caller
supplies and lets the underlying tool surface its own failure
through the FAILED exit code.

---

## Correctness Properties

1. **fsutil-direct argv.** Every write verb dispatches
   `["fsutil", "quota", ...]` directly without a PowerShell
   wrapper.
2. **Group-quota gap surfaced as FAILED.** `set_group_quota`
   returns FAILED with the documented detail token; never
   invokes the runner.
3. **Group queries raise.** `get_group_quota` raises
   `WindowsQuotaQueryError` carrying the documented detail.
4. **Idempotent enable / disable.** Repeated invocations on a
   volume already in the requested state return SKIPPED.
5. **Idempotent set.** Setting limits that already match is
   SKIPPED.
6. **No elevation on reads.** Every read passes
   `elevated=False`.
7. **Dry-run never dispatches** for the supported writes.
8. **Bytes are bytes.** No scaling is performed; the soft /
   hard byte values are passed through.

---

## Testing Strategy

- `_MockCommandRunner` recording every call and returning
  pre-seeded `RunResult` values keyed by argv tuple.
- Per-verb command-construction tests asserting the exact argv.
- Per-verb idempotency tests.
- Per-verb dry-run tests asserting no runner call (except
  `set_group_quota`, where the test asserts the FAILED-with-
  detail outcome).
- Parser tests for `fsutil quota query`.
- `set_group_quota` returns FAILED with documented detail.
- `get_group_quota` raises with documented detail.
- Unknown user → `get_user_quota` returns None.
- Elevation-on-writes test.
- No-elevation-on-reads test.
- Failure-surface test (mutation non-zero → FAILED with stderr
  captured).

No live `fsutil` invocation.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|------|-------|--------------|
| Volume mount-point management | `3-filesystem-management` | Drive-letter and mount-point assignments live there; this feature consumes whatever volume token the caller supplies. |
| Target accounts / groups | `3-user-group-management` | Caller ensures the user identifier is valid; SIDs and DOMAIN\\name forms both work with `fsutil quota`. |
| FSRM dirquota | Future extension | Out of scope for the pilot. |

---

## Out of Scope (Documented Drift From Contract)

The Windows pilot intentionally narrows scope to:

- USER target only via `fsutil quota`.  GROUP returns FAILED
  with `"ntfs_group_quota_unsupported"`.  PROJECT is not
  surfaced.
- Inode quotas: NTFS has no inode-quota concept.  The
  `WindowsQuotaInfo.{used,soft,hard}_inodes` fields are always
  `None` on read.
- Grace periods: NTFS has no grace-period concept.
  `WindowsQuotaInfo.grace_period_seconds` is always `None`.
- FSRM (per-directory quotas, templates, notification actions)
  is documented as a follow-up extension.

These deviations are purposeful: they keep the Windows pilot
small, universally available (no FSRM install required), and
aligned with the cross-OS contract's "FAILED with descriptive
detail" pattern for unsupported (fstype, target)
combinations.
