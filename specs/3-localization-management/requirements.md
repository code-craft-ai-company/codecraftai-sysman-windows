# Requirements

## Introduction

The localization management feature provides the
`codecraftai-sysman-windows` package with the ability to query and set
host-wide locale, keymap, and timezone settings on Windows hosts.
This spec implements the localization-management contract defined at
`codecraftai-sysman-common/specs/contracts/localization-management/contract.md`
for Windows using the PowerShell cmdlets `Get-Culture` /
`Set-Culture`, `Get-WinSystemLocale` / `Set-WinSystemLocale`,
`Get-WinUserLanguageList` / `Set-WinUserLanguageList`, `Get-TimeZone`
/ `Set-TimeZone`, and the .NET reflective enumerator
`[System.Globalization.CultureInfo]::GetCultures(AllCultures)`.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public verbs. The feature
does not decide which locale, keymap, or timezone the host should
carry — it provides the capability to apply, observe, and idempotently
re-apply those values reliably and with full auditability. Every
PowerShell scriptblock is dispatched through the
`codecraftai-interfaces` command-runner abstraction; this module does
not import `subprocess`, does not call `open`, and does not consume an
`IFileOperator`.

The OS-prefixed types in this module are
`WindowsLocalizationManager`, `WindowsLocalizationManagerConfig`,
`WindowsLocalizationActionStatus` (alias of the package-shared
`ActionStatus`), `WindowsLocalizationActionResult`,
`WindowsLocaleInfo`, `WindowsKeymapInfo`, and
`WindowsLocalizationQueryError`. There is no `WindowsTimezoneInfo`
dataclass: `get_timezone` returns a plain `str` (the Windows-format
timezone ID).

> **Critical: IANA <-> Windows timezone mapping.** Windows's
> `Set-TimeZone -Id` requires Windows-format timezone IDs
> (`"Pacific Standard Time"`), not IANA-style strings
> (`"America/Los_Angeles"`). The cross-OS contract has callers
> passing IANA-style values. The Windows manager translates via a
> bundled `_IANA_TO_WINDOWS_TZ` mapping table of the ~30 most common
> entries; values that already match a Windows-format ID pass through
> unchanged. When a value cannot be resolved, the manager returns
> `FAILED` with `message="unknown_timezone_id"`.

---

## Requirements

### Requirement 1: Query Current Locale

**User Story:** As a maintenance coordinator, I want to query the
active culture (per-user) and the system-wide locale, so that I can
compare both against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_locale()` THE SYSTEM SHALL invoke
   `Get-Culture | ConvertTo-Json -Compress -Depth 5` through the
   command runner with `elevated=False`.
2. THE SYSTEM SHALL invoke `Get-WinSystemLocale | ConvertTo-Json
   -Compress -Depth 5` to enumerate the machine-wide locale; a
   non-zero exit on this read collapses to an empty
   `system_default`.
3. THE SYSTEM SHALL return a `WindowsLocaleInfo` carrying the parsed
   culture `Name` as `language`, the derived region tag when present
   (e.g. `"US"` for `"en-US"`), the parsed system locale as
   `system_default`, and the verbatim parsed payloads in `raw`.
4. WHEN `Get-Culture` exits non-zero AND emits stderr THE SYSTEM
   SHALL raise `WindowsLocalizationQueryError`.
5. WHEN `Get-Culture` exits non-zero AND emits no stderr THE SYSTEM
   SHALL fall through to a parse with empty inputs.
6. THE SYSTEM SHALL never pass `elevated=True` to the runner.

---

### Requirement 2: Set System Locale

**User Story:** As a maintenance coordinator, I want to set the
user culture (and optionally the machine-wide locale), so that the
host applies the declared user-facing language.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_locale(locale)` THE SYSTEM SHALL
   invoke `Set-Culture -CultureInfo '<locale>'` through the runner.
2. WHEN the manager's `apply_system_wide` is True AND the
   `Set-Culture` write succeeds THE SYSTEM SHALL additionally
   invoke `Set-WinSystemLocale -SystemLocale '<locale>'` through
   the runner.
3. WHEN the parsed user culture already equals the requested value
   AND (when `apply_system_wide` is True) the parsed system locale
   also matches THE SYSTEM SHALL return
   `WindowsLocalizationActionResult(status=SKIPPED)` without
   invoking any write.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner. The result's
   `requires_reboot` field SHALL reflect the configured
   `apply_system_wide`.
5. WHEN the runner reports a non-zero exit on `Set-Culture` THE
   SYSTEM SHALL return `status=FAILED` with `exit_code`, `stdout`,
   `stderr`, `command` populated, and SHALL NOT dispatch
   `Set-WinSystemLocale`.
6. WHEN the `Set-WinSystemLocale` write succeeds THE SYSTEM SHALL
   set `result.requires_reboot=True` (the cmdlet requires a reboot
   to take full effect).
7. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for every write step.
8. THE SYSTEM SHALL populate `result.setting="locale"` and
   `result.previous_value` (when the read probe succeeded) on every
   returned result.

> **Per-user vs system-wide.** `Set-Culture` mutates the current
> user's culture and does NOT require Administrator privileges.
> `Set-WinSystemLocale` mutates the machine-wide locale and DOES
> require Administrator privileges plus a reboot. The manager
> defaults to per-user; callers opt in to system-wide via
> `apply_system_wide=True`.

---

### Requirement 3: Enumerate Supported Locales

**User Story:** As a maintenance coordinator, I want to enumerate
every culture the .NET runtime knows about, so that I can validate
locale values before attempting to set them.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_supported_locales()` THE SYSTEM
   SHALL invoke
   `[System.Globalization.CultureInfo]::GetCultures([System.Globalization.CultureTypes]::AllCultures)
   | Select-Object Name | ConvertTo-Json -Compress -Depth 5`.
2. THE SYSTEM SHALL return a `list[str]` of culture names.
3. WHEN the cmdlet exits non-zero THE SYSTEM SHALL raise
   `WindowsLocalizationQueryError`.

---

### Requirement 4: Query Current Keymap

**User Story:** As a maintenance coordinator, I want to query the
user's preferred-language list (the Windows analogue of "keymap"),
so that I can compare it against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_keymap()` THE SYSTEM SHALL invoke
   `Get-WinUserLanguageList | Select-Object LanguageTag,
   InputMethodTips | ConvertTo-Json -Compress -Depth 5`.
2. THE SYSTEM SHALL parse the resulting JSON array and return a
   `WindowsKeymapInfo` whose `name` is the head language tag (the
   empty string when the list is empty), whose `input_methods` is
   the head entry's `InputMethodTips` array, and whose
   `language_list` is the ordered list of every language tag.
3. THE SYSTEM SHALL set `WindowsKeymapInfo.layout` equal to `name`,
   and `variant` / `model` / `options` to `None` (Windows does not
   surface those concepts).
4. THE SYSTEM SHALL accept the bare-object shape `ConvertTo-Json`
   emits when there is exactly one row.
5. WHEN the cmdlet exits non-zero AND emits stderr THE SYSTEM SHALL
   raise `WindowsLocalizationQueryError`.

---

### Requirement 5: Set Keymap

**User Story:** As a maintenance coordinator, I want to set the
user's primary language (and its bound input method), so that the
host applies a declared keyboard layout.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_keymap(keymap)` THE SYSTEM SHALL
   invoke `Set-WinUserLanguageList -LanguageList '<keymap>' -Force`.
2. WHEN the parsed head language tag already equals `keymap` THE
   SYSTEM SHALL return `status=SKIPPED` without invoking the runner.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, `command`
   populated.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for the write step.
6. THE SYSTEM SHALL populate `result.setting="keymap"` on every
   returned result.

---

### Requirement 6: Enumerate Supported Keymaps

**User Story:** As a maintenance coordinator, I want a best-effort
enumeration of the input methods the host can present, so that I
can validate keymap values before attempting to set them.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_supported_keymaps()` THE SYSTEM
   SHALL invoke `Get-WinUserLanguageList` and surface the union of
   every parsed language tag and every input-method TIP currently
   bound to the head entry.
2. THE SYSTEM SHALL deduplicate the union while preserving order.
3. THE SYSTEM SHALL never raise on this verb; failures collapse to
   the empty list.

> **Best-effort surface.** Windows does not expose a flat keymap
> list comparable to Linux's `localectl list-keymaps`. The cross-OS
> contract acknowledges this; callers requiring richer enumeration
> should consult MSDN's official mapping data directly.

---

### Requirement 7: Query Current Timezone

**User Story:** As a maintenance coordinator, I want to query the
active system timezone, so that I can compare it against the
declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_timezone()` THE SYSTEM SHALL
   invoke `Get-TimeZone | Select-Object Id, DisplayName |
   ConvertTo-Json -Compress -Depth 5` with `elevated=False`.
2. THE SYSTEM SHALL parse the resulting JSON and return the trimmed
   `Id` field as the Windows-format timezone identifier (e.g.
   `"Pacific Standard Time"`).
3. WHEN `Get-TimeZone` exits non-zero THE SYSTEM SHALL raise
   `WindowsLocalizationQueryError`.

---

### Requirement 8: Set Timezone

**User Story:** As a maintenance coordinator, I want to set the
system timezone, so that local-clock displays and scheduled tasks
honour the requested wall clock.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_timezone(tz)` AND `tz` is an
   IANA-form timezone present in the bundled mapping table THE
   SYSTEM SHALL resolve it to the corresponding Windows-format ID
   and invoke `Set-TimeZone -Id '<windows-id>'`.
2. WHEN `tz` is already a Windows-format ID (i.e. it appears as a
   value in the bundled mapping table) THE SYSTEM SHALL invoke
   `Set-TimeZone -Id '<tz>'` without translation.
3. WHEN `tz` cannot be resolved to a Windows-format ID THE SYSTEM
   SHALL return `WindowsLocalizationActionResult(status=FAILED,
   message="unknown_timezone_id")` without invoking the runner.
4. WHEN the parsed timezone already matches the resolved
   Windows-format ID THE SYSTEM SHALL return `status=SKIPPED`.
   Comparison is case-sensitive on Windows IDs.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with
   the resolved Windows-format ID embedded in the would-have-been
   argv.
6. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, `command`
   populated.
7. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner.
8. THE SYSTEM SHALL populate `result.setting="timezone"` and
   `result.previous_value` (when the read probe succeeded) on every
   returned result.

---

### Requirement 9: Enumerate Supported Timezones

**User Story:** As a maintenance coordinator, I want to enumerate
every Windows-format timezone ID the host knows about, so that I
can validate timezone values before attempting to set them.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_supported_timezones()` THE SYSTEM
   SHALL invoke `Get-TimeZone -ListAvailable | Select-Object Id |
   ConvertTo-Json -Compress -Depth 5`.
2. THE SYSTEM SHALL return a `list[str]` of Windows-format IDs.
3. WHEN the cmdlet exits non-zero THE SYSTEM SHALL raise
   `WindowsLocalizationQueryError`.

---

### Requirement 10: Elevation and Aspect Composition

**User Story:** As a maintenance coordinator, I want every mutating
verb to honour the manager's elevation policy and to be safe under
arbitrary aspect composition on the runner, so that the manager
behaves identically against a `LocalCommandRunner`,
`SSHCommandRunner`, or any wrapped variant.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner on every write
   (`set_locale`, `set_keymap`, `set_timezone`).
2. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL never pass `elevated=True`.
3. THE SYSTEM SHALL never pass `elevated=True` on any read
   (`get_locale`, `list_supported_locales`, `get_keymap`,
   `get_supported_keymaps`, `get_timezone`,
   `get_supported_timezones`, every idempotency probe).
4. THE SYSTEM SHALL hold no state across calls beyond the injected
   config and runner.
5. THE SYSTEM SHALL be safe under any retry / persistence /
   timeout aspect composition on the runner.
