# Requirements

## Introduction

The network configuration feature provides the `automojo-sysman-linux-windows` package with
the ability to set the system hostname, manage `/etc/hosts` entries, configure DNS
resolution, manage NTP/Chrony sources and verify sync status, and manage NetworkManager
connections on Windows Linux systems.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *what* network
configuration to apply — it provides the capability to *do it* reliably, idempotently, and
with full auditability.  All system commands are executed exclusively through the
`automojo-interfaces` command-runner abstraction, enabling dry-run simulation, auditing,
and test mocking without requiring a live system.

---

## Requirements

### Requirement 1: Set the System Hostname

**User Story:** As a maintenance coordinator, I want to set the system hostname, so that
the machine is identifiable by the correct name on the network and in system logs.

#### Acceptance Criteria

1. WHEN the coordinator provides a hostname string THE SYSTEM SHALL invoke
   `hostnamectl set-hostname <hostname>` via the command runner.
2. WHEN the current hostname already matches the desired hostname THE SYSTEM SHALL skip
   execution and return a `NetworkActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `NetworkActionResult` with status
   `SUCCESS` and the new hostname.
4. WHEN the command fails THE SYSTEM SHALL return a `NetworkActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `NetworkActionResult` with status `DRY_RUN` showing the current and desired hostname.

---

### Requirement 2: Query the System Hostname

**User Story:** As a maintenance coordinator, I want to query the current system hostname,
so that I can determine whether a hostname change is required before taking any action.

#### Acceptance Criteria

1. WHEN the coordinator requests the hostname THE SYSTEM SHALL invoke
   `hostnamectl --json short` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `HostnameInfo` record containing
   the static hostname, transient hostname, and pretty hostname.
3. WHEN the command fails THE SYSTEM SHALL raise a `NetworkQueryError` with the exit code
   and captured stderr.

---

### Requirement 3: Add an /etc/hosts Entry

**User Story:** As a maintenance coordinator, I want to add a hostname-to-IP address
mapping to `/etc/hosts`, so that name resolution for a specific host is controlled locally
without relying on DNS.

#### Acceptance Criteria

1. WHEN the coordinator provides an IP address and one or more hostnames THE SYSTEM SHALL
   append a line in the format `<ip>  <hostname> [aliases...]` to `/etc/hosts` via the
   command runner.
2. WHEN an entry for the same IP address already exists in `/etc/hosts` THE SYSTEM SHALL
   update the existing line with the supplied hostnames and return status `SUCCESS`.
3. WHEN an identical entry already exists THE SYSTEM SHALL skip the write and return a
   `NetworkActionResult` with status `SKIPPED`.
4. WHEN the write succeeds THE SYSTEM SHALL return a `NetworkActionResult` with status
   `SUCCESS`.
5. WHEN the write fails THE SYSTEM SHALL return a `NetworkActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `NetworkActionResult` with status `DRY_RUN` with the diff of what would change.

---

### Requirement 4: Remove an /etc/hosts Entry

**User Story:** As a maintenance coordinator, I want to remove a hostname-to-IP address
mapping from `/etc/hosts`, so that stale or incorrect local name resolution entries are
cleaned up.

#### Acceptance Criteria

1. WHEN the coordinator provides an IP address THE SYSTEM SHALL remove the line for that
   IP from `/etc/hosts` via the command runner.
2. WHEN no entry for the given IP address exists THE SYSTEM SHALL skip the write and return
   a `NetworkActionResult` with status `SKIPPED`.
3. WHEN the removal succeeds THE SYSTEM SHALL return a `NetworkActionResult` with status
   `SUCCESS`.
4. WHEN the write fails THE SYSTEM SHALL return a `NetworkActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `NetworkActionResult` with status `DRY_RUN`.

---

### Requirement 5: Query /etc/hosts Entries

**User Story:** As a maintenance coordinator, I want to retrieve all entries in `/etc/hosts`,
so that I can determine which entries need to be added, updated, or removed.

#### Acceptance Criteria

1. WHEN the coordinator requests the hosts entries THE SYSTEM SHALL read `/etc/hosts` via
   the command runner.
2. WHEN the read succeeds THE SYSTEM SHALL return a list of `HostsEntry` records parsed
   from the non-comment lines.
3. WHEN the file is empty or contains only comments THE SYSTEM SHALL return an empty list.
4. WHEN the read fails THE SYSTEM SHALL raise a `NetworkQueryError` with the captured
   error detail.

---

### Requirement 6: Configure DNS Nameservers

**User Story:** As a maintenance coordinator, I want to set the DNS nameserver addresses
used by the system resolver, so that name resolution uses the correct DNS infrastructure.

#### Acceptance Criteria

1. WHEN the coordinator provides a list of nameserver IP addresses THE SYSTEM SHALL write
   `nameserver <ip>` lines to `/etc/resolv.conf` via the command runner, preserving any
   existing `search` and `domain` directives.
2. WHEN the current nameserver list in `/etc/resolv.conf` already matches the desired list
   THE SYSTEM SHALL skip the write and return a `NetworkActionResult` with status
   `SKIPPED`.
3. WHEN the write succeeds THE SYSTEM SHALL return a `NetworkActionResult` with status
   `SUCCESS`.
4. WHEN the write fails THE SYSTEM SHALL return a `NetworkActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `NetworkActionResult` with status `DRY_RUN` with the diff of what would change.

---

### Requirement 7: Configure DNS Search Domains

**User Story:** As a maintenance coordinator, I want to set the DNS search domain list in
`/etc/resolv.conf`, so that short hostnames are resolved correctly within the appropriate
domain hierarchy.

#### Acceptance Criteria

1. WHEN the coordinator provides a list of search domains THE SYSTEM SHALL update the
   `search` directive in `/etc/resolv.conf` via the command runner, preserving existing
   `nameserver` directives.
2. WHEN the current search domain list already matches the desired list THE SYSTEM SHALL
   skip the write and return a `NetworkActionResult` with status `SKIPPED`.
3. WHEN the write succeeds THE SYSTEM SHALL return a `NetworkActionResult` with status
   `SUCCESS`.
4. WHEN the write fails THE SYSTEM SHALL return a `NetworkActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL return a
   `NetworkActionResult` with status `DRY_RUN` with the diff of what would change.

---

### Requirement 8: Add an NTP/Chrony Server

**User Story:** As a maintenance coordinator, I want to add an NTP server to the Chrony
configuration, so that the system synchronises time against the correct time source.

#### Acceptance Criteria

1. WHEN the coordinator provides an NTP server address THE SYSTEM SHALL add a
   `server <address> iburst` line to `/etc/chrony.conf` via the command runner.
2. WHEN the server address is already present in the configuration THE SYSTEM SHALL skip
   the write and return a `NetworkActionResult` with status `SKIPPED`.
3. WHEN the write succeeds THE SYSTEM SHALL restart the `chronyd` service via the command
   runner so the new server takes effect immediately.
4. WHEN the write fails THE SYSTEM SHALL return a `NetworkActionResult` with status
   `FAILED` without restarting the service.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file or restart any service
   and SHALL return a `NetworkActionResult` with status `DRY_RUN`.

---

### Requirement 9: Remove an NTP/Chrony Server

**User Story:** As a maintenance coordinator, I want to remove an NTP server from the
Chrony configuration, so that the system no longer attempts to synchronise time against
a decommissioned or incorrect time source.

#### Acceptance Criteria

1. WHEN the coordinator provides an NTP server address THE SYSTEM SHALL remove the matching
   `server` line from `/etc/chrony.conf` via the command runner.
2. WHEN the server address is not present in the configuration THE SYSTEM SHALL skip the
   write and return a `NetworkActionResult` with status `SKIPPED`.
3. WHEN the removal succeeds THE SYSTEM SHALL restart the `chronyd` service via the command
   runner so the change takes effect immediately.
4. WHEN the write fails THE SYSTEM SHALL return a `NetworkActionResult` with status
   `FAILED` without restarting the service.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file or restart any service
   and SHALL return a `NetworkActionResult` with status `DRY_RUN`.

---

### Requirement 10: Query NTP Sync Status

**User Story:** As a maintenance coordinator, I want to query the current NTP
synchronisation status, so that I can verify time sync is healthy after applying
configuration changes.

#### Acceptance Criteria

1. WHEN the coordinator requests NTP sync status THE SYSTEM SHALL invoke
   `chronyc tracking` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `NtpSyncInfo` record containing
   the reference source, system time offset, and synchronisation status.
3. WHEN the command fails THE SYSTEM SHALL raise a `NetworkQueryError` with the exit code
   and captured stderr.

---

### Requirement 11: Add a NetworkManager Connection

**User Story:** As a maintenance coordinator, I want to create or update a NetworkManager
connection profile, so that a network interface is configured with the correct addressing,
routing, and DNS settings.

#### Acceptance Criteria

1. WHEN the coordinator provides a connection name and configuration THE SYSTEM SHALL
   invoke `nmcli connection add` or `nmcli connection modify` via the command runner
   depending on whether the connection already exists.
2. WHEN a connection with the supplied name already exists and all settings match THE
   SYSTEM SHALL skip execution and return a `NetworkActionResult` with status `SKIPPED`.
3. WHEN the operation succeeds THE SYSTEM SHALL invoke `nmcli connection up <name>` to
   activate the connection and return a `NetworkActionResult` with status `SUCCESS`.
4. WHEN the add or modify command fails THE SYSTEM SHALL return a `NetworkActionResult`
   with status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `NetworkActionResult` with status `DRY_RUN`.

---

### Requirement 12: Remove a NetworkManager Connection

**User Story:** As a maintenance coordinator, I want to delete a NetworkManager connection
profile, so that an unused or incorrect network configuration is removed from the system.

#### Acceptance Criteria

1. WHEN the coordinator provides a connection name THE SYSTEM SHALL invoke
   `nmcli connection delete <name>` via the command runner.
2. WHEN no connection with that name exists THE SYSTEM SHALL skip execution and return a
   `NetworkActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `NetworkActionResult` with status
   `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `NetworkActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `NetworkActionResult` with status `DRY_RUN`.

---

### Requirement 13: Query NetworkManager Connections

**User Story:** As a maintenance coordinator, I want to retrieve the list of NetworkManager
connection profiles and their active state, so that I can determine which connections need
to be added, modified, or removed.

#### Acceptance Criteria

1. WHEN the coordinator requests the connection list THE SYSTEM SHALL invoke
   `nmcli -t -f NAME,TYPE,DEVICE,STATE connection show` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a list of `NmConnectionInfo` records
   each containing name, type, device, and active state.
3. WHEN no connections are configured THE SYSTEM SHALL return an empty list.
4. WHEN the command fails THE SYSTEM SHALL raise a `NetworkQueryError` with the exit code
   and captured stderr.

---

### Requirement 14: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all network write commands to run with
the privileges required by the underlying tools, so that hostname, hosts file, resolver,
NTP, and NetworkManager changes succeed on a standard system where the process does not
run as root.

#### Acceptance Criteria

1. WHEN the system is configured to use sudo elevation THE SYSTEM SHALL pass `elevated=True`
   to the command runner for all write operations (hostnamectl, hosts file, resolv.conf,
   chrony.conf, nmcli connection add/modify/delete/up).
2. WHEN performing read-only operations (hostnamectl query, cat /etc/hosts,
   cat /etc/resolv.conf, chronyc tracking, nmcli show) THE SYSTEM SHALL NOT use elevation.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation and
   allow the command runner to surface the resulting permission error.
