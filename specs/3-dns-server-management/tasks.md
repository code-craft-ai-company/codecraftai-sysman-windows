# Tasks

## Pilot Scope

The pilot exposes eight verbs (`add_zone`, `remove_zone`,
`list_zones`, `add_record`, `remove_record`, `list_records`,
`reload_zone`, `get_zone_status`) through a single
`WindowsDnsServerManager`.  Every verb is dispatched through
the injected `ICommandRunner`.

---

## Implementation Checklist

- [x] Author `requirements.md` (~310 lines) describing the
      eleven requirements that drive the implementation.
- [x] Author `design.md` describing the architecture, the
      cross-OS DNS-server-model concept-mapping table, the
      Windows DNS Server role requirement, the PowerShell
      argv discipline, the record-type dispatch table, the
      idempotency rules, and the documented contract drift.
- [x] Author `dnsservermanagement.py` with:
    - [x] `WindowsDnsActionStatus` (alias of `ActionStatus`).
    - [x] `WindowsDnsManagerConfig` dataclass with the
          advisory `requires_dns_server_role` field.
    - [x] `WindowsDnsActionResult` dataclass.
    - [x] `WindowsDnsZoneSpec` dataclass with
          `replication_scope` for AD-integrated zones.
    - [x] `WindowsDnsZoneInfo` dataclass.
    - [x] `WindowsDnsRecordSpec` dataclass.
    - [x] `WindowsDnsRecordInfo` dataclass.
    - [x] `WindowsDnsZoneStatus` dataclass.
    - [x] `WindowsDnsQueryError` exception.
    - [x] `WindowsDnsServerManager` class with the eight
          verbs.
    - [x] Module-level renderer `_build_add_zone_script`.
    - [x] Module-level renderer
          `_build_add_record_command` with the type-dispatch
          table.
    - [x] Module-level renderer
          `_build_remove_record_command`.
    - [x] Module-level helper `_ps_quote` for single-quote
          escaping.
    - [x] Module-level parser `_parse_zones_json`.
    - [x] Module-level parser `_parse_records_json`.
    - [x] Module-level parser `_parse_zone_status_json`.
    - [x] Module-level helper
          `_extract_record_data_text`.
    - [x] Module-level helpers `_extract_ttl_seconds` /
          `_extract_priority`.
- [x] Author the test module with at least 14 tests
      covering:
    - [x] `add_zone` argv shape.
    - [x] `add_zone` SKIPPED when zone already exists.
    - [x] `add_zone` DRY_RUN does not invoke the runner.
    - [x] `remove_zone` argv shape.
    - [x] `remove_zone` SKIPPED when zone is absent.
    - [x] `list_zones` parser handles JSON output.
    - [x] `list_zones` raises on PowerShell failure.
    - [x] `add_record` argv shape with TTL token.
    - [x] `add_record` SKIPPED when record present.
    - [x] `remove_record` argv shape.
    - [x] `list_records` parses A / AAAA / CNAME / TXT.
    - [x] `reload_zone` invokes ``Sync-DnsServerZone``.
    - [x] `get_zone_status` parses
          ``Get-DnsServerZone | ConvertTo-Json``.
    - [x] Writes elevate when configured.
    - [x] Reads never elevate.
    - [x] Failed command surfaces stderr.
    - [x] Record-type dispatch sweep (A / AAAA / CNAME /
          MX / TXT / SRV / PTR / NS).
    - [x] `replication_scope` overrides `-ZoneFile`.
    - [x] `allow_update=True` emits
          ``-DynamicUpdate Secure``.

---

## Out-of-Scope Follow-ups

These items are intentionally deferred:

- **DNSSEC zone signing.** `Add-DnsServerSigningKey`,
  `Invoke-DnsServerZoneSign`.
- **Response-rate limiting.**
  `Set-DnsServerResponseRateLimiting`.
- **DnsServerScavenging / aging.**
  `Set-DnsServerScavenging`.
- **Advanced conditional forwarders.** Beyond the basic
  `Add-DnsServerConditionalForwarderZone` shape.
- **DNS over HTTPS / TLS listener configuration.**
- **`get_server_status`.** Daemon-level status delegated
  to `3-service-management`.
- **`-ComputerName` remote dispatch.** RSAT-on-client
  workflows.
- **SRV `Weight` / `Port` fields.** Pilot defaults both to
  `0` / `0`; a follow-up will lift them onto
  `WindowsDnsRecordSpec`.
- **AD-integrated zone introspection.** The pilot accepts
  `replication_scope` on `WindowsDnsZoneSpec` and renders
  the corresponding `-ReplicationScope` flag, but does NOT
  surface AD-integration state on
  `WindowsDnsZoneInfo`.

---

## Acceptance Snapshot

`requirements.md` covers eleven requirements.  `design.md`
documents the PowerShell DnsServer model, the cross-OS
concept mapping, the role requirement, the argv discipline,
the record-type dispatch table, and the spec-vs-contract
drift.

---

## Test Bar

- Minimum 14 tests; current count at completion: 19.
- Every test uses `_MockCommandRunner` — no live PowerShell
  invocations.
- The mock supports both argv-tuple and script-prefix
  matching so a single runner can stand in for the entire
  flow of an idempotent verb (probe + write).
