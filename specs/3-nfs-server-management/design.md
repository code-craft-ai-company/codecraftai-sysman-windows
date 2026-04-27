# Design

## Overview

The Windows nfs-server-management feature is implemented as a
single module, ``automojo.sysman.windows.nfsservermanagement``,
exposing a ``WindowsNfsServerManager`` class that wraps the
PowerShell ``NFS`` module (``New-NfsShare`` / ``Remove-NfsShare`` /
``Set-NfsShare`` / ``Get-NfsShare``) and the ``NfsService`` Windows
service through the ``ICommandRunner`` abstraction.

Follows the standard manager pattern; delegates daemon lifecycle to
``3-service-management``.

**See Also:**
- ``automojo-sysman-common/specs/contracts/nfs-server-management/contract.md``
  — the cross-OS abstract contract.
- ``automojo-sysman-linux-ubuntu/specs/3-nfs-server-management/`` /
  ``automojo-sysman-macos/specs/3-nfs-server-management/`` —
  Unix-flavoured implementations of the same contract that operate
  on an on-disk ``/etc/exports`` file.  Windows differs in that the
  share table is held in the registry / service-store, and shares
  are addressed by **share name**, not by path.
- ``3-service-management`` — ``NfsService`` lifecycle.
- ``3-firewall-management`` — opening the NFS RPC ports through
  the Windows Firewall.
- ``3-filesystem-management`` — Windows-side mount-points and
  drive-letter mapping (``Mount-NfsShare`` is exposed there).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsNfsServerManager                          │
│                                                             │
│  Writes:                                                    │
│    add_export(), remove_export(),                           │
│    reload_exports(), set_export_options()                   │
│  Reads:                                                     │
│    list_exports(), get_export_status()                      │
│                                                             │
│  Helpers:                                                   │
│    _build_powershell_argv()                                 │
│    _share_exists()                                          │
│    _lookup_share_by_path()                                  │
└────────────────┬──────────────────────────┬─────────────────┘
                 │                          │
                 ▼                          ▼
      ┌─────────────────────┐   ┌─────────────────────────┐
      │   ICommandRunner    │   │  PowerShell ``NFS``     │
      │                     │   │  module                 │
      └─────────────────────┘   └─────────────────────────┘
```

Every command flows through the injected ``ICommandRunner`` as a
PowerShell scriptblock dispatched by argv ``["powershell.exe",
"-NoProfile", "-NonInteractive", "-Command", "<scriptblock>"]``.
This argv shape ensures that ``$PROFILE`` cannot mutate cmdlet
behaviour and that PowerShell never blocks waiting for interactive
input.

---

## NFS Server Role Requirement

The PowerShell ``NFS`` module ships with the Windows Server "Server
for NFS" role (``Install-WindowsFeature -Name FS-NFS-Service``).
It is NOT available on Windows 10 / 11 client SKUs by default —
operators on client SKUs can install the Remote Server
Administration Tools (RSAT) and target a remote NFS server, but the
locally-dispatched cmdlets in this module will fail with
``CommandNotFoundException`` when the module is missing.

``WindowsNfsManagerConfig`` carries an advisory
``requires_nfs_server_role`` field (default ``True``) documenting
this requirement.  The manager itself does not probe for the role;
the post-write cmdlet failure surfaces the misconfiguration as a
``FAILED`` result with the cmdlet's stderr captured verbatim.

---

## Cross-OS Exports-Syntax Concept Map

The cross-OS contract intentionally generalises the verb shape but
not the underlying configuration surface.  Windows differs sharply
from the Unix family — see also the macOS / Ubuntu specs.

| Concept | Ubuntu / Linux | macOS (Darwin) | Windows (Server for NFS) |
|---------|----------------|----------------|--------------------------|
| File path | ``/etc/exports`` | ``/etc/exports`` | n/a — share table is in the registry |
| Line shape | ``<path> <client>(<opts>) <client>(<opts>)…`` | ``<path> -<opt> -<opt> <client> [<client>…]`` | n/a — ``New-NfsShare -Path <path> -Name <name>`` |
| Options separator | parenthesised, comma-separated | dash-prefixed, space-separated | named cmdlet parameters |
| Per-client options | yes (each client carries its own opts) | shared across the line | yes (per-permission rules attached to the share) |
| Wildcard host | literal ``*`` | ``-network 0.0.0.0 -mask 0.0.0.0`` | ``-AllowRootAccess`` / ``-EnableUnmappedAccess`` plus per-client ``Grant-NfsSharePermission`` |
| Reload command | ``exportfs -ra`` | ``nfsd update`` | ``Restart-Service NfsService`` |
| Per-export reload | yes (re-export all) | yes (whole-table refresh) | **no — service-wide restart only** |
| Addressing | by path | by path | **by share name** |

The Windows manager retains the per-client API surface
(``WindowsNfsClientSpec`` carries ``host`` and ``options``) for
cross-OS parity, but the underlying cmdlet does not directly
support the same level of per-client option granularity that
``/etc/exports`` does on Unix.  In the pilot, every share is
created with the canonical safe-default cmdlet parameters
(``-Authentication 'sys' -EnableUnmappedAccess $false -Permission
ReadWrite -AllowRootAccess $false``); the per-client option list on
the spec is recorded as the share's pseudo-flag list but does NOT
translate into ``Grant-NfsSharePermission`` cmdlet dispatches.

---

## Path → Share-Name Normalisation

Windows NFS shares are identified by **share name**, not by
filesystem path.  The contract's ``path`` parameter is the disk
path the share is rooted at (e.g., ``"D:\Shares\Public"``), and
``share_name`` is the canonical NFS-visible name (e.g.,
``"Public"``).

| Verb | How the share is addressed |
|------|----------------------------|
| ``add_export`` | ``WindowsNfsExportSpec.share_name`` (mandatory) |
| ``remove_export`` | derived from ``path`` via ``Get-NfsShare`` lookup |
| ``set_export_options`` | derived from ``path`` via ``Get-NfsShare`` lookup |
| ``get_export_status`` | derived from ``path`` via ``Get-NfsShare`` lookup |
| ``list_exports`` | every ``share_name`` returned by ``Get-NfsShare`` |
| ``reload_exports`` | n/a — service-wide |

When ``add_export`` receives a spec with no ``share_name`` the verb
returns ``FAILED`` with a precise message; the cmdlet is **not**
dispatched.  This is captured by the contract test
``test_share_name_required_for_writes``.

When ``remove_export`` / ``set_export_options`` /
``get_export_status`` cannot find a share matching the supplied
``path``, the verb returns ``SKIPPED`` (or an inactive status) —
this matches the cross-OS contract's idempotency invariant.

---

## Data Models

```python
WindowsNfsActionStatus = ActionStatus  # alias from featurebase


@dataclass
class WindowsNfsManagerConfig(FeatureConfig):
    powershell_executable: str = "powershell.exe"
    requires_nfs_server_role: bool = True
    service_name: str = "NfsService"


@dataclass
class WindowsNfsActionResult(FeatureActionResult):
    path: str | None = None
    share_name: str | None = None
    message: str | None = None


@dataclass
class WindowsNfsClientSpec:
    host: str = "*"
    options: list[str] = field(default_factory=lambda: [
        "ReadWrite", "Authentication=sys",
    ])


@dataclass
class WindowsNfsExportSpec:
    path: str
    clients: list[WindowsNfsClientSpec]
    share_name: str | None = None  # mandatory for writes


@dataclass
class WindowsNfsExportInfo:
    path: str
    share_name: str | None
    clients: list[WindowsNfsClientSpec]
    is_active: bool


@dataclass
class WindowsNfsExportStatus:
    path: str
    is_active: bool
    client_count: int
    flags: list[str]


class WindowsNfsQueryError(Exception):
    """Raised when a read fails for a reason other than absence."""
```

---

## Verb / Tool Mapping

| Verb | PowerShell cmdlet |
|------|-------------------|
| ``add_export`` | ``New-NfsShare -Name '<n>' -Path '<p>' -Authentication 'sys' -EnableUnmappedAccess $false -Permission ReadWrite -AllowRootAccess $false`` |
| ``remove_export`` | ``Remove-NfsShare -Name '<n>' -Force`` |
| ``list_exports`` | ``Get-NfsShare \| ConvertTo-Json -Compress -Depth 5`` |
| ``reload_exports`` | ``Restart-Service NfsService`` |
| ``get_export_status`` | ``Get-NfsShare -Name '<n>' \| ConvertTo-Json`` |
| ``set_export_options`` | ``Set-NfsShare -Name '<n>' <named-args>`` |

Reads use ``elevated=False``; writes use ``elevated`` per
``config.elevated``.

---

## Configuration

```python
@dataclass
class WindowsNfsManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsNfsServerManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.  Set
                                      to ``"pwsh.exe"`` to target
                                      PowerShell 7+.
        :param requires_nfs_server_role: Advisory boolean — the
                                         manager does NOT probe for
                                         the role.  Default ``True``.
        :param service_name: Windows service name for the NFS
                             daemon.  Default ``"NfsService"``.
    """
    powershell_executable: str = "powershell.exe"
    requires_nfs_server_role: bool = True
    service_name: str = "NfsService"
```

---

## Key Method Designs

### `add_export(spec)`

```
1. Validate spec.share_name is non-empty.  Empty → FAILED with
   message="share_name is required for Windows NFS exports".
2. Build the New-NfsShare scriptblock.
3. Build the canonical PowerShell argv:
   [powershell.exe, -NoProfile, -NonInteractive, -Command, <script>].
4. Dry-run? → DRY_RUN with the would-have-been argv.
5. Probe Get-NfsShare -Name '<share-name>' to detect an existing
   share with the same name + path → SKIPPED.
6. Dispatch.  Map exit code to SUCCESS / FAILED.
```

### `remove_export(path)`

```
1. Build canonical argv.
2. Dry-run? → DRY_RUN.
3. Look up share by path via Get-NfsShare | ConvertTo-Json.
   No matching share → SKIPPED.
4. Dispatch Remove-NfsShare -Name '<share-name>' -Force.
```

### `list_exports()`

```
1. Dispatch Get-NfsShare | ConvertTo-Json (elevated=False).
2. Parse the JSON output into list[WindowsNfsExportInfo].
3. is_active=True for every entry returned by Get-NfsShare —
   Get-NfsShare only returns active shares.
4. Return the list.
```

### `reload_exports()`

```
1. Build Restart-Service NfsService scriptblock.
2. Dry-run? → DRY_RUN.
3. Dispatch (elevated per config).
```

### `get_export_status(path)`

```
1. Dispatch Get-NfsShare | ConvertTo-Json.
2. Locate the share whose Path equals path.
3. Found → status with is_active=True, client_count=1 (Windows
   doesn't report per-client counts at this level), flags from
   permission/authentication fields.
4. Not found → inactive status.
```

### `set_export_options(path, options)`

```
1. Look up share by path.  None → SKIPPED.
2. Translate options into Set-NfsShare named parameters.
3. Compare against current share fields.  Match → SKIPPED.
4. Dry-run? → DRY_RUN.
5. Dispatch Set-NfsShare -Name '<share-name>' <named-args>.
```

---

## PowerShell Argv Shape

Every PowerShell scriptblock is dispatched as

```
["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
 "<scriptblock>"]
```

The flags ensure:
- ``-NoProfile`` — ``$PROFILE`` is not loaded; cmdlet behaviour is
  not mutated by user / system profiles.
- ``-NonInteractive`` — PowerShell never blocks waiting for
  interactive prompts.
- ``-Command`` — runs the supplied scriptblock and exits.

Single-quoted strings are used wherever the scriptblock embeds a
share name, path, or other dynamic value.  Embedded single quotes
are escaped by doubling per PowerShell's single-quoted-string
grammar.  This logic lives in ``_ps_quote(value)``.

---

## Worked Example — Adding A New Share

```python
runner = LocalCommandRunner()
config = WindowsNfsManagerConfig(elevated=True)
manager = WindowsNfsServerManager(runner=runner, config=config)
spec = WindowsNfsExportSpec(
    path=r"D:\Shares\Public",
    share_name="Public",
    clients=[WindowsNfsClientSpec(host="*", options=[
        "ReadWrite", "Authentication=sys",
    ])],
)
result = manager.add_export(spec=spec)
```

Dispatched commands:

1. ``["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
   "Get-NfsShare -Name 'Public' | ConvertTo-Json -Compress -Depth 5"]``
   (``elevated=False``) — idempotency probe.
2. ``["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
   "New-NfsShare -Name 'Public' -Path 'D:\\Shares\\Public'
   -Authentication 'sys' -EnableUnmappedAccess $false
   -Permission ReadWrite -AllowRootAccess $false"]``
   (``elevated=True``) — the actual share creation.

If step 1 returns a share whose ``Path`` matches
``D:\Shares\Public``, the verb returns ``SKIPPED`` and step 2 is
skipped.

---

## Edge Cases And Their Handling

| Edge case | Handling |
|-----------|----------|
| ``share_name`` is ``None`` or empty on ``add_export`` | ``FAILED`` with explicit message; runner is NOT invoked. |
| ``Get-NfsShare`` cmdlet not available (NFS Server role missing) | The probe returns non-zero with ``CommandNotFoundException``; the verb continues to the dispatch step which also fails with the same error.  The result's ``stderr`` captures the missing-cmdlet message verbatim. |
| ``Get-NfsShare -Name`` for a non-existent share returns non-zero | Idempotency probe interprets non-zero as "share does not exist"; the dispatch proceeds. |
| Path with embedded single quotes | ``_ps_quote`` doubles the quote per PowerShell grammar. |
| Path with backslashes | Backslashes are passed through verbatim — PowerShell single-quoted strings do not interpret escape sequences. |
| Multiple shares pointing at the same path | ``remove_export`` removes the first match; ``set_export_options`` updates the first match.  Operators with multi-share-per-path setups should call by share name explicitly via a future ``remove_export_by_name`` extension. |

---

## Dry-Run Behaviour

Dry-run disables every mutating cmdlet (``New-NfsShare``,
``Remove-NfsShare``, ``Set-NfsShare``, ``Restart-Service``).
Read-only probes (``Get-NfsShare``) run freely so that idempotency
decisions remain correct.

---

## Correctness Properties

1. **Idempotent shares.** ``add_export`` SKIPs when the share name
   exists with the same path; ``remove_export`` SKIPs when the
   path is not registered; ``set_export_options`` SKIPs when the
   options already match.
2. **Read isolation.** Read verbs and idempotency probes never
   pass ``elevated=True``.
3. **Service delegation.** This module never invokes
   ``Stop-Service`` / ``Start-Service``; daemon restart is
   delegated to ``3-service-management`` via the user-facing
   ``reload_exports`` (which uses ``Restart-Service`` because
   that is Windows's only "reload" surface).
4. **share-name addressability.** Writes that take a
   ``WindowsNfsExportSpec`` pass through the spec's ``share_name``
   verbatim; writes that take a ``path`` look up the share-name
   via ``Get-NfsShare`` first.

---

## Testing Strategy

- ``_MockCommandRunner`` records every call and returns pre-seeded
  ``RunResult`` values keyed by argv tuple OR by PowerShell-script
  prefix (mirrors the ``test_dnsservermanagement.py`` pattern).
- ``test_add_export_invokes_powershell`` exercises the canonical
  ``New-NfsShare`` argv.
- ``test_share_name_required_for_writes`` proves that a spec with
  empty ``share_name`` returns ``FAILED`` without invoking the
  runner.
- Idempotency, dry-run, elevation, and failure-mapping tests follow
  the standard shape.
- A dedicated test verifies that ``Restart-Service NfsService``
  argv is dispatched by ``reload_exports``.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| ``NfsService`` lifecycle | ``3-service-management`` | Mostly delegated.  ``reload_exports`` is the one verb that calls ``Restart-Service`` directly because Windows has no per-share reload. |
| Opening NFS RPC ports | ``3-firewall-management`` | Coordinator opens through the Windows Firewall there. |
| Client-side NFS mounts | ``3-filesystem-management`` | Out of scope — ``Mount-NfsShare`` lives there. |
| ``Grant-NfsSharePermission`` per-client ACLs | Out of scope for the pilot | A future extension will surface per-client permission granting. |

---

## Follow-Ups

- **Per-client permissions.** Add a verb that wraps
  ``Grant-NfsSharePermission -Name <share> -ClientName <client>
  -ClientType host -Permission <perm>`` to surface the per-client
  ACL grain that ``WindowsNfsClientSpec`` carries on the API.
- **AD-integrated authentication.** Surface ``-Authentication krb5``
  / ``krb5p`` parameters via ``set_export_options``.
- **NFS v3 vs v4 toggling.** Wire up ``Set-NfsServerConfiguration``
  to toggle protocol versions (out of scope for the pilot).
- **Multi-share-per-path.** Today ``remove_export(path)`` removes
  only the first match; a follow-up should expose
  ``remove_export_by_name(name)`` for the multi-share-per-path
  setup.

---

## Spec-vs-Contract Drift

This Windows spec deviates from the cross-OS contract in three
documented places:

1. **share-name addressability.** The contract addresses exports
   by ``path`` exclusively.  This Windows spec adds a mandatory
   ``share_name`` field on ``WindowsNfsExportSpec`` because
   Windows's underlying cmdlet surface requires it.  The contract
   accommodates this in the cross-OS table (`§Export-Path
   Mapping`); this spec follows that guidance.
2. **Per-client option fidelity.** The contract permits per-client
   options (which Linux supports natively).  This Windows spec
   accepts the per-client API surface but the pilot does NOT
   translate per-client options into ``Grant-NfsSharePermission``
   dispatches; the entire share is created with the canonical
   safe-default cmdlet parameters.  Per-client permission grants
   are a tracked follow-up.
3. **No per-share reload.** Windows does not expose a per-share
   reload cmdlet; ``reload_exports`` invokes ``Restart-Service
   NfsService`` (service-wide).  Callers should rarely need this
   — ``New-NfsShare`` / ``Remove-NfsShare`` update server-side
   state synchronously — but the verb is provided for contract
   parity.
