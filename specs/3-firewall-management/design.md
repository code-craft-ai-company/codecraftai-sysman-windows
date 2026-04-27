# Design

## Overview

The firewall management feature is implemented as a single module,
`automojo.sysman.windows.firewallmanagement`, that exposes a `WindowsFirewallManager`
class.  This spec implements the firewall-management contract defined at
`automojo-sysman-common/specs/contracts/firewall-management/contract.md` for
Windows using the PowerShell `NetSecurity` module (`New-NetFirewallRule`,
`Remove-NetFirewallRule`, `Get-NetFirewallRule`, `Get-NetFirewallProfile`,
`Get-NetConnectionProfile`).  The coordinator instantiates `WindowsFirewallManager`
and calls its methods to perform discrete maintenance actions against Windows
Firewall.

All state queries used for idempotency guards go through `Get-NetFirewallRule |
ConvertTo-Json`.  The JSON output is parsed in pure Python into a `WindowsZoneInfo`.
All system interaction is delegated to a command runner implementing the
`ICommandRunner` interface.  No direct filesystem access, no COM calls, and no direct
`subprocess` calls are made inside the module.

Windows Firewall applies rule changes immediately when the `NetSecurity` cmdlets
return — there is no separate "permanent" vs "runtime" split and no reload step.  The
`reload()` method is therefore a contract-mandated no-op that always returns
`SUCCESS`.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely (via `SSHCommandRunner` over OpenSSH on Windows or
a WinRM-capable runner).  When using a remote runner, the coordinator can track
reliability metrics and apply execution aspects (persistence, retry, timeout) without
any changes to the `WindowsFirewallManager` code.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/remote execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or Remote)                    │
│  - Configures aspects (optional)                            │
│  - Injects runner into WindowsFirewallManager               │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsFirewallManager                           │
│  automojo.sysman.windows.firewallmanagement                 │
│                                                             │
│  add_service()      remove_service()                        │
│  add_port()         remove_port()                           │
│  add_rich_rule()    remove_rich_rule()                      │
│  set_default_zone() reload()                                │
│  query_zone()       query_all_zones()                       │
│  query_state()                                              │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
│  — args begin with ["powershell.exe", "-NoProfile", "-Command", …]│
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - UAC for elevation  │      │ - reliability metrics│
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsFirewallManager`

**Location:** `source/packages/automojo/sysman/windows/firewallmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_service` | `(service, zone) → WindowsFirewallActionResult` | Resolve `(port, proto)` via bundled services table, `New-NetFirewallRule -DisplayName 'automojo-svc-<service>' -Profile <zone> …` |
| `remove_service` | `(service, zone) → WindowsFirewallActionResult` | `Remove-NetFirewallRule -DisplayName 'automojo-svc-<service>'` |
| `add_port` | `(port, protocol, zone) → WindowsFirewallActionResult` | `New-NetFirewallRule -DisplayName 'automojo-port-<port>-<protocol>' -Profile <zone> …` |
| `remove_port` | `(port, protocol, zone) → WindowsFirewallActionResult` | `Remove-NetFirewallRule -DisplayName 'automojo-port-<port>-<protocol>'` |
| `add_rich_rule` | `(rule, zone) → WindowsFirewallActionResult` | `New-NetFirewallRule <rule> -Profile <zone>` where rule is splatted parameters |
| `remove_rich_rule` | `(rule, zone) → WindowsFirewallActionResult` | Parse DisplayName from rule, `Remove-NetFirewallRule -DisplayName '<name>'` |
| `set_default_zone` | `(zone) → WindowsFirewallActionResult` | Documented no-op — `SKIPPED` for valid profile names, `FAILED` otherwise |
| `reload` | `() → WindowsFirewallActionResult` | Contract-mandated no-op — always `SUCCESS` with explanatory detail |
| `query_zone` | `(zone) → WindowsZoneInfo` | `Get-NetFirewallRule \| Where -Profile <zone> \| ConvertTo-Json` |
| `query_all_zones` | `() → list[WindowsZoneInfo]` | `query_zone` for each of `Domain`, `Private`, `Public` |
| `query_state` | `() → WindowsFirewallState` | `Get-NetFirewallProfile \| ConvertTo-Json` + `Get-NetConnectionProfile \| ConvertTo-Json` |

### `WindowsFirewallManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `powershell_executable` | `str` | `"powershell.exe"` | PowerShell executable to use |

---

## Data Models

### Result Types

```python
class WindowsFirewallActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class WindowsFirewallActionResult:
    status: WindowsFirewallActionStatus
    zone: str | None
    detail: str | None          # service name, port/proto, rule string, or zone name
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Query Result Types

```python
@dataclass
class WindowsZoneInfo:
    zone: str                   # "Domain" | "Private" | "Public"
    services: list[str]
    ports: list[str]            # each entry is "<port>/<protocol>"
    rich_rules: list[str]
    interfaces: list[str]       # populated from InterfaceAlias where present
    sources: list[str]          # populated from RemoteAddress where present
    is_default: bool            # True when this profile matches the active network category

@dataclass
class WindowsFirewallState:
    running: bool
    default_zone: str           # active network category, or ""

class WindowsFirewallQueryError(Exception):
    """Raised when a Get-NetFirewallRule / Get-NetFirewallProfile query fails."""
```

**Design note:** The contract defines `running` as "whether the firewall daemon /
service is active".  Windows Firewall is enabled per-profile.  This implementation
interprets `running=True` as "all three profiles (`Domain`, `Private`, `Public`) are
Enabled" and `running=False` as "at least one is disabled".  This conservative
interpretation is chosen because a coordinator that expects `running=True` to guarantee
that subsequent writes will take effect must not be given a false positive when a
profile that the coordinator later targets has been disabled.  The contract permits
this choice because it defers the interpretation of `running` to the per-OS spec.

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

All Windows Firewall commands are submitted as
`["powershell.exe", "-NoProfile", "-Command", <script>]`, with the script ending in
`| ConvertTo-Json -Compress -Depth 5` for read operations so that parsing is stable.

---

## Zone Mapping

On Windows the abstract `zone` parameter maps to a Windows Firewall profile:

- `zone == "Domain"` / `"Private"` / `"Public"` / `"Any"` → accepted.
- Any other value → operation returns `WindowsFirewallActionResult` with
  `status=FAILED` and `detail="Windows Firewall zone must be one of Domain, Private,
  Public, or Any; received '<zone>'"`.

`Any` is accepted on writes (it maps to applying the rule to all profiles) but is
never returned by `query_state()` as the default zone — the default zone is always
the specific network category reported by `Get-NetConnectionProfile`.

---

## Command Mappings

All commands are dispatched through
`["powershell.exe", "-NoProfile", "-Command", "<script>"]`.  The script text is
single-line and quoted-in using PowerShell escape conventions.

### Write Commands

| Operation | PowerShell script |
|---|---|
| Add service | `New-NetFirewallRule -DisplayName 'automojo-svc-<service>' -Direction Inbound -Action Allow -Protocol <proto> -LocalPort <port> -Profile <zone>` |
| Remove service | `Remove-NetFirewallRule -DisplayName 'automojo-svc-<service>'` |
| Add port | `New-NetFirewallRule -DisplayName 'automojo-port-<port>-<proto>' -Direction Inbound -Action Allow -Protocol <proto> -LocalPort <port> -Profile <zone>` |
| Remove port | `Remove-NetFirewallRule -DisplayName 'automojo-port-<port>-<proto>'` |
| Add rich rule | `New-NetFirewallRule <rule> -Profile <zone>` (rule is splatted parameters including `-DisplayName`) |
| Remove rich rule | `Remove-NetFirewallRule -DisplayName '<name>'` where `<name>` is parsed from the rule string |
| Set default zone | (no command — always `SKIPPED` or `FAILED`) |
| Reload | (no command — always `SUCCESS`) |

All write commands pass `elevated=True` when the config enables it.

### Read Commands (no elevation)

| Operation | PowerShell script |
|---|---|
| Query one zone | `Get-NetFirewallRule \| Where-Object { $_.Profile -match '<zone>' -or $_.Profile -eq 'Any' } \| ForEach-Object { … summary object … } \| ConvertTo-Json -Compress -Depth 5` |
| Query profile state | `Get-NetFirewallProfile \| ConvertTo-Json -Compress -Depth 5` |
| Query active network category | `Get-NetConnectionProfile \| ConvertTo-Json -Compress -Depth 5` |

Per the contract, reads MUST NOT pass `elevated=True`.  Most `Get-*` cmdlets in the
`NetSecurity` module succeed for non-elevated users; if a specific field is redacted
for non-admin sessions, the manager surfaces it as `None` / `""` in the parsed
`WindowsZoneInfo`.

---

## `Get-NetFirewallRule | ConvertTo-Json` Output Parsing

The read script emits a JSON array of objects.  To keep the output compact and stable
across Windows editions, the manager pipes each rule through
`ForEach-Object { [pscustomobject]@{ DisplayName=$_.DisplayName; Enabled=$_.Enabled;
Direction=$_.Direction; Action=$_.Action; Profile=$_.Profile; Protocol=(Get-
NetFirewallPortFilter -AssociatedNetFirewallRule $_).Protocol; LocalPort=(Get-
NetFirewallPortFilter -AssociatedNetFirewallRule $_).LocalPort;
RemoteAddress=(Get-NetFirewallAddressFilter -AssociatedNetFirewallRule
$_).RemoteAddress; InterfaceAlias=(Get-NetFirewallInterfaceFilter
-AssociatedNetFirewallRule $_).InterfaceAlias } }` before `ConvertTo-Json`.

A representative response:

```json
[
  {"DisplayName":"automojo-svc-ssh","Enabled":"True","Direction":"Inbound",
   "Action":"Allow","Profile":"Public","Protocol":"TCP","LocalPort":"22",
   "RemoteAddress":"Any","InterfaceAlias":null},
  {"DisplayName":"automojo-port-8080-tcp","Enabled":"True","Direction":"Inbound",
   "Action":"Allow","Profile":"Public","Protocol":"TCP","LocalPort":"8080",
   "RemoteAddress":"Any","InterfaceAlias":null},
  {"DisplayName":"internal-db","Enabled":"True","Direction":"Inbound",
   "Action":"Allow","Profile":"Private","Protocol":"TCP","LocalPort":"5432",
   "RemoteAddress":"10.0.0.0/8","InterfaceAlias":null}
]
```

The parser processes this JSON as follows:

| `DisplayName` pattern | Maps to `WindowsZoneInfo` field |
|---|---|
| `automojo-svc-<service>` | `services` gets `<service>` |
| `automojo-port-<port>-<protocol>` | `ports` gets `"<port>/<protocol>"` |
| anything else | `rich_rules` gets a reconstructed splatted-parameters string |

The parser:

1. Parses the JSON.  If the top-level value is a dict (PowerShell collapses single-item
   arrays), it wraps it into a one-element list before iterating.
2. For each entry, lower-cases the `Protocol` before emitting to the `ports` list.
3. Populates `interfaces` from non-null `InterfaceAlias` values (de-duplicated).
4. Populates `sources` from non-`Any` `RemoteAddress` values (de-duplicated).
5. Reconstructs rich rules as:
   `"-DisplayName '<name>' -Direction <dir> -Action <action> -Protocol <proto>
   -LocalPort <port> -RemoteAddress <addr>"` (omitting clauses with default / null
   values).

---

## `Get-NetFirewallProfile | ConvertTo-Json` Output Parsing

A representative response:

```json
[
  {"Name":"Domain","Enabled":"True","DefaultInboundAction":"Block"},
  {"Name":"Private","Enabled":"True","DefaultInboundAction":"Block"},
  {"Name":"Public","Enabled":"True","DefaultInboundAction":"Block"}
]
```

The parser extracts the `Enabled` field for each of the three profiles.  `running` is
`True` iff all three are `Enabled=True`.

## `Get-NetConnectionProfile | ConvertTo-Json` Output Parsing

A representative response:

```json
[
  {"InterfaceAlias":"Ethernet0","NetworkCategory":"Private"}
]
```

The parser extracts the first `NetworkCategory` value as the active profile.  If the
JSON is a single object rather than an array, it is wrapped into a one-element list.

---

## Idempotency Guards

All idempotency checks query `Get-NetFirewallRule` once, filtered to the target
profile, and compare against the expected `DisplayName`.

| Method | Guard | Field inspected |
|---|---|---|
| `add_service()` | Rule with `DisplayName=automojo-svc-<service>` already exists in profile | `WindowsZoneInfo.services` |
| `remove_service()` | Rule with that name does not exist | `WindowsZoneInfo.services` |
| `add_port()` | Rule with `DisplayName=automojo-port-<port>-<proto>` exists | `WindowsZoneInfo.ports` |
| `remove_port()` | Rule with that name does not exist | `WindowsZoneInfo.ports` |
| `add_rich_rule()` | Rule with the parsed `DisplayName` already exists | `WindowsZoneInfo.rich_rules` |
| `remove_rich_rule()` | Rule with that `DisplayName` does not exist | `WindowsZoneInfo.rich_rules` |
| `set_default_zone()` | N/A — always `SKIPPED` or `FAILED` | — |

`reload()` has no idempotency guard — it is a contract-mandated no-op returning
`SUCCESS`.

---

## Rich Rule Comparison

Windows rules are uniquely identified by `DisplayName`.  The idempotency check is
therefore an exact `DisplayName` equality test rather than a whitespace-normalised
substring match.  The manager MUST parse `-DisplayName '<name>'` from the rule string
using a regex that handles single-quoted and double-quoted forms.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `WindowsFirewallQueryError` | `query_zone()`, `query_all_zones()`, or the profile-query path of `query_state()` returns a non-zero exit code |

`Get-NetFirewallProfile` returning non-zero in `query_state()` is **not** an error —
it means Windows Firewall service is unavailable, which is surfaced as
`WindowsFirewallState(running=False, default_zone="")`.

All other failures from write operations are surfaced as
`WindowsFirewallActionResult` with `status=FAILED`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Service Resolution

*For any* known service name, `add_service()` and `remove_service()` should consult the bundled services table and operate on the resolved `(port, protocol)` pair.

**Validates: Requirements 1.1, 2.1**

### Property 2: Service Addition Command Construction

*For any* resolved `(port, protocol)` and valid profile `zone`, `add_service()` should dispatch a PowerShell `New-NetFirewallRule` command with `-DisplayName 'automojo-svc-<service>'`, `-Direction Inbound`, `-Action Allow`, `-Protocol <protocol>`, `-LocalPort <port>`, and `-Profile <zone>`.

**Validates: Requirements 1.1**

### Property 3: Unknown Service Handling

*For any* service name not found in the bundled services table, `add_service()` should return `FAILED` without invoking the runner.

**Validates: Requirements 1.2**

### Property 4: Invalid Profile Handling

*For any* value of `zone` not in `{Domain, Private, Public, Any}`, any write verb should return `FAILED` without invoking the runner.

**Validates: Requirements 1.3, 2.2, 3.2, 4.2, 5.3, 6.3, 7.2**

### Property 5: Service Addition Idempotency

*For any* service whose DisplayName already exists in the target profile, `add_service()` should return `SKIPPED` without invoking the runner for the write command.

**Validates: Requirements 1.4**

### Property 6: Service Removal Command Construction

*For any* service name and valid profile `zone`, `remove_service()` should dispatch `Remove-NetFirewallRule -DisplayName 'automojo-svc-<service>'`.

**Validates: Requirements 2.1**

### Property 7: Service Removal Idempotency

*For any* service whose DisplayName does not exist in the target profile, `remove_service()` should return `SKIPPED` without invoking the runner.

**Validates: Requirements 2.3**

### Property 8: Port Addition Command Construction

*For any* `(port, protocol, zone)`, `add_port()` should dispatch `New-NetFirewallRule -DisplayName 'automojo-port-<port>-<protocol>' -Direction Inbound -Action Allow -Protocol <protocol> -LocalPort <port> -Profile <zone>`.

**Validates: Requirements 3.1**

### Property 9: Port Addition Idempotency

*For any* port rule whose DisplayName already exists in the target profile, `add_port()` should return `SKIPPED` without invoking the runner.

**Validates: Requirements 3.3**

### Property 10: Port Removal Command Construction

*For any* `(port, protocol)`, `remove_port()` should dispatch `Remove-NetFirewallRule -DisplayName 'automojo-port-<port>-<protocol>'`.

**Validates: Requirements 4.1**

### Property 11: Port Removal Idempotency

*For any* port rule whose DisplayName does not exist in the target profile, `remove_port()` should return `SKIPPED` without invoking the runner.

**Validates: Requirements 4.3**

### Property 12: Rich Rule Addition Command Construction

*For any* rule string containing `-DisplayName '<name>'` and a valid profile `zone`, `add_rich_rule()` should dispatch `New-NetFirewallRule <rule> -Profile <zone>`.

**Validates: Requirements 5.1**

### Property 13: Rich Rule DisplayName Required

*For any* rule string that does NOT contain a `-DisplayName` clause, `add_rich_rule()` should return `FAILED` without invoking the runner.

**Validates: Requirements 5.2**

### Property 14: Rich Rule Addition Idempotency

*For any* rule whose parsed DisplayName already exists in the target profile, `add_rich_rule()` should return `SKIPPED` without invoking the runner.

**Validates: Requirements 5.4**

### Property 15: Rich Rule Removal Command Construction

*For any* rule string containing `-DisplayName '<name>'`, `remove_rich_rule()` should dispatch `Remove-NetFirewallRule -DisplayName '<name>'`.

**Validates: Requirements 6.1**

### Property 16: Rich Rule Removal Idempotency

*For any* rule whose parsed DisplayName does not exist in the target profile, `remove_rich_rule()` should return `SKIPPED` without invoking the runner.

**Validates: Requirements 6.4**

### Property 17: Default Zone Is No-Op for Valid Profiles

*For any* call to `set_default_zone(zone)` with `zone` in `{Domain, Private, Public, Any}`, the system should return `SKIPPED` without invoking the runner.

**Validates: Requirements 7.1**

### Property 18: Default Zone Rejects Invalid Profiles

*For any* call to `set_default_zone(zone)` with `zone` outside the valid profile set, the system should return `FAILED` without invoking the runner.

**Validates: Requirements 7.2**

### Property 19: Reload Always Succeeds

*For any* call to `reload()` in non-dry-run mode, the system should return `SUCCESS` with a detail explaining that Windows Firewall applies rules immediately, without invoking the runner.

**Validates: Requirements 8.1**

### Property 20: Zone Query Command Construction

*For any* call to `query_zone(zone)` with a valid profile, the system should dispatch a `Get-NetFirewallRule | Where-Object { $_.Profile -match '<zone>' -or $_.Profile -eq 'Any' } | … | ConvertTo-Json -Compress -Depth 5` script, without elevation.

**Validates: Requirements 9.1**

### Property 21: Zone Query Result Parsing

*For any* successful JSON output from the query script, `query_zone()` should return a `WindowsZoneInfo` with DisplayName-pattern-driven classification into services, ports, and rich_rules.

**Validates: Requirements 9.2**

### Property 22: Invalid Profile Query Raises

*For any* call to `query_zone(zone)` where `zone` is outside the valid profile set, the system should raise `WindowsFirewallQueryError`.

**Validates: Requirements 9.3**

### Property 23: Query All Zones Iteration

*For any* call to `query_all_zones()`, the system should invoke `query_zone` for each of `Domain`, `Private`, and `Public` and return the three results in order.

**Validates: Requirements 10.1**

### Property 24: Firewall State Enabled Parsing

*For any* `Get-NetFirewallProfile` output where all three profiles are `Enabled=True`, `query_state()` should set `running=True`.

**Validates: Requirements 11.2**

### Property 25: Firewall State Disabled Profile Parsing

*For any* `Get-NetFirewallProfile` output where at least one profile is `Enabled=False`, `query_state()` should set `running=False`.

**Validates: Requirements 11.3**

### Property 26: Firewall State Default Zone Parsing

*For any* `running=True` result, `query_state()` should set `default_zone` to the `NetworkCategory` from `Get-NetConnectionProfile`; when `running=False`, `default_zone=""`.

**Validates: Requirements 11.4**

### Property 27: Firewall Not Running Handling

*For any* failed `Get-NetFirewallProfile` command, `query_state()` should return `WindowsFirewallState(running=False, default_zone="")` without raising.

**Validates: Requirements 11.5**

### Property 28: Dry-Run Safety

*For any* write operation (`add_service`, `remove_service`, `add_port`, `remove_port`, `add_rich_rule`, `remove_rich_rule`, `set_default_zone`, `reload`) with `dry_run=True`, the command runner should never be invoked for write commands, and the result status should be `DRY_RUN`.

**Validates: Requirements 1.7, 2.6, 3.6, 4.6, 5.7, 6.7, 7.3, 8.2**

### Property 29: Elevation for Write Operations

*For any* write operation with `elevated=True` in the config, the command runner should be invoked with `elevated=True` for all write commands.

**Validates: Requirements 12.1**

### Property 30: No Elevation for Read Operations

*For any* read operation (`query_zone`, `query_all_zones`, `query_state`), the command runner should never be invoked with `elevated=True` regardless of the elevation configuration.

**Validates: Requirements 12.2**

### Property 31: Error Result Consistency

*For any* failed PowerShell command, the returned `WindowsFirewallActionResult` should have `status=FAILED` and contain the exit code and stderr from the command runner.

**Validates: Requirements 1.6, 2.5, 3.5, 4.5, 5.6, 6.6**

---

## Module Layout

```
source/packages/automojo/sysman/windows/
├── __init__.py
├── firewallmanagement.py         ← WindowsFirewallManager, config, models, exceptions
└── _services_table.py            ← bundled (service → port, protocol) mapping

source/testroots/testbase/
└── automojo/tests/sysman/windows/
    └── test_firewallmanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.windows.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.windows.firewallmanagement import (
    WindowsFirewallManager,
    WindowsFirewallManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())

config = WindowsFirewallManagerConfig(elevated=True, dry_run=False)
fw_mgr = WindowsFirewallManager(runner=runner, config=config)

# Add services to the Public profile
result = fw_mgr.add_service(service="ssh", zone="Public")
print(f"Add service: {result.status}")

result = fw_mgr.add_service(service="http", zone="Public")
print(f"Add service: {result.status}")

# Open custom port
port_result = fw_mgr.add_port(port=8080, protocol="tcp", zone="Public")
print(f"Add port: {port_result.status}")

# Add rich rule for specific source
rich_rule = (
    "-DisplayName 'internal-db' -Direction Inbound -Action Allow "
    "-Protocol TCP -LocalPort 5432 -RemoteAddress 10.0.0.0/8"
)
rule_result = fw_mgr.add_rich_rule(rule=rich_rule, zone="Private")
print(f"Add rich rule: {rule_result.status}")

# Reload is a documented no-op
reload_result = fw_mgr.reload()
print(f"Reload: {reload_result.status}")  # SUCCESS with explanatory detail

# Query zone state
zone_info = fw_mgr.query_zone(zone="Public")
print(f"Services: {zone_info.services}")
print(f"Ports: {zone_info.ports}")
```

### SSH Execution with Aspects

```python
from automojo.sysman.windows.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.windows.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from automojo.sysman.windows.storage import SQLiteStorage
from automojo.sysman.windows.firewallmanagement import (
    WindowsFirewallManager,
    WindowsFirewallManagerConfig,
)

ssh_config = SSHRunnerConfig(
    host="10.0.0.50",
    username="Administrator",
    key_filename="C:\\Users\\admin\\.ssh\\id_rsa",
)
runner = SSHCommandRunner(ssh_config)

storage = SQLiteStorage(path="C:\\maintenance\\commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

fw_mgr = WindowsFirewallManager(
    runner=runner,
    config=WindowsFirewallManagerConfig(elevated=True),
)

fw_mgr.add_service(service="ssh", zone="Public")
fw_mgr.add_port(port=443, protocol="tcp", zone="Public")
fw_mgr.reload()  # no-op, always SUCCESS

state = fw_mgr.query_state()
print(f"Windows Firewall running on all profiles: {state.running}")
print(f"Active profile: {state.default_zone}")
```

### Dry-Run Mode

```python
# Dry-run works identically for local and remote
config = WindowsFirewallManagerConfig(elevated=True, dry_run=True)
fw_mgr = WindowsFirewallManager(runner=runner, config=config)

result = fw_mgr.remove_service(service="ssh", zone="Public")
assert result.status == WindowsFirewallActionStatus.DRY_RUN
print("Would have removed ssh rule from Public profile")
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix.
- `Get-NetFirewallRule | ConvertTo-Json` mock responses drive all idempotency guards
  for service, port, and rich rule methods.
- `Get-NetFirewallProfile | ConvertTo-Json` and `Get-NetConnectionProfile |
  ConvertTo-Json` mocks drive `query_state()` tests, including the all-enabled,
  one-disabled, and non-zero-exit cases.
- Rich-rule `-DisplayName` parsing is tested in pure Python with representative rule
  strings, including both single-quoted and double-quoted forms.
- JSON parsing is tested against both array-shaped and single-object-shaped outputs
  (PowerShell collapses one-element arrays).
- Profile-validation is tested for every write verb with a value outside
  `{Domain, Private, Public, Any}`.
- A dedicated test proves `reload()` is a no-op that returns `SUCCESS` with the
  contract-documented detail string.
- A dedicated test proves `set_default_zone("Domain")` returns `SKIPPED` without
  invoking the runner.
- No test requires an elevated session or a live Windows Firewall instance.
