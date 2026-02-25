# Implementation Plan

- [ ] 1. Create the `networkconfiguration` module file
  - Create `source/packages/automojo/sysman/linux/windows/networkconfiguration.py`
  - Add module-level docstring noting the `NetworkManager` class wraps `nmcli` and is
    distinct from the system `NetworkManager` daemon
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14_

- [ ] 2. Define status enum and action result dataclass
  - Implement `NetworkActionStatus` enum: `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`
  - Implement `NetworkActionResult` dataclass with fields: `status`, `detail`, `diff`,
    `exit_code`, `stdout`, `stderr`, `command`
  - _Requirements: 1, 3, 4, 6, 7, 8, 9, 11, 12_

- [ ] 3. Define query result dataclasses
  - Implement `HostnameInfo` with fields: `static_hostname`, `transient_hostname`,
    `pretty_hostname`
  - Implement `HostsEntry` with fields: `ip`, `hostnames`
  - Implement `NtpSyncInfo` with fields: `reference_source`,
    `system_time_offset_seconds`, `is_synchronised`
  - Implement `NmConnectionInfo` with fields: `name`, `conn_type`, `device`, `state`
  - _Requirements: 2, 5, 10, 13_

- [ ] 4. Define `NmConnectionSpec` input dataclass
  - Add fields: `name`, `conn_type`, `device`, `ip4`, `gw4`, `dns4`, `autoconnect`
  - _Requirements: 11_

- [ ] 5. Define typed exceptions
  - Implement `NetworkQueryError` with `exit_code` and `stderr` attributes
  - _Requirements: 2, 5, 10, 13_

- [ ] 6. Implement `NetworkManagerConfig` dataclass
  - Add `dry_run: bool = False` field
  - Add `elevated: bool = False` field
  - Add `hosts_path: str = "/etc/hosts"` field
  - Add `resolv_conf_path: str = "/etc/resolv.conf"` field
  - Add `chrony_conf_path: str = "/etc/chrony.conf"` field
  - _Requirements: 1.5, 3.6, 4.5, 6.5, 7.5, 8.5, 9.5, 11.5, 12.5, 14_

- [ ] 7. Implement `NetworkManager.__init__` and `query_hostname()`
  - Accept `ICommandRunner` and `NetworkManagerConfig` as constructor arguments
  - Implement `query_hostname()` using `hostnamectl --json short` (no elevation)
  - Parse JSON output into `HostnameInfo`; default missing keys to empty string
  - Raise `NetworkQueryError` on non-zero exit
  - _Requirements: 2_

- [ ] 8. Implement `set_hostname()`
  - Call `query_hostname()` to get current static hostname
  - Return `SKIPPED` if static hostname already matches desired
  - Return `DRY_RUN` with current and desired values when `dry_run=True`
  - Build `hostnamectl set-hostname <hostname>`; pass `elevated` flag
  - _Requirements: 1_

- [ ] 9. Implement private `_read_hosts()` and `_parse_hosts()` helpers
  - `_read_hosts()`: run `cat <hosts_path>` via runner (no elevation); return stdout
  - `_parse_hosts()`: parse lines into list of `(ip, hostnames, original_line)` tuples;
    preserve comment and blank lines as `(None, None, original_line)`
  - _Requirements: 3, 4, 5_

- [ ] 10. Implement `query_hosts()`
  - Call `_read_hosts()` then `_parse_hosts()`
  - Return list of `HostsEntry` records from non-comment, non-blank lines
  - Raise `NetworkQueryError` on read failure
  - _Requirements: 5_

- [ ] 11. Implement `add_hosts_entry()`
  - Call `_read_hosts()` and `_parse_hosts()`
  - Check for existing entry with same IP:
    - If identical entry exists → return `SKIPPED`
    - If IP exists with different hostnames → replace the line
    - If IP absent → append new line
  - Compute diff; return `DRY_RUN` with diff when `dry_run=True`
  - Write via `tee <hosts_path>` with `elevated` flag
  - _Requirements: 3_

- [ ] 12. Implement `remove_hosts_entry()`
  - Call `_read_hosts()` and `_parse_hosts()`
  - Return `SKIPPED` if no line matches the given IP
  - Remove matching line; write updated content via `tee` with `elevated` flag
  - Honour `dry_run` flag
  - _Requirements: 4_

- [ ] 13. Implement private `_read_resolv_conf()` and `_parse_resolv_conf()` helpers
  - `_read_resolv_conf()`: run `cat <resolv_conf_path>` (no elevation); return stdout
  - `_parse_resolv_conf()`: categorise lines into `nameserver`, `search`, `domain`,
    and `other` groups preserving original text; return structured dict
  - _Requirements: 6, 7_

- [ ] 14. Implement `set_nameservers()`
  - Call `_read_resolv_conf()` and `_parse_resolv_conf()`
  - Compare existing nameserver list to desired; return `SKIPPED` if identical
  - Replace `nameserver` lines with new set; preserve all other lines in original order
  - Compute diff; return `DRY_RUN` with diff when `dry_run=True`
  - Write via `tee <resolv_conf_path>` with `elevated` flag
  - _Requirements: 6_

- [ ] 15. Implement `set_search_domains()`
  - Call `_read_resolv_conf()` and `_parse_resolv_conf()`
  - Compare existing search list to desired; return `SKIPPED` if identical
  - Replace `search` line with new directive; preserve all other lines
  - Compute diff; return `DRY_RUN` with diff when `dry_run=True`
  - Write via `tee <resolv_conf_path>` with `elevated` flag
  - _Requirements: 7_

- [ ] 16. Implement private `_read_chrony_conf()` and `_parse_chrony_servers()` helpers
  - `_read_chrony_conf()`: run `cat <chrony_conf_path>` (no elevation); return stdout
  - `_parse_chrony_servers()`: identify lines matching `^server\s+<address>`; return set
    of server addresses and the full line list for reconstruction
  - _Requirements: 8, 9_

- [ ] 17. Implement `add_ntp_server()`
  - Call `_read_chrony_conf()` and `_parse_chrony_servers()`
  - Return `SKIPPED` if address already present in server set
  - Append `server <address> iburst` line to content
  - Return `DRY_RUN` when `dry_run=True` (no write, no service restart)
  - Write via `tee <chrony_conf_path>` with `elevated` flag
  - On successful write: invoke `systemctl restart chronyd` with `elevated` flag
  - Return `FAILED` if write fails without restarting service
  - _Requirements: 8_

- [ ] 18. Implement `remove_ntp_server()`
  - Call `_read_chrony_conf()` and `_parse_chrony_servers()`
  - Return `SKIPPED` if address not present in server set
  - Remove matching `server` lines from content
  - Return `DRY_RUN` when `dry_run=True`
  - Write via `tee <chrony_conf_path>` with `elevated` flag
  - On successful write: invoke `systemctl restart chronyd` with `elevated` flag
  - _Requirements: 9_

- [ ] 19. Implement `query_ntp_sync()`
  - Build `chronyc tracking` command (no elevation)
  - Parse multi-line output to extract `Reference ID`, `System time`, and `Leap status`
  - Populate `NtpSyncInfo`; set `is_synchronised=True` when `Leap status` is `Normal`
  - Raise `NetworkQueryError` on non-zero exit
  - _Requirements: 10_

- [ ] 20. Implement `query_connections()`
  - Build `nmcli -t -f NAME,TYPE,DEVICE,STATE connection show` (no elevation)
  - Split each stdout line on `:` into four fields
  - Return list of `NmConnectionInfo` records; return empty list when no output
  - Raise `NetworkQueryError` on non-zero exit
  - _Requirements: 13_

- [ ] 21. Implement `add_connection()`
  - Call `query_connections()` to check whether connection name already exists
  - When absent: build `nmcli connection add` command from `NmConnectionSpec` fields
  - When present: build `nmcli connection modify` command for changed fields only;
    return `SKIPPED` if all fields match
  - Return `DRY_RUN` without calling runner when `dry_run=True`
  - Pass `elevated` flag; on success invoke `nmcli connection up <name>` with `elevated`
  - _Requirements: 11_

- [ ] 22. Implement `remove_connection()`
  - Call `query_connections()` to check existence; return `SKIPPED` if absent
  - Build `nmcli connection delete <name>`; pass `elevated` flag
  - Honour `dry_run` flag
  - _Requirements: 12_

- [ ] 23. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_networkconfiguration.py`
  - Implement `MockCommandRunner` keyed on command args prefix
  - Helper `make_hostnamectl_json(static, transient, pretty)` for hostname query mock
  - Helper `make_hosts_content(entries)` for `/etc/hosts` mock content
  - Helper `make_resolv_conf_content(nameservers, search)` for resolv.conf mock content
  - Helper `make_chrony_conf_content(servers)` for chrony.conf mock content
  - Helper `make_chronyc_tracking_output(ref, offset, leap_status)` for NTP mock
  - Helper `make_nmcli_output(connections)` for nmcli colon-delimited mock
  - Override `hosts_path`, `resolv_conf_path`, `chrony_conf_path` in all file tests
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14_

- [ ] 24. Write tests for `query_hostname()` and `set_hostname()`
  - Test query: JSON parsed into correct `HostnameInfo`
  - Test query: command fails — raises `NetworkQueryError`
  - Test set: hostname differs — `hostnamectl` issued, returns `SUCCESS`
  - Test set: hostname already matches — returns `SKIPPED`
  - Test set: dry-run — returns `DRY_RUN` with current and desired
  - _Requirements: 1, 2_

- [ ] 25. Write tests for hosts file methods
  - Test query: non-comment lines parsed into `HostsEntry` list
  - Test query: comment and blank lines excluded from result
  - Test add: IP absent — new line appended, file written
  - Test add: IP present with different hostnames — line replaced
  - Test add: identical entry — returns `SKIPPED`
  - Test add: dry-run — returns `DRY_RUN` with diff
  - Test remove: IP present — line removed, file written
  - Test remove: IP absent — returns `SKIPPED`
  - Test remove: dry-run — returns `DRY_RUN`
  - _Requirements: 3, 4, 5_

- [ ] 26. Write tests for resolv.conf methods
  - Test set_nameservers: list differs — nameserver lines replaced, others preserved
  - Test set_nameservers: list identical — returns `SKIPPED`
  - Test set_nameservers: dry-run — returns `DRY_RUN` with diff
  - Test set_search_domains: domains differ — search line replaced, nameservers preserved
  - Test set_search_domains: domains identical — returns `SKIPPED`
  - Test set_search_domains: dry-run — returns `DRY_RUN` with diff
  - _Requirements: 6, 7_

- [ ] 27. Write tests for chrony.conf methods and `query_ntp_sync()`
  - Test add_ntp_server: address absent — server line appended, chronyd restarted
  - Test add_ntp_server: address present — returns `SKIPPED`, no write, no restart
  - Test add_ntp_server: write fails — returns `FAILED`, chronyd NOT restarted
  - Test add_ntp_server: dry-run — returns `DRY_RUN`, no write, no restart
  - Test remove_ntp_server: address present — line removed, chronyd restarted
  - Test remove_ntp_server: address absent — returns `SKIPPED`
  - Test remove_ntp_server: dry-run — returns `DRY_RUN`
  - Test query_ntp_sync: output parsed into correct `NtpSyncInfo`
  - Test query_ntp_sync: `Leap status` not `Normal` — `is_synchronised=False`
  - Test query_ntp_sync: command fails — raises `NetworkQueryError`
  - _Requirements: 8, 9, 10_

- [ ] 28. Write tests for NetworkManager connection methods
  - Test query_connections: colon-delimited output parsed into `NmConnectionInfo` list
  - Test query_connections: no connections — returns empty list
  - Test query_connections: command fails — raises `NetworkQueryError`
  - Test add_connection: connection absent — `nmcli add` issued then `nmcli up`
  - Test add_connection: connection exists, fields differ — `nmcli modify` then `nmcli up`
  - Test add_connection: connection exists, all fields match — returns `SKIPPED`
  - Test add_connection: dry-run — returns `DRY_RUN`, no commands issued
  - Test remove_connection: connection exists — `nmcli delete` issued, returns `SUCCESS`
  - Test remove_connection: connection absent — returns `SKIPPED`
  - Test remove_connection: dry-run — returns `DRY_RUN`
  - _Requirements: 11, 12, 13_
