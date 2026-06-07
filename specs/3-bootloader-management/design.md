# Design

## Overview

The bootloader-management feature is implemented as a single
module, `ccai.sysman.windows.bootloadermanagement`, that
exposes a `WindowsBootloaderManager` class.  The maintenance
coordinator instantiates `WindowsBootloaderManager` and calls its
methods to perform discrete OS-level Boot Configuration Data
(BCD) actions: enumerating BCD entries, querying and setting the
default entry, adding and removing entries, and (no-op)
regenerate / apply hooks.

The coordinator drives all operations through a command runner
implementing the `ICommandRunner` interface.  The manager never
calls `subprocess`, `os.path`, or `open()` directly; it only
constructs argument lists and passes them to the injected runner.
This keeps business logic separate from execution and makes the
feature fully testable via mock collaborators without
Administrator access or a real BCD store.

The feature works identically whether commands execute locally
(via `LocalCommandRunner`) or remotely over PowerShell-Remoting /
SSH (via the package's remote runner).

---

## Distinction from `boot-management` (UEFI)

This is **not** the UEFI firmware boot-entry feature.  The two
features operate on different layers and on different bcdedit
namespaces:

```
+----------------------------------------------------------+
|                      Operator request                    |
+----------------------------------------------------------+
                            |
                +-----------+-----------+
                v                       v
+-------------------------+   +---------------------------+
|     boot-management     |   |  bootloader-management    |
|                         |   |                           |
|  Layer: FIRMWARE        |   |  Layer: OS-LEVEL          |
|  Tool : bcdedit /enum   |   |  Tool : bcdedit /enum     |
|         firmware,       |   |         osloader,         |
|         efibootmgr      |   |         {bootmgr}         |
|  Store: NVRAM (UEFI)    |   |  Store: BCD osloader      |
|                         |   |         entries           |
+-------------------------+   +---------------------------+
                |                       |
                +-----------+-----------+
                            v
+----------------------------------------------------------+
|                       Boot sequence                      |
|                                                          |
|  1. UEFI firmware reads NVRAM (boot-management)          |
|  2. Firmware launches the Windows Boot Manager           |
|     loader recorded in NVRAM                             |
|  3. Boot Manager reads BCD (THIS feature)                |
|  4. Boot Manager launches the OS loader recorded in BCD  |
+----------------------------------------------------------+
```

`bcdedit` shares one process for both layers; the namespace
argument decides which layer is touched.  This module never
invokes `bcdedit /enum firmware` and never mutates `{fwbootmgr}`.
The only `{fwbootmgr}`-touching call is a read-only
`bcdedit /enum {fwbootmgr}` used to merge any firmware-side entries
the operator has explicitly added (per the cross-OS contract);
this read does not affect the firmware store.

---

## Cross-OS Bootloader-Model Mapping

The cross-OS contract abstracts over a wide range of OS-level
bootloader implementations.  Windows maps onto the contract verbs
as follows:

| Cross-OS verb              | Windows native primitive                                             | Reduces to NO-OP / FAILED on Windows? |
|----------------------------|----------------------------------------------------------------------|----------------------------------------|
| `list_entries`             | `bcdedit /enum osloader` + `bcdedit /enum {fwbootmgr}` + bootmgr read | NO                                    |
| `get_default_entry`        | `bcdedit /enum {bootmgr} /v` (parse `default` line)                  | NO                                     |
| `set_default_entry`        | `bcdedit /default {<guid>}`                                          | NO                                     |
| `add_entry`                | `bcdedit /create [{<guid>}] /d ... /application <app>` + `/set`s + `/displayorder /addlast` | NO         |
| `remove_entry`             | `bcdedit /delete {<guid>}`                                           | NO                                     |
| `update_bootloader_config` | (none -- BCD writes are immediate)                                   | YES -- SUCCESS no-op                   |
| `regenerate_config`        | (none -- BCD is its own source)                                      | YES -- SUCCESS no-op                   |

Comparison with sister implementations:

| OS      | OS-level loader        | Default-entry store                  | Apply step                           | Reboot required for change? |
|---------|------------------------|--------------------------------------|--------------------------------------|------------------------------|
| RHEL    | GRUB 2                 | `grub2-editenv`                      | `grub2-mkconfig`                     | YES                          |
| Ubuntu  | GRUB 2 / systemd-boot  | `GRUB_DEFAULT` / `bootctl`           | `update-grub` / `bootctl update`     | YES                          |
| macOS   | bless / iBoot          | NVRAM `efi-boot-device`              | (firmware-managed; no user step)     | YES                          |
| Windows | BCD                    | `bcdedit /default {<guid>}`          | (immediate; no apply step)           | YES                          |

---

## Why SUCCESS no-ops for `update_bootloader_config` / `regenerate_config`?

Windows BCD writes (`bcdedit /default`, `bcdedit /create`,
`bcdedit /set`, `bcdedit /delete`) take effect the moment they
return.  There is no separate "apply" or "regenerate" phase: the
on-disk BCD store is the source of truth and the boot manager
reads it directly at the next boot.

The cross-OS contract permits two responses to a verb that does
not map onto the native facility:

1. Return `SUCCESS` with a `message` indicating the no-op
   rationale.
2. Return `FAILED` with an explanatory `detail`.

For these two verbs this spec deliberately chooses option (1)
because the *intent* of "regenerate / apply the bootloader config"
is satisfied by the immediate effect of the preceding write.
Returning `FAILED` would mislead compliance reporting into
believing the host's bootloader configuration was broken when in
fact every prior write already took effect.  The `message` field
carries the no-op rationale so that the operator sees the honest
explanation in the result payload.

---

## GUID Format

BCD identifiers are GUIDs wrapped in braces.  The canonical form
used internally is the lower-case brace-wrapped GUID
(``{a1b2c3d4-...}``).  Built-in identifiers also accept aliases
such as ``{bootmgr}`` and ``{default}``; these aliases pass through
unchanged.

`_normalise_guid(value)` performs the normalisation:

1. If `value` is one of the recognised aliases (lower-cased),
   return it unchanged.
2. Otherwise, if `value` is brace-wrapped already, lower-case it.
3. Otherwise, wrap `value` in braces and lower-case it.
4. Validate the result against the canonical GUID regex; raise
   `ValueError` on mismatch.

Every public verb that consumes a GUID runs through this helper
so that callers can pass either form.  Aliases that resolve to
non-osloader namespaces (`{bootmgr}`, `{fwbootmgr}`,
`{globalsettings}`, ...) are explicitly skipped in the
`list_entries` parser; entries whose `identifier` is one of these
aliases are not osloader entries and would never be valid targets
for `set_default_entry` / `remove_entry`.

---

## Architecture

```
+-------------------------------------------------------------+
|              Maintenance Coordinator                        |
|  - Instantiates LocalCommandRunner / SSHCommandRunner       |
|  - Configures aspects (optional)                            |
|  - Injects runner into WindowsBootloaderManager             |
+----------------------+--------------------------------------+
                       | instantiates & injects
                       v
+-------------------------------------------------------------+
|                WindowsBootloaderManager                     |
|  ccai.sysman.windows.bootloadermanagement               |
|                                                             |
|  list_entries()           get_default_entry()               |
|  set_default_entry(id)    add_entry(spec)                   |
|  remove_entry(id)         update_bootloader_config()        |
|  regenerate_config()                                        |
+-------------------+-----------------------------------------+
                    | delegates commands to
                    v
+-------------------------------------------------------------+
|                     ICommandRunner                          |
|     run(args, elevated, capture_output)                     |
|     -> RunResult                                            |
+-------------------+-----------------------------------------+
                    |
       +------------+------------+
       v                         v
+----------------+      +-------------------+
| LocalCommand   |      | SSHCommand        |
| Runner         |      | Runner            |
+----------------+      +-------------------+
```

---

## Components

### `WindowsBootloaderManager`

**Location:** `source/packages/ccai/sysman/windows/bootloadermanagement.py`

The primary public class.  Constructed with an `ICommandRunner` and
a `WindowsBootloaderManagerConfig`.

| Method                                          | Description                                                              |
|-------------------------------------------------|--------------------------------------------------------------------------|
| `list_entries()` -> `list[WindowsBootEntryInfo]`  | Enumerate BCD osloader + firmware entries                              |
| `get_default_entry()` -> `WindowsBootEntryInfo`   | Return the entry whose GUID matches the `{bootmgr}` default           |
| `set_default_entry(entry_id)` -> `Result`         | Run `bcdedit /default {<guid>}` (idempotent)                          |
| `add_entry(spec)` -> `Result`                     | Create + set + displayorder via three `bcdedit` invocations           |
| `remove_entry(entry_id)` -> `Result`              | Run `bcdedit /delete {<guid>}` (idempotent)                           |
| `update_bootloader_config()` -> `Result`          | SUCCESS no-op (BCD writes are immediate)                              |
| `regenerate_config()` -> `Result`                 | SUCCESS no-op (BCD is its own source)                                 |

### `WindowsBootloaderManagerConfig`

A dataclass holding runtime configuration for
`WindowsBootloaderManager`.

| Field                  | Type     | Default          | Description                                                       |
|------------------------|----------|------------------|-------------------------------------------------------------------|
| `dry_run`              | `bool`   | `False`          | When `True`, no mutating commands are executed                    |
| `elevated`             | `bool`   | `False`          | When `True`, mutating commands are dispatched with elevation      |
| `bcdedit_executable`   | `str`    | `bcdedit.exe`    | The `bcdedit` executable path / PATH-resolvable name              |

---

## Data Models

### `WindowsBootloaderActionStatus`

```python
WindowsBootloaderActionStatus = ActionStatus  # alias

class ActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"
```

### `WindowsBootloaderActionResult`

```python
@dataclass
class WindowsBootloaderActionResult(FeatureActionResult):
    status: ActionStatus
    detail: Optional[str]
    exit_code: Optional[int]
    stdout: Optional[str]
    stderr: Optional[str]
    command: Optional[list[str]]
    entry_id: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None
```

### `WindowsBootEntryInfo`

```python
@dataclass
class WindowsBootEntryInfo:
    id: str
    name: str
    kernel: Optional[str] = None
    initrd: Optional[str] = None
    cmdline: Optional[str] = None
    device: Optional[str] = None
    is_default: bool = False
    boot_count: Optional[int] = None
```

### `WindowsBootEntrySpec`

```python
@dataclass
class WindowsBootEntrySpec:
    id: Optional[str]
    name: str
    application: str = "osloader"
    device: Optional[str] = None
    path: Optional[str] = None
    options: Optional[dict] = None
```

### Exceptions

| Exception                      | Raised When                                                |
|--------------------------------|------------------------------------------------------------|
| `WindowsBootloaderQueryError`  | A read fails unexpectedly, or the default cannot be resolved. |

---

## Correctness Properties

### Property 1: Idempotent `set_default_entry`

For any GUID, calling `set_default_entry()` when the supplied GUID
is already the `{bootmgr}` default should return a result with
status `SKIPPED` and must not invoke `bcdedit /default`.

**Validates: Requirements 3.2, 8.1**

### Property 2: Idempotent `add_entry`

For any spec whose GUID and description match an existing entry,
calling `add_entry()` should return a result with status `SKIPPED`
and must not invoke `bcdedit /create`, `/set`, or `/displayorder`.

**Validates: Requirements 4.7, 8.2**

### Property 3: Idempotent `remove_entry`

For any GUID not present in the BCD store, calling
`remove_entry()` should return a result with status `SKIPPED` and
must not invoke `bcdedit /delete`.

**Validates: Requirements 5.2, 8.3**

### Property 4: GUID Normalisation

For any input GUID -- bare or brace-wrapped, upper- or lower-case
-- the dispatched `bcdedit` argv must carry the canonical
brace-wrapped lower-case form.  The result's `entry_id` field must
also carry the canonical form.

**Validates: Requirements 3.1, 5.1**

### Property 5: Multi-Step `add_entry` Failure Surfacing

For any `add_entry` call that fails at one of its three steps
(`/create`, `/set`, `/displayorder`), the result must be `FAILED`
with the failed step's argv in `command` and the runner stderr in
`stderr`.

**Validates: Requirements 4.6**

### Property 6: Auto-Generated GUID Round-Trip

For any `add_entry` call with `spec.id=None`, the auto-generated
GUID parsed from `bcdedit /create` stdout must be carried through
to subsequent `/set` and `/displayorder` calls and must be
returned in `result.entry_id` on success.

**Validates: Requirements 4.2**

### Property 7: Dry-Run Safety

For any mutating operation with `dry_run=True`, the command runner
must never be invoked with a mutating command and the returned
result's `status` must be `DRY_RUN`.

**Validates: Requirements 10.1, 10.2**

### Property 8: Elevation Consistency

For any mutating operation with `elevated=True` in the config,
every `bcdedit` invocation that mutates state must be called with
`elevated=True`; for any read-only query, elevation must never be
requested.

**Validates: Requirements 9.1, 9.2, 9.3, 9.4**

### Property 9: Status Mapping Correctness

For any operation result from the command runner, the
`WindowsBootloaderActionResult.status` must correctly reflect the
runner's exit code: `0` maps to `SUCCESS`, non-zero maps to
`FAILED` (unless skipped or dry-run), with the exit code and
captured stderr preserved on `FAILED` results.

**Validates: Requirements 3.4, 4.6, 5.4, 12.2**

### Property 10: No UEFI Firmware Touch

For any verb invocation, no `bcdedit` argv may equal
`/enum firmware` and no `bcdedit /set` may target `{fwbootmgr}`.
The only `{fwbootmgr}`-touching call is the read-only
`bcdedit /enum {fwbootmgr}` used by `list_entries`.

**Validates: Requirements 11.1, 11.2**

### Property 11: Reboot Signalling

For any successful write, `result.requires_reboot` must be `True`
because the boot manager consults the BCD store only at boot.
For the SUCCESS no-op verbs (`update_bootloader_config` /
`regenerate_config`), `requires_reboot` must be `False`.

**Validates: Requirements 3.6, 6.3, 7.3, 12.1**

### Property 12: Default-GUID Cross-Reference

For any `get_default_entry` call, the returned entry's `id` must
equal the `default` GUID parsed from `bcdedit /enum {bootmgr} /v`,
and `is_default` must be `True`.

**Validates: Requirements 2.1, 2.2**

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

`WindowsBootloaderManager` depends on this interface, injected at
construction time.

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult` (from `codecraftai-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
```

### Elevation Strategy

When `WindowsBootloaderManagerConfig.elevated` is `True`,
`WindowsBootloaderManager` passes `elevated=True` to
`ICommandRunner.run()` for every mutating `bcdedit` invocation.
The runner is responsible for using whatever privilege-escalation
mechanism is appropriate for the environment (Administrator
elevation token, scheduled-task surrogate, etc).  Read-only
queries never request elevation; the runner surfaces the
access-denied error on UAC-protected hosts when the process
lacks elevation.

---

## Command Mappings

| Operation                  | Command                                                              |
|----------------------------|----------------------------------------------------------------------|
| `list_entries`             | `bcdedit /enum osloader` + `bcdedit /enum {fwbootmgr}`               |
| `get_default_entry`        | `bcdedit /enum {bootmgr} /v` (parse `default` line)                  |
| `set_default_entry`        | `bcdedit /default {<guid>}`                                          |
| `add_entry` (create)       | `bcdedit /create [{<guid>}] /d "<name>" /application <application>`  |
| `add_entry` (per option)   | `bcdedit /set {<guid>} <key> <value>`                                |
| `add_entry` (displayorder) | `bcdedit /displayorder {<guid>} /addlast`                            |
| `remove_entry`             | `bcdedit /delete {<guid>}`                                           |
| `update_bootloader_config` | (none -- SUCCESS no-op)                                              |
| `regenerate_config`        | (none -- SUCCESS no-op)                                              |
| Idempotency probes         | `bcdedit /enum {bootmgr} /v`, `bcdedit /enum osloader`               |

---

## Error Handling

### Typed Exceptions

| Exception                      | Raised When                                                      |
|--------------------------------|------------------------------------------------------------------|
| `WindowsBootloaderQueryError`  | `bcdedit /enum` fails unexpectedly, or default cannot be resolved. |

All other failures are surfaced as a
`WindowsBootloaderActionResult` with `status=FAILED` carrying the
underlying `exit_code`, `stdout`, `stderr`, and the failed step's
`command`.  The coordinator decides how to respond without
catching exceptions for normal operational failures.

### Multi-Step `add_entry` Error Surfacing

`add_entry` runs up to N+2 `bcdedit` invocations: one `/create`,
one `/set` per option, and one `/displayorder`.  When any step
fails, the manager returns `FAILED` immediately with the failing
step's argv populated in `command`.  Earlier steps that succeeded
are NOT rolled back -- the BCD store may be left in a partial
state and the operator should consult the result `command` /
`stderr` and run `bcdedit /export` / `bcdedit /import` to recover
when needed.

### Idempotency Guards

Before executing mutating commands, `WindowsBootloaderManager`
reads current state via `bcdedit /enum {bootmgr} /v` and
`bcdedit /enum osloader` to determine whether action is needed.
When the system is already in the desired state the method
returns `SKIPPED` without invoking the apply step.  This makes
all mutating operations safe to call multiple times.

### Destructive-Change Safety

BCD misconfiguration can render the host unbootable.  This
module:

1. Sets `requires_reboot=True` on every successful write.
2. Surfaces the underlying `bcdedit` stderr verbatim on `FAILED`
   results.
3. Documents in the module docstring that callers SHOULD pair
   every write with a `bcdedit /export` backup and SHOULD verify
   the resulting state through `list_entries` before rebooting.

---

## Module Layout

```
source/packages/ccai/sysman/windows/
+-- __init__.py
+-- bootloadermanagement.py          (this feature)

source/testroots/testbase/ccai/tests/sysman/windows/
+-- test_bootloadermanagement.py
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` injected into
  `WindowsBootloaderManager` at construction time.
- Tests verify the *arguments* passed to the runner, including
  GUID-format normalisation and the multi-step `add_entry`
  sequence.
- Each public verb has at minimum: a success path test, a skip /
  idempotency test where applicable, a failure path test, and a
  dry-run test.
- The GUID-format-normalisation test exercises both bare and
  brace-wrapped, mixed-case input.
- The no-op verbs are exercised by tests that assert the runner
  was not invoked.
- No test requires Administrator access, a live BCD store, or a
  real `bcdedit.exe`.

### Coverage Targets

At least 14 unit tests covering every public verb, every
idempotency path, the GUID normalisation, the elevation policy on
writes, the no-elevation policy on reads, and the failed-command
stderr passthrough.

---

## Usage Examples

### Local Execution (Set Default Boot Entry)

```python
from ccai.sysman.windows.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from ccai.sysman.windows.bootloadermanagement import (
    WindowsBootloaderManager,
    WindowsBootloaderManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = WindowsBootloaderManagerConfig(elevated=True)
manager = WindowsBootloaderManager(runner=runner, config=config)

result = manager.set_default_entry(
    entry_id="{a1b2c3d4-1111-2222-3333-444455556666}",
)
print("set default: {} (reboot: {})".format(
    result.status, result.requires_reboot,
))
```

### Add a Custom OS Loader

```python
from ccai.sysman.windows.bootloadermanagement import (
    WindowsBootEntrySpec,
    WindowsBootloaderManager,
    WindowsBootloaderManagerConfig,
)

manager = WindowsBootloaderManager(
    runner=runner,
    config=WindowsBootloaderManagerConfig(elevated=True),
)
spec = WindowsBootEntrySpec(
    id=None,                      # auto-generated GUID
    name="Custom Windows",
    application="osloader",
    device="partition=E:",
    path="\\Windows\\system32\\winload.efi",
    options={"locale": "en-US"},
)
result = manager.add_entry(spec=spec)
print("add: {} (guid: {})".format(result.status, result.entry_id))
```

### Dry-Run Across Transports

```python
manager = WindowsBootloaderManager(
    runner=runner,
    config=WindowsBootloaderManagerConfig(elevated=True, dry_run=True),
)
result = manager.set_default_entry(
    entry_id="a1b2c3d4-1111-2222-3333-444455556666",
)
assert result.status.name == "DRY_RUN"
```
