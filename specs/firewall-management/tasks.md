# Implementation Plan

- [ ] 1. Create the `firewallmanagement` module file
  - Create `source/packages/automojo/sysman/linux/windows/firewallmanagement.py`
  - Add module-level docstring describing purpose, the permanent-first write strategy,
    and the coordinator-driven design
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12_

- [ ] 2. Define status enum and action result dataclass
  - Implement `FirewallActionStatus` enum: `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`
  - Implement `FirewallActionResult` dataclass with fields: `status`, `zone`, `detail`,
    `exit_code`, `stdout`, `stderr`, `command`
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8_

- [ ] 3. Define query result dataclasses
  - Implement `ZoneInfo` dataclass with fields: `zone`, `services`, `ports`, `rich_rules`,
    `interfaces`, `sources`, `is_default`
  - Implement `FirewallState` dataclass with fields: `running`, `default_zone`
  - _Requirements: 9, 10, 11_

- [ ] 4. Define typed exceptions
  - Implement `FirewallQueryError` with `exit_code` and `stderr` attributes
  - _Requirements: 9, 10, 11_

- [ ] 5. Implement `FirewallManagerConfig` dataclass
  - Add `dry_run: bool = False` field
  - Add `elevated: bool = False` field
  - _Requirements: 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.4, 12_

- [ ] 6. Implement `FirewallManager.__init__` and private `_parse_zone_block()` helper
  - Accept `ICommandRunner` and `FirewallManagerConfig` as constructor arguments
  - Implement `_parse_zone_block(block: str) -> ZoneInfo` to parse the output of a single
    `--list-all` block into a `ZoneInfo` record
  - Parse `services:`, `ports:`, `rich rules:`, `interfaces:`, `sources:` fields
  - Normalise rich rule whitespace during parsing
  - _Requirements: 9, 10_

- [ ] 7. Implement `query_zone()`
  - Build `firewall-cmd --zone=<zone> --list-all` (no elevation)
  - On exit 0: parse stdout via `_parse_zone_block()`; return `ZoneInfo`
  - On non-zero exit: raise `FirewallQueryError` with exit code and stderr
  - _Requirements: 9_

- [ ] 8. Implement `query_all_zones()`
  - Build `firewall-cmd --list-all-zones` (no elevation)
  - Split stdout on blank lines to isolate per-zone blocks
  - Parse each block via `_parse_zone_block()`; return list of `ZoneInfo`
  - Raise `FirewallQueryError` on non-zero exit
  - _Requirements: 10_

- [ ] 9. Implement `query_state()`
  - Run `firewall-cmd --state` (no elevation)
  - If exit 0: `running=True`; else: return `FirewallState(running=False, default_zone="")`
  - Run `firewall-cmd --get-default-zone` (no elevation); raise `FirewallQueryError` on
    unexpected failure
  - Return `FirewallState(running=True, default_zone=<stdout.strip()>)`
  - _Requirements: 11_

- [ ] 10. Implement private `_run_permanent_write()` helper
  - Accept command args, zone, and detail string
  - Return `DRY_RUN` result without calling runner when `dry_run=True`
  - Call runner with `elevated` flag
  - Map `RunResult` to `FirewallActionResult` (`SUCCESS` on exit 0, `FAILED` otherwise)
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 12_

- [ ] 11. Implement `add_service()` and `remove_service()`
  - Call `query_zone()` to get current `ZoneInfo`
  - `add_service()`: return `SKIPPED` if service in `ZoneInfo.services`; else call
    `_run_permanent_write()` with `--add-service=<service>` arg
  - `remove_service()`: return `SKIPPED` if service not in `ZoneInfo.services`; else call
    `_run_permanent_write()` with `--remove-service=<service>` arg
  - _Requirements: 1, 2_

- [ ] 12. Implement `add_port()` and `remove_port()`
  - Call `query_zone()` to get current `ZoneInfo`
  - Compose port string as `<port>/<protocol>`
  - `add_port()`: return `SKIPPED` if port string in `ZoneInfo.ports`; else call
    `_run_permanent_write()` with `--add-port=<port>/<proto>` arg
  - `remove_port()`: return `SKIPPED` if port string not in `ZoneInfo.ports`; else call
    `_run_permanent_write()` with `--remove-port=<port>/<proto>` arg
  - _Requirements: 3, 4_

- [ ] 13. Implement private `_normalise_rule()` helper
  - Accept a rich rule string
  - Collapse multiple spaces to single space; strip leading/trailing whitespace
  - Return normalised string
  - _Requirements: 5, 6_

- [ ] 14. Implement `add_rich_rule()` and `remove_rich_rule()`
  - Call `query_zone()` to get current `ZoneInfo`
  - Normalise both the supplied rule and each rule in `ZoneInfo.rich_rules` before
    comparing
  - `add_rich_rule()`: return `SKIPPED` if normalised rule already present; else call
    `_run_permanent_write()` with `--add-rich-rule=<rule>` arg
  - `remove_rich_rule()`: return `SKIPPED` if normalised rule not present; else call
    `_run_permanent_write()` with `--remove-rich-rule=<rule>` arg
  - _Requirements: 5, 6_

- [ ] 15. Implement `set_default_zone()`
  - Call `query_state()` to get current default zone
  - Return `SKIPPED` if `FirewallState.default_zone` already matches desired zone
  - Return `DRY_RUN` when `dry_run=True`
  - Build `firewall-cmd --set-default-zone=<zone>`; pass `elevated` flag
  - _Requirements: 7_

- [ ] 16. Implement `reload()`
  - Return `DRY_RUN` when `dry_run=True`
  - Build `firewall-cmd --reload`; pass `elevated` flag
  - Map result to `FirewallActionResult` with `zone=None`
  - _Requirements: 8_

- [ ] 17. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_firewallmanagement.py`
  - Implement `MockCommandRunner` keyed on command args prefix
  - Helper `make_zone_block(zone, services, ports, rich_rules, interfaces, sources)`
    returning a representative `--list-all` output string
  - Helper `make_all_zones_output(zone_blocks)` joining blocks with blank lines
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12_

- [ ] 18. Write tests for `_parse_zone_block()` and `query_zone()`
  - Test parse: zone with multiple services, ports, rich rules — all fields populated
  - Test parse: zone with empty fields — lists are empty
  - Test parse: rich rule spanning indented line correctly captured
  - Test query_zone: exit 0 — returns `ZoneInfo`
  - Test query_zone: non-zero exit — raises `FirewallQueryError`
  - _Requirements: 9_

- [ ] 19. Write tests for `query_all_zones()` and `query_state()`
  - Test query_all_zones: two-zone output parsed into list of two `ZoneInfo` records
  - Test query_all_zones: command fails — raises `FirewallQueryError`
  - Test query_state: firewalld running — `running=True`, `default_zone` populated
  - Test query_state: firewalld not running — `running=False`, `default_zone` empty,
    no exception raised
  - Test query_state: get-default-zone fails unexpectedly — raises `FirewallQueryError`
  - _Requirements: 10, 11_

- [ ] 20. Write tests for `add_service()` and `remove_service()`
  - Test add: service absent from zone — `--add-service` command issued, returns `SUCCESS`
  - Test add: service already in zone — returns `SKIPPED`, no command issued
  - Test add: command fails — returns `FAILED`
  - Test add: dry-run — returns `DRY_RUN`
  - Test add: elevated mode — runner called with `elevated=True`
  - Test remove: service in zone — `--remove-service` command issued, returns `SUCCESS`
  - Test remove: service not in zone — returns `SKIPPED`
  - Test remove: dry-run — returns `DRY_RUN`
  - _Requirements: 1, 2_

- [ ] 21. Write tests for `add_port()` and `remove_port()`
  - Test add: port/proto absent — `--add-port` issued, returns `SUCCESS`
  - Test add: port/proto already open — returns `SKIPPED`
  - Test add: command fails — returns `FAILED`
  - Test add: dry-run — returns `DRY_RUN`
  - Test remove: port/proto open — `--remove-port` issued, returns `SUCCESS`
  - Test remove: port/proto not open — returns `SKIPPED`
  - Test remove: dry-run — returns `DRY_RUN`
  - _Requirements: 3, 4_

- [ ] 22. Write tests for `add_rich_rule()` and `remove_rich_rule()`
  - Test add: rule absent — `--add-rich-rule` issued, returns `SUCCESS`
  - Test add: identical rule present (exact match) — returns `SKIPPED`
  - Test add: identical rule present (whitespace variant) — normalised match, returns
    `SKIPPED`
  - Test add: command fails — returns `FAILED`
  - Test add: dry-run — returns `DRY_RUN`
  - Test remove: rule present — `--remove-rich-rule` issued, returns `SUCCESS`
  - Test remove: rule absent — returns `SKIPPED`
  - Test remove: dry-run — returns `DRY_RUN`
  - _Requirements: 5, 6_

- [ ] 23. Write tests for `set_default_zone()` and `reload()`
  - Test set_default_zone: zone differs — command issued, returns `SUCCESS`
  - Test set_default_zone: zone already matches — returns `SKIPPED`
  - Test set_default_zone: command fails — returns `FAILED`
  - Test set_default_zone: dry-run — returns `DRY_RUN`
  - Test reload: command issued unconditionally, returns `SUCCESS`
  - Test reload: command fails — returns `FAILED`
  - Test reload: dry-run — returns `DRY_RUN`, no command issued
  - _Requirements: 7, 8_
