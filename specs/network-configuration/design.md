# Design

## Overview

The network configuration feature is implemented as a single module,
`automojo.sysman.linux.windows.networkconfiguration`, that exposes a `NetworkManager` class.
The coordinator instantiates `NetworkManager` and calls its methods to perform discrete
maintenance actions across hostname, hosts file, DNS resolver, NTP/Chrony, and
NetworkManager connection management.

All system interaction is delegated to a command runner provided by `automojo-interfaces`.
File reads use `cat` through the runner; file writes use `tee` through the runner.
No direct file system access or `subprocess` calls are made inside the module.

> **Naming note:** The class is named `NetworkManager` within this package.  It wraps
> the `nmcli` CLI to interact with the system's `NetworkManager` daemon — it is not the
> daemon itself.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│          NetworkManager                 │  automojo.sysman.linux.windows.networkconfiguration
│                                         │
│  set_hostname()    query_hostname()     │
│  add_hosts_entry() remove_hosts_entry() │
│  query_hosts()                          │
│  set_nameservers() set_search_domains() │
│  add_ntp_server()  remove_ntp_server()  │
│  query_ntp_sync()                       │
│  add_connection()  remove_connection()  │
│  query_connections()                    │
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

### `NetworkManager` (class)

**Location:** `source/packages/automojo/sysman/linux/windows/networkconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `set_hostname` | `(hostname) → NetworkActionResult` | Set static hostname via hostnamectl |
| `query_hostname` | `() → HostnameInfo` | Query hostname fields via hostnamectl |
| `add_hosts_entry` | `(ip, hostnames) → NetworkActionResult` | Add or update /etc/hosts entry |
| `remove_hosts_entry` | `(ip) → NetworkActionResult` | Remove /etc/hosts line by IP |
| `query_hosts` | `() → list[HostsEntry]` | Return parsed /etc/hosts entries |
| `set_nameservers` | `(nameservers) → NetworkActionResult` | Set nameserver lines in resolv.conf |
| `set_search_domains` | `(domains) → NetworkActionResult` | Set search directive in resolv.conf |
| `add_ntp_server` | `(address) → NetworkActionResult` | Add server line to chrony.conf |
| `remove_ntp_server` | `(address) → NetworkActionResult` | Remove server line from chrony.conf |
| `query_ntp_sync` | `() → NtpSyncInfo` | Parse chronyc tracking output |
| `add_connection` | `(spec: NmConnectionSpec) → NetworkActionResult` | Add or modify NM connection |
| `remove_connection` | `(name) → NetworkActionResult` | Delete NM connection by name |
| `query_connections` | `() → list[NmConnectionInfo]` | List NM connections |

### `NetworkManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands or writes executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `hosts_path` | `str` | `"/etc/hosts"` | Path to hosts file |
| `resolv_conf_path` | `str` | `"/etc/resolv.conf"` | Path to resolver config |
| `chrony_conf_path` | `str` | `"/etc/chrony.conf"` | Path to Chrony config |

---

## Data Models

### Input Types

```python
@dataclass
class NmConnectionSpec:
    name: str
    conn_type: str              # e.g. "ethernet", "wifi", "bond"
    device: str | None = None
    ip4: str | None = None      # CIDR notation e.g. "192.168.1.10/24"
    gw4: str | None = None
    dns4: list[str] = field(default_factory=list)
    autoconnect: bool = True
```

### Result Types

```python
class NetworkActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class NetworkActionResult:
    status: NetworkActionStatus
    detail: str | None          # human-readable context (hostname, IP, connection name)
    diff: str | None            # unified diff for file-write DRY_RUN results
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Query Result Types

```python
@dataclass
class HostnameInfo:
    static_hostname: str
    transient_hostname: str
    pretty_hostname: str

@dataclass
class HostsEntry:
    ip: str
    hostnames: list[str]        # first element is primary name; rest are aliases

@dataclass
class NtpSyncInfo:
    reference_source: str
    system_time_offset_seconds: float
    is_synchronised: bool

@dataclass
class NmConnectionInfo:
    name: str
    conn_type: str
    device: str
    state: str                  # "activated" | "deactivated" | "--"
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
        stdin: str | None = None,
    ) -> RunResult: ...
```

---

## Command Mappings

### Hostname

| Operation | Command |
|---|---|
| Query hostname | `hostnamectl --json short` |
| Set hostname | `hostnamectl set-hostname <hostname>` |

### /etc/hosts

| Operation | Command |
|---|---|
| Read hosts file | `cat <hosts_path>` |
| Write hosts file | `tee <hosts_path>` with content on stdin |

### DNS Resolver

| Operation | Command |
|---|---|
| Read resolv.conf | `cat <resolv_conf_path>` |
| Write resolv.conf | `tee <resolv_conf_path>` with content on stdin |

### NTP / Chrony

| Operation | Command |
|---|---|
| Read chrony.conf | `cat <chrony_conf_path>` |
| Write chrony.conf | `tee <chrony_conf_path>` with content on stdin |
| Restart chronyd | `systemctl restart chronyd` |
| Query sync status | `chronyc tracking` |

### NetworkManager (nmcli)

| Operation | Command |
|---|---|
| List connections | `nmcli -t -f NAME,TYPE,DEVICE,STATE connection show` |
| Add connection | `nmcli connection add type <type> con-name <name> [options...]` |
| Modify connection | `nmcli connection modify <name> [options...]` |
| Activate connection | `nmcli connection up <name>` |
| Delete connection | `nmcli connection delete <name>` |

---

## File Management Strategy

All file reads use `cat <path>` through the runner.  All writes use `tee <path>` with
content passed as `stdin`.  Each write method follows the read-parse-modify-write pattern:

1. Read current content via `cat`.
2. Parse in pure Python.
3. Apply the requested mutation in memory.
4. Compare mutated content to original; return `SKIPPED` if identical.
5. Compute unified diff for `DRY_RUN` result.
6. Write mutated content via `tee` when not in dry-run mode.

### /etc/hosts Parsing

Lines are parsed into `(ip, hostnames)` tuples.  Comment lines (`#`) and blank lines are
preserved verbatim in the output so they survive a read-write cycle unchanged.

### /etc/resolv.conf Parsing

Lines are categorised by directive prefix: `nameserver`, `search`, `domain`, or other.
`set_nameservers()` replaces all `nameserver` lines while leaving `search`, `domain`, and
unknown lines intact.  `set_search_domains()` replaces the `search` line while leaving
`nameserver` and other lines intact.

### /etc/chrony.conf Parsing

Lines matching `^server\s+<address>` are identified as NTP server entries.
`add_ntp_server()` appends a `server <address> iburst` line when not already present.
`remove_ntp_server()` removes lines matching the address.  All other lines are preserved.
After a successful file write, `systemctl restart chronyd` is invoked to apply the change.

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `set_hostname()` | Current static hostname matches desired | `hostnamectl --json short` |
| `add_hosts_entry()` | Identical line already exists | `cat /etc/hosts` parse |
| `remove_hosts_entry()` | No line for that IP | `cat /etc/hosts` parse |
| `set_nameservers()` | Nameserver list identical | `cat /etc/resolv.conf` parse |
| `set_search_domains()` | Search domain list identical | `cat /etc/resolv.conf` parse |
| `add_ntp_server()` | Server address already present | `cat /etc/chrony.conf` parse |
| `remove_ntp_server()` | Server address not present | `cat /etc/chrony.conf` parse |
| `add_connection()` | Connection exists with all fields matching | `nmcli show` parse |
| `remove_connection()` | No connection with that name | `nmcli show` parse |

---

## `hostnamectl --json short` Parsing

`hostnamectl --json short` returns a JSON object.  The relevant fields are:

```json
{
  "StaticHostname": "myhost.example.com",
  "TransientHostname": "myhost",
  "PrettyHostname": ""
}
```

These are mapped directly to `HostnameInfo` fields.  Missing keys default to empty string.

## `chronyc tracking` Parsing

`chronyc tracking` produces a multi-line key-value output.  The parser extracts:
- `Reference ID` line → `reference_source`
- `System time` line → `system_time_offset_seconds` (float, direction sign applied)
- `Leap status` line → `is_synchronised` (`True` when value is `Normal`)

## `nmcli -t` Parsing

`nmcli -t -f NAME,TYPE,DEVICE,STATE connection show` produces colon-delimited lines.
Each line maps to a `NmConnectionInfo` record by splitting on `:`.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `NetworkQueryError` | `query_hostname()`, `query_hosts()`, `query_ntp_sync()`, or `query_connections()` returns an unexpected non-zero exit code |

All other failures from write operations are surfaced as `NetworkActionResult` with
`status=FAILED`.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── networkconfiguration.py       ← NetworkManager class, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_networkconfiguration.py
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix.
- `hostnamectl`, `cat /etc/hosts`, `cat /etc/resolv.conf`, `cat /etc/chrony.conf`,
  `nmcli show`, and `chronyc tracking` mock responses drive all idempotency and query logic.
- `hosts_path`, `resolv_conf_path`, and `chrony_conf_path` are overridden in config for
  all file-operation tests.
- File mutation logic (hosts, resolv.conf, chrony.conf) is tested by supplying mock `cat`
  output and verifying the `tee` stdin content.
- `chronyc tracking` parsing tests use representative multi-line output strings.
- `nmcli -t` parsing tests verify correct field splitting on colons.
- No test requires root access or a live network stack.
