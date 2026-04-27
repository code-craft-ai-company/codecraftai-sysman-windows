# Design

## Overview

The precision-time-management feature is implemented as a single
module,
`automojo.sysman.windows.precisiontimemanagement`, exposing a
`WindowsPrecisionTimeManager` class that wraps `w32tm.exe` (the
Windows Time Service utility) plus `net.exe` and `sc.exe` for the
``W32Time`` service lifecycle.  All external calls flow through the
`ICommandRunner` abstraction.

The manager follows the locked-in pattern of every Layer-3 manager
in this package:

* A `BaseFeatureManager` subclass that ferries the canonical fields
  (`runner`, `config`) onto `self`.
* A feature-prefixed status enum, action result, and config
  dataclass vendored on top of the package's `featurebase`.
* OS-prefixed types throughout (`Windows*`).
* No live filesystem access.  Persistent configuration lives in the
  registry under
  `HKLM\SYSTEM\CurrentControlSet\Services\W32Time` and is mutated
  through `w32tm /config /update` rather than direct registry
  manipulation.

**See Also:**
- `3-network-configuration` — companion infrastructure feature.
- `automojo-sysman-linux-redhat/specs/3-precision-time-management/`
  — the RHEL reference, which targets the IEEE 1588 PTP surface and
  is not applicable to Windows.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsPrecisionTimeManager                      │
│                                                             │
│  Reads:                                                     │
│    get_time_status(), get_time_sources(), get_drift()       │
│  Writes:                                                    │
│    add_time_source(), remove_time_source(),                 │
│    set_time_servers(), force_sync_now(),                    │
│    enable_sync(), disable_sync()                            │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
       ┌─────────────────────┐
       │   ICommandRunner    │
       └─────────────────────┘
                 │
   ┌─────────────┼──────────────────┐
   ▼             ▼                  ▼
 w32tm.exe     net.exe           sc.exe
(query/         (start/           (query/
 config/         stop)             config)
 stripchart/
 resync)
```

---

## Configuration

```python
@dataclass
class WindowsPrecisionTimeManagerConfig(FeatureConfig):
    service_name: str = "w32time"
```

`dry_run` and `elevated` are inherited from `FeatureConfig`.
`service_name` is overridable for air-gapped or specialised builds
that target a non-default unit.

---

## Verb-to-Tool Mapping

| Verb | Command |
|------|---------|
| `get_time_status` | `w32tm /query /status /verbose` |
| `get_time_sources` | `w32tm /query /peers` |
| `add_time_source` | recompute peers, `w32tm /config /manualpeerlist:"<list>" /syncfromflags:manual /update`, `net stop`, `net start` |
| `remove_time_source` | recompute peers, same `/config /update` + service-bounce |
| `set_time_servers` | `w32tm /config /manualpeerlist:"<list>" /syncfromflags:manual /update` + service-bounce |
| `force_sync_now` | `w32tm /resync /force` |
| `get_drift` | read current peer, `w32tm /stripchart /computer:<server> /samples:1 /dataonly` |
| `enable_sync` | `sc config <service> start= auto` then `net start <service>` |
| `disable_sync` | `net stop <service>` then `sc config <service> start= disabled` |

The manager never invokes a subprocess directly; every external
call routes through the runner.

---

## Data Models

```python
WindowsPrecisionTimeActionStatus = ActionStatus  # alias


@dataclass
class WindowsPrecisionTimeActionResult(FeatureActionResult):
    server: Optional[str] = None


@dataclass
class WindowsTimeStatus:
    enabled: bool
    synchronized: bool
    current_server: Optional[str]
    stratum: Optional[int]
    last_sync: Optional[str]
    offset_seconds: Optional[float]


@dataclass
class WindowsTimeSourceInfo:
    server: str
    state: str                          # 'selected' | 'candidate' | 'unreachable'
    stratum: Optional[int]
    offset_seconds: Optional[float]
    last_response: Optional[str]


@dataclass
class WindowsTimeDrift:
    offset_seconds: float
    frequency_ppm: Optional[float]
    jitter_seconds: Optional[float]
    stratum: Optional[int]


class WindowsPrecisionTimeQueryError(Exception):
    ...
```

`WindowsPrecisionTimeActionResult` extends the canonical
`FeatureActionResult` with a single new field, `server`, that
records the target of `add_time_source` / `remove_time_source`.
Verbs that do not target a single server populate `server=None`.

The contract's abstract `TimeStatus.sync_service_active` is
collapsed into `enabled`; both names denote the same property — "the
W32Time service is running".

`frequency_ppm` and `jitter_seconds` on `WindowsTimeDrift` are
always `None` — Windows does not expose those values through the
`w32tm` user-space surface.

---

## Key Method Designs

### `get_time_status()`

1. Dispatch `w32tm /query /status /verbose` (no elevation).
2. On non-zero exit AND a known inactive-service stderr signature
   (``0x80070426``, ``service has not been started``,
   ``service is not running``, ``could not connect to``), return
   `WindowsTimeStatus(enabled=False, synchronized=False, ...)`.
3. On non-zero exit otherwise, raise
   `WindowsPrecisionTimeQueryError`.
4. On success, parse the keyed-line payload and extract `Source`,
   `Stratum` (leading integer token), `Last Successful Sync Time`
   (treat ``unspecified`` as `None`), and `Phase Offset` (parse the
   trailing ``s`` suffix).
5. Mark `synchronized=True` only when both `last_sync` and a
   positive `stratum` are present.

### `get_time_sources()`

1. Dispatch `w32tm /query /peers` (no elevation).
2. On inactive-service stderr, return the empty list.
3. On non-zero exit otherwise, raise.
4. Split the payload on blank lines into stanzas; for each stanza
   parse `Peer:` (strip the trailing ``,0xFLAG`` token), `State:`
   (classify onto selected/candidate/unreachable), `Stratum:`
   (leading integer token), and
   `Last Successful Sync Time:` (echoed as `last_response`).

### `add_time_source(server)`

1. Compute the would-have-been `/config /update` command.
2. Dry-run short-circuit.
3. Probe the current peer list via `w32tm /query /peers`.
4. If `server` is already in the list, return `SKIPPED`.
5. Render the new peer list space-joined and dispatch
   `w32tm /config /manualpeerlist:"<rendered>"
   /syncfromflags:manual /update`.
6. Bounce the service via `net stop <service>` and
   `net start <service>`.  ``net stop`` is permitted to fail (the
   service may already be stopped); ``net start`` failure surfaces
   as the verb's `FAILED` outcome.

### `remove_time_source(server)` and `set_time_servers(servers)`

Same shape with the appropriate idempotency guard.

### `force_sync_now()`

* Dispatch `w32tm /resync /force` honouring `config.elevated`.
* No idempotency guard — the verb is operational.

### `get_drift()`

1. Read `get_time_status()`.
2. If no current peer is reported, return a zero-value
   `WindowsTimeDrift` rather than raise.
3. Dispatch
   `w32tm /stripchart /computer:<peer> /samples:1 /dataonly` (no
   elevation).
4. Parse the trailing ``±<num>s`` token off the first matching
   data line.

### `enable_sync()` / `disable_sync()`

* `enable_sync`: probe `sc query <service>`; if RUNNING, return
  `SKIPPED`.  Otherwise `sc config <service> start= auto`, then
  `net start <service>`.  A failed `sc config` step short-circuits
  before `net start`.
* `disable_sync`: probe `sc query <service>`; if not RUNNING,
  return `SKIPPED`.  Otherwise `net stop <service>`, then
  `sc config <service> start= disabled`.  A failed `net stop`
  short-circuits before `sc config`.

---

## File-I/O Discipline

* All external calls route through the runner.  No registry
  manipulation, no PowerShell.  Persistent configuration lives in
  the registry but is mutated exclusively through
  `w32tm /config /update`.

---

## Dry-Run Behaviour

Every write verb honours `config.dry_run`.  Dry-run returns
`status=DRY_RUN` and records the would-have-been command on
`result.command` without invoking the runner.  Dry-run does NOT
short-circuit reads.

---

## Elevation Behaviour

* Reads (`w32tm /query /status`, `w32tm /query /peers`,
  `w32tm /stripchart`, `sc query <service>`) MUST always pass
  `elevated=False`.
* Writes (`w32tm /config /update`, `w32tm /resync /force`,
  `net start`, `net stop`, `sc config`) MUST honour
  `config.elevated`.

---

## Correctness Properties

1. **Idempotency.** `add_time_source` / `remove_time_source` /
   `set_time_servers` / `enable_sync` / `disable_sync` SKIP when
   the live state already matches the requested state.
2. **`force_sync_now` is operational.**  No idempotency guard.
3. **Inactive-service resilience.** `get_time_status` and
   `get_time_sources` degrade to inactive / empty snapshots when
   stderr matches the known "service not started" signatures.
4. **No subprocess.** The manager never constructs its own
   subprocess; every external call routes through the runner.
5. **`frequency_ppm` / `jitter_seconds` honesty.** Always `None` —
   Windows does not surface these values through `w32tm`.

---

## Testing Strategy

- A `_MockCommandRunner` records every `run` call and returns
  pre-seeded `RunResult` values keyed by argv tuple.
- Parser tests for `w32tm /query /status /verbose`,
  `w32tm /query /peers`, and `w32tm /stripchart /dataonly`.
- Idempotency tests for `add_time_source` / `remove_time_source` /
  `set_time_servers` driven by pre-seeded `w32tm /query /peers`
  output.
- Idempotency tests for `enable_sync` / `disable_sync` driven by
  pre-seeded `sc query` output.
- Dry-run tests proving the runner is never invoked.
- Elevation policy tests proving every write call passes
  `elevated=True` and every read call passes `elevated=False`.
- Inactive-service tests proving `get_time_status` returns
  `enabled=False` rather than raising on the
  ``0x80070426`` signature.
- A "failed command surfaces stderr" test proving the captured
  exit code, stderr, and command appear on the result.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| W32Time service lifecycle | `3-service-management` (canonical) | `enable_sync` / `disable_sync` invoke `net.exe` and `sc.exe` directly so this verb works without a separate ServiceManager |
| Timezone | `3-localization-management` (future) | Disjoint surface |
| High-accuracy / PTP-mode tuning | Out of scope | Not addressed |

---

## Spec-vs-Contract Drift

* **`sync_service_active` collapsed.** The contract's
  `TimeStatus.sync_service_active` is collapsed into `enabled` on
  `WindowsTimeStatus`; both names denote the same property — "the
  W32Time service is running".
* **`last_sync_time` shape.** The contract types
  `last_sync_time: datetime`.  This implementation ships
  `last_sync: Optional[str]` because `w32tm` emits a localized
  human-formatted timestamp; round-tripping into `datetime` would
  force locale and timezone assumptions.  The string is the native
  `w32tm` payload.
* **`stratum` and `offset_seconds` on `WindowsTimeStatus`.** The
  contract does not name `offset_seconds` on the status record; the
  Windows type adds it because `w32tm /query /status` exposes a
  ``Phase Offset`` field cheaply.
* **`WindowsTimeSourceInfo` extends the contract.** Adds `stratum`
  and `last_response` because `w32tm /query /peers` reports them.
* **`WindowsTimeDrift` field naming.** The contract's
  `drift_ppm`/`rms_offset_seconds` are renamed to
  `frequency_ppm`/`jitter_seconds`; an `offset_seconds` field is
  the primary drift measure.  `frequency_ppm` and `jitter_seconds`
  are always `None` because `w32tm` does not surface them.
* **No `IFileOperator`.** The contract permits an `IFileOperator`
  collaborator; this implementation routes every external call
  through the runner.  Persistent configuration is mutated through
  `w32tm /config /update` rather than direct registry edits.
* **Service lifecycle uses Win32 utilities.** The contract is
  agnostic; this implementation chooses `net.exe` + `sc.exe` over
  PowerShell `Start-Service` / `Set-Service` so a host without
  PowerShell still answers.
