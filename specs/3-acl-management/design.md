# Design

## Overview

The Windows ACL manager wraps the NTFS DACL toolchain — PowerShell
`Get-Acl` / `Set-Acl` for reads and the canonical SDDL round-trip,
and `icacls.exe` for write fan-outs — and exposes the cross-OS
ACL-management contract through a single class,
`WindowsAclManager`, dispatched entirely through the injected
`ICommandRunner`.

This design document covers:

1. The cross-OS ACL-model divergence that drives the per-OS naming
   and per-OS spec divergence.
2. The Windows-specific verb-to-command mapping table.
3. The principal / permission / rights / inheritance model.
4. The PowerShell `ConvertTo-Json` parsing rules (single-record vs
   multi-record array shapes).
5. Idempotency, dry-run, elevation, and error-surface behaviour.
6. Internal helpers (token rendering, parsing, comparison,
   validation).

---

## Architecture

```
+---------------------------------------------------------------+
|                       WindowsAclManager                       |
|---------------------------------------------------------------|
|  Read verbs                                                   |
|     get_acl(path)         -> Get-Acl | ConvertTo-Json -> ACL  |
|     list_aces(path)       -> get_acl(path).aces               |
|                                                               |
|  Write verbs                                                  |
|     set_acl(path, spec)   -> icacls /reset; icacls /grant:r * |
|     add_ace(path, spec)   -> icacls /grant:r OR /deny         |
|     remove_ace(path, p)   -> icacls /remove                   |
|     clear_acl(path)       -> icacls /reset                    |
|     copy_acl(src, dst)    -> Get-Acl src | Set-Acl dst        |
|                                                               |
|  Helpers                                                      |
|     _build_powershell_argv -> ["powershell.exe", -NoProfile,  |
|                                -NonInteractive, -Command, ..] |
|     _render_icacls_ace_arguments -> ["/grant:r", "user:(M)"]  |
|     _parse_get_acl_payload -> WindowsAclInfo                  |
|     _normalize_access_records -> object | array -> [dicts]    |
|     _ace_already_present                                      |
|     _acl_matches_spec                                         |
|     _acl_infos_equal                                          |
|     _find_unknown_rights_tokens                               |
+---------------------------------------------------------------+
                           |
                           |  every read / write / probe
                           v
                +-----------------------+
                |   ICommandRunner      |
                |   (injected)          |
                +-----------------------+
                           |
                           v
                +-----------------------+
                |  powershell.exe       |
                |  icacls.exe           |
                +-----------------------+
```

---

## Cross-OS ACL-Model Divergence

ACLs are nominally a single concept ("who can do what to a path"),
but the three OS families this product targets carry deeply
divergent ACL models.  Pretending the divergence does not exist
produces leaky abstractions; making the divergence explicit lets
each per-OS implementation stay honest to its native model.

| Dimension | POSIX (Ubuntu / RHEL) | macOS (NFSv4-style) | Windows (NTFS / NT DACL) |
|-----------|-----------------------|---------------------|--------------------------|
| Permission set | 3 bits per principal: `r`, `w`, `x`. | ~20 fine-grained tokens (`read`, `write`, `execute`, `delete`, `append`, `readattr`, `writeattr`, `readextattr`, `writeextattr`, `readsecurity`, `writesecurity`, `chown`, `list`, `search`, `add_file`, `add_subdirectory`, `delete_child`, `file_inherit`, `directory_inherit`, `limit_inherit`, `only_inherit`). | `[FileSystemRights]` enum: `FullControl`, `Modify`, `ReadAndExecute`, `Read`, `Write`, `ListDirectory`, `ReadData`, `WriteData`, `AppendData`, `Delete`, `DeleteSubdirectoriesAndFiles`, `ChangePermissions`, `TakeOwnership`, `Synchronize`, `ExecuteFile`, `ReadAttributes`, `WriteAttributes`, `ReadExtendedAttributes`, `WriteExtendedAttributes`, `ReadPermissions`, `Traverse`. |
| ACE polarity | Always allow.  POSIX has no deny ACEs. | Allow or deny per ACE. | Allow or deny per ACE. |
| Inheritance | Default ACL on directories, applied on creation; no per-ACE inheritance flags. | Per-ACE flags: `file_inherit`, `directory_inherit`, `limit_inherit`, `only_inherit`. | Per-ACE flags: `ContainerInherit` (CI), `ObjectInherit` (OI), plus a `NoPropagateInherit` flag and a propagation flag set. |
| Effective-rights mask | A `mask::` row that clamps every named entry's effective rights. | None. | None. |
| Principal model | POSIX user / group name resolved via NSS; plus `other` and `mask` pseudo-principals. | Native user / group resolved via Directory Services; `everyone` keyword. | SID resolved via LSA, surfaced as `DOMAIN\name` and well-known names (`BUILTIN\Administrators`, `Everyone`, `NT AUTHORITY\SYSTEM`). |
| Native tooling | `setfacl` / `getfacl`. | `chmod +a` / `chmod -a` / `chmod -N` / `ls -le`. | `Get-Acl` / `Set-Acl` (PowerShell, `[System.Security.AccessControl.FileSystemSecurity]`); `icacls.exe` (legacy CLI). |
| Default-ACL equivalent | First-class (the `default:` prefix). | None on HFS+ / APFS — inheritance is per-ACE flag-driven. | None on NTFS — inheritance is per-ACE flag-driven. |
| Canonical serialisation | `getfacl` text. | `ls -le` text. | SDDL string (`O:...G:...D:...`). |

The contract papers over this divergence by reducing the ACE shape
to `principal + rights + allow + inherited + is_default`.  Each
per-OS implementation MAY (and on Windows, does) carry additional
fields that have no cross-OS analogue.

This Windows spec exposes the NTFS-native subset directly.  Where
the contract's abstract `default_entries` field exists for POSIX
parity, the Windows manager always returns an empty list — the
canonical Windows inheritance idiom is per-ACE inheritance flags
(`ObjectInherit` / `ContainerInherit`), not a partition.  The
explicit-vs-inherited distinction is preserved on
`WindowsAceInfo.is_inherited`; the explicit ACEs are the only ones
mutated by the write verbs.

---

## Module Layout

```
source/packages/ccai/sysman/windows/aclmanagement.py
```

Contents (top-to-bottom):

1. Module docstring (verb catalogue, file-I/O policy, elevation
   policy, idempotency policy, ACL-model commentary, PowerShell
   argv shape).
2. Module-level constants (binary names, flag tokens, rights
   vocabulary).
3. `WindowsAclActionStatus` (alias of the package-shared
   `ActionStatus`).
4. `WindowsAcePermission` enum (`ALLOW`, `DENY`).
5. `WindowsAceInheritance` enum (`NONE`, `OBJECT_INHERIT`,
   `CONTAINER_INHERIT`, `BOTH`).
6. `WindowsAclManagerConfig` dataclass.
7. `WindowsAclActionResult` dataclass.
8. `WindowsAceInfo` / `WindowsAceSpec` / `WindowsAclInfo`
   dataclasses.
9. `WindowsAclQueryError` exception.
10. `WindowsAclManager` class.
11. Module-level helpers: token rendering, parsing, validation,
    comparison.

---

## Components

| Component | Role |
|-----------|------|
| `WindowsAclManager` | Public verb surface; dispatches every command through `_runner.run(...)`. |
| `WindowsAclManagerConfig` | Carries the PowerShell / `icacls` executable names and the inherited `dry_run` / `elevated` flags. |
| `WindowsAceSpec` | Caller-supplied write-shape ACE (principal + permission + rights + inheritance). |
| `WindowsAceInfo` | Parsed read-shape ACE (adds `is_inherited`). |
| `WindowsAclInfo` | Per-path snapshot.  Carries `owner`, `group`, `aces`, `default_aces` (always empty), and `sddl`. |
| `WindowsAclQueryError` | Raised on read failure with multiline message body. |
| `WindowsAcePermission` | `ALLOW` / `DENY` polarity enum. |
| `WindowsAceInheritance` | `NONE` / `OBJECT_INHERIT` / `CONTAINER_INHERIT` / `BOTH` flag enum. |

---

## Data Models

### `WindowsAclManagerConfig`

| Field | Type | Description |
|-------|------|-------------|
| `dry_run` | `bool` | Inherited.  When `True`, write verbs short-circuit. |
| `elevated` | `bool` | Inherited.  When `True`, write steps run elevated. |
| `powershell_executable` | `str` | PATH-resolved binary (default `"powershell.exe"`). |
| `icacls_executable` | `str` | PATH-resolved binary (default `"icacls.exe"`). |

### `WindowsAclActionResult`

| Field | Type | Description |
|-------|------|-------------|
| `status` | `WindowsAclActionStatus` | Outcome enum. |
| `detail` | `str \| None` | Human-readable scope (typically the path). |
| `exit_code` | `int \| None` | Runner exit code. |
| `stdout` | `str \| None` | Runner stdout. |
| `stderr` | `str \| None` | Runner stderr. |
| `command` | `list[str] \| None` | Argv that was (or would have been) dispatched. |
| `path` | `str \| None` | The target path. |
| `principal` | `str \| None` | The principal acted on (when applicable). |
| `message` | `str \| None` | Human-readable message for SKIPPED / FAILED. |

### `WindowsAceSpec`

| Field | Type | Description |
|-------|------|-------------|
| `principal` | `str` | `DOMAIN\name`, `BUILTIN\<group>`, `Everyone`, etc. |
| `permission` | `WindowsAcePermission` | `ALLOW` or `DENY`. |
| `rights` | `list[str]` | `[FileSystemRights]` tokens. |
| `inheritance` | `WindowsAceInheritance \| None` | Defaults to `None` (rendered as `NONE`). |

### `WindowsAceInfo`

| Field | Type | Description |
|-------|------|-------------|
| `principal` | `str` | `IdentityReference.Value` from `Get-Acl`. |
| `permission` | `WindowsAcePermission` | Coerced from `AccessControlType`. |
| `rights` | `list[str]` | Comma-split `FileSystemRights` text. |
| `inheritance` | `WindowsAceInheritance` | Coerced from `InheritanceFlags`. |
| `is_inherited` | `bool` | Coerced from `IsInherited`. |

### `WindowsAclInfo`

| Field | Type | Description |
|-------|------|-------------|
| `path` | `str` | The path the ACL was queried for. |
| `owner` | `str \| None` | Owner principal name. |
| `group` | `str \| None` | Group principal name (NTFS rarely populates this). |
| `aces` | `list[WindowsAceInfo]` | ACEs in source order. |
| `default_aces` | `list[WindowsAceInfo]` | Empty on Windows.  Carried for cross-OS-contract parity. |
| `sddl` | `str \| None` | Security Descriptor Definition Language string. |

---

## Verb-to-Command Mapping (Windows)

| Verb | Tool invocation | Notes |
|------|-----------------|-------|
| `get_acl(path)` | PowerShell scriptblock wrapping `Get-Acl -Path '<path>' \| Select-Object Owner,Group,Sddl,Access \| ConvertTo-Json -Compress -Depth 5`. | Reads `Owner` / `Group` / `Sddl` directly; `Access` is the per-ACE list. |
| `list_aces(path)` | Same as `get_acl`; result `aces` list returned. | NTFS has no default partition. |
| `set_acl(path, acl_spec)` | `icacls '<path>' /reset` then `icacls '<path>' /grant:r '<principal>:<rights>'` (or `/deny`) per ACE. | Two-step "reset then fan-out".  `:r` requests "replace" semantics so any inherited duplicates after `/reset` do not double up. |
| `add_ace(path, principal, rights)` | `icacls '<path>' /grant:r '<principal>:<rights>'` (or `/deny`). | Long-form rights pass through; canonical-letter aliases (`F`, `M`, `RX`, `R`, `W`) are substituted by the renderer. |
| `remove_ace(path, principal)` | `icacls '<path>' /remove '<principal>'`. | `/remove` accepts only the principal and removes every matching grant / deny. |
| `clear_acl(path)` | `icacls '<path>' /reset`. | Resets to inherited-only. |
| `copy_acl(src, dst)` | PowerShell pipeline `Get-Acl -Path '<src>' \| Set-Acl -Path '<dst>'`. | Single PowerShell scriptblock; security descriptor travels as a `[FileSystemSecurity]` object. |

The "fan-out one icacls call per ACE" pattern in `set_acl` is the
central Windows-specific dispatch choice.  `Set-Acl` does accept a
fully constructed `[FileSystemSecurity]` object (a single round-trip
write), but constructing that object in PowerShell from a list of
caller-supplied `WindowsAceSpec` records requires inline
`New-Object System.Security.AccessControl.FileSystemAccessRule`
calls, which are far harder to inspect, debug, and partially-fail
than a sequence of `icacls /grant:r` invocations.  We therefore
fan out over `icacls`: each ACE corresponds to exactly one runner
call, and a per-ACE failure surfaces immediately with the offending
ACE token preserved on `command`.

`copy_acl` is the one verb where the `Get-Acl | Set-Acl` pipeline is
materially better than the `icacls` fan-out, because the security
descriptor travels as a single in-memory object and we never need
to enumerate the ACEs on either side.

---

## PowerShell Argv Shape

Every PowerShell scriptblock is dispatched as:

```python
[
    config.powershell_executable,   # "powershell.exe"
    "-NoProfile",                   # ignore $PROFILE
    "-NonInteractive",              # never prompt
    "-Command",
    "<scriptblock body>",
]
```

This shape:

* Pins the binary to a config-overridable name (tests inject a
  wrapper path).
* Guarantees `$PROFILE` cannot mutate cmdlet behaviour.
* Guarantees PowerShell never blocks waiting for interactive input.
* Lines up with the rest of the Wave-3 Windows landings (every
  Windows feature module emits the same prefix).

---

## Token Rendering Rules

Every `icacls` argv tail follows the canonical form:

```
[<verb>, "<principal>:<inheritance-prefix><rights-body>"]
```

Where:

| Field | Render |
|-------|--------|
| `<verb>` | `/grant:r` for `ALLOW`, `/deny` for `DENY`. |
| `<principal>` | Verbatim from `WindowsAceSpec.principal`. |
| `<inheritance-prefix>` | Empty for `NONE`; `(OI)` for `OBJECT_INHERIT`; `(CI)` for `CONTAINER_INHERIT`; `(OI)(CI)` for `BOTH`. |
| `<rights-body>` | `(<comma-joined letter-or-token list>)` — short letters when in the alias map, otherwise verbatim long-form. |

Examples:

```python
WindowsAceSpec("DOMAIN\\alice", ALLOW, ["Modify"])
    -> ["/grant:r", "DOMAIN\\alice:(M)"]

WindowsAceSpec("BUILTIN\\Users", DENY, ["Write"])
    -> ["/deny", "BUILTIN\\Users:(W)"]

WindowsAceSpec("DOMAIN\\alice", ALLOW, ["Modify"], inheritance=BOTH)
    -> ["/grant:r", "DOMAIN\\alice:(OI)(CI)(M)"]

WindowsAceSpec("Everyone", ALLOW, ["ReadAttributes", "WriteAttributes"])
    -> ["/grant:r", "Everyone:(ReadAttributes,WriteAttributes)"]
```

The full PowerShell argv that wraps this for `add_ace` is then:

```python
[icacls_executable, path, "/grant:r", "DOMAIN\\alice:(M)"]
```

(no PowerShell prefix — `icacls.exe` is invoked directly through the
runner, which is exactly the Wave-3 Windows pattern for legacy CLI
tools).

---

## Output Parsing for `Get-Acl | ConvertTo-Json`

The PowerShell scriptblock dispatched by `get_acl` is:

```powershell
$acl = Get-Acl -Path '<path>';
$payload = [PSCustomObject]@{
    Owner = $acl.Owner;
    Group = $acl.Group;
    Sddl = $acl.Sddl;
    Access = $acl.Access
};
$payload | ConvertTo-Json -Depth 5 -Compress
```

The JSON payload for a directory with three explicit ACEs and one
inherited ACE looks like:

```json
{
  "Owner": "BUILTIN\\Administrators",
  "Group": "DOMAIN\\Domain Users",
  "Sddl": "O:BAG:DUD:PAI(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)(D;;DCLCRPCR;;;S-1-1-0)",
  "Access": [
    {
      "FileSystemRights": "FullControl",
      "AccessControlType": 0,
      "IdentityReference": {"Value": "BUILTIN\\Administrators"},
      "IsInherited": false,
      "InheritanceFlags": 3,
      "PropagationFlags": 0
    },
    {
      "FileSystemRights": "ReadAndExecute, Synchronize",
      "AccessControlType": 0,
      "IdentityReference": {"Value": "BUILTIN\\Users"},
      "IsInherited": false,
      "InheritanceFlags": 3,
      "PropagationFlags": 0
    },
    {
      "FileSystemRights": "Modify, Synchronize",
      "AccessControlType": 1,
      "IdentityReference": {"Value": "Everyone"},
      "IsInherited": false,
      "InheritanceFlags": 0,
      "PropagationFlags": 0
    },
    {
      "FileSystemRights": "ReadAndExecute, Synchronize",
      "AccessControlType": 0,
      "IdentityReference": {"Value": "NT AUTHORITY\\SYSTEM"},
      "IsInherited": true,
      "InheritanceFlags": 3,
      "PropagationFlags": 0
    }
  ]
}
```

### Single-record array shape

`ConvertTo-Json` collapses single-element arrays into the bare
object form.  When the path has exactly one ACE, the payload looks
like:

```json
{
  "Owner": "BUILTIN\\Administrators",
  "Group": null,
  "Sddl": "O:BAG:DUD:PAI(A;;FA;;;BA)",
  "Access": {
    "FileSystemRights": "FullControl",
    "AccessControlType": 0,
    "IdentityReference": {"Value": "BUILTIN\\Administrators"},
    "IsInherited": false,
    "InheritanceFlags": 3,
    "PropagationFlags": 0
  }
}
```

The parser accepts both shapes — `_normalize_access_records` wraps
the bare-object form into a single-element list.  This is the same
ConvertTo-Json quirk handled by every other Windows feature module
in this package.

### Parse rules

1. The top-level payload must be a JSON object; any other shape
   yields an empty `WindowsAclInfo`.
2. `Owner` / `Group` / `Sddl` are read directly; missing or empty
   fields produce `None`.
3. `Access` is normalised to a list (bare-object -> single-element
   list).
4. Each `Access` record is parsed into a `WindowsAceInfo`:
   * `IdentityReference.Value` (or the bare-string form when
     PowerShell collapses the dict) yields `principal`.
   * `AccessControlType` is coerced from either an integer
     (`0=Allow`, `1=Deny`) or a string (`"Allow"` / `"Deny"`).
   * `FileSystemRights` is a comma-separated string; trimmed
     tokens populate `rights`.
   * `IsInherited` is a JSON boolean (also accepted as `"true"` /
     `"1"` strings for tolerance).
   * `InheritanceFlags` is coerced via `_coerce_inheritance` from
     either an integer (`0=None`, `1=ContainerInherit`,
     `2=ObjectInherit`, `3=both`) or a flags-style string.

The parser intentionally does NOT attempt to recover from truly
malformed `Get-Acl` output — `ConvertTo-Json` emits a stable shape
on every supported PowerShell host, and any deviation indicates a
real bug we want to surface (not paper over).

---

## Idempotency Guards

Each write verb runs an idempotency probe BEFORE dispatching:

| Verb | Probe behaviour |
|------|----------------|
| `set_acl` | Compares the explicit (non-inherited) parsed ACEs against `acl_spec` on a tuple of `(principal, permission, rights_set, inheritance)`.  Source order does not matter; `rights_set` is compared as a `frozenset` so token order within an ACE does not matter either. |
| `add_ace` | Returns `SKIPPED` when an explicit ACE matching `(principal, permission, rights_set, inheritance)` is already present. |
| `remove_ace` | Returns `SKIPPED` when no explicit ACE matches the spec's `principal`, regardless of permission / rights. |
| `clear_acl` | Returns `SKIPPED` when the parsed ACL has zero explicit (non-inherited) ACEs.  Inherited ACEs do NOT count. |
| `copy_acl` | Returns `SKIPPED` when the source and destination SDDL strings match (the canonical NTFS round-trip form).  Falls back to a sorted-tuple compare of the explicit ACE list when SDDL is absent on either side. |

When a probe `Get-Acl` invocation itself fails, the manager falls
through and dispatches the write anyway (where possible).  The
write step's failure mode then reports the underlying condition,
and the caller sees a clean `FAILED` result.

---

## Dry-Run Model

`config.dry_run=True` short-circuits every write verb at the very
top of the method, BEFORE any probe runs.  This means a dry-run
invocation does NOT consult `Get-Acl`, does NOT touch the runner,
and reports the would-have-been argv on `command`.

For `set_acl` (which fans out into multiple `icacls` calls), the
dry-run result populates `command` with a flattened pseudo-argv
that encodes every step joined by literal `&&` markers — this gives
the caller a faithful preview of the full sequence without
committing to any one ordering of dispatch.

---

## Elevation Model

| Verb | Elevation |
|------|-----------|
| `get_acl`, `list_aces` | Never elevate. |
| Idempotency probes (`Get-Acl` calls inside write verbs) | Never elevate. |
| Source-side and destination-side reads of `copy_acl` | Never elevate. |
| `icacls /reset`, `/grant:r`, `/deny`, `/remove` | `config.elevated`. |
| `Get-Acl | Set-Acl` pipeline | `config.elevated` (the pipeline is the dispatched write). |

NTFS DACL writes typically require Administrator on protected
paths.  The elevation flag is the caller's contract for "I want
this manager to run mutating commands as Administrator via the
runner's elevation mechanism".  Reads are deliberately excluded —
`Get-Acl` against a world-readable path does not need elevation,
and even Administrator-protected reads should be surfaced as a
typed `WindowsAclQueryError` rather than auto-elevated.

---

## Error Surface

| Surface | Trigger | Behaviour |
|---------|---------|-----------|
| `WindowsAclQueryError` | `Get-Acl` exits non-zero on a read (`get_acl` / `list_aces`). | Raised with the failed argv, exit code, and stderr captured on the exception. |
| `WindowsAclActionResult(status=FAILED)` | `icacls` (or the `Get-Acl \| Set-Acl` pipeline) exits non-zero on any write step. | Returned (NOT raised) with `exit_code`, `stdout`, `stderr`, `command` populated.  Subsequent ACE writes within a fan-out are NOT dispatched. |
| `WindowsAclActionResult(status=FAILED)` | A rights token outside the canonical `[FileSystemRights]` vocabulary is supplied. | Returned BEFORE invoking the runner; `message` enumerates the rejected tokens. |
| Source-read failure inside `copy_acl` | `Get-Acl '<src>'` exits non-zero. | Returned as a `FAILED` result populated from the read step's exception state. |
| Idempotency-probe failures | `Get-Acl` exits non-zero inside a write verb. | The probe is silently treated as "no current state known"; the write proceeds.  The write's failure surface reports the underlying condition cleanly. |

This split mirrors the rest of the package: read errors raise
typed exceptions, write errors return typed results.

---

## Correctness Properties

The following 22 properties are verified by the test module and / or
documented as invariants in the source:

1. `get_acl(path)` issues exactly one PowerShell scriptblock call
   with the canonical four-flag prefix.
2. `get_acl(path)` raises `WindowsAclQueryError` on non-zero exit
   with the failed argv, exit code, and stderr captured.
3. `get_acl(path)` accepts both the array-of-objects and the
   single-bare-object `Access` shapes from `ConvertTo-Json`.
4. `get_acl(path)` accepts both the integer (`0`/`1`) and the
   string (`"Allow"`/`"Deny"`) encodings of `AccessControlType`.
5. `get_acl(path)` coerces `InheritanceFlags` from integer-flag
   and string-flag encodings.
6. `list_aces(path)` returns the parsed `aces` list in source
   order.
7. `set_acl(path, spec)` dispatches `icacls /reset` followed by
   one `icacls /grant:r` (or `/deny`) per ACE, in spec order.
8. `set_acl(path, spec)` returns `SKIPPED` when the parsed ACL
   already matches.
9. `set_acl(path, spec)` honours `dry_run` BEFORE consulting the
   runner.
10. `set_acl(path, spec)` short-circuits with `FAILED` on the first
    per-ACE failure; subsequent ACEs are NOT dispatched.
11. `set_acl(path, spec)` rejects unknown rights tokens BEFORE
    dispatching.
12. `add_ace(path, spec)` dispatches exactly one `icacls /grant:r`
    or `icacls /deny` call.
13. `add_ace(path, spec)` returns `SKIPPED` when the same ACE is
    already present.
14. `add_ace(path, spec)` honours inheritance flags in the
    rendered token (`(OI)` / `(CI)` / `(OI)(CI)`).
15. `add_ace(path, spec)` rejects unknown rights tokens BEFORE
    dispatching.
16. `remove_ace(path, spec)` dispatches `icacls /remove
    '<principal>'`.
17. `remove_ace(path, spec)` returns `SKIPPED` when no explicit
    ACE matches the principal.
18. `clear_acl(path)` dispatches `icacls /reset` and skips when
    the explicit ACL is empty.
19. `copy_acl(src, dst)` dispatches the `Get-Acl | Set-Acl`
    pipeline as a single PowerShell scriptblock.
20. `copy_acl(src, dst)` returns `SKIPPED` when the source and
    destination SDDL strings match.
21. Every write passes `elevated=True` to the runner when the
    manager is configured with `elevated=True`.
22. Every read passes `elevated=False` to the runner regardless of
    the manager's `elevated` configuration.

---

## Configuration

`WindowsAclManagerConfig` extends `FeatureConfig` with two
PATH-overridable binary names:

| Field | Default | Purpose |
|-------|---------|---------|
| `powershell_executable` | `"powershell.exe"` | PATH-resolved Windows PowerShell 5.1 binary; tests can inject a wrapper. |
| `icacls_executable` | `"icacls.exe"` | PATH-resolved legacy CLI binary; tests can inject a wrapper. |
| `dry_run` (inherited) | `False` | When `True`, write verbs short-circuit. |
| `elevated` (inherited) | `False` | When `True`, write steps run elevated. |

There is no other configuration: ACL state lives entirely on the
target paths, and both tools are stateless wrappers on the kernel
ACL syscalls.

---

## Aspect Composition

`WindowsAclManager` is safe under any aspect composition on the
runner because:

1. The manager never spawns a subprocess directly — every command
   flows through `self._runner.run(...)`.
2. The manager re-queries ACL state per write rather than caching;
   retry-aspect re-dispatches do not see stale state.
3. The manager's only state is the injected runner and config;
   per-call state lives in local variables.
4. Persistence aspects on the runner cannot mutate
   `WindowsAclActionResult` shape; the manager always builds the
   result from the captured `RunResult`.

---

## Usage Examples

### Local execution (no elevation)

```python
from ccai.sysman.runners.localcommandrunner import LocalCommandRunner

from ccai.sysman.windows.aclmanagement import (
    WindowsAceSpec,
    WindowsAcePermission,
    WindowsAclManager,
    WindowsAclManagerConfig,
)

runner = LocalCommandRunner()
config = WindowsAclManagerConfig()
manager = WindowsAclManager(runner=runner, config=config)

info = manager.get_acl(path="C:\\Data")
for ace in info.aces:
    print(ace.principal, ace.permission, ace.rights)
```

### Aspect composition (write verbs elevated)

```python
config = WindowsAclManagerConfig(elevated=True)
manager = WindowsAclManager(runner=runner, config=config)

spec = WindowsAceSpec(
    principal="DOMAIN\\alice",
    permission=WindowsAcePermission.ALLOW,
    rights=["Modify"],
)
result = manager.add_ace(path="C:\\Data", principal=spec)
assert result.status.value in ("success", "skipped")
```

### Dry-run preview

```python
config = WindowsAclManagerConfig(dry_run=True, elevated=True)
manager = WindowsAclManager(runner=runner, config=config)

result = manager.clear_acl(path="C:\\Data")
assert result.status.value == "dry_run"
print(result.command)
```

---

## Testing Strategy

The test module `test_aclmanagement.py` covers:

* Parsing `Get-Acl | ConvertTo-Json` output for both the multi-ACE
  array form and the single-ACE bare-object form.
* `set_acl` dispatching the canonical reset-then-fan-out sequence.
* `set_acl` `SKIPPED` when the parsed ACL already matches.
* `set_acl` `DRY_RUN` short-circuiting the runner.
* `add_ace` dispatching the rendered `icacls` argv.
* `add_ace` `SKIPPED` when the ACE is already present.
* `add_ace` rendering inheritance flags into the icacls token.
* `remove_ace` dispatching `icacls /remove`.
* `remove_ace` `SKIPPED` when no ACE matches the principal.
* `clear_acl` dispatching `icacls /reset`.
* `copy_acl` dispatching the `Get-Acl | Set-Acl` pipeline.
* `copy_acl` skipping when source and destination SDDL match.
* `list_aces` returning the full flat list.
* Writes elevating, reads never elevating.
* Failed commands surfacing stderr on the result.
* Unknown rights tokens returning `FAILED` BEFORE dispatch.

Total: 18 tests, all using a `_MockCommandRunner` that records
every call and returns pre-seeded `RunResult` values keyed by argv
tuple.  No live PowerShell / `icacls` is invoked.

---

## ACL-Model Concept Mapping (POSIX vs macOS NFSv4-style vs NTFS)

| Concept | POSIX | macOS NFSv4-style | NTFS DACL |
|---------|-------|-------------------|-----------|
| Allow ACE | `user:alice:rwx` | `user:alice allow read,write,execute` | `(A;;FA;;;<sid>)` SDDL fragment / `/grant:r alice:(F)` icacls |
| Deny ACE | (not expressible) | `user:alice deny delete` | `(D;;DC;;;<sid>)` SDDL fragment / `/deny alice:(W)` icacls |
| Inheritance | Default ACL partition (`default:user:...`) | `file_inherit` / `directory_inherit` rights tokens | `(OI)` / `(CI)` flag prefix on the `icacls` argument; `InheritanceFlags` enum on `Get-Acl` |
| Effective-rights mask | `mask::rwx` row | (none) | (none) |
| Canonical text form | `getfacl` output | `ls -le` index-prefixed lines | SDDL string on `Get-Acl.Sddl` |
| Round-trip stream | `setfacl --set-file=-` | (no native stream input) | `Get-Acl \| Set-Acl` pipeline (object) or `icacls /save` + `/restore` (textual) |

This Windows manager exposes the NTFS-native subset directly.  When
a cross-OS caller needs to translate between the per-OS shapes,
that translation lives in the `codecraftai-interfaces` adapter layer
(out of scope for this spec).

---

## Spec-vs-Contract Drift

The cross-OS contract uses the names `AclSet` (for the per-path
snapshot), `AclEntry` (for an ACE), and `AclPrincipal` (for a
principal record carrying `kind` / `name` / `sid`).  This Windows
spec exposes:

* `WindowsAclInfo` instead of `WindowsAclSet` — `Info` aligns with
  the rest of this package's read-shape types.
* `WindowsAceInfo` and `WindowsAceSpec` instead of
  `WindowsAclEntry` — the `Info` / `Spec` split distinguishes
  parsed read shapes from caller-supplied write shapes.
* `WindowsAcePermission` (`ALLOW` / `DENY`) — the contract folds
  permission polarity into a single `allow: bool` boolean on
  `AclEntry`; this spec exposes the equivalent as a typed enum for
  symmetry with the other Windows sysman enums.
* No `WindowsAclPrincipal` dataclass — the principal is carried as
  a single `str` on `WindowsAceInfo` / `WindowsAceSpec`.  Windows
  principals resolve through LSA and surface as
  `DOMAIN\name` strings; the SID is recoverable from `Get-Acl.Sddl`
  when callers need it.
* `inheritance: WindowsAceInheritance` on `WindowsAceInfo` /
  `WindowsAceSpec` — the contract pushes inheritance into a generic
  `is_default` / `inherited` pair; this spec exposes the NTFS-
  native flag set as a typed enum.
* `is_inherited: bool` on `WindowsAceInfo` — the explicit-vs-
  inherited distinction is critical for write idempotency and is
  surfaced directly.
* `sddl: str | None` on `WindowsAclInfo` — no cross-OS analogue;
  the canonical NTFS round-trip serialisation is exposed for
  `copy_acl` byte-equality and for callers that need the raw SDDL.
* The `recursive: bool` parameter on every write verb in the
  contract is intentionally NOT exposed on this OS in this pilot;
  `icacls` carries `/T` for recursion and `Set-Acl` accepts an
  enumerated child set, but recursive ACL walks demand a richer
  reporting shape than the per-path verbs provide and will be
  delivered by a follow-up `3-acl-management-recursive` spec.

Cross-OS callers that need the contract's names will see them in
the `codecraftai-interfaces` adapter layer when that layer
materialises; the per-OS managers favour names that align with the
rest of their package.

---

## Performance Notes

`Get-Acl` typically completes in 50-200 ms (PowerShell startup
dominates); `icacls /reset` and `icacls /grant:r` are similarly
cheap.  The per-ACE fan-out in `set_acl` means N ACEs becomes 1 +
N runner calls — for a typical host-management workload (single-
digit ACEs per path), this is well under one second total.  No
caching is performed across calls; the per-call cost is acceptable
and PowerShell-process startup dominates anyway.

For callers that need to batch the same ACL across many paths, the
recommended pattern is to render the spec once and call `set_acl`
per path.  A future `apply_acl_template` verb may collapse those
into a single multi-target invocation behind a richer reporting
shape.

---

## Backwards Compatibility

This is a new feature; there are no backwards-compatibility
constraints.  The cross-OS contract pins the public verb names; we
follow them.  The OS-prefixed type names are pinned by this spec;
future additions go through new specs.
