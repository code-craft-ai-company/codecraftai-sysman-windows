# Requirements

## Introduction

The nfs-server-management feature provides the
``automojo-sysman-windows`` package with the ability to manage a
local NFS server on Windows hosts.  Windows ships an "NFS Server"
role (a.k.a. "Server for NFS") on Windows Server SKUs; the
PowerShell ``NFS`` module exposes ``New-NfsShare`` /
``Remove-NfsShare`` / ``Set-NfsShare`` / ``Get-NfsShare`` cmdlets to
manage NFS shares, and the daemon itself runs as the ``NfsService``
Windows service.  This spec implements the cross-OS
nfs-server-management contract defined at
``automojo-sysman-common/specs/contracts/nfs-server-management/contract.md``
for Windows, scoped to the PowerShell ``NFS`` module.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *what* exports policy the server enforces —
it provides the capability to *apply* policy reliably, idempotently,
and with full auditability.  All system commands (``New-NfsShare``,
``Remove-NfsShare``, ``Set-NfsShare``, ``Get-NfsShare``,
``Restart-Service NfsService``) are executed exclusively through the
``automojo-interfaces`` command-runner abstraction inside a
PowerShell scriptblock.  No ``subprocess``, no direct ``open``, and
no direct ``pathlib`` calls are made inside this module.

**NFS Server role requirement.** The ``NFS`` PowerShell module ships
with the Windows Server "NFS Server" role; it is NOT available on
Windows 10 / 11 client SKUs.  ``WindowsNfsManagerConfig`` carries an
advisory ``requires_nfs_server_role`` flag (default ``True``) that
documents the requirement; the manager itself does NOT probe for the
role.  Hosts without the role surface ``FAILED`` at the cmdlet
boundary with the cmdlet's ``CommandNotFoundException`` captured in
``stderr``.

**Share-name addressability.** Unlike Linux and macOS, Windows
identifies NFS shares by **share name**, not by filesystem path.
Every Windows verb that operates on a single share takes a share
name; the ``WindowsNfsExportSpec`` therefore carries a mandatory
``share_name`` field alongside the abstract ``path``.  The contract's
``path``→``share-name`` normalisation is documented in design.md.

This feature complements — and does not duplicate — the
``NfsService`` lifecycle owned by ``3-service-management``, the
RPC-port firewall surface owned by ``3-firewall-management``, and the
client-side mounting surface owned by ``3-filesystem-management``.

---

## Requirements

### Requirement 1: Add an Export

**User Story:** As a maintenance coordinator, I want to register an
NFS share on a Windows server, so that the declared share set
matches the desired state and the daemon advertises the new share
immediately.

#### Acceptance Criteria

1. WHEN the coordinator calls ``add_export(spec)`` THE SYSTEM SHALL
   build a PowerShell scriptblock invoking ``New-NfsShare -Name
   '<share-name>' -Path '<path>' -Authentication 'sys'
   -EnableUnmappedAccess $false -Permission ReadWrite -AllowRootAccess
   $false`` and dispatch it via the runner.
2. WHEN ``spec.share_name`` is ``None`` or empty THE SYSTEM SHALL
   return ``status=FAILED`` with ``message="share_name is required
   for Windows NFS exports"`` without invoking the runner.
3. WHEN ``Get-NfsShare -Name '<share-name>'`` reports the share is
   already registered with the same path THE SYSTEM SHALL return
   ``status=SKIPPED`` without invoking ``New-NfsShare``.
4. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``
   without invoking the runner.
5. WHEN the runner exits non-zero THE SYSTEM SHALL return
   ``status=FAILED`` with ``exit_code``, ``stdout``, ``stderr``, and
   ``command`` populated.

---

### Requirement 2: Remove an Export

**User Story:** As a maintenance coordinator, I want to unregister
an NFS share from a Windows server, so that the daemon stops
advertising the share and the declared share set matches the desired
state.

#### Acceptance Criteria

1. WHEN the coordinator calls ``remove_export(path)`` THE SYSTEM
   SHALL look up the share whose ``Path`` matches ``path`` via
   ``Get-NfsShare`` and dispatch
   ``Remove-NfsShare -Name '<share-name>' -Force`` for the matching
   share.
2. WHEN no share advertises ``path`` THE SYSTEM SHALL return
   ``status=SKIPPED``.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   ``status=FAILED``.

---

### Requirement 3: List Exports

**User Story:** As a maintenance coordinator, I want to enumerate
every NFS share registered on the server, so that I can compare
against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls ``list_exports()`` THE SYSTEM SHALL
   invoke ``Get-NfsShare | ConvertTo-Json -Compress -Depth 5`` via
   the runner with ``elevated=False``.
2. WHEN the runner exits zero THE SYSTEM SHALL parse the JSON output
   into a ``list[WindowsNfsExportInfo]`` with ``path``,
   ``share_name``, ``clients``, and ``is_active`` populated.
3. WHEN the JSON output is empty (no shares) THE SYSTEM SHALL return
   an empty list.
4. WHEN the runner exits non-zero AND ``stderr`` is non-empty THE
   SYSTEM SHALL raise ``WindowsNfsQueryError``.

---

### Requirement 4: Reload Exports

**User Story:** As a maintenance coordinator, I want to reload the
NFS service so that recent share registrations take effect on
clients holding stale negotiated state.

#### Acceptance Criteria

1. WHEN the coordinator calls ``reload_exports()`` THE SYSTEM SHALL
   invoke ``Restart-Service NfsService`` via the runner.
2. THE SYSTEM SHALL document in this spec and in design.md that
   Windows has no per-share reload cmdlet — ``Restart-Service`` is
   the only available "reload"; the per-share daemon-side state is
   already updated synchronously by ``New-NfsShare`` /
   ``Remove-NfsShare``, so callers should rarely need to invoke
   this verb.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.
4. WHEN the runner exits 0 THE SYSTEM SHALL return ``status=SUCCESS``.
5. WHEN the runner exits non-zero THE SYSTEM SHALL return
   ``status=FAILED``.
6. ``reload_exports`` MUST NOT skip — it is operational and
   unconditional.

---

### Requirement 5: Get Export Status

**User Story:** As a maintenance coordinator, I want to query
whether a specific share is currently registered, so that post-write
checks can confirm the share is live before downstream mounts
proceed.

#### Acceptance Criteria

1. WHEN the coordinator calls ``get_export_status(path)`` THE SYSTEM
   SHALL invoke ``Get-NfsShare`` and locate the share whose ``Path``
   matches ``path``.
2. WHEN a share is found THE SYSTEM SHALL return a
   ``WindowsNfsExportStatus`` with ``is_active=True``,
   ``client_count`` set to the number of distinct
   ``ClientGroups``-style entries (best-effort; ``Get-NfsShare`` is
   share-scoped, not client-scoped, so the count is typically ``1``
   when active and ``0`` when inactive), and ``flags`` populated
   from the share's permission / authentication fields.
3. WHEN no share advertises ``path`` THE SYSTEM SHALL return a
   ``WindowsNfsExportStatus`` with ``is_active=False``,
   ``client_count=0``, and ``flags=[]``.
4. WHEN ``Get-NfsShare`` exits non-zero AND ``stderr`` is non-empty
   THE SYSTEM SHALL raise ``WindowsNfsQueryError``.

---

### Requirement 6: Set Export Options

**User Story:** As a maintenance coordinator, I want to update the
options of an existing share, so that the share's permissions and
authentication can be adjusted without un-registering and re-
registering.

#### Acceptance Criteria

1. WHEN the coordinator calls ``set_export_options(path, options)``
   THE SYSTEM SHALL look up the share whose ``Path`` matches ``path``
   and dispatch ``Set-NfsShare -Name '<share-name>' <named-args>``
   with the supplied options translated to PowerShell parameters.
2. WHEN no share advertises ``path`` THE SYSTEM SHALL return
   ``status=SKIPPED``.
3. WHEN every requested option already matches the existing share
   THE SYSTEM SHALL return ``status=SKIPPED``.
4. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.

---

### Requirement 7: Share-Name Required for Writes

**User Story:** As a maintenance coordinator, I want every write
verb that registers a share to require a share name explicitly, so
that the ambiguity of name-vs-path addressing is resolved up front.

#### Acceptance Criteria

1. WHEN ``add_export(spec)`` is called with ``spec.share_name``
   ``None`` or empty THE SYSTEM SHALL return ``status=FAILED``
   with ``message="share_name is required for Windows NFS
   exports"`` and SHALL NOT invoke the runner.
2. WHEN ``set_export_options(path, options)`` cannot derive a share
   name from ``path`` (no matching share) THE SYSTEM SHALL return
   ``status=SKIPPED`` rather than ``FAILED``.

---

### Requirement 8: Elevation Policy

**User Story:** As a maintenance coordinator, I want every write
that mutates server state to elevate, so that operations succeed on
Windows hosts where the process does not run as Administrator.

#### Acceptance Criteria

1. WHEN ``elevated=True`` THE SYSTEM SHALL pass ``elevated=True``
   for every write verb (``add_export``, ``remove_export``,
   ``reload_exports``, ``set_export_options``).
2. WHEN performing reads (``list_exports``, ``get_export_status``)
   THE SYSTEM SHALL pass ``elevated=False`` regardless of the
   config.  Probes that drive idempotency decisions inside write
   paths (e.g., ``Get-NfsShare -Name <name>``) also do NOT
   elevate.
3. WHEN ``elevated=False`` THE SYSTEM SHALL pass ``elevated=False``
   on every path.

---

### Requirement 9: Dry-Run

**User Story:** As a maintenance coordinator, I want to preview
every mutation without dispatching, so that diff-vs-state reports
can be generated before changes go live.

#### Acceptance Criteria

1. WHEN ``dry_run=True`` THE SYSTEM SHALL NOT dispatch any mutating
   PowerShell cmdlet (``New-NfsShare``, ``Remove-NfsShare``,
   ``Set-NfsShare``, ``Restart-Service``).
2. WHEN ``dry_run=True`` THE SYSTEM SHALL still allow read-only
   probes that drive idempotency decisions
   (``Get-NfsShare -Name``).
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``
   for every mutating verb with ``command`` populated to the
   would-have-been argv.

---

### Requirement 10: Failure Reporting

**User Story:** As a maintenance coordinator, I want every failed
write to surface a structured result, so that downstream automation
can categorise failures without scraping logs.

#### Acceptance Criteria

1. WHEN any write verb's underlying runner call exits non-zero THE
   SYSTEM SHALL return ``status=FAILED`` with ``exit_code``,
   ``stdout``, ``stderr``, and ``command`` populated from the
   runner's ``RunResult``.
2. WHEN a read verb's underlying runner call exits non-zero with
   non-empty ``stderr`` THE SYSTEM SHALL raise
   ``WindowsNfsQueryError`` carrying ``command``, ``exit_code``,
   and ``stderr``.

---

### Requirement 11: Idempotency Across Verbs

**User Story:** As a maintenance coordinator, I want every
configuration-mutating verb to be safe to call repeatedly, so that
declarative reconciliation loops do not produce duplicate shares.

#### Acceptance Criteria

1. WHEN ``add_export(spec)`` is called and a share at
   ``spec.share_name`` already exists with the same ``Path`` THE
   SYSTEM SHALL return ``status=SKIPPED``.
2. WHEN ``remove_export(path)`` is called and no share advertises
   ``path`` THE SYSTEM SHALL return ``status=SKIPPED``.
3. WHEN ``set_export_options(path, options)`` is called and every
   option already matches the existing share THE SYSTEM SHALL
   return ``status=SKIPPED``.
4. WHEN ``reload_exports()`` is called THE SYSTEM SHALL NOT short-
   circuit.
