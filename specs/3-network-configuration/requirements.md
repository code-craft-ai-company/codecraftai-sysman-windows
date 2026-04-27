# Requirements

## Introduction

The network-configuration feature provides the `automojo-sysman-windows`
package with the ability to query and configure network adapters, routes,
DNS servers, and the system hostname on Windows systems.  This spec
implements the network-configuration contract defined at
`automojo-sysman-common/specs/contracts/network-configuration/contract.md`
for Windows using the `NetTCPIP` and `DnsClient` PowerShell modules
(`New-NetIPAddress`, `Set-DnsClientServerAddress`, `Get-NetAdapter`,
etc.) plus `Rename-Computer` for the hostname.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The feature
does not decide *which* adapters should be configured — it provides the
capability to *do it* reliably, idempotently, and with full auditability.
All system commands are executed exclusively through the
`automojo-interfaces` command-runner abstraction (invoking
`powershell.exe -NoProfile -NonInteractive -Command <script>`).

Writes require an Administrator token; reads do not.  The manager honors
the `config.elevated` flag.  All JSON-bearing outputs are parsed in pure
Python via `json.loads` on the cmdlet's `ConvertTo-Json` output.

---

## Requirements

### Requirement 1: Query All Interfaces

**User Story:** As a maintenance coordinator, I want to list every
network adapter so that reporting and idempotency workflows can
enumerate adapters without parsing raw tool output.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_interfaces()` THE SYSTEM SHALL
   dispatch `powershell -Command "Get-NetAdapter | ConvertTo-Json"`
   through the runner without elevation, then dispatch
   `powershell -Command "Get-NetIPAddress | ConvertTo-Json"` to
   enrich each adapter with its `<addr>/<prefix>` entries.
2. WHEN both dispatches succeed THE SYSTEM SHALL return a list of
   `WindowsInterfaceInfo` with `name=<InterfaceAlias>`,
   `mac_address=<MacAddress>`, `addresses=<list of
   "<IPAddress>/<PrefixLength>">`, `gateway` (looked up via
   `Get-NetRoute -DestinationPrefix 0.0.0.0/0` matching on
   `InterfaceAlias`), `state` (`"up"` when `Status` is `"Up"`,
   otherwise `"down"`), and `mtu=<MtuSize>`.
3. WHEN either dispatch exits non-zero THE SYSTEM SHALL raise a
   `WindowsNetworkQueryError`.

---

### Requirement 2: Query a Single Interface

**User Story:** As a maintenance coordinator, I want to query a single
adapter's detail, so that downstream logic can branch on existence and
state without iterating `get_interfaces()`.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_interface(name)` THE SYSTEM SHALL
   dispatch `powershell -Command "Get-NetAdapter -Name '<name>' |
   ConvertTo-Json"` through the runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL enrich the adapter with
   `Get-NetIPAddress -InterfaceAlias '<name>' | ConvertTo-Json` and
   return a `WindowsInterfaceInfo` populated using the same rules
   as `get_interfaces()`.
3. WHEN the command exits non-zero with stderr matching `No MSFT_NetAdapter
   objects found` THE SYSTEM SHALL return `None`.
4. WHEN the command exits non-zero for any other reason THE SYSTEM
   SHALL raise a `WindowsNetworkQueryError`.

---

### Requirement 3: Configure an Interface

**User Story:** As a maintenance coordinator, I want to assign a static
IP address, netmask (prefix length), and gateway to an adapter, so that
persistent networking is in place and the runtime state matches.

#### Acceptance Criteria

1. WHEN the coordinator calls `configure_interface(name, address,
   netmask, gateway)` THE SYSTEM SHALL dispatch a PowerShell script
   that first removes any existing IP on the adapter
   (`Remove-NetIPAddress -InterfaceAlias '<name>' -Confirm:$false`)
   and then calls `New-NetIPAddress -InterfaceAlias '<name>'
   -IPAddress '<address>' -PrefixLength <N> [-DefaultGateway
   '<gateway>']` through the runner with `elevated=True` when
   configured.
2. WHEN `netmask` is a dotted-quad mask (e.g. `255.255.255.0`) THE
   SYSTEM SHALL convert it to an integer prefix length by counting
   set bits; when `netmask` is already a string of a small integer
   (e.g. `"24"`) THE SYSTEM SHALL pass it directly.
3. WHEN `gateway` is `None` THE SYSTEM SHALL omit the
   `-DefaultGateway` parameter.
4. WHEN `get_interface(name).addresses` already contains
   `<address>/<prefix>` AND the adapter's default gateway (from
   `Get-NetRoute`) matches `<gateway>` THE SYSTEM SHALL skip
   execution and return `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `WindowsNetworkActionResult` with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`
   with the captured `exit_code`, `stdout`, `stderr`, and `command`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the
   command and SHALL return `status=DRY_RUN`.

---

### Requirement 4: Enable an Interface

**User Story:** As a maintenance coordinator, I want to enable a
network adapter so that traffic can flow.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_interface(name)` THE SYSTEM
   SHALL dispatch `powershell -Command "Enable-NetAdapter -Name
   '<name>'"` through the runner with `elevated=True` when
   configured.
2. WHEN `Get-NetAdapter -Name '<name>'` already reports `Status=Up`
   THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 5: Disable an Interface

**User Story:** As a maintenance coordinator, I want to disable a
network adapter so that traffic does not flow.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_interface(name)` THE SYSTEM
   SHALL dispatch `powershell -Command "Disable-NetAdapter -Name
   '<name>' -Confirm:$false"` through the runner with
   `elevated=True` when configured.
2. WHEN `Get-NetAdapter -Name '<name>'` already reports
   `Status=Disabled` THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 6: Add a Route

**User Story:** As a maintenance coordinator, I want to add a static
route so that traffic destined for a specific prefix flows through a
chosen gateway.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_route(dest, gateway, interface)`
   THE SYSTEM SHALL dispatch `powershell -Command "New-NetRoute
   -InterfaceAlias '<interface>' -DestinationPrefix '<dest>'
   -NextHop '<gateway>'"` through the runner with `elevated=True`
   when configured.
2. WHEN `interface` is `None` THE SYSTEM SHALL return
   `status=FAILED` with `message="interface_required_on_windows"` —
   `New-NetRoute` requires an `-InterfaceAlias`.
3. WHEN `Get-NetRoute -DestinationPrefix '<dest>'` already reports
   a matching `(InterfaceAlias, NextHop)` THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 7: Remove a Route

**User Story:** As a maintenance coordinator, I want to remove a
static route so that a previous indirection is revoked.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_route(dest)` THE SYSTEM SHALL
   dispatch `powershell -Command "Remove-NetRoute -DestinationPrefix
   '<dest>' -Confirm:$false"` through the runner with
   `elevated=True` when configured.
2. WHEN no route with `DestinationPrefix=<dest>` exists THE SYSTEM
   SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 8: Query All Routes

**User Story:** As a maintenance coordinator, I want to list every
route so that reporting workflows can enumerate routes without parsing
raw tool output.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_routes()` THE SYSTEM SHALL
   dispatch `powershell -Command "Get-NetRoute | ConvertTo-Json"`
   through the runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL return a list of
   `WindowsRouteInfo` records with `destination=<DestinationPrefix>`,
   `gateway=<NextHop if not "0.0.0.0" / "::" else None>`,
   `interface=<InterfaceAlias>`, and `metric=<RouteMetric>`.
3. WHEN the command fails THE SYSTEM SHALL raise a
   `WindowsNetworkQueryError`.

---

### Requirement 9: Set DNS Servers

**User Story:** As a maintenance coordinator, I want to set the list
of DNS servers so that name resolution uses the operator-supplied
nameservers.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_dns_servers(servers)` THE SYSTEM
   SHALL dispatch `powershell -Command "Set-DnsClientServerAddress
   -InterfaceAlias '<iface>' -ServerAddresses @('<s1>', '<s2>', ...)"`
   through the runner with `elevated=True` when configured.  When
   `servers` is empty THE SYSTEM SHALL dispatch
   `Set-DnsClientServerAddress -InterfaceAlias '<iface>'
   -ResetServerAddresses`.
2. WHEN `get_dns_servers()` already reports the supplied list
   (order-preserved) THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

The interface targeted by `set_dns_servers` is the default-route
interface (looked up via
`Get-NetRoute -DestinationPrefix 0.0.0.0/0` and reading
`InterfaceAlias`).

---

### Requirement 10: Query DNS Servers

**User Story:** As a maintenance coordinator, I want to retrieve the
current DNS server list, so that idempotency guards and reporting
workflows can reason about current state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_dns_servers()` THE SYSTEM SHALL
   dispatch `powershell -Command "Get-DnsClientServerAddress
   -InterfaceAlias '<iface>' | Select-Object -ExpandProperty
   ServerAddresses | ConvertTo-Json"` through the runner without
   elevation.
2. WHEN the command succeeds AND stdout is an empty JSON array or
   `null` THE SYSTEM SHALL return `[]`.
3. WHEN the command succeeds AND stdout is a JSON array of strings
   THE SYSTEM SHALL return those strings preserving their order.
4. WHEN the command fails THE SYSTEM SHALL raise a
   `WindowsNetworkQueryError`.

---

### Requirement 11: Set Search Domains

**User Story:** As a maintenance coordinator, I want to set the DNS
search domain suffix list, so that short-name lookups resolve under
the operator-supplied suffixes.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_search_domains(domains)` THE
   SYSTEM SHALL dispatch `powershell -Command
   "Set-DnsClientGlobalSetting -SuffixSearchList @('<d1>', '<d2>',
   ...)"` through the runner with `elevated=True` when configured.
   When `domains` is empty THE SYSTEM SHALL dispatch
   `Set-DnsClientGlobalSetting -SuffixSearchList @()`.
2. WHEN `Get-DnsClientGlobalSetting | Select SuffixSearchList`
   already reports the supplied list THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 12: Get Hostname

**User Story:** As a maintenance coordinator, I want to query the
current system hostname so that downstream logic can branch on it.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_hostname()` THE SYSTEM SHALL
   dispatch `powershell -Command "[System.Net.Dns]::GetHostName()"`
   through the runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL return the stripped
   stdout as the hostname string.
3. WHEN the command fails THE SYSTEM SHALL raise a
   `WindowsNetworkQueryError`.

---

### Requirement 13: Set Hostname

**User Story:** As a maintenance coordinator, I want to set the
system hostname so that the machine reports the operator-supplied
name.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_hostname(name)` THE SYSTEM SHALL
   dispatch `powershell -Command "Rename-Computer -NewName '<name>'
   -Force"` through the runner with `elevated=True` when configured.
2. WHEN `[System.Net.Dns]::GetHostName()` already equals `<name>`
   THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return
   `status=SUCCESS` AND the `detail` field of the returned
   `WindowsNetworkActionResult` MUST include `requires_reboot=True`
   because `Rename-Computer` defers the change until the next boot.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 14: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all network
write operations to run with an Administrator token, so that write
operations succeed on a standard Windows system.

#### Acceptance Criteria

1. WHEN `config.elevated=True` THE SYSTEM SHALL pass `elevated=True`
   to the runner for every write (configure_interface,
   enable_interface, disable_interface, add_route, remove_route,
   set_dns_servers, set_search_domains, set_hostname).
2. WHEN performing read-only operations (get_interfaces,
   get_interface, get_routes, get_dns_servers, get_hostname) THE
   SYSTEM SHALL NOT pass `elevated=True`, regardless of config.
3. WHEN the process is already running as Administrator THE SYSTEM
   SHALL NOT pass `elevated=True`.
4. WHEN `config.elevated=False` THE SYSTEM SHALL NEVER pass
   `elevated=True`; a resulting permission error SHALL surface as
   `status=FAILED` with the runner's exit code / stderr for writes
   or as `WindowsNetworkQueryError` for reads.
