# Requirements

## Introduction

The vpn-client-management feature provides the `codecraftai-sysman-windows`
package with the ability to install, configure, and lifecycle the **Tailscale**
client and the **Twingate Connector** on Windows hosts. This spec implements
the vpn-client-management contract defined at
`codecraftai-sysman-common/specs/contracts/vpn-client-management/contract.md`
for Windows using the official MSI distributions installed via `msiexec
/quiet`, with the resulting Windows Services controlled by `Set-Service` and
`sc.exe`.

A maintenance coordinator drives all operations through this feature's
public interfaces. All system commands flow through `ICommandRunner` (typically
backed by PowerShell over WinRM), with the `aspects` parameter forwarded on
every call. Companion features
[`1-command-execution`](../1-command-execution/requirements.md) and
[`2-maintenance-reporting`](../2-maintenance-reporting/requirements.md)
supply the runner and reporting primitives this feature composes on. A
companion `3-service-management` feature (planned) will eventually own the
`Set-Service` / `sc.exe` primitives reused here.

---

## Requirements

### Requirement 1: Install Tailscale via Official MSI

**User Story:** As a maintenance coordinator, I want to install Tailscale on
a Windows host using the official MSI, so that the `tailscaled` Windows
Service and the `tailscale.exe` CLI are present.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_tailscale(version=None,
   channel="stable", aspects=…)` THE SYSTEM SHALL download the Tailscale
   MSI to `<msi_cache_dir>\tailscale-setup-<version>.msi` via
   `Invoke-WebRequest -OutFile`, and install it via
   `msiexec /i <path> /quiet /norestart`.
2. WHEN `version` is a concrete string THE SYSTEM SHALL substitute it into
   the download URL.
3. WHEN Tailscale is already installed at the requested version THE SYSTEM
   SHALL return `WindowsVPNActionResult(status=SKIPPED, provider=TAILSCALE)`.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for the `msiexec` invocation (UAC elevation token).
5. IF `msiexec` exits non-zero THEN THE SYSTEM SHALL return
   `WindowsVPNActionResult(status=FAILED, …)` populated with exit code,
   stdout, and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `WindowsVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 2: Install the Twingate Connector via Official MSI

**User Story:** As a maintenance coordinator, I want to install the Twingate
Connector on a Windows host using the official MSI, so that the connector
Windows Service is registered.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_twingate_connector(version=None,
   aspects=…)` THE SYSTEM SHALL download the Twingate Connector MSI via
   `Invoke-WebRequest -OutFile` and install it via
   `msiexec /i <path> /quiet /norestart`.
2. WHEN `version` is a concrete string THE SYSTEM SHALL substitute it into
   the download URL.
3. WHEN the Connector is already installed at the requested version THE
   SYSTEM SHALL return `WindowsVPNActionResult(status=SKIPPED,
   provider=TWINGATE)`.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for the `msiexec` invocation.
5. IF `msiexec` fails THEN THE SYSTEM SHALL return
   `WindowsVPNActionResult(status=FAILED, …)`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `WindowsVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 3: Configure a Provider

**User Story:** As a maintenance coordinator, I want to write the Twingate
Connector configuration via PowerShell so the daemon has the inputs it needs
before bring-up.

#### Acceptance Criteria

1. WHEN the coordinator calls `configure(provider=VPNProvider.TWINGATE,
   settings=…, aspects=…)` THE SYSTEM SHALL render
   `%ProgramData%\Twingate\connector.conf` via the runner using
   `Set-Content -Encoding UTF8` (or equivalent), with `elevated=True`
   WHERE the runner is configured for elevation.
2. WHEN `provider=VPNProvider.TAILSCALE` THE SYSTEM SHALL return
   `WindowsVPNActionResult(status=SKIPPED, provider=TAILSCALE,
   detail="tailscale has no on-disk configure step on windows")`.
3. WHEN the rendered content already matches what is on disk THE SYSTEM
   SHALL return `WindowsVPNActionResult(status=SKIPPED, …)`.
4. IF the file write fails THEN THE SYSTEM SHALL return
   `WindowsVPNActionResult(status=FAILED, …)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and
   SHALL return `WindowsVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 4: Bring the Tunnel Up

**User Story:** As a maintenance coordinator, I want to ensure the
Windows Service is running and authenticate the host to the tailnet
(Tailscale) or network (Twingate).

#### Acceptance Criteria

1. WHEN the coordinator calls `bring_up(provider=TAILSCALE, authkey=<key>,
   hostname=<name>, …, aspects=…)` THE SYSTEM SHALL invoke
   `Set-Service -Name Tailscale -StartupType Automatic`,
   `Start-Service -Name Tailscale`, and then `tailscale up --authkey=<key>
   --hostname=<name> …` via the runner.
2. WHEN the coordinator calls `bring_up(provider=TWINGATE, …)` THE SYSTEM
   SHALL invoke `Set-Service -Name twingate.connector -StartupType
   Automatic` and `Start-Service -Name twingate.connector` after
   `configure` has written the token.
3. WHEN `status(...)` already shows `running=True` with matching hostname/
   tags THE SYSTEM SHALL return
   `WindowsVPNActionResult(status=SKIPPED, …)`.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for `Set-Service`, `Start-Service`, and `tailscale up`.
5. IF the command fails because the process lacks an elevated token THEN
   THE SYSTEM SHALL return `WindowsVPNActionResult(status=FAILED, …)`
   carrying a stable `elevation_blocked` token in `detail` consistent with
   the package-wide Windows elevation convention.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command
   and SHALL return `WindowsVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 5: Bring the Tunnel Down

**User Story:** As a maintenance coordinator, I want to take the host off
the overlay without uninstalling.

#### Acceptance Criteria

1. WHEN the coordinator calls `bring_down(provider=TAILSCALE, aspects=…)`
   THE SYSTEM SHALL invoke `tailscale down` followed by `Stop-Service
   -Name Tailscale`.
2. WHEN the coordinator calls `bring_down(provider=TWINGATE, aspects=…)`
   THE SYSTEM SHALL invoke `Stop-Service -Name twingate.connector`.
3. WHEN `status` already shows `running=False` THE SYSTEM SHALL return
   `WindowsVPNActionResult(status=SKIPPED, …)`.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command
   and SHALL return `WindowsVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 6: Report Client Status

**User Story:** As a maintenance coordinator, I want to read the current
state of the VPN client without invoking writes.

#### Acceptance Criteria

1. WHEN the coordinator calls `status(provider=TAILSCALE, aspects=…)` THE
   SYSTEM SHALL invoke `tailscale status --json` without elevation and
   parse the JSON into a `WindowsVPNClientStatus`.
2. WHEN backend state is `Running` THE SYSTEM SHALL populate
   `running=True`, `self_address`, `hostname`, `tailnet`,
   `peer_addresses`, and `version`.
3. WHEN the coordinator calls `status(provider=TWINGATE, …)` THE SYSTEM
   SHALL invoke `Get-Service twingate.connector | ConvertTo-Json` and
   parse the resulting JSON into a
   `WindowsVPNClientStatus(self_address=None, peer_addresses={})`.
4. WHEN the daemon binary is not installed THE SYSTEM SHALL return
   `WindowsVPNClientStatus(installed=False, running=False, …)`.
5. IF the status command fails for any other reason THEN THE SYSTEM SHALL
   raise `WindowsVPNClientQueryError`.

---

### Requirement 7: Uninstall

**User Story:** As a maintenance coordinator, I want to uninstall a VPN
client and optionally purge its state.

#### Acceptance Criteria

1. WHEN the coordinator calls `uninstall(provider=TAILSCALE, purge=False,
   aspects=…)` THE SYSTEM SHALL stop the service and invoke
   `msiexec /x <ProductCode> /quiet /norestart` for the installed MSI's
   product code (looked up via `Get-Package` or the Uninstall registry
   key).
2. WHEN `purge=True` THE SYSTEM SHALL additionally remove
   `%ProgramData%\Tailscale` after MSI removal.
3. WHEN the coordinator calls `uninstall(provider=TWINGATE, …)` THE SYSTEM
   SHALL apply the equivalent steps for the Twingate Connector MSI.
4. WHEN the package is already absent THE SYSTEM SHALL return
   `WindowsVPNActionResult(status=SKIPPED, …)`.
5. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for `msiexec` and any state-directory `Remove-Item`
   step.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command
   and SHALL return `WindowsVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 8: Aspects Forwarding

**User Story:** As a maintenance coordinator, I want every behaviour-
bearing operation to take an `aspects` parameter.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept an `aspects: Aspects` parameter on every write
   and read verb.
2. THE SYSTEM SHALL forward `aspects` to the runner verbatim.
3. THE SYSTEM SHALL NOT apply any `@retry` or behaviour-modifying decorator.
