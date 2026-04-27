# Design

## Overview

The Windows remote-log-forwarding feature is implemented as a
single module,
`automojo.sysman.windows.remotelogforwarding`, exposing a
`WindowsRemoteLogForwardingManager` class that manages Windows
Event Forwarding (WEF) subscriptions through the
`wecutil.exe` CLI and a PowerShell scriptblock for the test
verb.  Every verb is dispatched through the
`ICommandRunner` abstraction.

The cross-OS contract for this feature lives in
`automojo-sysman-common/specs/contracts/remote-log-forwarding/contract.md`.
The RHEL canonical implementation lives in
`automojo-sysman-linux-redhat/specs/3-remote-log-forwarding/`.

---

## Windows Event Forwarding — The Native Model

Windows Event Forwarding (WEF) is the only Microsoft-supported
native mechanism for shipping Event Log records between Windows
hosts.  WEF runs on top of WS-Management (WinRM) and ships
native `EventLog` records — NOT syslog records.

WEF supports two collection modes:

| Mode               | Description                                          |
|--------------------|------------------------------------------------------|
| Source-Initiated   | Source hosts push events to the collector.  Subscriptions are authored on the collector via `wecutil cs <subscription.xml>`. |
| Collector-Initiated| Collector pulls events from each source on a schedule.  Subscriptions name each source explicitly.                          |

This manager implements **the collector-side configuration in
source-initiated mode**: it authors a subscription document on
the host running the manager, registers it with the local
Windows Event Collector service, and lets the source hosts
discover the subscription via Group Policy or local WinRM
configuration (out of scope here).

The on-disk subscription XML lives at
`<wec_subscription_dir>\<id>.xml`.  The default
`wec_subscription_dir` is
`C:\ProgramData\Automojo\WEC` so that the file does not collide
with operator-authored subscriptions under
`%SystemRoot%\System32\winevt\`.

---

## WEF vs. Syslog — A Critical Distinction

WEF carries events between Windows hosts only.  It does NOT
speak the syslog wire protocol.  Operators who need to ship
events to a non-Windows collector (rsyslog, syslog-ng, Splunk,
Elastic, Datadog) MUST deploy a third-party shipper such as:

| Tool           | Approach                                              |
|----------------|-------------------------------------------------------|
| Winlogbeat     | Beats-family shipper that reads from EventLog and emits to Logstash / Elasticsearch / syslog. |
| NXLog          | Windows-native shipper with `im_msvistalog` input and `om_udp` / `om_tcp` / `om_ssl` outputs. |
| Splunk UF      | Splunk Universal Forwarder reads EventLog and ships to Splunk indexers.                  |

This module exists for the WEF use case (collector-side
subscription authoring) and does NOT attempt to bridge the
syslog gap.  The module docstring repeats this limitation so
that callers see it in `help()` output.

---

## Architecture

```
+-------------------------------------------------------------+
|            Maintenance Coordinator                          |
+-----------------------+-------------------------------------+
                        |
                        v
+-------------------------------------------------------------+
|      WindowsRemoteLogForwardingManager                      |
|                                                             |
|  Write verbs (idempotent + dry-run aware):                  |
|    add_forwarder(spec)                                      |
|    remove_forwarder(id)                                     |
|    enable_forwarder(id)                                     |
|    disable_forwarder(id)                                    |
|                                                             |
|  Probe verbs:                                               |
|    test_forwarder(id)                                       |
|                                                             |
|  Read verbs:                                                |
|    list_forwarders()                                        |
|    get_forwarder_status(id)                                 |
+--------------------------+----------------------------------+
                           |
                           v
                +----------+-----------+
                |     ICommandRunner   |
                +----------+-----------+
                           |
                ___________|___________
                |                       |
                v                       v
   +-------------------+    +---------------------+
   | LocalCommandRunner|    | SSHCommandRunner    |
   +---------+---------+    +-----------+---------+
              |                          |
              |     aspects compose here |
              +------------+-------------+
                           |
                           v
        +-----------------------------------+
        | PersistenceAspect, RetryAspect,   |
        | TimeoutAspect (transparent)       |
        +-----------------------------------+
```

---

## Forwarding Model — Concept Mapping

The cross-OS contract uses an abstract
`(destination_host, destination_port, transport, query)` tuple.
Windows maps it to a WEF subscription document:

| Abstract field          | WEF rendering                                |
|-------------------------|----------------------------------------------|
| `destination_host`      | `<Address>` element (typically `localhost`,  |
|                         | because the collector hosts the subscription)|
| `destination_port`      | Selects the `TransportName` (5985 -> HTTP,   |
|                         | 5986 -> HTTPS).                              |
| `transport`             | `HTTP` or `HTTPS` element value.             |
| `query`                 | XPath query embedded in `<Query>` element.   |

Compared with the Ubuntu and macOS implementations:

| OS      | Native concept                          | Notes                                   |
|---------|-----------------------------------------|-----------------------------------------|
| Ubuntu  | rsyslog drop-in under `/etc/rsyslog.d/` | Full TCP / UDP / TLS support.           |
| macOS   | `/etc/syslog.conf` legacy syslogd       | UDP-only, legacy-syslog subset.         |
| Windows | WEF subscription via `wecutil`          | Windows-to-Windows only.                |
| RHEL    | rsyslog (canonical)                     | Reference implementation.               |

---

## Configuration

```python
@dataclass
class WindowsForwarderManagerConfig(FeatureConfig):
    powershell_executable: str = "powershell.exe"
    wecutil_executable: str    = "wecutil.exe"
    wec_subscription_dir: str  = r"C:\ProgramData\Automojo\WEC"
    test_event_source: str     = "automojo-test"
    test_event_id: int         = 4242
```

`dry_run` and `elevated` are inherited from `FeatureConfig`.

---

## Data Models

```python
class WindowsForwarderActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class WindowsForwarderActionResult(FeatureActionResult):
    forwarder_id: str | None = None
    path:         str | None = None


@dataclass
class WindowsForwarderSpec:
    id:        str
    host:      str
    port:      int    = 5985
    protocol:  str    = "http"   # "http" | "https"
    query:     str    = "*"
    description: str = ""
    log_file:  str    = "ForwardedEvents"
    enabled:   bool   = True
    tls_ca_cert: str | None = None  # accepted for parity


@dataclass
class WindowsForwarderInfo:
    id:                 str
    host:               str
    port:               int
    protocol:           str
    enabled:            bool
    path:               str | None
    selector_or_query:  str | None


@dataclass
class WindowsForwarderStatus:
    id:              str
    enabled:         bool
    last_event_time: str | None = None
    error_count:     int | None = None
    bytes_sent:      int | None = None


@dataclass
class WindowsForwarderTestResult(FeatureActionResult):
    forwarder_id:    str = ""
    test_message_id: str | None = None


class WindowsForwarderQueryError(Exception):
    """Read-side failure that is not 'absent'."""
```

---

## Subscription XML Rendering

The renderer is a pure function: `_render_subscription_xml(spec,
config)`.  It produces a deterministic XML document so that the
idempotency comparison works.

```xml
<Subscription xmlns="http://schemas.microsoft.com/2006/03/windows/events/subscription">
    <SubscriptionId>automojo-fwd-collector1</SubscriptionId>
    <SubscriptionType>SourceInitiated</SubscriptionType>
    <Description>automojo collector1</Description>
    <Enabled>true</Enabled>
    <Uri>http://schemas.microsoft.com/wbem/wsman/1/windows/EventLog</Uri>
    <ConfigurationMode>Custom</ConfigurationMode>
    <Query>
        &lt;QueryList&gt;
            &lt;Query Id="0"&gt;
                &lt;Select Path="*"&gt;*&lt;/Select&gt;
            &lt;/Query&gt;
        &lt;/QueryList&gt;
    </Query>
    <ReadExistingEvents>false</ReadExistingEvents>
    <TransportName>HTTP</TransportName>
    <LogFile>ForwardedEvents</LogFile>
    <ContentFormat>RenderedText</ContentFormat>
</Subscription>
```

The XPath query is embedded inside the `<Query>` element with
the surrounding `<QueryList>` markup.  Operators who need
finer-grained queries (e.g. `*[System[(Level=1 or Level=2)]]`)
pass the inner XPath via `WindowsForwarderSpec.query`.

---

## Key Method Designs

### `add_forwarder(spec)`

1. Validate the spec.  When `spec.host` or `spec.id` is empty,
   return `FAILED` with a stderr that names the missing field.
2. Render the desired XML via `_render_subscription_xml`.
3. Build the on-disk path
   `<wec_subscription_dir>\<id>.xml`.
4. Read the existing on-disk XML via PowerShell `Get-Content`.
5. When the existing XML matches the rendered desired XML
   byte-for-byte → `SKIPPED`.
6. When `config.dry_run` → `DRY_RUN`.
7. Dispatch a PowerShell scriptblock that creates the
   subscription directory (if missing) and writes the XML via
   `Set-Content -Encoding UTF8`.
8. On non-zero exit → return `FAILED`.
9. Dispatch `wecutil.exe cs <path>` to register the
   subscription.
10. On non-zero exit → return `FAILED`.

### `remove_forwarder(id)`

1. Probe registration via `wecutil.exe gs <id>`.
2. When the subscription is not registered AND the on-disk XML
   is missing → `SKIPPED`.
3. When `config.dry_run` → `DRY_RUN`.
4. Dispatch `wecutil.exe ds <id>` to delete the subscription.
5. Dispatch a PowerShell `Remove-Item` to delete the on-disk
   XML, when present.

### `enable_forwarder(id)` / `disable_forwarder(id)`

1. Probe registration via `wecutil.exe gs <id>`.
2. When the requested state already matches the registered
   state → `SKIPPED`.
3. When `config.dry_run` → `DRY_RUN`.
4. Dispatch `wecutil.exe ss <id> /e:true|false`.

### `list_forwarders()`

1. Dispatch `wecutil.exe es` without elevation.
2. For each subscription name in the listing, dispatch
   `wecutil.exe gs <id>` and parse `Enabled`,
   `TransportName`, `Query`, and the address from the formatted
   output.
3. Return a list of `WindowsForwarderInfo`.
4. Failures other than the listing simply being empty raise
   `WindowsForwarderQueryError`.

### `test_forwarder(id)`

1. Build the test message ID:
   `forwarder-test-<int(time.time())>`.
2. Dispatch a PowerShell scriptblock:
   ```powershell
   $src = 'automojo-test'
   if (-not [System.Diagnostics.EventLog]::SourceExists($src)) {
       New-EventLog -LogName Application -Source $src
   }
   Write-EventLog -LogName Application -Source $src `
       -EventId 4242 -EntryType Information `
       -Message 'forwarder-test-<timestamp>'
   ```
3. Wrap the runner result into `WindowsForwarderTestResult`.
4. Never raises on dispatch failure.

### `get_forwarder_status(id)`

1. Dispatch `wecutil.exe gs <id>` without elevation.
2. When the subscription is not registered (exit code != 0
   plus stderr containing the standard "subscription not
   found" message) → return a status whose `enabled` is
   `False`.
3. Otherwise parse `Enabled` from the formatted output.
4. `last_event_time`, `error_count`, and `bytes_sent` are
   parsed from the `RuntimeStatus` block when present;
   otherwise `None`.

---

## PowerShell Scriptblock Conventions

Every PowerShell dispatch uses the
`["powershell.exe", "-NoProfile", "-NonInteractive",
"-Command", "<script>"]` argv shape so that:

- `$PROFILE` cannot mutate cmdlet behaviour.
- PowerShell never blocks waiting for interactive input.
- The same argv works under PowerShell 5.1 and PowerShell 7+.

Single quotes inside scriptblocks are escaped via the standard
PowerShell `''` doubling rule before interpolation.

---

## Dry-Run Behaviour

Dry-run disables every mutating dispatch (`Set-Content`,
`Remove-Item`, `wecutil cs|ds|ss`).  Reads (`wecutil es`,
`wecutil gs`, `Get-Content`) and the
`Write-EventLog`-based probe run unconditionally.  Dry-run
results report the would-have-been argv on the result's
`command` field.

---

## Idempotency

| Verb                    | Idempotency rule                                     |
|-------------------------|------------------------------------------------------|
| `add_forwarder`         | SKIP when on-disk XML matches rendered XML.          |
| `remove_forwarder`      | SKIP when subscription not registered + no on-disk XML. |
| `enable_forwarder`      | SKIP when already enabled.                           |
| `disable_forwarder`     | SKIP when already disabled.                          |
| `test_forwarder`        | No idempotency guard.                                |
| `list_forwarders`       | n/a (read).                                          |
| `get_forwarder_status`  | n/a (read).                                          |

---

## Elevation

| Situation                                  | Behaviour                          |
|--------------------------------------------|------------------------------------|
| `elevated=True`, write verb                | Pass `elevated=True` to runner.    |
| `elevated=True`, read verb                 | Do NOT elevate.                    |
| `elevated=False`                           | Never elevate.                     |
| `test_forwarder`                           | Never elevates (read-class verb).  |

---

## Correctness Properties

1. **Deterministic XML.** The renderer is a pure function of
   the spec; identical inputs produce identical outputs.
2. **Byte-stable idempotency.** `add_forwarder` compares the
   rendered XML to the on-disk XML as raw bytes.
3. **Reads never elevate.** Every parsing path goes through
   `wecutil es|gs` or `Get-Content` without `elevated=True`.
4. **Probe never raises.** `test_forwarder` wraps every failure
   in the result record.

---

## Out of Scope (Documented Limitations)

The following are NOT implemented:

- **Syslog-protocol forwarding.** WEF is Windows-to-Windows
  only.  Use Winlogbeat / NXLog for syslog.
- **Source-side WinRM listener configuration.** Configuring the
  WinRM listener on each source host is delegated to Group
  Policy / Ansible / DSC and is outside this module's scope.
- **Collector-Initiated mode.** Only Source-Initiated mode is
  rendered.
- **Custom QueryList composition with multiple `<Select>`
  elements.** The renderer accepts a single XPath via
  `spec.query`.
- **Channel ACL adjustments via `wevtutil sl <channel>
  /ca:...`.** Out of scope.
- **`RuntimeStatus` parsing for full
  `last_event_time` / `error_count` / `bytes_sent` coverage.**
  Best-effort parser only.

---

## Testing Strategy

- `_MockCommandRunner` records every call and returns
  pre-seeded `RunResult` values keyed by argv tuple.
- One command-construction test per write verb covers the
  primary argv shape.
- One idempotency test per write verb proves `SKIPPED` when
  the registered state already matches the requested state.
- One dry-run test per write verb proves the runner is not
  invoked beyond the read-side probes.
- One `list_forwarders` parser test covers a multi-subscription
  listing with mixed enabled / disabled subscriptions.
- One subscription-XML generation test asserts the rendered
  document includes the required schema attribute and the
  `Subscription`, `Enabled`, `TransportName`, and
  `Query` elements.
- One reload-failure test proves the mutating verb returns
  `FAILED` when `wecutil.exe cs` fails.
- One elevation test proves writes elevate and reads do not.
- One `test_forwarder` test asserts the PowerShell argv shape.

---

## Spec-vs-Contract Drift

| Drift point                                | Resolution                                          |
|--------------------------------------------|-----------------------------------------------------|
| Contract uses `ForwarderProbeResult` (reachability + latency) | The pilot returns `WindowsForwarderTestResult` because `Write-EventLog` does NOT round-trip — there is no probe to time. |
| Contract `ForwarderTransport` enum         | The pilot uses a `protocol: str` field with values `"http"` / `"https"` (matching WEF transports) plus `"tcp"` / `"udp"` accepted-but-ignored tokens for cross-OS parity. |
| Contract `selector` (Linux concept)        | The pilot exposes `query` (XPath) instead, matching the Windows native filter language.  `WindowsForwarderInfo.selector_or_query` carries the XPath. |
| Contract `last_sent_at` / `queue_depth` on `ForwarderInfo` | Moved onto `WindowsForwarderStatus` and parsed best-effort from the `wecutil gs` `RuntimeStatus` block. |
| Contract destination is the collector      | WEF's `<Address>` element actually names the local collector; the cross-OS `destination_host` model bends to fit. |

The drift is intentional and documented; the cross-OS contract
recognises that WEF's data model differs structurally from
syslog.

---

## Seams With Other Features

| Seam                                | Owner                                | This Feature        |
|-------------------------------------|--------------------------------------|---------------------|
| Local Event-Log surface             | `3-log-management`                   | Out of scope here.  |
| TLS material                        | `3-certificate-issuance` / trust-mgmt| Caller installs.    |
| Windows Event Collector service     | `3-service-management`               | Out of scope here.  |
| WinRM listener configuration        | n/a (Group Policy / DSC)             | Out of scope here.  |
| Third-party shipper installation    | n/a (operator action)                | Out of scope here.  |
