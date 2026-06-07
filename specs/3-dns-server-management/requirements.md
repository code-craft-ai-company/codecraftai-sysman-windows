# Requirements

## Introduction

The dns-server-management feature provides the
`codecraftai-sysman-windows` package with the ability to declare,
audit, and reload Windows DNS Server zones using the
PowerShell `DnsServer` module.  It is the cross-OS counterpart
to the BIND-based RHEL / Ubuntu / macOS variants.

The Windows implementation diverges entirely from the
BIND-based variants.  Windows hosts the DNS service through
the "DNS Server" role; the management surface is a set of
PowerShell cmdlets shipped in the `DnsServer` module
(`Add-DnsServerPrimaryZone`, `Get-DnsServerZone`,
`Add-DnsServerResourceRecordA` / `-AAAA` / `-CName` / ...,
`Remove-DnsServerResourceRecord`, `Sync-DnsServerZone`).

Windows DNS Server role requirement:
The `DnsServer` PowerShell module ships with the Windows Server
"DNS Server" role.  It is NOT available on Windows 10 / 11
client SKUs by default — operators on client SKUs can install
the Remote Server Administration Tools (RSAT) and target a
remote DNS server, but the locally-dispatched cmdlets in this
module will fail with
`ParameterBindingValidationException` /
`CommandNotFoundException` when the module is missing.  The
`WindowsDnsManagerConfig` carries an advisory
`requires_dns_server_role` field documenting this requirement.

All cmdlets are dispatched through the
`codecraftai-interfaces` `ICommandRunner` abstraction by wrapping
each PowerShell scriptblock in
`["powershell.exe", "-NoProfile", "-NonInteractive",
"-Command", "<script>"]`.  All mutating operations honour
dry-run mode and config-driven elevation.

This feature complements — but does not duplicate — the
`3-network-configuration` DNS-client surface, which owns the
host's resolver settings via `Set-DnsClientServerAddress`.

The reference implementation for shape, idempotency, dry-run,
and result mapping lives in
`codecraftai-sysman-linux-redhat/specs/3-dns-server-management/`.

---

## Requirements

### Requirement 1: Add a Zone

**User Story:** As a maintenance coordinator, I want to declare
a new authoritative DNS zone on the Windows host so that
downstream resolvers can begin querying it.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_zone(name, spec)` AND
   `spec.zone_type` is `"primary"` THE SYSTEM SHALL dispatch
   `Add-DnsServerPrimaryZone -Name '<name>' -ZoneFile
   '<zone_file>'` through PowerShell.
2. WHEN `spec.zone_type` is `"secondary"` THE SYSTEM SHALL
   dispatch `Add-DnsServerSecondaryZone -Name '<name>'`.
3. WHEN `spec.zone_type` is `"stub"` THE SYSTEM SHALL dispatch
   `Add-DnsServerStubZone -Name '<name>'`.
4. WHEN `spec.zone_type` is `"forward"` THE SYSTEM SHALL
   dispatch `Add-DnsServerConditionalForwarderZone -Name
   '<name>'`.
5. WHEN `spec.replication_scope` is set (e.g., `"Domain"`)
   THE SYSTEM SHALL pass `-ReplicationScope <scope>` rather
   than `-ZoneFile`, producing an Active-Directory-integrated
   zone.
6. WHEN `spec.allow_update` is `True` AND the cmdlet is
   `Add-DnsServerPrimaryZone` THE SYSTEM SHALL pass
   `-DynamicUpdate Secure`.
7. WHEN `Get-DnsServerZone -Name '<name>'` reports the zone
   already exists THE SYSTEM SHALL return `SKIPPED` without
   dispatching the add cmdlet.

---

### Requirement 2: Remove a Zone

**User Story:** As a maintenance coordinator, I want to remove
a zone declaration so that the Windows DNS service stops
answering for it.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_zone(name)` AND the
   zone is registered THE SYSTEM SHALL dispatch
   `Remove-DnsServerZone -Name '<name>' -Force` through
   PowerShell.
2. WHEN the zone is not registered THE SYSTEM SHALL return
   `SKIPPED` without dispatching the remove cmdlet.

---

### Requirement 3: List Zones

**User Story:** As a maintenance coordinator, I want to
enumerate the zones the Windows DNS service knows about so
that I can audit drift.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_zones()` THE SYSTEM SHALL
   dispatch `Get-DnsServerZone | Select Name, ZoneType,
   ZoneFile, DynamicUpdate, IsDsIntegrated | ConvertTo-Json
   -Compress -Depth 5` through PowerShell.
2. THE SYSTEM SHALL parse the JSON output into one
   `WindowsDnsZoneInfo` per zone.
3. WHEN the JSON parser cannot consume the output THE SYSTEM
   SHALL return an empty list rather than raising.
4. WHEN PowerShell exits non-zero with a non-empty stderr
   THE SYSTEM SHALL raise `WindowsDnsQueryError`.
5. THE SYSTEM SHALL NOT pass `elevated=True` for the
   listing dispatch.

---

### Requirement 4: Add a Resource Record

**User Story:** As a maintenance coordinator, I want to add a
resource record to a zone so that clients can resolve the new
name.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_record(zone, record)` THE
   SYSTEM SHALL dispatch the type-specific
   `Add-DnsServerResourceRecord*` cmdlet for the spec's
   `record_type`:

       A     → Add-DnsServerResourceRecordA
       AAAA  → Add-DnsServerResourceRecordAAAA
       CNAME → Add-DnsServerResourceRecordCName
       MX    → Add-DnsServerResourceRecordMX
       TXT   → Add-DnsServerResourceRecord -Txt
       SRV   → Add-DnsServerResourceRecord -Srv
       PTR   → Add-DnsServerResourceRecordPtr
       NS    → Add-DnsServerResourceRecord -NS
2. THE SYSTEM SHALL pass the type-specific named parameter
   for the rdata (`-IPv4Address`, `-IPv6Address`,
   `-HostNameAlias`, `-MailExchange`, `-DescriptiveText`,
   `-DomainName`, `-PtrDomainName`, `-NameServer`).
3. WHEN the spec's `ttl` is set THE SYSTEM SHALL pass
   `-TimeToLive (New-TimeSpan -Seconds <ttl>)`.
4. WHEN the spec's `priority` is set AND the record type is
   `MX` THE SYSTEM SHALL pass `-Preference <priority>`.
5. WHEN the spec's `priority` is set AND the record type is
   `SRV` THE SYSTEM SHALL pass `-Priority <priority>`.
6. WHEN an identical record (same `name`, `record_type`,
   normalised `value`) is already present THE SYSTEM SHALL
   return `SKIPPED`.
7. WHEN the spec's `record_type` is not in the supported set
   THE SYSTEM SHALL return `FAILED` with a stderr payload
   that names the unsupported type.

---

### Requirement 5: Remove a Resource Record

**User Story:** As a maintenance coordinator, I want to remove
a resource record so that clients stop resolving the stale
name.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_record(zone, record)`
   AND a matching record is present THE SYSTEM SHALL
   dispatch
   `Remove-DnsServerResourceRecord -ZoneName '<zone>' -Name
   '<record.name>' -RRType '<record.record_type>' -Force`
   through PowerShell.
2. WHEN no matching record is present THE SYSTEM SHALL
   return `SKIPPED`.

---

### Requirement 6: List Records

**User Story:** As a maintenance coordinator, I want to
enumerate the records in a zone so that I can audit drift.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_records(zone)` THE
   SYSTEM SHALL dispatch
   `Get-DnsServerResourceRecord -ZoneName '<zone>' |
   ConvertTo-Json -Compress -Depth 5` through PowerShell.
2. THE SYSTEM SHALL parse the JSON output into one
   `WindowsDnsRecordInfo` per record, extracting the
   ``HostName``, ``RecordType``, the type-specific
   ``RecordData`` field, the ``TimeToLive`` seconds, and the
   priority for MX / SRV records.
3. WHEN PowerShell exits non-zero with a non-empty stderr
   THE SYSTEM SHALL raise `WindowsDnsQueryError`.

---

### Requirement 7: Reload a Zone

**User Story:** As a maintenance coordinator, I want to force
a zone resync so that the Windows DNS service picks up
out-of-band changes.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_zone(name)` THE SYSTEM
   SHALL dispatch `Sync-DnsServerZone -Name '<name>'`
   through PowerShell unconditionally.
2. THE SYSTEM SHALL honour `config.dry_run` and elevation,
   but SHALL NOT skip on any state-based idempotency check.

---

### Requirement 8: Get Zone Status

**User Story:** As a maintenance coordinator, I want to query
the runtime state of a zone so that I can confirm it is
loaded and report its serial.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_zone_status(name)` THE
   SYSTEM SHALL dispatch
   `Get-DnsServerZone -Name '<name>' | ConvertTo-Json
   -Compress -Depth 5` through PowerShell.
2. THE SYSTEM SHALL parse the JSON output into a
   `WindowsDnsZoneStatus` whose `is_loaded` is `True` when
   the cmdlet returned a record, and whose `serial` /
   `last_loaded` / `transferred_from` fields are populated
   from `SerialNumber`, `LastZoneUpdate`, and
   `MasterServers`.
3. WHEN the cmdlet exits non-zero with a non-empty stderr
   THE SYSTEM SHALL raise `WindowsDnsQueryError`.

---

### Requirement 9: PowerShell Argv Discipline

**User Story:** As a maintenance coordinator, I want every
PowerShell dispatch to be deterministic and non-interactive.

#### Acceptance Criteria

1. THE SYSTEM SHALL invoke PowerShell with
   `-NoProfile -NonInteractive -Command <script>` so that
   `$PROFILE` cannot mutate cmdlet behaviour and PowerShell
   never blocks waiting for interactive input.
2. THE SYSTEM SHALL escape every embedded value passed to a
   single-quoted PowerShell string by doubling the embedded
   single quote.
3. THE SYSTEM SHALL pass the configured
   `WindowsDnsManagerConfig.powershell_executable` as the
   first argv token so that operators on PowerShell 7 can
   set it to `"pwsh.exe"`.

---

### Requirement 10: Dry-Run, Elevation, and Error Reporting

**User Story:** As a maintenance coordinator, I want every
write verb to honour dry-run and to elevate predictably.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch
   any PowerShell cmdlet (including the
   `Get-DnsServerZone` idempotency probe — dry-run results
   carry the would-have-been argv but invoke nothing).
2. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner on every write dispatch.
3. WHEN PowerShell returns non-zero on a write THE SYSTEM
   SHALL return `WindowsDnsActionResult(status=FAILED,
   exit_code=<code>, stderr=<captured>)` without raising.
4. THE SYSTEM SHALL never elevate read verbs (`list_zones`,
   `list_records`, `get_zone_status`).

---

### Requirement 11: Documented Limitations

**User Story:** As a maintenance coordinator, I want the
documentation to enumerate every native Windows DNS feature
this pilot does not cover so that I do not file false bugs.

#### Acceptance Criteria

1. THE `design.md` SHALL list DNSSEC zone signing, DNSKEY /
   ZSK rollover, response-rate limiting, the
   `DnsServerScavenging` / aging surface, conditional
   forwarders beyond the basic `Add-DnsServerConditionalForwarderZone`
   shape, and DNS over HTTPS / TLS listener configuration as
   out of scope.
2. THE `design.md` SHALL note the Windows DNS Server role
   requirement and the Windows-10/11 client-SKU caveat.
3. THE `design.md` SHALL describe the spec-vs-contract drift
   discovered during implementation in a dedicated section.
