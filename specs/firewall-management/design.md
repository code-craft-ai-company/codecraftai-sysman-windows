# Design

## Overview

The firewall management feature is implemented as a single module,
`automojo.sysman.linux.windows.firewallmanagement`, that exposes a `FirewallManager` class.
The coordinator instantiates `FirewallManager` and calls its methods to perform discrete
maintenance actions against the `firewalld` daemon via the `firewall-cmd` CLI.

All state queries used for idempotency guards go through `firewall-cmd --zone=<zone>
--list-all` or `firewall-cmd --list-all-zones`.  These are parsed in pure Python.  No
direct file system access or `subprocess` calls are made inside the module.

The permanent flag (`--permanent`) is always used for write operations so that changes
survive a reload or reboot.  After a batch of permanent changes the coordinator is
expected to call `reload()` to apply them to the runtime configuration.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│            FirewallManager              │  automojo.sysman.linux.windows.firewallmanagement
│                                         │
│  add_service()      remove_service()    │
│  add_port()         remove_port()       │
│  add_rich_rule()    remove_rich_rule()  │
│  set_default_zone() reload()            │
│  query_zone()       query_all_zones()   │
│  query_state()                          │
└───────────────────┬─────────────────────┘
                    │ delegates to
                    ▼
┌─────────────────────────────────────────┐
│        ICommandRunner                   │  automojo-interfaces
│  run(args, elevated=False) → RunResult  │
└─────────────────────────────────────────┘
```

---

## Components

### `FirewallManager`

**Location:** `source/packages/automojo/sysman/linux/windows/firewallmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_service` | `(service, zone) → FirewallActionResult` | Add service to zone (permanent) |
| `remove_service` | `(service, zone) → FirewallActionResult` | Remove service from zone (permanent) |
| `add_port` | `(port, protocol, zone) → FirewallActionResult` | Open port/protocol in zone (permanent) |
| `remove_port` | `(port, protocol, zone) → FirewallActionResult` | Close port/protocol in zone (permanent) |
| `add_rich_rule` | `(rule, zone) → FirewallActionResult` | Add rich rule to zone (permanent) |
| `remove_rich_rule` | `(rule, zone) → FirewallActionResult` | Remove rich rule from zone (permanent) |
| `set_default_zone` | `(zone) → FirewallActionResult` | Set the default zone |
| `reload` | `() → FirewallActionResult` | Apply permanent changes to runtime |
| `query_zone` | `(zone) → ZoneInfo` | Query services, ports, rich rules for a zone |
| `query_all_zones` | `() → list[ZoneInfo]` | Query all zones |
| `query_state` | `() → FirewallState` | Query running state and default zone |

### `FirewallManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |

---

## Data Models

### Result Types

```python
class FirewallActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class FirewallActionResult:
    status: FirewallActionStatus
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
class ZoneInfo:
    zone: str
    services: list[str]
    ports: list[str]            # each entry is "<port>/<protocol>"
    rich_rules: list[str]
    interfaces: list[str]
    sources: list[str]
    is_default: bool

@dataclass
class FirewallState:
    running: bool
    default_zone: str
```

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

---

## Command Mappings

### Write Commands (always use `--permanent`)

| Operation | Command |
|---|---|
| Add service | `firewall-cmd --zone=<zone> --add-service=<service> --permanent` |
| Remove service | `firewall-cmd --zone=<zone> --remove-service=<service> --permanent` |
| Add port | `firewall-cmd --zone=<zone> --add-port=<port>/<proto> --permanent` |
| Remove port | `firewall-cmd --zone=<zone> --remove-port=<port>/<proto> --permanent` |
| Add rich rule | `firewall-cmd --zone=<zone> --add-rich-rule=<rule> --permanent` |
| Remove rich rule | `firewall-cmd --zone=<zone> --remove-rich-rule=<rule> --permanent` |
| Set default zone | `firewall-cmd --set-default-zone=<zone>` |
| Reload | `firewall-cmd --reload` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Query one zone | `firewall-cmd --zone=<zone> --list-all` |
| Query all zones | `firewall-cmd --list-all-zones` |
| Query running state | `firewall-cmd --state` |
| Get default zone | `firewall-cmd --get-default-zone` |

---

## `firewall-cmd --list-all` Output Parsing

`firewall-cmd --zone=<zone> --list-all` produces output in the following format:

```
public (active)
  target: default
  icmp-block-inversion: no
  interfaces: eth0
  sources:
  services: dhcpv6-client ssh
  ports: 8080/tcp 9090/tcp
  protocols:
  forward: no
  masquerade: no
  forward-ports:
  source-ports:
  icmp-blocks:
  rich rules:
        rule family="ipv4" source address="10.0.0.0/8" accept
```

The parser processes this output as follows:

| Output line prefix | Maps to `ZoneInfo` field |
|---|---|
| First line (zone name) | `zone` (strip `(active)` suffix) |
| `services:` | `services` — split on whitespace |
| `ports:` | `ports` — split on whitespace |
| `rich rules:` | `rich_rules` — subsequent indented lines until next field |
| `interfaces:` | `interfaces` — split on whitespace |
| `sources:` | `sources` — split on whitespace |

`is_default` is populated by comparing the zone name to the result of
`firewall-cmd --get-default-zone` in `query_state()` or set externally by
`query_all_zones()`.

## `firewall-cmd --list-all-zones` Output Parsing

`firewall-cmd --list-all-zones` produces the same per-zone block format as `--list-all`
repeated for every configured zone, separated by blank lines.  The parser splits the
output on blank lines to isolate each zone block, then applies the same per-block parsing
as `query_zone()`.

---

## Idempotency Guards

All idempotency checks call `query_zone()` first to obtain the current `ZoneInfo`, then
compare the desired state to the current state in pure Python.

| Method | Guard | Field inspected |
|---|---|---|
| `add_service()` | Service already in zone | `ZoneInfo.services` |
| `remove_service()` | Service not in zone | `ZoneInfo.services` |
| `add_port()` | Port/proto already open | `ZoneInfo.ports` |
| `remove_port()` | Port/proto not open | `ZoneInfo.ports` |
| `add_rich_rule()` | Identical rule already present | `ZoneInfo.rich_rules` |
| `remove_rich_rule()` | Rule not present | `ZoneInfo.rich_rules` |
| `set_default_zone()` | Default zone already matches | `FirewallState.default_zone` |

`reload()` has no idempotency guard — it is always executed when not in dry-run mode.

---

## Rich Rule Comparison

Rich rule strings are compared after normalising whitespace (collapsing multiple spaces to
one, stripping leading/trailing whitespace) to avoid false mismatches from formatting
differences between what the coordinator supplies and what `firewall-cmd --list-all`
returns.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `FirewallQueryError` | `query_zone()`, `query_all_zones()`, or `query_state()` (get-default-zone path) returns a non-zero exit code |

`firewall-cmd --state` returning non-zero is **not** an error — it means firewalld is not
running, which is surfaced as `FirewallState(running=False, default_zone="")`.

All other failures from write operations are surfaced as `FirewallActionResult` with
`status=FAILED`.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── firewallmanagement.py         ← FirewallManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_firewallmanagement.py
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix.
- `firewall-cmd --zone=<zone> --list-all` mock responses drive all idempotency guards for
  service, port, and rich rule methods.
- `firewall-cmd --list-all-zones` mock responses drive `query_all_zones()` parsing tests.
- `firewall-cmd --state` and `firewall-cmd --get-default-zone` mocks drive `query_state()`
  tests including the non-running case.
- Rich rule normalisation is tested in pure Python with representative rule strings.
- `--list-all` and `--list-all-zones` parsing are tested with representative multi-line
  output strings including zones with multiple services, ports, and rich rules.
- No test requires root access or a live `firewalld` daemon.
