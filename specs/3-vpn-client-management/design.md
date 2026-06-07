# Design

## Overview

The vpn-client-management feature is implemented as a single module,
`ccai.sysman.windows.vpnclientmanagement`, that exposes a
`WindowsVPNClientManager` class subclassing `VPNClientManagementBase` from
`codecraftai-sysman-common`. The manager installs and lifecycles **Tailscale**
and the **Twingate Connector** on Windows using the official MSI
distributions, installed via `msiexec /quiet`, with the resulting Windows
Services controlled by `Set-Service` / `Start-Service` / `Stop-Service` and
queried via `Get-Service`.

All commands route through `ICommandRunner` (typically a PowerShell-over-
WinRM runner). MSI downloads use `Invoke-WebRequest -OutFile`; configuration
file writes use `Set-Content`. No direct subprocess use, no direct
filesystem writes.

**See Also:**
- [vpn-client-management contract](../../../codecraftai-sysman-common/specs/contracts/vpn-client-management/contract.md)
- [vpn-client-management-base](../../../codecraftai-sysman-common/specs/vpn-client-management-base/design.md)
- [1-command-execution](../1-command-execution/design.md)
- [2-maintenance-reporting](../2-maintenance-reporting/design.md)
- [Ubuntu sibling impl](../../../codecraftai-sysman-linux-ubuntu/specs/3-vpn-client-management/design.md)
- [macOS sibling impl](../../../codecraftai-sysman-macos/specs/3-vpn-client-management/design.md)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (PowerShell-over-WinRM)              │
│  - Configures aspects                                       │
│  - Injects runner + WindowsVPNClientManagerConfig           │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsVPNClientManager                          │
│  ccai.sysman.windows.vpnclientmanagement                │
│  inherits VPNClientManagementBase                           │
│                                                             │
│  install_tailscale()  install_twingate_connector()          │
│  configure()          bring_up()                            │
│  bring_down()         status()                              │
│  uninstall()                                                │
└────────────────────────┬────────────────────────────────────┘
                         │
        ┌────────────────┼─────────────────┐
        ▼                ▼                 ▼
┌─────────────────┐ ┌────────────┐ ┌──────────────────┐
│ Invoke-Web +    │ │ Set-Service│ │ tailscale /      │
│ msiexec /quiet  │ │ Start-     │ │ twingate CLIs    │
│                 │ │ Service /  │ │ + Get-Service    │
│                 │ │ Stop-Svc   │ │                  │
└────────┬────────┘ └─────┬──────┘ └────────┬─────────┘
         │                │                 │
         └────────────────┼─────────────────┘
                          ▼
                   ICommandRunner (PowerShell)
```

---

## Components

### `WindowsVPNClientManager`

**Location:** `source/packages/ccai/sysman/windows/vpnclientmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `install_tailscale` | `(*, version, channel, aspects)` | `Invoke-WebRequest` + `msiexec /i … /quiet` |
| `install_twingate_connector` | `(*, version, aspects)` | Same shape, Twingate MSI |
| `configure` | `(*, provider, settings, aspects)` | Write `%ProgramData%\Twingate\connector.conf`; SKIPPED for Tailscale |
| `bring_up` | `(*, provider, authkey, hostname, …, aspects)` | `Set-Service` + `Start-Service` + `tailscale up …` / `Set-Service` + `Start-Service` for Twingate |
| `bring_down` | `(*, provider, aspects)` | `tailscale down` + `Stop-Service` / `Stop-Service` for Twingate |
| `status` | `(*, provider, aspects)` | `tailscale status --json` / `Get-Service … \| ConvertTo-Json` |
| `uninstall` | `(*, provider, purge, aspects)` | `msiexec /x <ProductCode> /quiet` + optional state cleanup |

### `WindowsVPNClientManagerConfig`

Extends `VPNClientManagerConfig`:

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `tailscale_service_name` | `str` | `"Tailscale"` | Windows Service name |
| `twingate_service_name` | `str` | `"twingate.connector"` | Windows Service name |
| `twingate_config_path` | `str` | `"C:\\ProgramData\\Twingate\\connector.conf"` | Twingate config |
| `msi_cache_dir` | `str` | `"C:\\Windows\\Temp\\codecraftai-vpn-msi"` | Where downloaded MSIs are staged |
| `tailscale_product_name` | `str` | `"Tailscale"` | Used to look up the MSI ProductCode |
| `twingate_product_name` | `str` | `"Twingate Connector"` | Used to look up the MSI ProductCode |

---

## Data Models

Re-export shared contract types under Windows-prefixed aliases:

```python
WindowsVPNActionStatus     = VPNActionStatus
WindowsVPNActionResult     = VPNActionResult
WindowsVPNClientStatus     = VPNClientStatus
WindowsVPNClientQueryError = VPNClientQueryError
```

---

## Provider-Backend Mapping

| Provider | Install command | Service | Bring-up command | Bring-down command | Status command |
|---|---|---|---|---|---|
| Tailscale | `Invoke-WebRequest -OutFile <path>` + `msiexec /i <path> /quiet /norestart` | `Tailscale` | `Set-Service` + `Start-Service` + `tailscale up …` | `tailscale down` + `Stop-Service` | `tailscale status --json` |
| Twingate | `Invoke-WebRequest -OutFile <path>` + `msiexec /i <path> /quiet /norestart` | `twingate.connector` | `Set-Service` + `Start-Service` (after `configure`) | `Stop-Service` | `Get-Service twingate.connector \| ConvertTo-Json` |

---

## Command Mappings

### Installation

| Step | Command (PowerShell) |
|---|---|
| Create cache dir | `New-Item -ItemType Directory -Force -Path <msi_cache_dir>` |
| Download MSI | `Invoke-WebRequest -Uri <url> -OutFile <path>` |
| Install MSI | `msiexec /i <path> /quiet /norestart` |
| Look up product | `Get-Package -Name <product_name>` or query `HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*` |

### Configuration

| Provider | Path | Mechanism |
|---|---|---|
| Tailscale | (none) | Returned `SKIPPED` |
| Twingate | `<twingate_config_path>` | `Set-Content -Path <path> -Value <body> -Encoding UTF8` |

### Service Lifecycle

| Action | Command |
|---|---|
| Set startup | `Set-Service -Name <name> -StartupType Automatic` |
| Start | `Start-Service -Name <name>` |
| Stop | `Stop-Service -Name <name>` |
| Query | `Get-Service -Name <name> \| ConvertTo-Json` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Tailscale status | `tailscale status --json` |
| Twingate status | `Get-Service twingate.connector \| ConvertTo-Json` |
| Installed check | `Get-Package -Name <product_name>` |

---

## Idempotency Guards

| Verb | Guard | Mechanism |
|---|---|---|
| `install_tailscale` | Package present at requested version | `Get-Package -Name Tailscale` |
| `install_twingate_connector` | Same | `Get-Package -Name 'Twingate Connector'` |
| `configure` (Twingate) | Rendered content matches on-disk | `Get-Content <path>` via runner |
| `bring_up` | `status()` already shows `running=True` and matching hostname/tags | Pre-call status query |
| `bring_down` | `status()` already shows `running=False` | Pre-call status query |
| `uninstall` | Package already absent | `Get-Package` returns nothing |

---

## Elevation Policy

- `msiexec`, `Set-Service`, `Start-Service`, `Stop-Service`, `tailscale up`,
  and state-directory `Remove-Item` operations pass `elevated=True` when
  `config.elevated=True` (UAC token).
- Reads (`tailscale status`, `Get-Service`, `Get-Package`) never elevate.

### Elevation-Blocked Convention

When a write fails because the calling process lacks an elevated token, the
manager surfaces a stable `elevation_blocked` token in `detail`, consistent
with the package-wide Windows convention also used by
`3-firewall-management` and `3-service-management`. Callers may inspect this
token to distinguish "wrong privileges" from a genuine product failure.

---

## Module Layout

```
source/packages/ccai/sysman/windows/
├── __init__.py
└── vpnclientmanagement.py     ← WindowsVPNClientManager, config, aliases

source/testroots/testbase/
└── ccai/tests/sysman/windows/
    └── test_vpnclientmanagement.py
```

---

## See Also

- [vpn-client-management contract](../../../codecraftai-sysman-common/specs/contracts/vpn-client-management/contract.md)
- [vpn-client-management-base](../../../codecraftai-sysman-common/specs/vpn-client-management-base/design.md)
- [1-command-execution](../1-command-execution/design.md) — PowerShell-over-WinRM runner

---

## Testing Strategy

All tests use a `MockCommandRunner` keyed on the command-args prefix. No
test requires a live Windows host, MSI, Tailscale, or Twingate
installation.

**Example-based tests:**

- One command-construction test per write verb per provider.
- One idempotency test per write verb.
- One dry-run test per write verb.
- One parser test per provider — `tailscale status --json` and
  `Get-Service … | ConvertTo-Json` mock outputs.
- One elevation test proving writes pass `elevated=True` and reads do not.
- One `elevation_blocked` test proving the stable detail token is surfaced
  when the runner reports a UAC failure.
- One MSI-ProductCode lookup test verifying `uninstall` finds the
  product code via `Get-Package`.

**Property tests (hypothesis):**

- `install_tailscale` idempotency — for any generated
  `(version, channel)`, second call returns `SKIPPED`.
- `uninstall` idempotency for both providers.
- `bring_up` idempotency on matching state.
- Dry-run safety across generated inputs.
