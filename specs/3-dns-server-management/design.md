# Design

## Overview

The Windows dns-server-management feature is implemented as a
single module,
`ccai.sysman.windows.dnsservermanagement`, exposing a
`WindowsDnsServerManager` class that wraps the PowerShell
`DnsServer` module through the `ICommandRunner` abstraction.

The module follows the standard manager pattern shared with
every other Layer 3 feature in this package: it consumes the
`BaseFeatureManager` mixin from
`ccai.sysman.windows.featurebase`, accepts an injected
runner plus a `WindowsDnsManagerConfig`, and dispatches every
PowerShell scriptblock through the runner.

The cross-OS contract for this feature lives in
`codecraftai-sysman-common/specs/contracts/dns-server-management/contract.md`.
The RHEL canonical implementation lives in
`codecraftai-sysman-linux-redhat/specs/3-dns-server-management/`.

**See Also:**
- `3-network-configuration` — owns DNS *client* (resolver)
  configuration on Windows via
  `Set-DnsClientServerAddress` /
  `Set-DnsClientGlobalSetting`.
- `3-service-management` — `DNS` Windows service lifecycle
  (the Windows DNS Server role exposes its daemon as a
  Windows service named `DNS`).
- `3-firewall-management` — opening UDP/TCP 53 through the
  Windows firewall; out of scope here.
- `3-certificate-issuance` — TLS material for DoH listeners;
  out of scope for this pilot.

---

## DNS Server Role Requirement

The `DnsServer` PowerShell module ships with the Windows
Server "DNS Server" role.  It is NOT installed by default on
Windows 10 / 11 client SKUs.

| Host SKU                            | Module availability  | Notes                                         |
|-------------------------------------|----------------------|-----------------------------------------------|
| Windows Server with DNS Server role | Available locally    | Canonical target; this manager works.         |
| Windows Server without DNS role     | Not available        | Manager dispatches surface FAILED with stderr.|
| Windows 10 / 11 (RSAT installed)    | Available remotely   | Cmdlets accept `-ComputerName`; pilot does NOT pass that flag — RSAT users target a remote DNS server through SSH-based runner. |
| Windows 10 / 11 (no RSAT)           | Not available        | Manager dispatches surface FAILED with stderr.|

The manager itself does NOT probe for the role.  Operators
declare their intent via
`WindowsDnsManagerConfig.requires_dns_server_role = True`
(default) and the manager surfaces missing-module failures as
`FAILED` results with the cmdlet's stderr captured verbatim.

---

## Architecture

```
+-------------------------------------------------------------+
|            Maintenance Coordinator                          |
+-----------------------+-------------------------------------+
                        |
                        v
+-------------------------------------------------------------+
|       WindowsDnsServerManager                               |
|                                                             |
|  Write verbs (idempotent + dry-run aware):                  |
|    add_zone(name, spec)                                     |
|    remove_zone(name)                                        |
|    add_record(zone, record)                                 |
|    remove_record(zone, record)                              |
|    reload_zone(name)                                        |
|                                                             |
|  Read verbs:                                                |
|    list_zones()                                             |
|    list_records(zone)                                       |
|    get_zone_status(name)                                    |
+--------------------------+----------------------------------+
                           |
                           v
                +----------+-----------+
                |     ICommandRunner   |
                +----------+-----------+
                           |
                ___________|___________
               |                       |
               v                       v
   +-------------------+    +---------------------+
   | LocalCommandRunner|    | SSHCommandRunner    |
   +---------+---------+    +-----------+---------+
             |                          |
             |     aspects compose here |
             +------------+-------------+
                          |
                          v
        +-----------------------------------+
        | PersistenceAspect, RetryAspect,   |
        | TimeoutAspect (transparent)       |
        +-----------------------------------+
```

---

## Cross-OS DNS-Server-Model Concept Mapping

The cross-OS contract uses an abstract
`(zone, record, serial)` triple plus a backend-agnostic
`zone_type` enum.  Each OS maps these abstractions onto its
native tooling:

| Abstract concept   | Windows (DnsServer module)                   | Ubuntu (BIND 9)                              | macOS (BIND ships with macOS)                | RHEL (BIND, canonical)            |
|--------------------|----------------------------------------------|----------------------------------------------|----------------------------------------------|-----------------------------------|
| Zone declaration   | `Add-DnsServerPrimaryZone -Name '<zone>'`    | `zone "<name>" {}` block in                  | `zone "<name>" {}` block in                  | `zone "<name>" {}` block in       |
|                    | `-ZoneFile '<zone>.dns'` /                   | `/etc/bind/named.conf.local`                 | `/etc/named.conf`                            | `/etc/named.conf` /               |
|                    | `-ReplicationScope <scope>`                  | (sentinel-bracketed)                         | (sentinel-bracketed)                         | `/etc/named/zones.d/`             |
| AD-integrated zone | `-ReplicationScope Domain`                   | n/a                                          | n/a                                          | n/a                               |
| Add A record       | `Add-DnsServerResourceRecordA`               | edit zone file + bump SOA + `rndc reload`    | edit zone file + bump SOA + `rndc reload`    | edit zone file + bump SOA +       |
|                    |                                              |                                              |                                              | `rndc reload`                     |
| Add AAAA record    | `Add-DnsServerResourceRecordAAAA`            | same                                         | same                                         | same                              |
| Add CNAME record   | `Add-DnsServerResourceRecordCName`           | same                                         | same                                         | same                              |
| Add MX record      | `Add-DnsServerResourceRecordMX`              | same                                         | same                                         | same                              |
| Add TXT record     | `Add-DnsServerResourceRecord -Txt`           | same                                         | same                                         | same                              |
| Add SRV record     | `Add-DnsServerResourceRecord -Srv`           | same                                         | same                                         | same                              |
| Remove record      | `Remove-DnsServerResourceRecord`             | strip line + bump SOA + `rndc reload`        | strip line + bump SOA + `rndc reload`        | strip line + bump SOA +           |
|                    |                                              |                                              |                                              | `rndc reload`                     |
| List zones         | `Get-DnsServerZone | ConvertTo-Json`         | parse `named.conf.local` +                   | parse `named.conf` +                         | parse `named.conf` +              |
|                    |                                              | `named-checkconf -p`                         | `named-checkconf -p`                         | `named-checkconf -p`              |
| List records       | `Get-DnsServerResourceRecord -ZoneName ...`  | parse zone-data file                         | parse zone-data file                         | parse zone-data file              |
| Reload zone        | `Sync-DnsServerZone -Name <zone>`            | `rndc reload <zone>`                         | `rndc reload <zone>`                         | `rndc reload <zone>`              |
| Zone status        | `Get-DnsServerZone -Name <zone> |            | `rndc zonestatus <zone>`                     | `rndc zonestatus <zone>`                     | `rndc zonestatus <zone>`          |
|                    | ConvertTo-Json`                              |                                              |                                              |                                   |
| Service identity   | `DNS` Windows service                        | `bind9.service` systemd unit                 | `org.isc.named` launchd label                | `named.service` systemd unit      |

The Windows implementation diverges entirely from the
BIND-based variants — it dispatches PowerShell cmdlets and
parses their JSON output.  Operators authoring cross-OS
playbooks must use the OS-prefixed types throughout; the
abstract concepts are the same but the rendering is
platform-specific.

---

## Configuration

```python
@dataclass
class WindowsDnsManagerConfig(FeatureConfig):
    powershell_executable:    str  = "powershell.exe"
    requires_dns_server_role: bool = True
```

`dry_run` and `elevated` are inherited from `FeatureConfig`.

The `requires_dns_server_role` field is advisory — the
manager does NOT probe for the role.  Operators on RSAT-only
hosts should still set it to `True` because the
`DnsServer` module is what the cmdlets need to load.  The
field is recorded on the config so coordinators can correlate
intent with the manager's dispatch surface.

---

## Data Models

```python
class WindowsDnsActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class WindowsDnsActionResult(FeatureActionResult):
    zone:        str | None = None
    record_name: str | None = None
    message:     str | None = None


@dataclass
class WindowsDnsZoneSpec:
    name:              str
    zone_type:         str        = "primary"
    zone_file:         str | None = None
    allow_update:      bool       = False
    replication_scope: str | None = None  # AD-integrated zones


@dataclass
class WindowsDnsZoneInfo:
    name:         str
    zone_type:    str
    zone_file:    str | None
    record_count: int | None
    is_active:    bool


@dataclass
class WindowsDnsRecordSpec:
    name:        str
    record_type: str          # "A" | "AAAA" | "CNAME" | "MX" | ...
    value:       str
    ttl:         int | None   = None
    priority:    int | None   = None  # MX / SRV


@dataclass
class WindowsDnsRecordInfo:
    name:        str
    record_type: str
    value:       str
    ttl:         int | None   = None
    priority:    int | None   = None
    serial:      int | None   = None  # always None on Windows


@dataclass
class WindowsDnsZoneStatus:
    name:              str
    is_loaded:         bool
    serial:            int | None = None
    last_loaded:       str | None = None
    transferred_from:  str | None = None


class WindowsDnsQueryError(Exception):
    """Read-side failure that is not 'absent'."""
```

---

## PowerShell Argv Shape

Every PowerShell scriptblock is dispatched as:

```
[powershell_executable, "-NoProfile", "-NonInteractive",
 "-Command", "<script>"]
```

`-NoProfile` ensures `$PROFILE` cannot mutate cmdlet behaviour
(particularly important for SSH-based runners that may target
hosts with custom profiles).  `-NonInteractive` prevents
PowerShell from blocking on interactive prompts.

The default `powershell_executable` is `"powershell.exe"`.
Operators targeting PowerShell 7 set it to `"pwsh.exe"`.

---

## PowerShell Single-Quote Escaping

Every value embedded in a single-quoted PowerShell string is
escaped by doubling embedded single quotes
(`'foo''bar'` is the PowerShell escape for `foo'bar`).  Other
characters do NOT require escaping inside single-quoted
strings — backticks, dollar signs, and double quotes are all
safe.

This escape rule is implemented by `_ps_quote`.

---

## Record-Type Dispatch Table

`add_record` selects the cmdlet from the spec's
`record_type`:

| record_type | Cmdlet                                  | Type-specific named parameter |
|-------------|-----------------------------------------|-------------------------------|
| `A`         | `Add-DnsServerResourceRecordA`          | `-IPv4Address`                |
| `AAAA`      | `Add-DnsServerResourceRecordAAAA`       | `-IPv6Address`                |
| `CNAME`     | `Add-DnsServerResourceRecordCName`      | `-HostNameAlias`              |
| `MX`        | `Add-DnsServerResourceRecordMX`         | `-MailExchange` + `-Preference` |
| `TXT`       | `Add-DnsServerResourceRecord -Txt`      | `-DescriptiveText`            |
| `SRV`       | `Add-DnsServerResourceRecord -Srv`      | `-DomainName` + `-Priority`   |
| `PTR`       | `Add-DnsServerResourceRecordPtr`        | `-PtrDomainName`              |
| `NS`        | `Add-DnsServerResourceRecord -NS`       | `-NameServer`                 |

When `record.ttl` is set, the manager appends
`-TimeToLive (New-TimeSpan -Seconds <ttl>)` so that the cmdlet
sees the canonical PowerShell `TimeSpan` form.  Records
without a ttl fall back to the zone's default TTL.

When `record.priority` is set on an MX record the manager
emits `-Preference <int>` (the cmdlet's spelling); on SRV it
emits `-Priority <int>`.  SRV records additionally carry
`Weight` and `Port` fields the pilot sets to `0`/`0` — a
follow-up will lift those into the spec.

`remove_record` always dispatches the generic
`Remove-DnsServerResourceRecord -RRType '<record_type>'
-Force` form because Windows does not ship type-specific
remove cmdlets.

---

## Idempotency

`add_zone` probes `Get-DnsServerZone -Name '<name>' |
ConvertTo-Json -Compress -Depth 5` and skips when the cmdlet
exits zero with non-empty stdout.

`remove_zone` uses the same probe and skips when the cmdlet
exits non-zero.

`add_record` lists the existing records in the zone via
`Get-DnsServerResourceRecord -ZoneName '<zone>' |
ConvertTo-Json` and skips when an entry whose
`(HostName, RecordType, normalised_value)` tuple matches the
spec is already present.  Value normalisation collapses
trailing dots so that Windows's canonical FQDN form
(`mail.example.com.`) compares equal to the bare-name form
(`mail.example.com`).

`remove_record` uses the same listing probe and skips when no
matching record is present.

`reload_zone` (`Sync-DnsServerZone`) and `get_zone_status`
never skip.

---

## Validate-Before-Apply

The contract requires every write verb to validate the
rendered configuration before dispatching the apply command.
Windows does NOT ship a separate validator like
`named-checkconf`; the cmdlets validate parameters
synchronously and return non-zero on rejection.  The
manager's `_wrap_run_result` helper maps non-zero exits to
`FAILED` and surfaces the cmdlet's stderr verbatim.

The behavioural effect is the same as the BIND-based
implementations: a malformed spec never reaches the running
DNS service, and the operator sees the cmdlet's error
verbatim in the result's `stderr` field.

---

## Key Method Designs

### `add_zone(name, spec)`

1. Render the `Add-DnsServer*Zone` scriptblock from the spec
   (`zone_type`, `zone_file`, `replication_scope`,
   `allow_update`).
2. When `config.dry_run` → `DRY_RUN`.
3. Probe `Get-DnsServerZone -Name '<name>'`.  When the zone
   exists → `SKIPPED`.
4. Dispatch the rendered scriptblock through PowerShell.
5. Map the runner result via `_wrap_run_result`.

### `remove_zone(name)`

1. Render `Remove-DnsServerZone -Name '<name>' -Force`.
2. When `config.dry_run` → `DRY_RUN`.
3. Probe; when absent → `SKIPPED`.
4. Dispatch.

### `add_record(zone, record)`

1. Validate the spec.
2. Render the type-specific cmdlet via
   `_build_add_record_command`.
3. When `config.dry_run` → `DRY_RUN`.
4. Probe via `Get-DnsServerResourceRecord`; when an
   identical record is present → `SKIPPED`.
5. Dispatch.

### `remove_record(zone, record)`

1. Validate the spec.
2. Render `Remove-DnsServerResourceRecord ...`.
3. When `config.dry_run` → `DRY_RUN`.
4. Probe; when absent → `SKIPPED`.
5. Dispatch.

### `reload_zone(name)`

1. When `config.dry_run` → `DRY_RUN`.
2. Dispatch `Sync-DnsServerZone -Name '<name>'`
   unconditionally; never skips.

### `list_zones()`

1. Dispatch
   `Get-DnsServerZone | Select Name,ZoneType,ZoneFile,
   DynamicUpdate,IsDsIntegrated | ConvertTo-Json -Compress
   -Depth 5`.
2. Parse the JSON output into one `WindowsDnsZoneInfo` per
   row.  Both the dict-root (single object) and array-root
   shapes are handled.
3. Non-zero exit with a non-empty stderr raises
   `WindowsDnsQueryError`.

### `list_records(zone)`

1. Dispatch
   `Get-DnsServerResourceRecord -ZoneName '<zone>' |
   ConvertTo-Json -Compress -Depth 5`.
2. Parse each row, extracting the type-specific
   `RecordData` field via `_extract_record_data_text`.
3. TTL and priority are extracted via
   `_extract_ttl_seconds` / `_extract_priority`.

### `get_zone_status(name)`

1. Dispatch
   `Get-DnsServerZone -Name '<name>' | ConvertTo-Json
   -Compress -Depth 5`.
2. When the cmdlet exits zero with a non-empty body →
   `is_loaded=True` and the parsed `SerialNumber`,
   `LastZoneUpdate`, `MasterServers` fields populate the
   status.
3. When the cmdlet exits non-zero with a non-empty stderr →
   raise.

---

## Dry-Run Behaviour

Dry-run disables every PowerShell dispatch (including the
read-side idempotency probes that precede a write).
Read-only verbs (`list_zones`, `list_records`,
`get_zone_status`) are NOT gated on dry-run — they always
dispatch.

---

## Idempotency Summary

| Verb              | Idempotency rule                                      |
|-------------------|-------------------------------------------------------|
| `add_zone`        | SKIP when `Get-DnsServerZone` finds the zone.         |
| `remove_zone`     | SKIP when `Get-DnsServerZone` reports the zone absent.|
| `add_record`      | SKIP when an identical (name, type, value) is present.|
| `remove_record`   | SKIP when no matching record is present.              |
| `reload_zone`     | No idempotency guard.                                 |
| `list_zones`      | n/a (read).                                           |
| `list_records`    | n/a (read).                                           |
| `get_zone_status` | n/a (read).                                           |

---

## Elevation

| Situation                     | Behaviour                          |
|-------------------------------|------------------------------------|
| `elevated=True`, write verb   | Pass `elevated=True` to runner.    |
| `elevated=True`, read verb    | Do NOT elevate.                    |
| `elevated=False`              | Never elevate.                     |
| Idempotency probes            | Read-class; never elevate.         |

The default `elevated=True` is appropriate for production
because the `DnsServer` cmdlets require the caller to be a
member of `DnsAdmins` (or local Administrators) to mutate
zones and records.  Read-class cmdlets do not require the
elevation token on a healthy install.

---

## Service Reload

Windows DNS Server treats every zone mutation as
immediate-effect — the cmdlets update both the in-memory
zone state and the on-disk zone file atomically.  No explicit
service reload is required after `Add-DnsServerResourceRecord*`
or `Remove-DnsServerResourceRecord`.

`reload_zone` exposes `Sync-DnsServerZone` for the case
where the operator has hand-edited the on-disk zone file
(rare on Windows but possible) and wants the running service
to re-read it.

Daemon restarts (the `DNS` Windows service) are NOT
initiated by this manager; they are delegated to
`3-service-management`.

---

## Correctness Properties

1. **Cmdlet discipline.** Every dispatch goes through the
   canonical PowerShell argv shape; no shell metacharacters
   are interpreted.
2. **Single-quote escape.** Every embedded value is
   single-quote-escaped via `_ps_quote`.
3. **Record-type dispatch.** The
   `_build_add_record_command` table is the single source of
   truth for which cmdlet handles which record type.
4. **Reads never elevate.**
5. **Dry-run honours.** Every mutating dispatch is gated on
   `config.dry_run is False`.

---

## Out of Scope (Documented Limitations)

The following native Windows DNS features are NOT implemented
in this pilot.  They are recorded as follow-ups in
`tasks.md`:

- **DNSSEC zone signing.**  `Add-DnsServerSigningKey`,
  `Invoke-DnsServerZoneSign`, `DNSKEY` / `RRSIG` rendering.
- **Response-rate limiting.**  `Set-DnsServerResponseRateLimiting`.
- **DnsServerScavenging / aging.**  `Set-DnsServerScavenging`.
- **Advanced conditional forwarders.**  Beyond the basic
  `Add-DnsServerConditionalForwarderZone` shape — master
  list management, recursion controls, etc.
- **DNS over HTTPS / TLS listener configuration.**
- **`get_server_status`.**  The contract specifies it; the
  pilot does not implement it.  Daemon-level status is
  delegated to `3-service-management`.
- **`-ComputerName` remote dispatch.**  RSAT users on
  client SKUs target a remote DNS server.  The pilot does
  NOT pass `-ComputerName` — operators use SSH-based
  runners or remote PowerShell sessions.
- **SRV `Weight` / `Port` fields.**  The pilot sets both to
  `0` / `0` for SRV records; a follow-up will surface them
  on `WindowsDnsRecordSpec`.

---

## Testing Strategy

- `_MockCommandRunner` records every call and returns
  pre-seeded `RunResult` values.  In addition to argv-tuple
  matching the mock supports a `script_results` mapping
  keyed by PowerShell script-prefix so tests can seed the
  scriptblock-aware behaviour.
- One command-construction test per write verb covers the
  primary scriptblock shape.
- One idempotency test per write verb proves `SKIPPED` when
  the on-disk state already matches the requested state.
- One dry-run test per write verb proves the runner is not
  invoked at all.
- One `list_zones` JSON parser test covers a multi-zone
  output.
- One `list_records` JSON parser test covers A, AAAA,
  CNAME, TXT.
- One `get_zone_status` parser test covers the loaded path.
- One record-type dispatch test sweeps every supported
  record type to confirm the right cmdlet is selected.
- One elevation test proves writes elevate and reads do
  not.
- One failure-surfacing test proves PowerShell stderr is
  echoed onto the result.

---

## Spec-vs-Contract Drift

| Drift point                                                | Resolution                                                                                          |
|------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| Contract uses a `DnsServerBackend` enum.                   | Windows pilot is single-backend (`WINDOWS_DNS`); the enum value is recorded only for cross-OS parity. |
| Contract names `DnsZoneKind`.                              | The pilot uses a `zone_type: str` field that the renderer maps to the cmdlet variant.               |
| Contract specifies `DnsRecord` (no priority field).        | The pilot adds `priority: int | None` so MX / SRV records do not have to embed priority into `value`. |
| Contract specifies `DnsServerStatus`.                      | The pilot does NOT implement `get_server_status`; daemon-level status is delegated to `3-service-management`. |
| Contract specifies `DnsBackendMismatchError`.              | Windows pilot is single-backend; the error is reserved for cross-OS parity.                          |
| Contract specifies `serial` on `DnsRecordInfo`.            | Windows does not expose a per-record serial; the field is always `None`.                             |
| Contract is silent about AD-integrated zones.              | The pilot adds `replication_scope` to `WindowsDnsZoneSpec` so the renderer can emit `-ReplicationScope` rather than `-ZoneFile`. |

---

## Seams With Other Features

| Seam                                   | Owner                                | This Feature        |
|----------------------------------------|--------------------------------------|---------------------|
| Resolver / `Set-DnsClientServerAddress`| `3-network-configuration`            | Out of scope here.  |
| `DNS` Windows service lifecycle        | `3-service-management`               | `Sync-DnsServerZone` only; restarts delegated. |
| UDP / TCP 53 firewall opening          | `3-firewall-management`              | Out of scope here.  |
| TLS material for DoH                   | `3-certificate-issuance` / trust-mgmt| Out of scope here.  |
| DNS Server role install                | `3-package-management`               | Operators install the role manually; the manager does not.  |
