# Design

## Overview

The mail-transport-management feature is implemented as a single
module,
`automojo.sysman.windows.mailtransportmanagement`, exposing a
`WindowsMailTransportManager` class.  Unlike the Ubuntu / macOS
implementations — which both wrap a fully functional Postfix command
suite — this manager is a **best-effort wrapper around a feature gap
in the underlying OS**.

**Windows has no built-in MTA equivalent.**  After the deprecation
of the IIS SMTP feature on consumer Windows SKUs, Microsoft's
recommendation for outbound mail is one of:

1. A third-party relay (hMailServer, MailEnable, ChangeMail).
2. An external SMTP gateway operated by infrastructure.
3. Direct PowerShell `Send-MailMessage` invocations against an
   external relay (no local queue / management).

To honour the cross-OS contract while documenting the gap, the
manager exposes a `WindowsMtaKind` discriminator and produces stable
detail strings for the unsupported paths so callers can react
declaratively.  This is the same pattern used by
`auditmanagement.disable_auditing()` for its `allow_destructive=False`
path — an explicit FAILED-with-explanation rather than silent
no-op or surprising raise.

**The Windows pilot is intentionally a placeholder for production
mail workloads.**  Operators who need real mail capability should
run their MTA on Linux or macOS.  The audience for this manager is
maintenance automation that needs to run cross-OS without crashing
on Windows hosts.

**See Also:**
- `automojo-sysman-linux-ubuntu` `3-mail-transport-management` —
  full Postfix coverage on Ubuntu.
- `automojo-sysman-linux-redhat` `3-mail-transport-management` —
  canonical Postfix coverage.
- `automojo-sysman-macos` `3-mail-transport-management` — Postfix
  coverage on macOS (with daemon-disabled-by-default caveat).
- `auditmanagement.py` — pattern reference for graceful FAILED on
  unsupported operations.

---

## Architecture

```
+-----------------------------------------------------------------+
|            Maintenance Coordinator                              |
+-----------------------------------------------------------------+
                       |
                       v
+-----------------------------------------------------------------+
|       WindowsMailTransportManager                               |
|                                                                 |
|  Reads (always elevated=False):                                 |
|     get_mta_config()      -> kind switch                        |
|       NONE / THIRD_PARTY  -> synthetic empty WindowsMtaConfig   |
|       IIS_SMTP            -> powershell Get-WmiObject + parse   |
|     list_aliases()        -> always []                          |
|     get_queue_status()    -> kind switch                        |
|       NONE / THIRD_PARTY  -> synthetic empty status             |
|       IIS_SMTP            -> powershell Get-ChildItem + parse   |
|                                                                 |
|  Writes (elevated per config):                                  |
|     set_mta_config()      -> kind switch                        |
|       NONE                -> FAILED windows_no_built_in_mta     |
|       THIRD_PARTY         -> FAILED windows_third_party_mta_... |
|       IIS_SMTP            -> powershell Get-WmiObject + Put_()  |
|     add_alias()           -> always FAILED with stable detail   |
|     remove_alias()        -> always FAILED with stable detail   |
|     test_mail_delivery()  -> kind switch; IIS_SMTP uses         |
|                              powershell Send-MailMessage        |
|     flush_queue()         -> kind switch; IIS_SMTP uses         |
|                              powershell Restart-Service SMTPSVC |
+-----------------------------------------------------------------+
                       |
                       v
+-----------------------------------------------------------------+
|                ICommandRunner                                   |
+-----------------------------------------------------------------+
```

---

## Configuration

```python
class WindowsMtaKind(Enum):
    NONE = "none"
    IIS_SMTP = "iis_smtp"
    THIRD_PARTY = "third_party"


@dataclass
class WindowsMtaManagerConfig(FeatureConfig):
    mta_kind: WindowsMtaKind = WindowsMtaKind.NONE
    iis_smtp_queue_dir: str = (
        r"%SystemRoot%\System32\inetsrv\mailroot\Queue"
    )
    powershell_executable: str = "powershell.exe"
    iis_smtp_service_name: str = "SMTPSVC"
    iis_smtp_localhost: str = "localhost"
```

Inherited from `FeatureConfig`:

- `dry_run: bool = False`
- `elevated: bool = False`

Operators MUST opt into IIS SMTP coverage explicitly by passing
`mta_kind=WindowsMtaKind.IIS_SMTP` — the default is `NONE` so that
every write returns FAILED with `windows_no_built_in_mta` until
configured otherwise.

---

## Data Models

```python
class WindowsMtaActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class WindowsMtaActionResult(FeatureActionResult):
    setting: str | None = None
    local: str | None = None


@dataclass
class WindowsMtaConfig:
    mta_kind: WindowsMtaKind = WindowsMtaKind.NONE
    myhostname: str | None = None
    mydomain: str | None = None
    myorigin: str | None = None
    relayhost: str | None = None
    mynetworks: str | None = None
    inet_interfaces: str | None = None
    inet_protocols: str | None = None
    mailbox_size_limit: int | None = None
    message_size_limit: int | None = None
    extras: dict[str, str] = field(default_factory=dict)
    detail: str | None = None


@dataclass
class WindowsMtaAliasInfo:
    local: str
    remote: str


@dataclass
class WindowsMtaQueueStatus:
    pending_count: int = 0
    deferred_count: int = 0
    total_size_bytes: int | None = None
    queue_age_oldest: str | None = None


class WindowsMtaQueryError(Exception):
    ...
```

---

## MTA Mapping

The cross-OS contract requires every implementation to document the
default MTA, the configuration paths it manages, and the
"no-MTA-installed" detection procedure.

| Concept                      | Windows                                        |
|------------------------------|------------------------------------------------|
| Default MTA                  | None (Windows has no built-in MTA)             |
| Optional MTA flavours        | IIS SMTP (legacy, Server SKUs only),           |
|                              | hMailServer / MailEnable (third-party)         |
| Service unit (IIS SMTP)      | `SMTPSVC` Windows service                      |
| Primary config (IIS SMTP)    | WMI namespace `root/MicrosoftIISv2`,           |
|                              | class `IIsSmtpServerSetting`                   |
| Aliases concept              | None (IIS SMTP has no native aliases)          |
| Queue directory (IIS SMTP)   | `%SystemRoot%\System32\inetsrv\mailroot\Queue` |
| Reload signal (IIS SMTP)     | `Restart-Service SMTPSVC`                      |
| Detection                    | `mta_kind` config value (operator-supplied;    |
|                              | the manager does NOT auto-detect installed MTAs)|

### Cross-OS MTA Concept Mapping

The cross-OS contract abstracts the same conceptual operations
across very different MTA management stories.  The Windows column
is intentionally sparse to highlight the gap:

| Concept              | Ubuntu / macOS / RHEL (Postfix)         | Windows IIS SMTP                                     | Windows NONE / THIRD_PARTY  |
|----------------------|-----------------------------------------|------------------------------------------------------|------------------------------|
| Daemon lifecycle     | `systemctl` / `launchctl`               | Windows Service Manager (SCM)                        | N/A                          |
| Daemon reload        | `systemctl reload postfix` /            | `Restart-Service SMTPSVC`                            | N/A                          |
|                      | `launchctl kickstart -k`                |                                                      |                              |
| Default state        | Installed (Linux) or                    | Not installed by default; available as Server role   | Absent                       |
|                      | Present-but-unloaded (macOS)            |                                                      |                              |
| Directive read       | `postconf -n`                           | `Get-WmiObject IIsSmtpServerSetting`                 | N/A                          |
| Directive write      | `postconf -e <key>=<value>`             | WMI property assignment + `$s.Put_()`                | N/A                          |
| Aliases file         | `/etc/aliases`                          | None                                                 | N/A                          |
| Queue read           | `mailq` / `postqueue -p`                | `Get-ChildItem` against queue directory              | N/A                          |
| Queue flush          | `postqueue -f`                          | `Restart-Service SMTPSVC` (heavy-handed)             | N/A                          |
| Probe                | `sendmail -t`                           | `Send-MailMessage` (PowerShell)                      | N/A                          |

The Windows MTA gap is not just a missing tool — it is a missing
*concept*.  Aliases have no Windows equivalent at all; the queue is a
flat directory rather than a structured deferred-vs-active store; the
"reload" signal is a service restart rather than a SIGHUP.

---

## Key Method Designs

### `get_mta_config()`

Branch on `mta_kind`:

1. `NONE`: Return synthetic config with `mta_kind=NONE` and a
   descriptive `detail` immediately — no runner call.
2. `THIRD_PARTY`: Same as `NONE` but with the
   `not_implemented` detail string.
3. `IIS_SMTP`: Build the `Get-WmiObject` PowerShell pipeline,
   dispatch with `elevated=False`, parse JSON via
   `_parse_iissmtp_get_output`.  On runner non-zero, return a
   synthetic shape with the stderr summary as `detail` (no raise —
   best-effort).

### `set_mta_config(spec)`

Branch on `mta_kind`:

1. `NONE`: Return FAILED with `_DETAIL_NO_MTA`.
2. `THIRD_PARTY`: Return FAILED with `_DETAIL_THIRD_PARTY_NOT_IMPL`.
3. `IIS_SMTP`: Honour dry-run (DRY_RUN with placeholder argv).
   Build the PowerShell pipeline via `_build_iissmtp_set_script`
   that emits property-setter statements for every spec field that
   maps to a known IIS SMTP property, followed by `$s.Put_()`.
   Dispatch and wrap the result.

The pilot does NOT perform a per-property idempotency probe for
IIS SMTP — the WMI surface does not offer cheap per-property
comparisons.  Callers wanting per-property skip semantics should
read first via `get_mta_config()` and gate the write client-side.

### `add_alias` / `remove_alias`

These verbs always return FAILED.  The detail string is one of:

- `_DETAIL_NO_MTA` (when `mta_kind=NONE`)
- `_DETAIL_THIRD_PARTY_NOT_IMPL` (when `mta_kind=THIRD_PARTY`)
- `_DETAIL_IIS_SMTP_NO_ALIASES` (when `mta_kind=IIS_SMTP`)

No runner call is made on any path.

### `list_aliases()`

Always returns `[]`.  No runner call.

### `test_mail_delivery(to, from_addr, subject)`

Branch on `mta_kind`:

1. `NONE` / `THIRD_PARTY`: Return FAILED with the appropriate
   detail.
2. `IIS_SMTP`: Build the `Send-MailMessage` PowerShell pipeline
   targeting `iis_smtp_localhost`.  Honour dry-run.  Dispatch and
   wrap.

Single quotes in addresses / subjects are escaped by doubling,
which is the correct PowerShell single-quoted-literal escape.

### `get_queue_status()`

Branch on `mta_kind`:

1. `NONE` / `THIRD_PARTY`: Return zeroed status, no runner call.
2. `IIS_SMTP`: Build the `Get-ChildItem` PowerShell pipeline
   targeting `iis_smtp_queue_dir`, dispatch with `elevated=False`,
   parse JSON.  `Count` -> `pending_count`; `Sum` ->
   `total_size_bytes`.  `deferred_count` is always 0 (the IIS SMTP
   queue directory does not partition active vs deferred).

### `flush_queue()`

Branch on `mta_kind`:

1. `NONE` / `THIRD_PARTY`: Return FAILED.
2. `IIS_SMTP`: Build `Restart-Service -Name '<service>' -Force`
   PowerShell pipeline.  Honour dry-run.  Dispatch.

The "flush" semantics on Windows IIS SMTP differ from Postfix's
`postqueue -f`.  Postfix re-attempts delivery without restarting
the daemon; IIS SMTP has no "flush queue" RPC, so the canonical
approach is to bounce the service.  This is heavier-handed than
the Linux equivalent but matches Microsoft's published guidance.

---

## Idempotency Model

| Verb                    | Idempotency probe                      | Skip when                                  |
|-------------------------|----------------------------------------|--------------------------------------------|
| `set_mta_config`        | None (best-effort, no per-property     | Never (no SKIPPED outcome)                 |
|                         | probe for IIS SMTP)                    |                                            |
| `add_alias`             | None — always FAILED                   | Never                                      |
| `remove_alias`          | None — always FAILED                   | Never                                      |
| `test_mail_delivery`    | None — operational                     | Never                                      |
| `flush_queue`           | None — operational                     | Never                                      |

The Windows pilot intentionally trades idempotency for clarity.
Callers that need per-property idempotency can drive it
client-side by reading `get_mta_config()` and only calling
`set_mta_config()` when there is divergence.

---

## Stable Detail Strings

The Windows pilot exposes three stable detail strings as
module-level constants:

- `_DETAIL_NO_MTA = "windows_no_built_in_mta"`
- `_DETAIL_THIRD_PARTY_NOT_IMPL = "windows_third_party_mta_not_implemented"`
- `_DETAIL_IIS_SMTP_NO_ALIASES = "windows_iis_smtp_aliases_unsupported"`

These are placed on the `stderr` field of the result so that
existing automation that scrapes stderr for failure context picks
them up unmodified.  Callers writing typed automation can match on
these constants directly.

---

## Dry-Run Behaviour

Dry-run is honoured ONLY for `mta_kind=IIS_SMTP`.  When
`mta_kind=NONE` or `mta_kind=THIRD_PARTY`, every write returns
FAILED unconditionally — the kind gate is upstream of the dry-run
gate.  This is intentional: callers should not get DRY_RUN results
that imply "we would have done X" when the system fundamentally
cannot do X.

---

## Elevation Policy

| Verb                    | Elevated when `config.elevated=True` AND `mta_kind=IIS_SMTP` | Notes                                  |
|-------------------------|--------------------------------------------------------------|----------------------------------------|
| `get_mta_config`        | NO — read                                                    |                                        |
| `set_mta_config`        | YES                                                          | WMI Put requires admin token           |
| `add_alias`             | N/A — never invokes runner                                   |                                        |
| `remove_alias`          | N/A — never invokes runner                                   |                                        |
| `list_aliases`          | N/A — never invokes runner                                   |                                        |
| `test_mail_delivery`    | YES                                                          | Send-MailMessage may need admin        |
| `get_queue_status`      | NO — read                                                    | Get-ChildItem for queue dir            |
| `flush_queue`           | YES                                                          | Restart-Service requires admin token   |

Reads NEVER pass `elevated=True` — the contract is unambiguous.

---

## Correctness Properties

1. **Stable failure surface.** Every NONE / THIRD_PARTY write
   returns FAILED with a stable detail string.  Callers can
   discriminate "unsupported" from "broken" without scraping logs.
2. **No silent no-op.** Unsupported writes return FAILED, not
   SUCCESS.  This prevents misleading audit trails.
3. **Read isolation.** No read verb passes `elevated=True`; no
   read verb mutates state.
4. **Best-effort reads.** IIS SMTP read failures return synthetic
   shapes with `detail` populated — they do NOT raise.  The pilot
   chooses graceful degradation over hard failure for reads.
5. **Dry-run safety.** When `mta_kind=IIS_SMTP` and `dry_run=True`,
   no PowerShell pipeline is dispatched.  When `mta_kind=NONE`,
   dry-run is moot (FAILED returned upstream).

---

## Failure Surface

Every write verb returns a `WindowsMtaActionResult`.  On runner
non-zero (IIS SMTP path) the manager populates:

- `status = FAILED`
- `exit_code` = runner's exit code
- `stdout` / `stderr` = runner's captured streams
- `command` = the PowerShell argv that ran

On unsupported-kind paths (NONE / THIRD_PARTY) the manager
populates:

- `status = FAILED`
- `stderr` = the stable detail string
- `command = None`

Read verbs do NOT raise on the IIS SMTP path — they return a
synthetic shape with `detail` populated.  This differs from the
Ubuntu / macOS pilots where read failures raise; the Windows pilot
prefers graceful degradation.

---

## Module-Level Helpers

| Helper                                      | Role                                            |
|---------------------------------------------|-------------------------------------------------|
| `_build_iissmtp_get_script`                 | PowerShell `Get-WmiObject` script               |
| `_build_iissmtp_set_script`                 | PowerShell property-setter + `Put_()` script    |
| `_build_iissmtp_queue_status_script`        | PowerShell `Get-ChildItem` script               |
| `_build_send_mailmessage_script`            | PowerShell `Send-MailMessage` script            |
| `_ps_escape`                                | Single-quote-doubling escape for PowerShell     |
| `_parse_iissmtp_get_output`                 | JSON -> `WindowsMtaConfig`                      |
| `_parse_iissmtp_queue_status_output`        | JSON -> `WindowsMtaQueueStatus`                 |
| `_extract_json_value`                       | Tolerant JSON key/value extractor               |

---

## Testing Strategy

- `_MockCommandRunner` records every `run` call and returns
  pre-seeded `RunResult` values keyed by argv tuple.  No live
  PowerShell process is invoked.
- Tests cover every kind branch on every verb:
  - `mta_kind=NONE` write paths return FAILED with
    `windows_no_built_in_mta`.
  - `mta_kind=IIS_SMTP` write paths dispatch the expected
    PowerShell argv and respect dry-run / elevation.
  - Read paths return synthetic shapes when `mta_kind=NONE` and
    parsed shapes when `mta_kind=IIS_SMTP`.
- Parser unit tests on `_parse_iissmtp_get_output` and
  `_parse_iissmtp_queue_status_output` drive the JSON path
  directly.
- Script-builder unit tests verify the PowerShell command text
  contains the expected tokens and escapes.

---

## Seams With Other Features

| Seam                              | Owner                                    | This Feature                |
|-----------------------------------|------------------------------------------|-----------------------------|
| `SMTPSVC` Windows service         | `3-service-management`                   | Restart only                |
| TLS cert / key                    | `3-certificate-issuance` / trust         | Out of scope here           |
| Firewall rules for SMTP           | `3-firewall-management`                  | Out of scope here           |
| Queue directory storage           | `3-storage-management`                   | Out of scope here           |
| Third-party MTA install           | `3-package-management`                   | Out of scope here           |

---

## Out of Scope

- Native support for hMailServer, MailEnable, ChangeMail, or any
  other third-party MTA.  `mta_kind=THIRD_PARTY` is a placeholder.
- Per-property idempotency on `set_mta_config` for IIS SMTP.
- Auto-detection of an installed MTA.  The operator MUST pass
  `mta_kind` explicitly.
- IIS SMTP virtual servers beyond the first (the pilot uses
  `Select-Object -First 1` against the WMI query).
- Aliases / virtual addressing on any Windows MTA (no native
  surface).
- Per-message tracing beyond `test_mail_delivery`.
- Greylisting and anti-spam integration.

---

## Open Questions

1. Should the Windows pilot auto-detect installed MTAs and pick
   `mta_kind` automatically?  Today the operator must opt in.  An
   auto-detect probe (e.g., `Get-Service SMTPSVC` + a check for
   common third-party install paths) would let `mta_kind=NONE`
   "upgrade" to IIS_SMTP transparently.  Risk: silently mutating
   behaviour based on host state.
2. Should the pilot handle multiple IIS SMTP virtual servers?
   Today `Select-Object -First 1` picks the first one.  A
   multi-virtual-server install would need a `virtual_server_id`
   config field.
3. Should `flush_queue` use `Set-MailboxTransportConfig` or
   similar Exchange cmdlets when targeting Exchange (a third-party
   MTA superset)?  The pilot does not target Exchange explicitly.
4. Should `test_mail_delivery` capture and parse the
   `Send-MailMessage` output to confirm queue acceptance?  Today
   the pilot just checks exit code.
