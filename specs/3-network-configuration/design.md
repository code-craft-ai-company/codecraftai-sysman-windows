# Design

## Overview

The network-configuration feature is implemented as a single module,
`ccai.sysman.windows.networkconfiguration`, that exposes a
`WindowsNetworkConfigurationManager` class.  This spec implements the
network-configuration contract defined at
`codecraftai-sysman-common/specs/contracts/network-configuration/contract.md`
for Windows using the `NetTCPIP`, `NetAdapter`, `DnsClient`, and
hostname cmdlets available on Windows 10 / 11 / Server 2019+.  The
coordinator instantiates the manager with a command runner and a config,
then calls the contract's verbs.

All system interaction happens through `ICommandRunner`, which dispatches
the PowerShell invocations (`powershell.exe -NoProfile -NonInteractive
-Command <script>`).  No direct `subprocess` call lives in the manager.
No `IFileOperator` is required; `Set-DnsClientServerAddress`,
`New-NetIPAddress`, and `Rename-Computer` own their own persistence
(they write to the registry and the system configuration store).

Reads parse `ConvertTo-Json` output in pure Python.  Writes use
PowerShell parameter binding so no string interpolation is needed for
values supplied by the coordinator — the argv that winds up on the
runner is `["powershell", "-NoProfile", "-NonInteractive", "-Command",
<script>]`.

The feature works identically whether commands execute locally (via
`WindowsLocalCommandRunner`) or remotely over WinRM (via
`WindowsWinRMCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/WinRM execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or WinRM)                     │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsNetworkConfigurationManager   │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│    WindowsNetworkConfigurationManager                       │
│  ccai.sysman.windows.networkconfiguration               │
│                                                             │
│  get_interfaces()     get_interface()                       │
│  configure_interface()                                      │
│  enable_interface()   disable_interface()                   │
│  add_route()          remove_route()      get_routes()      │
│  set_dns_servers()    get_dns_servers()                     │
│  set_search_domains()                                       │
│  get_hostname()       set_hostname()                        │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to (argv = powershell -Command ...)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsNetworkConfigurationManager`

**Location:** `source/packages/ccai/sysman/windows/networkconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `get_interfaces` | `() → list[WindowsInterfaceInfo]` | `Get-NetAdapter \| ConvertTo-Json` + `Get-NetIPAddress \| ConvertTo-Json` |
| `get_interface` | `(name) → WindowsInterfaceInfo \| None` | `Get-NetAdapter -Name '<name>' \| ConvertTo-Json` |
| `configure_interface` | `(name, address, netmask, gateway) → WindowsNetworkActionResult` | `Remove-NetIPAddress` + `New-NetIPAddress` |
| `enable_interface` | `(name) → WindowsNetworkActionResult` | `Enable-NetAdapter -Name '<name>'` |
| `disable_interface` | `(name) → WindowsNetworkActionResult` | `Disable-NetAdapter -Name '<name>' -Confirm:$false` |
| `add_route` | `(dest, gateway, interface) → WindowsNetworkActionResult` | `New-NetRoute -InterfaceAlias '<iface>' -DestinationPrefix '<dest>' -NextHop '<gw>'` |
| `remove_route` | `(dest) → WindowsNetworkActionResult` | `Remove-NetRoute -DestinationPrefix '<dest>' -Confirm:$false` |
| `get_routes` | `() → list[WindowsRouteInfo]` | `Get-NetRoute \| ConvertTo-Json` |
| `set_dns_servers` | `(servers) → WindowsNetworkActionResult` | `Set-DnsClientServerAddress -ServerAddresses` (or `-ResetServerAddresses`) |
| `get_dns_servers` | `() → list[str]` | `Get-DnsClientServerAddress \| Select ServerAddresses \| ConvertTo-Json` |
| `set_search_domains` | `(domains) → WindowsNetworkActionResult` | `Set-DnsClientGlobalSetting -SuffixSearchList` |
| `get_hostname` | `() → str` | `[System.Net.Dns]::GetHostName()` |
| `set_hostname` | `(name) → WindowsNetworkActionResult` | `Rename-Computer -NewName '<name>' -Force` |

### `WindowsNetworkConfigurationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands dispatch |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |

---

## Data Models

### Result Types

```python
class WindowsNetworkActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"


@dataclass
class WindowsNetworkActionResult:
    status: WindowsNetworkActionStatus
    interface: str | None
    detail: dict[str, object] | None   # {"requires_reboot": True, ...}
    diff: str | None                   # unused on Windows
    message: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Interface / Route Types

```python
@dataclass
class WindowsInterfaceInfo:
    name: str                          # InterfaceAlias
    mac_address: str | None            # MacAddress
    addresses: list[str]               # <IPAddress>/<PrefixLength>
    gateway: str | None
    state: str                         # "up" / "down"
    mtu: int | None


@dataclass
class WindowsRouteInfo:
    destination: str
    gateway: str | None
    interface: str | None
    metric: int | None
```

### Exceptions

```python
class WindowsNetworkQueryError(Exception):
    """Raised when a Get-* cmdlet fails unexpectedly."""
```

---

## Interface-Name Mapping

Windows uses **adapter aliases** (`Ethernet`, `Wi-Fi`, `vEthernet
(Default Switch)`).  These are the names accepted by every
`Get-NetAdapter`, `New-NetIPAddress`, `New-NetRoute`, and
`Set-DnsClientServerAddress` cmdlet.  The manager accepts the alias
directly as the `name` parameter — no mapping step is required because
all relevant cmdlets agree on the alias identifier.

Writes reach runtime and persistent state naturally: each cmdlet writes
to `%SystemRoot%\System32\config\SYSTEM` (for IP bindings) or the DNS
client registry keys, and `Rename-Computer` writes to
`HKLM\SYSTEM\CurrentControlSet\Control\ComputerName`.

---

## Command Mappings

### Write Commands

| Operation | PowerShell command |
|---|---|
| configure_interface | `Remove-NetIPAddress -InterfaceAlias '<n>' -Confirm:$false; New-NetIPAddress -InterfaceAlias '<n>' -IPAddress '<a>' -PrefixLength <p> [-DefaultGateway '<g>']` |
| enable_interface | `Enable-NetAdapter -Name '<n>'` |
| disable_interface | `Disable-NetAdapter -Name '<n>' -Confirm:$false` |
| add_route | `New-NetRoute -InterfaceAlias '<iface>' -DestinationPrefix '<dest>' -NextHop '<gw>'` |
| remove_route | `Remove-NetRoute -DestinationPrefix '<dest>' -Confirm:$false` |
| set_dns_servers | `Set-DnsClientServerAddress -InterfaceAlias '<iface>' -ServerAddresses @('<s1>', ...)` |
| set_dns_servers (empty) | `Set-DnsClientServerAddress -InterfaceAlias '<iface>' -ResetServerAddresses` |
| set_search_domains | `Set-DnsClientGlobalSetting -SuffixSearchList @('<d1>', ...)` |
| set_hostname | `Rename-Computer -NewName '<name>' -Force` |

### Read Commands (no elevation)

| Operation | PowerShell command |
|---|---|
| get_interfaces | `Get-NetAdapter \| ConvertTo-Json` + `Get-NetIPAddress \| ConvertTo-Json` |
| get_interface | `Get-NetAdapter -Name '<name>' \| ConvertTo-Json` |
| get_routes | `Get-NetRoute \| ConvertTo-Json` |
| get_dns_servers | `Get-DnsClientServerAddress -InterfaceAlias '<iface>' \| Select-Object -ExpandProperty ServerAddresses \| ConvertTo-Json` |
| get_hostname | `[System.Net.Dns]::GetHostName()` |

Each PowerShell invocation is wrapped into argv:

```
["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", <script>]
```

All writes pass `elevated=True` when `config.elevated=True`.  Reads
never elevate.

---

## Output Parsing

### `Get-NetAdapter | ConvertTo-Json` Output

```json
[
  {
    "Name": "Ethernet",
    "InterfaceAlias": "Ethernet",
    "Status": "Up",
    "MacAddress": "00-11-22-33-44-55",
    "MtuSize": 1500
  },
  {
    "Name": "Wi-Fi",
    "InterfaceAlias": "Wi-Fi",
    "Status": "Disabled",
    "MacAddress": "aa-bb-cc-dd-ee-ff",
    "MtuSize": 1500
  }
]
```

When only a single adapter is present, PowerShell emits a bare object
rather than a one-element array; the parser normalises both into a
Python list via `data if isinstance(data, list) else [data]`.

The parser maps `Status=="Up"` → `state="up"` and any other value
(e.g. `Disabled`, `Disconnected`) → `state="down"`.

### `Get-NetIPAddress | ConvertTo-Json` Output

```json
[
  {
    "InterfaceAlias": "Ethernet",
    "IPAddress": "10.0.1.50",
    "PrefixLength": 24,
    "AddressFamily": "IPv4"
  }
]
```

Addresses are grouped by `InterfaceAlias` and rendered as
`<IPAddress>/<PrefixLength>`.

### `Get-NetRoute | ConvertTo-Json` Output

```json
[
  {
    "DestinationPrefix": "0.0.0.0/0",
    "NextHop": "10.0.1.1",
    "InterfaceAlias": "Ethernet",
    "RouteMetric": 25
  },
  {
    "DestinationPrefix": "224.0.0.0/4",
    "NextHop": "0.0.0.0",
    "InterfaceAlias": "Ethernet",
    "RouteMetric": 256
  }
]
```

Parser rules:

- `NextHop == "0.0.0.0"` or `"::"` → `gateway = None` (link-local).
- `DestinationPrefix` is already CIDR.
- `RouteMetric` → `metric`.

### `Get-DnsClientServerAddress` Output

`Select-Object -ExpandProperty ServerAddresses | ConvertTo-Json` emits
either a JSON array of strings, a single string, or `null` (when no
DNS servers are configured).  The parser normalises:

- `null` / empty array → `[]`
- bare string → `["<string>"]`
- array → the array

### `Rename-Computer` Output

`Rename-Computer -NewName '<name>' -Force` emits a warning to stdout
on success:

```
WARNING: The changes will take effect after you restart the computer ...
```

Exit 0 with that warning → `status=SUCCESS` with
`detail={"requires_reboot": True}`.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `configure_interface` | address `<addr>/<prefix>` already bound to the adapter AND default gateway matches | `get_interface(name)` + `Get-NetRoute 0.0.0.0/0` |
| `enable_interface` | adapter `Status=="Up"` | `Get-NetAdapter -Name '<name>'` |
| `disable_interface` | adapter `Status=="Disabled"` | `Get-NetAdapter -Name '<name>'` |
| `add_route` | equivalent `(dest, NextHop, InterfaceAlias)` present | `get_routes()` |
| `remove_route` | no route for `dest` | `get_routes()` |
| `set_dns_servers` | current list already matches (order-preserved) | `get_dns_servers()` |
| `set_search_domains` | `SuffixSearchList` already matches | `Get-DnsClientGlobalSetting` |
| `set_hostname` | `[System.Net.Dns]::GetHostName()` already equals `<name>` | `get_hostname()` |

---

## Error Handling

| Exception | Raised When |
|---|---|
| `WindowsNetworkQueryError` | Any `Get-*` cmdlet exits non-zero for a reason other than "not found" |

All write failures surface as
`WindowsNetworkActionResult(status=FAILED, ...)` carrying the runner's
exit code, stdout, stderr, and a `message` populated for the
documented failure paths.

---

## Correctness Properties

### Property 1: get_interfaces Adapter Enumeration

*For any* `Get-NetAdapter | ConvertTo-Json` output, `get_interfaces()` should return one `WindowsInterfaceInfo` per entry and map `Status=="Up"` to `state="up"`.

**Validates: Requirements 1.1, 1.2**

### Property 2: get_interfaces Address Join

*For any* `Get-NetIPAddress | ConvertTo-Json` output, `get_interfaces()` should group `<IPAddress>/<PrefixLength>` entries by `InterfaceAlias` and attach them to the matching adapter.

**Validates: Requirements 1.2**

### Property 3: get_interfaces JSON Normalisation

*For any* single-adapter output emitted as a bare JSON object (not array), `get_interfaces()` should still return a one-element list.

**Validates: Requirements 1.1**

### Property 4: get_interface Missing Returns None

*For any* `get_interface(name)` where stderr matches `No MSFT_NetAdapter objects found`, the result is `None`.

**Validates: Requirements 2.3**

### Property 5: get_interface Unexpected Error

*For any* other non-zero exit from `Get-NetAdapter`, `get_interface` raises `WindowsNetworkQueryError`.

**Validates: Requirements 2.4**

### Property 6: configure_interface Dotted-Quad Netmask Conversion

*For any* `configure_interface(name, address, "255.255.255.0", gateway)`, the rendered `New-NetIPAddress` script passes `-PrefixLength 24`.

**Validates: Requirements 3.2**

### Property 7: configure_interface Null-Gateway Omission

*For any* `configure_interface(name, address, netmask, None)`, the rendered script omits `-DefaultGateway`.

**Validates: Requirements 3.3**

### Property 8: configure_interface Idempotency

*For any* adapter whose current `addresses` already include `<address>/<prefix>` and whose default gateway already matches, `configure_interface` returns `SKIPPED`.

**Validates: Requirements 3.4**

### Property 9: enable_interface Idempotency

*For any* adapter whose `Status` is already `"Up"`, `enable_interface` returns `SKIPPED`.

**Validates: Requirements 4.2**

### Property 10: disable_interface Idempotency

*For any* adapter whose `Status` is already `"Disabled"`, `disable_interface` returns `SKIPPED`.

**Validates: Requirements 5.2**

### Property 11: add_route Requires Interface

*For any* `add_route(dest, gateway, None)`, the result is `FAILED` with `message="interface_required_on_windows"` and the runner is not invoked.

**Validates: Requirements 6.2**

### Property 12: add_route Idempotency

*For any* route where a matching `(DestinationPrefix, NextHop, InterfaceAlias)` already exists, `add_route` returns `SKIPPED`.

**Validates: Requirements 6.3**

### Property 13: remove_route Idempotency

*For any* destination absent from `Get-NetRoute`, `remove_route` returns `SKIPPED`.

**Validates: Requirements 7.2**

### Property 14: get_routes Gateway Normalisation

*For any* `Get-NetRoute` entry with `NextHop == "0.0.0.0"` or `"::"`, the returned `WindowsRouteInfo.gateway` is `None`.

**Validates: Requirements 8.2**

### Property 15: set_dns_servers Empty Uses Reset

*For any* `set_dns_servers([])` call, the dispatched script uses `-ResetServerAddresses` instead of `-ServerAddresses @()`.

**Validates: Requirements 9.1**

### Property 16: set_dns_servers Idempotency

*For any* DNS-server list already matching `get_dns_servers()`, `set_dns_servers` returns `SKIPPED`.

**Validates: Requirements 9.2**

### Property 17: get_dns_servers Normalisation

*For any* `Get-DnsClientServerAddress` output that emits `null`, an empty array, a bare string, or a JSON array, `get_dns_servers` returns a Python list (possibly empty).

**Validates: Requirements 10.2, 10.3**

### Property 18: set_search_domains Empty Sets Empty Array

*For any* `set_search_domains([])` call, the dispatched script passes `-SuffixSearchList @()`.

**Validates: Requirements 11.1**

### Property 19: set_search_domains Idempotency

*For any* search-domain list already matching current `SuffixSearchList`, `set_search_domains` returns `SKIPPED`.

**Validates: Requirements 11.2**

### Property 20: get_hostname Returns Stripped Stdout

*For any* successful `[System.Net.Dns]::GetHostName()` dispatch, `get_hostname` returns the stripped stdout.

**Validates: Requirements 12.1, 12.2**

### Property 21: set_hostname Idempotency

*For any* `set_hostname(name)` where `[System.Net.Dns]::GetHostName()` already equals `name`, the result is `SKIPPED`.

**Validates: Requirements 13.2**

### Property 22: set_hostname Reboot Flag

*For any* successful `set_hostname(name)` dispatch, `detail` includes `requires_reboot=True`.

**Validates: Requirements 13.3**

### Property 23: Dry-Run Safety

*For any* write method with `dry_run=True`, the runner is never invoked and the result status is `DRY_RUN`.

**Validates: Requirements 3.7, 4.5, 5.5, 6.6, 7.5, 9.5, 11.5, 13.5**

### Property 24: Elevation for Writes

*For any* write with `config.elevated=True`, the runner is invoked with `elevated=True`.

**Validates: Requirements 14.1**

### Property 25: No Elevation for Reads

*For any* read, the runner is never invoked with `elevated=True`.

**Validates: Requirements 14.2**

### Property 26: Error Result Consistency

*For any* non-zero exit from a write command, the returned `WindowsNetworkActionResult` has `status=FAILED` and carries the runner's exit code and stderr.

**Validates: Requirements 3.6, 4.4, 5.4, 6.5, 7.4, 9.4, 11.4, 13.4**

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
└── networkconfiguration.py    ← WindowsNetworkConfigurationManager,
                                  config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_networkconfiguration.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.windows.runners import (
    WindowsLocalCommandRunner, WindowsLocalRunnerConfig,
)
from ccai.sysman.windows.networkconfiguration import (
    WindowsNetworkConfigurationManager,
    WindowsNetworkConfigurationManagerConfig,
)

runner = WindowsLocalCommandRunner(WindowsLocalRunnerConfig())
config = WindowsNetworkConfigurationManagerConfig(elevated=True, dry_run=False)
net_mgr = WindowsNetworkConfigurationManager(runner=runner, config=config)

net_mgr.configure_interface(
    name="Ethernet", address="10.0.1.50", netmask="255.255.255.0",
    gateway="10.0.1.1",
)
net_mgr.set_dns_servers(servers=["1.1.1.1", "8.8.8.8"])
net_mgr.set_hostname(name="win-worker-01")

for iface in net_mgr.get_interfaces():
    print(f"{iface.name}\t{iface.state}\t{iface.addresses}")
```

### WinRM Execution With Aspects

```python
from ccai.sysman.windows.runners import (
    WindowsWinRMCommandRunner, WindowsWinRMRunnerConfig,
)
from ccai.sysman.windows.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from ccai.sysman.windows.storage import SQLiteStorage

runner = WindowsWinRMCommandRunner(
    WindowsWinRMRunnerConfig(host="win-fleet-01", username="DOMAIN\\ops"),
)
runner.add_aspect(aspect=PersistenceAspect(
    PersistenceAspectConfig(storage_backend=SQLiteStorage(path="C:/logs/audit.db")),
))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

net_mgr = WindowsNetworkConfigurationManager(
    runner=runner,
    config=WindowsNetworkConfigurationManagerConfig(elevated=True),
)
net_mgr.add_route(dest="172.16.0.0/12", gateway="10.0.1.254", interface="Ethernet")
```

### Dry-Run Mode

```python
config = WindowsNetworkConfigurationManagerConfig(elevated=True, dry_run=True)
net_mgr = WindowsNetworkConfigurationManager(runner=runner, config=config)
result = net_mgr.set_hostname(name="win-worker-02")
assert result.status == WindowsNetworkActionStatus.DRY_RUN
print(result.command)
```

---

## Testing Strategy

1. All tests use a `MockCommandRunner` keyed on argv prefix (the
   first PowerShell cmdlet name in the `-Command` script).
2. `Get-NetAdapter | ConvertTo-Json` mock outputs cover: multi-
   adapter arrays, single-adapter bare objects, `Up` vs `Disabled`
   status, and missing `MacAddress` for virtual adapters.
3. `Get-NetIPAddress | ConvertTo-Json` mock drives the
   `configure_interface` idempotency test and the `get_interfaces`
   join test.
4. `Get-NetRoute | ConvertTo-Json` mock exercises: default-route
   lookup for `gateway` population, `NextHop="0.0.0.0"` →
   `gateway=None`, and multi-interface scenarios.
5. `Set-DnsClientServerAddress` tests cover: empty list uses
   `-ResetServerAddresses`, populated list uses `-ServerAddresses
   @(...)`, idempotent match of current state.
6. `Set-DnsClientGlobalSetting -SuffixSearchList` tests cover the
   empty-list-as-empty-array case and the idempotency match.
7. `set_hostname` tests cover: idempotent match of current
   hostname, successful rename populating
   `detail.requires_reboot=True`, and failure surfacing `FAILED`.
8. `add_route` tests cover: `interface=None` → `FAILED` with
   `message="interface_required_on_windows"` and no dispatch.
9. Dry-run tests prove every write short-circuits without invoking
   the runner.
10. Elevation tests prove writes pass `elevated=True` when
    configured and reads never do.
11. No test requires Administrator access or a live Windows system.

---

## Scope Mapping

| Contract verb | Windows implementation |
|---|---|
| `get_interfaces` | `Get-NetAdapter + Get-NetIPAddress` |
| `get_interface` | `Get-NetAdapter -Name '<name>'` |
| `configure_interface` | `Remove-NetIPAddress` + `New-NetIPAddress` |
| `enable_interface` | `Enable-NetAdapter -Name '<name>'` |
| `disable_interface` | `Disable-NetAdapter -Name '<name>' -Confirm:$false` |
| `add_route` | `New-NetRoute -InterfaceAlias '<iface>' -DestinationPrefix '<dest>' -NextHop '<gw>'` |
| `remove_route` | `Remove-NetRoute -DestinationPrefix '<dest>' -Confirm:$false` |
| `get_routes` | `Get-NetRoute | ConvertTo-Json` |
| `set_dns_servers` | `Set-DnsClientServerAddress` |
| `get_dns_servers` | `Get-DnsClientServerAddress` |
| `set_search_domains` | `Set-DnsClientGlobalSetting -SuffixSearchList` |
| `get_hostname` | `[System.Net.Dns]::GetHostName()` |
| `set_hostname` | `Rename-Computer -NewName '<name>' -Force` (requires reboot) |

---

## Extensions Beyond the Contract

### `requires_reboot` Detail

The base contract's `NetworkActionResult.detail` is documented as a
free-form field; Windows' `Rename-Computer` defers the actual rename
until the next boot.  This implementation populates
`detail={"requires_reboot": True}` on any successful `set_hostname`
so coordinators can surface the pending reboot in their reports.

### Immediate-Rename Extension

A future extension may call `Restart-Computer -Force` after a
successful `set_hostname` behind a separate
`set_hostname_and_reboot(name)` verb.  This stays outside the base
contract because rebooting the target mid-workflow is a global side
effect the coordinator should opt into explicitly.

### `NetIPInterface` MTU Management

The contract exposes `MtuSize` in `WindowsInterfaceInfo` as a read-only
field.  A future extension may add `set_interface_mtu(name, mtu)`
dispatching `Set-NetIPInterface -InterfaceAlias '<n>' -NlMtuBytes <n>`
behind a flag — not part of the base contract surface.
